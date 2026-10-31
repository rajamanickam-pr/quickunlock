"""Privileged helper, run through pkexec by the settings app.

    pkexec /usr/libexec/quickunlock/quickunlock-helper <command>
    pkexec /usr/libexec/quickunlock/quickunlock-helper-restrict <command>

Commands read their input as JSON on stdin and print JSON on stdout.
The helper only ever acts on the account of the user who ran pkexec
(PKEXEC_UID).

The two entry points have separate polkit actions:
  quickunlock-helper           asks for the user's password every time
  quickunlock-helper-restrict  no password, but only accepts changes that
                               make unlocking *harder* (turn a method off,
                               remove data, raise strictness). That check is
                               done here, not by polkit.
"""

import json
import math
import os
import pwd
import sys
import syslog

from . import pin as pinmod
from . import pamconf
from .store import UserStore, clean_state

LIMITS = {"face": (128, 20)}   # (dimension, max samples)

CONFIG_TYPES = {
    "pin_enabled": bool, "face_enabled": bool,
    "require_password_after_boot": bool,
    "face_threshold": (0.30, 0.80), "face_timeout": (1.0, 10.0),
    "camera": str,
}

RESTRICTED_CMDS = {"status", "clear-pin", "clear-face", "remove-all", "set-config"}

AUDIT = {
    "set-pin": "PIN set", "clear-pin": "PIN removed", "clear-face": "face data removed",
    "set-face": "face data enrolled", "reset-lockout": "PIN lockout cleared",
    "remove-all": "all data removed",
}


class HelperError(Exception):
    pass


def target_user() -> str:
    uid = os.environ.get("PKEXEC_UID")
    if uid is not None:
        return pwd.getpwuid(int(uid)).pw_name
    if os.geteuid() == 0 and os.environ.get("QUICKUNLOCK_USER"):
        return os.environ["QUICKUNLOCK_USER"]          # admin / tests
    raise HelperError("must be started through pkexec")


def audit(user: str, msg: str) -> None:
    syslog.openlog("quickunlock-helper", 0, syslog.LOG_AUTHPRIV)
    syslog.syslog(syslog.LOG_NOTICE, f"{user}: {msg}")


def _embeddings(data, kind):
    import numpy as np
    dim, max_n = LIMITS[kind]
    arr = np.asarray(data.get("embeddings"), dtype=np.float32)
    if arr.ndim != 2 or arr.shape[1] != dim or not 1 <= arr.shape[0] <= max_n:
        raise HelperError(f"expected 1-{max_n} {kind} embeddings of size {dim}")
    if not np.all(np.isfinite(arr)):
        raise HelperError("invalid numbers in embeddings")
    norms = np.linalg.norm(arr, axis=1)
    if np.any(norms < 0.5) or np.any(norms > 1.5):
        raise HelperError("embeddings must be normalised")
    return arr / norms[:, None]


def _clean_config(update: dict) -> dict:
    out = {}
    for k, v in update.items():
        t = CONFIG_TYPES.get(k)
        if t is None:
            raise HelperError(f"unknown setting {k}")
        if t is bool:
            if not isinstance(v, bool):
                raise HelperError(f"{k} must be true/false")
        elif t is str:
            if not isinstance(v, str) or len(v) > 128 or any(c in v for c in "\n\0 ;|&$`"):
                raise HelperError(f"bad value for {k}")
            if k == "camera" and v != "auto" and not v.startswith("/dev/video"):
                raise HelperError("camera must be auto or /dev/videoN")
        else:
            lo, hi = t
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) \
                    or not lo <= v <= hi:
                raise HelperError(f"{k} must be between {lo} and {hi}")
            v = float(v)
        out[k] = v
    return out


def tightens(key: str, value, cfg: dict) -> bool:
    """True if setting `key` to `value` can only make unlocking harder."""
    if key.endswith("_enabled"):
        return value is False
    if key == "require_password_after_boot":
        return value is True
    if key == "face_threshold":
        return value >= cfg[key]
    if key == "face_timeout":
        return value <= cfg[key]
    return False          # e.g. camera: switching IR -> RGB weakens face unlock


def run(cmd: str, data: dict, user: str, restricted: bool = False) -> dict:
    store = UserStore(user)
    if restricted:
        if cmd not in RESTRICTED_CMDS:
            raise HelperError("This change needs your password")
        if cmd == "set-config":
            cfg = store.config()
            if not all(tightens(k, v, cfg) for k, v in _clean_config(data).items()):
                raise HelperError("This change needs your password")

    if cmd == "status":
        store.ensure()
        st = store.status()
        st["pam_installed"] = pamconf.is_installed()
        store.write_status()
        return st

    if cmd == "set-pin":
        p = str(data.get("pin", ""))
        try:
            rec = pinmod.make_record(p)
        except ValueError as e:
            raise HelperError(str(e))
        with store.locked():
            store.save_pin_record(rec)
            store.save_state(clean_state())
        cfg = store.config()
        cfg["pin_enabled"] = True
        store.save_config(cfg)
        return {"ok": True}

    if cmd in ("clear-pin", "clear-face"):
        kind = cmd.split("-")[1]
        cfg = store.config()
        cfg[f"{kind}_enabled"] = False
        store.save_config(cfg)
        if kind == "pin":
            store.save_pin_record(None)
        else:
            store.save_templates(kind, None)
        return {"ok": True}

    if cmd == "set-face":
        store.save_templates("face", _embeddings(data, "face"))
        cfg = store.config()
        cfg["face_enabled"] = True
        store.save_config(cfg)
        return {"ok": True}

    if cmd == "verify-face":
        import numpy as np
        templates = store.templates("face")
        if templates is None:
            raise HelperError("no face enrolled")
        emb = _embeddings({"embeddings": [data.get("embedding")]}, "face")[0]
        t = templates / np.linalg.norm(templates, axis=1, keepdims=True)
        score = float(np.max(t @ emb))
        cfg = store.config()
        return {"score": score, "threshold": cfg["face_threshold"],
                "match": score >= cfg["face_threshold"]}

    if cmd == "set-config":
        upd = _clean_config(data)
        cfg = store.config()
        if upd.get("pin_enabled") and store.pin_record() is None:
            raise HelperError("set a PIN first")
        if upd.get("face_enabled") and store.templates("face") is None:
            raise HelperError("set up face unlock first")
        cfg.update(upd)
        store.save_config(cfg)
        return {"ok": True}

    if cmd == "reset-lockout":
        with store.locked():
            store.save_state(clean_state())
        return {"ok": True}

    if cmd == "remove-all":
        store.remove_all()
        return {"ok": True}

    raise HelperError(f"unknown command {cmd}")


def main(argv=None, restricted: bool = False) -> int:
    argv = sys.argv[1:] if argv is None else argv
    os.umask(0o077)
    try:
        if os.geteuid() != 0:
            raise HelperError("helper must run as root (via pkexec)")
        if not argv:
            raise HelperError("no command")
        raw = sys.stdin.read(1_000_000) if not sys.stdin.isatty() else ""
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            raise HelperError("input must be a JSON object")
        user = target_user()
        result = run(argv[0], data, user, restricted)
        if argv[0] in AUDIT:
            audit(user, AUDIT[argv[0]])
        elif argv[0] == "set-config":
            audit(user, "settings changed: " + ", ".join(f"{k}={v}" for k, v in sorted(data.items())))
        print(json.dumps(result))
        return 0
    except (HelperError, ValueError) as e:
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
