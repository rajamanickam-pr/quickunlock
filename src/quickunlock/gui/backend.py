"""Talks to the privileged helper (via pkexec) without blocking the UI."""

import json
import os
import pwd
import subprocess
import sys
import threading

from gi.repository import GLib

from .. import paths
from ..store import read_public_status

USER = pwd.getpwuid(os.getuid()).pw_name
if os.geteuid() == 0 and os.environ.get("QUICKUNLOCK_USER"):
    USER = os.environ["QUICKUNLOCK_USER"]      # development mode


def _helper_cmd(cmd: str) -> tuple[list[str], dict]:
    env = dict(os.environ)
    if os.geteuid() == 0:
        # Running as root (development / testing): call the helper directly.
        env["QUICKUNLOCK_USER"] = USER
        src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return [sys.executable, "-c",
                f"import sys; sys.path.insert(0, {src!r}); from quickunlock.helper import main; "
                f"sys.exit(main([{cmd!r}]))"], env
    return ["pkexec", paths.HELPER, cmd], env


def call_helper(cmd: str, data: dict | None, callback) -> None:
    """Run a helper command in a thread; callback(result_dict) on the UI thread."""
    def work():
        argv, env = _helper_cmd(cmd)
        try:
            p = subprocess.run(argv, input=json.dumps(data or {}), capture_output=True,
                               text=True, env=env, timeout=120)
            if p.returncode in (126, 127) and not p.stdout:
                res = {"error": "Authentication was cancelled", "cancelled": True}
            else:
                try:
                    res = json.loads(p.stdout.strip().splitlines()[-1])
                except (IndexError, json.JSONDecodeError):
                    res = {"error": (p.stderr or "helper failed").strip()[-300:]}
        except (OSError, subprocess.TimeoutExpired) as e:
            res = {"error": str(e)}
        GLib.idle_add(callback, res)
    threading.Thread(target=work, daemon=True).start()


def run_in_thread(fn, callback, *args) -> None:
    """Run fn(*args) in a thread; callback(result, error) on the UI thread."""
    def work():
        try:
            r, err = fn(*args), None
        except Exception as e:   # surface any error to the UI
            r, err = None, e
        GLib.idle_add(callback, r, err)
    threading.Thread(target=work, daemon=True).start()


def status() -> dict:
    st = read_public_status(USER)
    st["pam_installed"] = _pam_installed()
    return st


def _pam_installed() -> bool:
    from ..pamconf import is_installed
    try:
        return is_installed()
    except PermissionError:
        return True


def enable_pam(callback) -> None:
    def work():
        argv = ["pkexec", "/usr/bin/quickunlock-pam-setup", "enable"]
        if os.geteuid() == 0:
            argv = argv[1:]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=120)
            res = {"ok": p.returncode == 0, "error": p.stderr.strip()}
        except OSError as e:
            res = {"ok": False, "error": str(e)}
        GLib.idle_add(callback, res)
    threading.Thread(target=work, daemon=True).start()
