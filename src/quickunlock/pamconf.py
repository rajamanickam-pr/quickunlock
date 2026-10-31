"""Insert / remove QuickUnlock lines in /etc/pam.d/gdm-password.

Only the GDM login & lock-screen stack is touched. sudo, su, ssh, polkit
and the TTY login keep asking for the real password.

Resulting stack (Ubuntu):

    auth requisite pam_nologin.so
    auth required  pam_succeed_if.so user != root quiet_success
    # >>> quickunlock >>>
    auth [success=done default=ignore] pam_exec.so quiet stdout  .../quickunlock-pam face
    auth [success=done default=ignore] pam_exec.so quiet stdout  .../quickunlock-pam voice
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
"""

import os
import shutil

from . import paths

BEGIN, END = "# >>> quickunlock >>>", "# <<< quickunlock <<<"
RBEGIN, REND = "# >>> quickunlock-reset >>>", "# <<< quickunlock-reset <<<"


def _block(entry: str) -> list[str]:
    return [
        BEGIN,
        f"auth [success=done default=ignore] pam_exec.so quiet stdout {entry} face",
        f"auth [success=done default=ignore] pam_exec.so quiet stdout {entry} voice",
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


def render(text: str, entry: str = None) -> str:
    entry = entry or paths.PAM_ENTRY
    lines = strip(text.splitlines())
    idx = next((i for i, ln in enumerate(lines)
                if ln.split() and ln.split()[0] == "@include" and ln.split()[1:2] == ["common-auth"]), None)
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
    return "\n".join(new) + "\n"


def is_installed(path: str = None) -> bool:
    path = path or paths.PAM_GDM_FILE
    try:
        return BEGIN in open(path).read()
    except FileNotFoundError:
        return False


def _write(path: str, text: str) -> None:
    st = os.stat(path)
    tmp = path + ".quickunlock-tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, st.st_mode & 0o7777)
    try:
        os.chown(tmp, st.st_uid, st.st_gid)
    except PermissionError:
        pass
    os.replace(tmp, path)


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


def main(argv=None) -> int:
    import sys
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        print("installed" if is_installed() else "not installed")
        return 0
    if os.geteuid() != 0:
        print("Run with sudo.", file=sys.stderr)
        return 1
    if cmd == "enable":
        print("Added QuickUnlock to " + paths.PAM_GDM_FILE if install() else "Already enabled.")
        return 0
    if cmd == "disable":
        print("Removed QuickUnlock from " + paths.PAM_GDM_FILE if uninstall() else "Already disabled.")
        return 0
    print("usage: quickunlock-pam-setup [status|enable|disable]", file=sys.stderr)
    return 2
