"""Drive the real Linux-PAM library through the stack QuickUnlock installs.

No root needed: pam_start_confdir() (libpam >= 1.4) reads the PAM config
from a temporary directory, built from this machine's /etc/pam.d files with
our lines added by pamconf.render(). pam_exec runs the source tree against a
scratch data directory.

Only the correct-PIN case uses the real login name (and it never reaches
pam_unix). Every case that falls through to the password check uses a user
that doesn't exist, so no real password is ever tried.
"""

import ctypes
import ctypes.util
import json
import os
import pwd
import shutil
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from test_core import GDM, GOOD_PIN  # noqa: E402

_lib = ctypes.util.find_library("pam")
libpam = ctypes.CDLL(_lib) if _lib else None
if libpam is None or not hasattr(libpam, "pam_start_confdir"):
    pytest.skip("needs libpam >= 1.4 (pam_start_confdir)", allow_module_level=True)
if not any(os.path.exists(f"{d}/security/pam_exec.so")
           for d in ("/lib/x86_64-linux-gnu", "/usr/lib/x86_64-linux-gnu", "/lib/aarch64-linux-gnu",
                     "/usr/lib/aarch64-linux-gnu", "/usr/lib64", "/lib64", "/usr/lib")):
    pytest.skip("pam_exec.so not found", allow_module_level=True)

libc = ctypes.CDLL(ctypes.util.find_library("c"))
libc.strdup.restype = ctypes.c_void_p
libc.strdup.argtypes = [ctypes.c_char_p]
libc.calloc.restype = ctypes.c_void_p
libc.calloc.argtypes = [ctypes.c_size_t, ctypes.c_size_t]
libpam.pam_strerror.restype = ctypes.c_char_p


class PamMessage(ctypes.Structure):
    _fields_ = [("msg_style", ctypes.c_int), ("msg", ctypes.c_char_p)]


class PamResponse(ctypes.Structure):
    _fields_ = [("resp", ctypes.c_void_p), ("resp_retcode", ctypes.c_int)]


CONV = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.POINTER(PamMessage)),
                        ctypes.POINTER(ctypes.POINTER(PamResponse)), ctypes.c_void_p)


class PamConv(ctypes.Structure):
    _fields_ = [("conv", CONV), ("appdata_ptr", ctypes.c_void_p)]


PAM_SUCCESS, PAM_CONV_ERR = 0, 19
PROMPT, INFO = (1, 2), 4

FAKE_USER = "qutestnobody"       # must not exist on the machine
ME = pwd.getpwuid(os.getuid()).pw_name


def authenticate(confdir, user, answers):
    """Returns (rc, prompts, info_messages)."""
    queue, prompts, infos = list(answers), [], []

    def conv(n, msgs, resp, _):
        out = ctypes.cast(libc.calloc(n, ctypes.sizeof(PamResponse)), ctypes.POINTER(PamResponse))
        for i in range(n):
            m = msgs[i].contents
            if m.msg_style in PROMPT:
                prompts.append(m.msg.decode())
                if not queue:
                    return PAM_CONV_ERR
                out[i].resp = libc.strdup(queue.pop(0).encode())
            elif m.msg_style == INFO:
                infos.append(m.msg.decode())
        resp[0] = out
        return 0

    cb = CONV(conv)
    pc = PamConv(cb, None)
    h = ctypes.c_void_p()
    assert libpam.pam_start_confdir(b"gdm-password", user.encode(), ctypes.byref(pc),
                                    str(confdir).encode(), ctypes.byref(h)) == PAM_SUCCESS
    rc = libpam.pam_authenticate(h, 0)
    libpam.pam_end(h, rc)
    return rc, prompts, infos


def _make_stack(tmp_path, data, name, patch=""):
    """PAM config dir whose pam_exec lines run `patch` + pamauth.main()."""
    from quickunlock import pamconf
    entry = tmp_path / f"{name}-entry"
    py = (f"import sys; sys.path.insert(0, {json.dumps(os.path.join(ROOT, 'src'))}); "
          f"from quickunlock import pamauth; {patch} sys.exit(pamauth.main())")
    entry.write_text(f"#!/bin/sh\nexport QUICKUNLOCK_DATA={data}\nexec {sys.executable} -I -c '{py}' \"$@\"\n")
    entry.chmod(0o755)
    conf = tmp_path / f"{name}-pam.d"
    conf.mkdir()
    src = "/etc/pam.d/gdm-password"
    base = open(src).read() if os.path.exists(src) else GDM
    (conf / "gdm-password").write_text(pamconf.render(base, entry=str(entry)))
    for inc in ("common-auth", "common-account", "common-session", "common-password"):
        if os.path.exists(f"/etc/pam.d/{inc}"):
            shutil.copy(f"/etc/pam.d/{inc}", conf)
    return conf


@pytest.fixture()
def stack(tmp_path, monkeypatch):
    try:
        pwd.getpwnam(FAKE_USER)
        pytest.skip(f"user {FAKE_USER} exists")
    except KeyError:
        pass
    data = tmp_path / "data"
    monkeypatch.setattr("quickunlock.paths.DATA_ROOT", str(data))
    from quickunlock import pin
    from quickunlock.store import UserStore
    for user in (ME, FAKE_USER):
        s = UserStore(user)
        s.save_pin_record(pin.make_record(GOOD_PIN))
        cfg = s.config()
        cfg.update(pin_enabled=True, require_password_after_boot=False)
        s.save_config(cfg)
    return tmp_path, data


def test_correct_pin_unlocks_with_one_prompt(stack):
    tmp, data = stack
    rc, prompts, _ = authenticate(_make_stack(tmp, data, "a"), ME, [GOOD_PIN])
    assert rc == PAM_SUCCESS and len(prompts) == 1


def test_password_is_not_asked_twice(stack):
    """Text that isn't the PIN goes on to pam_unix without a second prompt."""
    tmp, data = stack
    conf = _make_stack(tmp, data, "b")
    for user in (FAKE_USER, "qutestnodata"):          # with and without QuickUnlock data
        rc, prompts, _ = authenticate(conf, user, ["not-a-pin", "second-prompt"])
        assert rc != PAM_SUCCESS and len(prompts) == 1


def test_lockout_message_reaches_the_screen(stack):
    tmp, data = stack
    conf = _make_stack(tmp, data, "c")
    infos = []
    for _ in range(3):
        rc, _, i = authenticate(conf, FAKE_USER, ["0000"])
        assert rc != PAM_SUCCESS
        infos += i
    assert any("PIN is locked" in m for m in infos)


def test_hung_quickunlock_falls_through_to_password(stack):
    tmp, data = stack
    mark = tmp / "hung-started"
    conf = _make_stack(tmp, data, "d", "import time, pathlib; pamauth.BUDGET = 2; "
                                       f"pamauth._pin = lambda *a: (pathlib.Path({json.dumps(str(mark))})"
                                       ".touch(), time.sleep(60));")
    t0 = time.monotonic()
    rc, prompts, _ = authenticate(conf, FAKE_USER, [GOOD_PIN, "second-prompt"])
    assert mark.exists()                       # QuickUnlock really hung ...
    assert rc != PAM_SUCCESS and len(prompts) == 1
    assert time.monotonic() - t0 < 10          # ... and the stack moved on anyway
