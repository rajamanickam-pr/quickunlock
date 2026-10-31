"""PIN hashing and verification with brute-force protection."""

import base64
import binascii
import hashlib
import hmac
import os
import time

from .store import PIN_HARD_LOCK_FAILS, PIN_MAX_FAILS_BEFORE_DELAY, UserStore, clean_state

MIN_LEN, MAX_LEN = 4, 12
# scrypt cost: ~50-100 ms per check, 32 MiB RAM.
_N, _R, _P = 2 ** 15, 8, 1
_MAXMEM = 64 * 1024 * 1024
MAX_DELAY = 900.0

# Phone-keypad lines and other well-known picks that the pattern rules
# below don't catch.
_COMMON = {
    "2580", "0852", "1470", "0741", "7410", "0147", "3690", "0963", "9630", "0369",
    "1590", "0951", "7530", "0357", "1379", "1397", "7913", "7931",
    "1004", "0007", "0069", "5683", "1478", "1236", "2468", "1357", "8520",
    "147258", "258369", "159753", "741852", "852963", "963852", "147852", "789456",
}


def _is_sequence(pin: str) -> bool:
    return pin in "0123456789012" or pin in "9876543210987"


def _is_repeated_block(pin: str) -> bool:
    """1212, 123123, 4545 ... (a shorter block repeated)."""
    n = len(pin)
    return any(n % k == 0 and pin == pin[:k] * (n // k) for k in range(1, n // 2 + 1))


def _is_doubled(pin: str) -> bool:
    """1122, 112233, 7788 ... (every digit typed twice)."""
    return len(pin) % 2 == 0 and pin[0::2] == pin[1::2]


def validate(pin: str) -> None:
    if not pin.isdigit() or not pin.isascii():
        raise ValueError("PIN must contain digits only")
    if not MIN_LEN <= len(pin) <= MAX_LEN:
        raise ValueError(f"PIN must be {MIN_LEN}-{MAX_LEN} digits long")
    if len(set(pin)) == 1:
        raise ValueError("PIN cannot be a single repeated digit")
    if _is_sequence(pin):
        raise ValueError("PIN cannot be a simple sequence")
    if _is_repeated_block(pin) or _is_doubled(pin):
        raise ValueError("PIN cannot be a repeating pattern")
    if len(pin) == 4 and 1950 <= int(pin) <= 2029:
        raise ValueError("PIN cannot be a year")
    if pin in _COMMON:
        raise ValueError("This PIN is too common. Choose a less predictable one")


def looks_like_pin(token: str) -> bool:
    return token.isascii() and token.isdigit() and MIN_LEN <= len(token) <= MAX_LEN


def make_record(pin: str) -> dict:
    validate(pin)
    salt = os.urandom(16)
    h = hashlib.scrypt(pin.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM, dklen=32)
    return {"alg": "scrypt", "n": _N, "r": _R, "p": _P,
            "salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(h).decode()}


def valid_record(rec) -> bool:
    """Reject damaged or hostile records before they reach scrypt."""
    try:
        n, r, p = rec["n"], rec["r"], rec["p"]
        if rec.get("alg") != "scrypt" or not all(type(x) is int for x in (n, r, p)):
            return False
        if not (2 ** 10 <= n <= 2 ** 20 and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 4):
            return False
        if 128 * r * (n + 2) + 128 * r * p > _MAXMEM:      # OpenSSL's own memory formula
            return False
        salt = base64.b64decode(rec["salt"], validate=True)
        want = base64.b64decode(rec["hash"], validate=True)
        return len(salt) >= 16 and len(want) == 32
    except (KeyError, TypeError, AttributeError, binascii.Error, ValueError):
        return False


def _matches(rec: dict, pin: str) -> bool:
    salt = base64.b64decode(rec["salt"])
    want = base64.b64decode(rec["hash"])
    got = hashlib.scrypt(pin.encode(), salt=salt, n=rec["n"], r=rec["r"], p=rec["p"],
                         maxmem=_MAXMEM, dklen=len(want))
    return hmac.compare_digest(got, want)


def lock_delay(fails: int) -> float:
    """Seconds the PIN is locked after `fails` consecutive failures."""
    if fails < PIN_MAX_FAILS_BEFORE_DELAY:
        return 0.0
    return min(30.0 * 2 ** (fails - PIN_MAX_FAILS_BEFORE_DELAY), MAX_DELAY)


class PinResult:
    OK = "ok"
    WRONG = "wrong"
    LOCKED = "locked"            # temporary delay
    HARD_LOCKED = "hard_locked"  # password required, re-enable in the app
    NOT_SET = "not_set"
    BAD_RECORD = "bad_record"    # pin.json damaged: ignored, password still works


def attempt(store: UserStore, token: str, now: float | None = None) -> tuple[str, float]:
    """Check a PIN attempt and update the failure counters.

    Returns (result, seconds_remaining_for_lock)."""
    now = time.time() if now is None else now
    rec = store.pin_record()
    if rec is None:
        return PinResult.NOT_SET, 0.0
    if not valid_record(rec):
        return PinResult.BAD_RECORD, 0.0

    with store.locked():
        st = store.state()
        if st.get("pin_hard_locked"):
            return PinResult.HARD_LOCKED, 0.0
        # Clamp, so a clock that jumped backwards can't stretch the lock.
        remaining = min(float(st.get("pin_locked_until", 0)) - now, MAX_DELAY)
        if remaining > 0:
            return PinResult.LOCKED, remaining

        # Record the attempt as a failure *before* checking it. If the check
        # is interrupted (watchdog, crash, power cut) the guess still counts.
        fails = int(st.get("pin_fails", 0)) + 1
        delay = lock_delay(fails)
        hard = fails >= PIN_HARD_LOCK_FAILS
        store.save_state({"pin_fails": fails, "pin_locked_until": now + delay if delay else 0.0,
                          "pin_hard_locked": hard})

        if _matches(rec, token):
            store.save_state(clean_state())
            return PinResult.OK, 0.0
    if hard:
        return PinResult.HARD_LOCKED, 0.0
    return (PinResult.LOCKED, delay) if delay else (PinResult.WRONG, 0.0)
