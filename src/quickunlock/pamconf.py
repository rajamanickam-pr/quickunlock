"""Insert / remove QuickUnlock lines in /etc/pam.d/gdm-password.

Only the GDM login & lock-screen stack is touched. sudo, su, ssh, polkit
and the TTY login keep asking for the real password.

Resulting stack (Ubuntu):

    auth requisite pam_nologin.so
    auth required  pam_succeed_if.so user != root quiet_success
    # >>> quickunlock >>>
    auth [success=done default=ignore] pam_exec.so quiet stdout  .../quickunlock-pam face
    auth [success=done default=ignore] pam_exec.so quiet stdout expose_authtok .../quickunlock-pam pin
    # <<< quickunlock <<<
    @include common-auth
    # >>> quickunlock-reset >>>
    auth optional pam_exec.so quiet .../quickunlock-pam reset
    # <<< quickunlock-reset <<<
    auth optional pam_gnome_keyring.so
    ...

"pin" receives whatever was typed in the password box. If it is not the
PIN, pam_unix (inside common-auth) re-uses the same text as the password,
so you can type either your PIN or your password into the one field.
"reset" only runs after common-auth succeeded (common-auth ends with
pam_deny on failure) and clears PIN lockouts after a password login.

`disable` leaves a marker in /var/lib/quickunlock so that package upgrades
and gdm3 updates (`auto`) don't quietly switch QuickUnlock back on.
"""

import os
import shutil
import tempfile

from . import paths

BEGIN, END = "# >>> quickunlock >>>", "# <<< quickunlock <<<"
RBEGIN, REND = "# >>> quickunlock-reset >>>", "# <<< quickunlock-reset <<<"


def _block(entry: str) -> list[str]:
    return [
        BEGIN,
        f"auth [success=done default=ignore] pam_exec.so quiet stdout {entry} face",
        f"auth [success=done default=ignore] pam_exec.so quiet stdout expose_authtok {entry} pin",
        END,
    ]


def _reset_block(entry: str) -> list[str]:
    return [RBEGIN, f"auth optional pam_exec.so quiet {entry} reset", REND]


def strip(lines: list[str]) -> list[str]:
    out, skipping = [], False
    for ln in lines:
        s = ln.strip()
        if s in (BEGIN, RBEGIN):
            skipping = True
            continue
        if s in (END, REND):
            skipping = False
            continue
        if not skipping:
            out.append(ln)
    return out


def _is_common_auth(ln: str) -> bool:
    return ln.split()[:2] == ["@include", "common-auth"]


def render(text: str, entry: str = None) -> str:
    entry = entry or paths.PAM_ENTRY
    lines = strip(text.splitlines())
    idx = next((i for i, ln in enumerate(lines) if _is_common_auth(ln)), None)
    if idx is None:
        # Non-Ubuntu layout: put our lines before the first "auth" line that
        # is not pam_nologin / pam_succeed_if.
        idx = next((i for i, ln in enumerate(lines)
                    if ln.split()[:1] == ["auth"] and not any(m in ln for m in
                                                              ("pam_nologin", "pam_succeed_if"))),
                   None)
        if idx is None:
            raise RuntimeError("Could not find the auth section in " + paths.PAM_GDM_FILE)
        new = lines[:idx] + _block(entry) + lines[idx:]
    else:
        new = lines[:idx] + _block(entry) + [lines[idx]] + _reset_block(entry) + lines[idx + 1:]
    out = "\n".join(new) + "\n"
    check(out, entry)
    return out


def check(text: str, entry: str = None) -> None:
    """Refuse to write a stack that doesn't look exactly as intended."""
    entry = entry or paths.PAM_ENTRY
    lines = [ln.strip() for ln in text.splitlines()]
    for marker in (BEGIN, END):
        if lines.count(marker) != 1:
            raise RuntimeError(f"expected exactly one '{marker}' line")
    b = lines.index(BEGIN)
    if lines[b:b + 4] != _block(entry):
        raise RuntimeError("QuickUnlock block is not what was expected")
    if any(ln.split()[:1] == ["auth"] and not any(m in ln for m in ("pam_nologin", "pam_succeed_if"))
           for ln in lines[:b]):
        raise RuntimeError("QuickUnlock block must come before the password check")
    inc = [i for i, ln in enumerate(lines) if _is_common_auth(ln)]
    if inc:
        if len(inc) != 1 or inc[0] != b + 4 or lines[inc[0] + 1:inc[0] + 4] != _reset_block(entry):
            raise RuntimeError("@include common-auth must sit between the QuickUnlock blocks")
    elif RBEGIN in lines:
        raise RuntimeError("reset block without common-auth")


def is_installed(path: str = None) -> bool:
    path = path or paths.PAM_GDM_FILE
    try:
        return BEGIN in open(path).read()
    except FileNotFoundError:
        return False


def _write(path: str, text: str) -> None:
    """Atomic and durable: a power cut leaves either the old or the new file."""
    st = os.stat(path)
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".quickunlock-")
    try:
        os.fchmod(fd, st.st_mode & 0o7777)
        try:
            os.fchown(fd, st.st_uid, st.st_gid)
        except PermissionError:
            pass
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def install(path: str = None) -> bool:
    path = path or paths.PAM_GDM_FILE
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path} not found - is GDM installed?")
    old = open(path).read()
    new = render(old)
    if new == old:
        return False
    backup = f"{path}.quickunlock-backup"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    _write(path, new)
    return True


def uninstall(path: str = None) -> bool:
    path = path or paths.PAM_GDM_FILE
    if not os.path.exists(path):
        return False
    old = open(path).read()
    new = "\n".join(strip(old.splitlines())) + "\n"
    if new == old:
        return False
    _write(path, new)
    return True


def admin_disabled() -> bool:
    return os.path.exists(paths.pam_disabled_marker())


def set_admin_disabled(disabled: bool) -> None:
    marker = paths.pam_disabled_marker()
    if disabled:
        os.makedirs(os.path.dirname(marker), mode=0o755, exist_ok=True)
        with open(marker, "w") as f:
            f.write("QuickUnlock was switched off with 'quickunlock-pam-setup disable'.\n")
    else:
        try:
            os.unlink(marker)
        except FileNotFoundError:
            pass


def main(argv=None) -> int:
    import sys
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        state = "installed" if is_installed() else "not installed"
        print(state + (" (switched off by 'disable')" if admin_disabled() else ""))
        return 0
    if os.geteuid() != 0:
        print("Run with sudo.", file=sys.stderr)
        return 1
    if cmd == "enable":
        set_admin_disabled(False)
        print("Added QuickUnlock to " + paths.PAM_GDM_FILE if install() else "Already enabled.")
        return 0
    if cmd == "disable":
        set_admin_disabled(True)
        print("Removed QuickUnlock from " + paths.PAM_GDM_FILE if uninstall() else "Already disabled.")
        return 0
    if cmd == "auto":      # package install / upgrade / gdm3 trigger
        if admin_disabled():
            print("QuickUnlock was switched off with 'disable'; leaving it off.")
            return 0
        print("Added QuickUnlock to " + paths.PAM_GDM_FILE if install() else "Already enabled.")
        return 0
    print("usage: quickunlock-pam-setup [status|enable|disable|auto]", file=sys.stderr)
    return 2
