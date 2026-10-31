"""Entry point executed by pam_exec.so at the GDM login / lock screen.

    quickunlock-pam face    try face unlock            (exit 0 = unlock)
    quickunlock-pam voice   try voice unlock
    quickunlock-pam pin     check the typed token as a PIN (expose_authtok)
    quickunlock-pam reset   password login succeeded: clear PIN lockouts

Any non-zero exit makes PAM fall through to the normal password check, so
a bug here can never lock anybody out - it only skips the shortcut.
Lines written to stdout are shown on the lock screen (pam_exec "stdout").
"""

import os
import subprocess
import sys
import syslog

OK, SKIP = 0, 1
ALLOWED_SERVICES = {"gdm-password"}


def say(msg: str) -> None:
    print(msg, flush=True)


def log(msg: str) -> None:
    syslog.openlog("quickunlock", 0, syslog.LOG_AUTHPRIV)
    syslog.syslog(syslog.LOG_NOTICE, msg)


def user_has_session(user: str) -> bool:
    """True if the user is already logged in (i.e. this is an *unlock*)."""
    try:
        p = subprocess.run(["loginctl", "show-user", user, "--property=State", "--value"],
                           capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0 and p.stdout.strip() in ("active", "online")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mode = argv[0] if argv else ""
    user = os.environ.get("PAM_USER", "")
    service = os.environ.get("PAM_SERVICE", "")
    token = ""
    if mode == "pin":   # always drain the pipe pam_exec writes the password into
        token = sys.stdin.buffer.read(4096).split(b"\0", 1)[0].decode("utf-8", "replace").rstrip("\n")

    if os.environ.get("PAM_TYPE") != "auth" or service not in ALLOWED_SERVICES:
        return SKIP
    if os.environ.get("PAM_RHOST"):
        return SKIP                      # never for remote logins

    from .store import UserStore, check_user
    try:
        check_user(user)
    except ValueError:
        return SKIP
    store = UserStore(user)
    if not os.path.isdir(store.dir):
        return SKIP

    if mode == "reset":
        st = store.state()
        if st.get("pin_fails") or st.get("pin_hard_locked"):
            store.save_state({"pin_fails": 0, "pin_locked_until": 0.0, "pin_hard_locked": False})
        return SKIP

    cfg = store.config()
    if cfg.get("require_password_after_boot", True) and not user_has_session(user):
        return SKIP

    try:
        if mode == "pin":
            return _pin(store, cfg, token)
        if mode == "face":
            return _face(store, cfg)
        if mode == "voice":
            return _voice(store, cfg)
    except Exception as e:  # never break the login screen
        log(f"{mode} error for {user}: {e!r}")
    return SKIP


def _pin(store, cfg, token: str) -> int:
    if not cfg.get("pin_enabled"):
        return SKIP
    from . import pin
    if not pin.looks_like_pin(token):
        return SKIP                       # probably the real password
    result, remaining = pin.attempt(store, token)
    if result == pin.PinResult.OK:
        log(f"PIN unlock for {store.user}")
        return OK
    if result == pin.PinResult.LOCKED:
        say(f"Too many wrong PINs. PIN is locked for {int(remaining + 0.99)} seconds - "
            "use your password instead.")
    elif result == pin.PinResult.HARD_LOCKED:
        say("PIN is disabled after too many wrong attempts. Sign in with your password.")
    log(f"PIN failure for {store.user}: {result}")
    return SKIP


def _face(store, cfg) -> int:
    if not cfg.get("face_enabled"):
        return SKIP
    templates = store.templates("face")
    if templates is None or len(templates) == 0:
        return SKIP
    from . import face
    if face.lid_closed():
        return SKIP
    say("Looking for your face…")
    engine = face.FaceEngine()
    try:
        cam = face.Camera(cfg.get("camera", "auto"))
    except RuntimeError:
        return SKIP
    with cam:
        ok, score = engine.verify_stream(cam, templates, float(cfg["face_threshold"]),
                                         float(cfg["face_timeout"]))
    log(f"face {'match' if ok else 'no match'} for {store.user} (score {score:.3f})")
    if ok:
        say("Face recognised.")
        return OK
    return SKIP


def _voice(store, cfg) -> int:
    if not cfg.get("voice_enabled"):
        return SKIP
    templates = store.templates("voice")
    if templates is None or len(templates) == 0:
        return SKIP
    from . import voice
    say("Say your voice phrase now…")
    wave = voice.record(float(cfg["voice_seconds"]), cfg.get("microphone", "auto"))
    engine = voice.VoiceEngine()
    try:
        emb = engine.embed(wave)
    except ValueError:
        say("Didn't catch that.")
        return SKIP
    score = engine.similarity(emb, templates)
    ok = score >= float(cfg["voice_threshold"])
    log(f"voice {'match' if ok else 'no match'} for {store.user} (score {score:.3f})")
    if ok:
        say("Voice recognised.")
        return OK
    return SKIP


if __name__ == "__main__":
    sys.exit(main())
