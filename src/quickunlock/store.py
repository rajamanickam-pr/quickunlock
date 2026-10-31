"""Per-user storage.

Layout (all owned by root):

  /var/lib/quickunlock/users/<user>/        0700
      config.json     settings (methods enabled, thresholds, devices)
      pin.json        scrypt hash of the PIN
      face.npy        enrolled face embeddings  (N x 128 float32)
      voice.npy       enrolled voice embeddings (N x 256 float32)
      state.json      failed-attempt counters written by the PAM helper
  /var/lib/quickunlock/status/<user>.json   0644, no secrets

Secrets are root-only so that a program running in the user's session
cannot silently swap in somebody else's face or PIN. Changing them goes
through the pkexec helper, which asks for the user's password, just as
Windows asks for your password before you can set a PIN.
"""

import io
import json
import os
import re
import tempfile

from . import paths

USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")

DEFAULT_CONFIG = {
    "pin_enabled": False,
    "face_enabled": False,
    "voice_enabled": False,
    # Only allow PIN/face/voice after you have logged in once with your
    # password since boot (keeps the keyring and disk-encryption story sane).
    "require_password_after_boot": True,
    "face_threshold": 0.45,      # SFace cosine similarity (OpenCV suggests 0.363)
    "face_timeout": 3.0,         # seconds to look for a face at the lock screen
    "camera": "auto",            # "auto" or a device path such as /dev/video2
    "voice_threshold": 0.60,     # speaker-embedding cosine similarity
    "voice_seconds": 3.0,
    "microphone": "auto",        # "auto" or an ALSA device such as plughw:1,0
}

PIN_MAX_FAILS_BEFORE_DELAY = 3
PIN_HARD_LOCK_FAILS = 10


def check_user(user: str) -> str:
    if not user or not USER_RE.match(user):
        raise ValueError(f"invalid user name: {user!r}")
    return user


def _atomic_write(path: str, data: bytes, mode: int) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _read_json(path: str, default):
    try:
        with open(path, "rb") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


class UserStore:
    def __init__(self, user: str):
        self.user = check_user(user)
        self.dir = paths.user_dir(user)

    # -- helpers ---------------------------------------------------------
    def _p(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def ensure(self) -> None:
        os.makedirs(os.path.dirname(self.dir), mode=0o700, exist_ok=True)
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        os.chmod(self.dir, 0o700)

    # -- config ----------------------------------------------------------
    def config(self) -> dict:
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(_read_json(self._p("config.json"), {}))
        return cfg

    def save_config(self, cfg: dict) -> None:
        clean = {k: cfg[k] for k in DEFAULT_CONFIG if k in cfg}
        self.ensure()
        _atomic_write(self._p("config.json"), json.dumps(clean, indent=2).encode(), 0o600)
        self.write_status()

    # -- PIN -------------------------------------------------------------
    def pin_record(self):
        return _read_json(self._p("pin.json"), None)

    def save_pin_record(self, rec) -> None:
        self.ensure()
        if rec is None:
            self._unlink("pin.json")
        else:
            _atomic_write(self._p("pin.json"), json.dumps(rec).encode(), 0o600)
        self.write_status()

    # -- biometric templates --------------------------------------------
    def templates(self, kind: str):
        import numpy as np
        try:
            return np.load(self._p(f"{kind}.npy"), allow_pickle=False)
        except FileNotFoundError:
            return None

    def save_templates(self, kind: str, arr) -> None:
        import numpy as np
        self.ensure()
        if arr is None:
            self._unlink(f"{kind}.npy")
        else:
            buf = io.BytesIO()
            np.save(buf, np.asarray(arr, dtype=np.float32), allow_pickle=False)
            _atomic_write(self._p(f"{kind}.npy"), buf.getvalue(), 0o600)
        self.write_status()

    # -- failure state ---------------------------------------------------
    def state(self) -> dict:
        return _read_json(self._p("state.json"), {"pin_fails": 0, "pin_locked_until": 0.0,
                                                  "pin_hard_locked": False})

    def save_state(self, st: dict) -> None:
        self.ensure()
        _atomic_write(self._p("state.json"), json.dumps(st).encode(), 0o600)
        self.write_status()

    def _unlink(self, name: str) -> None:
        try:
            os.unlink(self._p(name))
        except FileNotFoundError:
            pass

    def remove_all(self) -> None:
        for n in ("config.json", "pin.json", "face.npy", "voice.npy", "state.json"):
            self._unlink(n)
        try:
            os.unlink(paths.status_file(self.user))
        except FileNotFoundError:
            pass

    # -- public status ---------------------------------------------------
    def status(self) -> dict:
        cfg = self.config()
        face = self.templates("face")
        voice = self.templates("voice")
        st = self.state()
        return {
            "pin_set": self.pin_record() is not None,
            "pin_enabled": bool(cfg["pin_enabled"]),
            "pin_hard_locked": bool(st.get("pin_hard_locked")),
            "face_samples": 0 if face is None else int(len(face)),
            "face_enabled": bool(cfg["face_enabled"]),
            "voice_samples": 0 if voice is None else int(len(voice)),
            "voice_enabled": bool(cfg["voice_enabled"]),
            "config": {k: v for k, v in cfg.items() if not k.endswith("_enabled")},
        }

    def write_status(self) -> None:
        p = paths.status_file(self.user)
        os.makedirs(os.path.dirname(p), mode=0o755, exist_ok=True)
        _atomic_write(p, json.dumps(self.status(), indent=2).encode(), 0o644)


def read_public_status(user: str) -> dict:
    """Readable by the unprivileged settings app."""
    default = {"pin_set": False, "pin_enabled": False, "pin_hard_locked": False,
               "face_samples": 0, "face_enabled": False, "voice_samples": 0,
               "voice_enabled": False,
               "config": {k: v for k, v in DEFAULT_CONFIG.items() if not k.endswith("_enabled")}}
    data = _read_json(paths.status_file(check_user(user)), None)
    if data:
        default.update(data)
    return default
