"""Privileged helper, run through pkexec by the settings app.

    pkexec /usr/libexec/quickunlock/quickunlock-helper <command>

Commands read their input as JSON on stdin and print JSON on stdout.
The helper only ever acts on the account of the user who ran pkexec
(PKEXEC_UID); polkit asks for that user's own password first.
"""

import json
import math
import os
import pwd
import sys

from . import pin as pinmod
from . import pamconf
from .store import UserStore

LIMITS = {"face": (128, 20), "voice": (256, 10)}   # (dimension, max samples)

CONFIG_TYPES = {
    "pin_enabled": bool, "face_enabled": bool, "voice_enabled": bool,
    "require_password_after_boot": bool,
    "face_threshold": (0.30, 0.80), "face_timeout": (1.0, 10.0),
    "voice_threshold": (0.30, 0.90), "voice_seconds": (2.0, 6.0),
    "camera": str, "microphone": str,
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


def run(cmd: str, data: dict, user: str) -> dict:
    store = UserStore(user)
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
        store.save_pin_record(rec)
        store.save_state({"pin_fails": 0, "pin_locked_until": 0.0, "pin_hard_locked": False})
        cfg = store.config()
        cfg["pin_enabled"] = True
        store.save_config(cfg)
        return {"ok": True}

    if cmd in ("clear-pin", "clear-face", "clear-voice"):
        kind = cmd.split("-")[1]
        cfg = store.config()
        cfg[f"{kind}_enabled"] = False
        store.save_config(cfg)
        if kind == "pin":
            store.save_pin_record(None)
        else:
            store.save_templates(kind, None)
        return {"ok": True}

    if cmd in ("set-face", "set-voice"):
        kind = cmd.split("-")[1]
        store.save_templates(kind, _embeddings(data, kind))
        cfg = store.config()
        cfg[f"{kind}_enabled"] = True
        store.save_config(cfg)
        return {"ok": True}

    if cmd in ("verify-face", "verify-voice"):
        import numpy as np
        kind = cmd.split("-")[1]
        templates = store.templates(kind)
        if templates is None:
            raise HelperError(f"no {kind} enrolled")
        emb = _embeddings({"embeddings": [data.get("embedding")]}, kind)[0]
        t = templates / np.linalg.norm(templates, axis=1, keepdims=True)
        if kind == "voice":
            from .voice import VoiceEngine
            score = VoiceEngine.similarity(emb, t)
        else:
            score = float(np.max(t @ emb))
        cfg = store.config()
        return {"score": score, "threshold": cfg[f"{kind}_threshold"],
                "match": score >= cfg[f"{kind}_threshold"]}

    if cmd == "set-config":
        upd = _clean_config(data)
        cfg = store.config()
        if upd.get("pin_enabled") and store.pin_record() is None:
            raise HelperError("set a PIN first")
        for kind in ("face", "voice"):
            if upd.get(f"{kind}_enabled") and store.templates(kind) is None:
                raise HelperError(f"set up {kind} unlock first")
        cfg.update(upd)
        store.save_config(cfg)
        return {"ok": True}

    if cmd == "reset-lockout":
        store.save_state({"pin_fails": 0, "pin_locked_until": 0.0, "pin_hard_locked": False})
        return {"ok": True}

    if cmd == "remove-all":
        store.remove_all()
        return {"ok": True}

    raise HelperError(f"unknown command {cmd}")


def main(argv=None) -> int:
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
        result = run(argv[0], data, target_user())
        print(json.dumps(result))
        return 0
    except (HelperError, ValueError) as e:
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
