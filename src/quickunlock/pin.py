"""PIN hashing and verification with brute-force protection."""

import base64
import hashlib
import hmac
import os
import time

from .store import PIN_HARD_LOCK_FAILS, PIN_MAX_FAILS_BEFORE_DELAY, UserStore

MIN_LEN, MAX_LEN = 4, 12
# scrypt cost: ~50-100 ms per check, 32 MiB RAM.
_N, _R, _P = 2 ** 15, 8, 1


def validate(pin: str) -> None:
    if not pin.isdigit() or not pin.isascii():
        raise ValueError("PIN must contain digits only")
    if not MIN_LEN <= len(pin) <= MAX_LEN:
        raise ValueError(f"PIN must be {MIN_LEN}-{MAX_LEN} digits long")
    if len(set(pin)) == 1:
        raise ValueError("PIN cannot be a single repeated digit")
    if pin in "0123456789012" or pin in "9876543210987":
        raise ValueError("PIN cannot be a simple sequence")


def looks_like_pin(token: str) -> bool:
    return token.isascii() and token.isdigit() and MIN_LEN <= len(token) <= MAX_LEN


def make_record(pin: str) -> dict:
    validate(pin)
    salt = os.urandom(16)
    h = hashlib.scrypt(pin.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=64 * 1024 * 1024, dklen=32)
    return {"alg": "scrypt", "n": _N, "r": _R, "p": _P,
            "salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(h).decode()}


def _matches(rec: dict, pin: str) -> bool:
    if rec.get("alg") != "scrypt":
        return False
    salt = base64.b64decode(rec["salt"])
    want = base64.b64decode(rec["hash"])
    got = hashlib.scrypt(pin.encode(), salt=salt, n=rec["n"], r=rec["r"], p=rec["p"],
                         maxmem=64 * 1024 * 1024, dklen=len(want))
    return hmac.compare_digest(got, want)


def lock_delay(fails: int) -> float:
    """Seconds the PIN is locked after `fails` consecutive failures."""
    if fails < PIN_MAX_FAILS_BEFORE_DELAY:
        return 0.0
    return min(30.0 * 2 ** (fails - PIN_MAX_FAILS_BEFORE_DELAY), 900.0)


class PinResult:
    OK = "ok"
    WRONG = "wrong"
    LOCKED = "locked"            # temporary delay
    HARD_LOCKED = "hard_locked"  # password required, re-enable in the app
    NOT_SET = "not_set"


def attempt(store: UserStore, token: str, now: float | None = None) -> tuple[str, float]:
    """Check a PIN attempt and update the failure counters.

    Returns (result, seconds_remaining_for_lock)."""
    now = time.time() if now is None else now
    rec = store.pin_record()
    if rec is None:
        return PinResult.NOT_SET, 0.0
    st = store.state()
    if st.get("pin_hard_locked"):
        return PinResult.HARD_LOCKED, 0.0
    remaining = float(st.get("pin_locked_until", 0)) - now
    if remaining > 0:
        return PinResult.LOCKED, remaining

    if _matches(rec, token):
        if st.get("pin_fails"):
            store.save_state({"pin_fails": 0, "pin_locked_until": 0.0, "pin_hard_locked": False})
        return PinResult.OK, 0.0

    fails = int(st.get("pin_fails", 0)) + 1
    st["pin_fails"] = fails
    if fails >= PIN_HARD_LOCK_FAILS:
        st["pin_hard_locked"] = True
        store.save_state(st)
        return PinResult.HARD_LOCKED, 0.0
    delay = lock_delay(fails)
    st["pin_locked_until"] = now + delay if delay else 0.0
    store.save_state(st)
    return (PinResult.LOCKED, delay) if delay else (PinResult.WRONG, 0.0)
