"""Unit tests that run without root or a camera.

    QUICKUNLOCK_MODELS=data/models python3 -m pytest tests
"""

import io
import json
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("QUICKUNLOCK_MODELS", os.path.join(ROOT, "data", "models"))

GOOD_PIN = "4831"


@pytest.fixture()
def datadir(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setattr("quickunlock.paths.DATA_ROOT", d)
    yield d
    shutil.rmtree(d)


def _store_with_pin(user="alice", pin_code=GOOD_PIN):
    from quickunlock import pin
    from quickunlock.store import UserStore
    s = UserStore(user)
    s.save_pin_record(pin.make_record(pin_code))
    return s


# ---------------------------------------------------------------- PIN rules
def test_pin_validation():
    from quickunlock import pin
    bad = ("12", "abcd", "12 34", "1234567890123",       # format
           "1111", "1234", "4321", "8901",                # repeat / sequence
           "1212", "4545", "123123", "1122", "112233",    # patterns
           "1990", "2001", "1950", "2029",                # years
           "2580", "0852", "1470", "1004", "147258")      # keypad / common
    for b in bad:
        with pytest.raises(ValueError):
            pin.validate(b)
    for good in (GOOD_PIN, "917364", "60381", "1949", "2030", "5821"):
        pin.validate(good)


# ---------------------------------------------------------------- PIN attempts
def test_pin_attempts_and_lockout(datadir):
    from quickunlock import pin
    s = _store_with_pin()
    assert pin.attempt(s, GOOD_PIN, now=0)[0] == pin.PinResult.OK
    assert pin.attempt(s, "1111", now=0)[0] == pin.PinResult.WRONG
    assert pin.attempt(s, "1112", now=0)[0] == pin.PinResult.WRONG
    res, wait = pin.attempt(s, "1113", now=0)        # 3rd failure -> delay
    assert res == pin.PinResult.LOCKED and wait == 30
    assert pin.attempt(s, GOOD_PIN, now=10)[0] == pin.PinResult.LOCKED   # even right PIN
    assert pin.attempt(s, GOOD_PIN, now=31)[0] == pin.PinResult.OK       # counter resets
    assert s.state()["pin_fails"] == 0
    t = 100.0
    for _ in range(9):
        res, wait = pin.attempt(s, "0000", now=t)
        t += wait + 1
    assert pin.attempt(s, "0000", now=t)[0] == pin.PinResult.HARD_LOCKED
    assert pin.attempt(s, GOOD_PIN, now=t + 10_000)[0] == pin.PinResult.HARD_LOCKED


def test_interrupted_attempt_still_counts(datadir, monkeypatch):
    """A check killed half-way (watchdog, crash) must not be a free guess."""
    from quickunlock import pin

    s = _store_with_pin()

    def killed(*_a):
        raise KeyboardInterrupt
    monkeypatch.setattr(pin, "_matches", killed)
    with pytest.raises(KeyboardInterrupt):
        pin.attempt(s, "0000", now=0)
    assert s.state()["pin_fails"] == 1


def _wrong_attempt(data_root, now):
    from quickunlock import paths, pin
    from quickunlock.store import UserStore
    paths.DATA_ROOT = data_root
    return pin.attempt(UserStore("alice"), "0000", now=now)[0]


def test_concurrent_attempts_cannot_skip_lockout(datadir):
    """Eight parallel wrong guesses: exactly 3 may be checked, then the lock holds."""
    from quickunlock import pin
    s = _store_with_pin()
    ctx = multiprocessing.get_context("fork")
    with ctx.Pool(8) as pool:
        results = pool.starmap(_wrong_attempt, [(datadir, 0.0)] * 8)
    assert s.state()["pin_fails"] == 3
    assert results.count(pin.PinResult.WRONG) == 2
    assert results.count(pin.PinResult.LOCKED) == 6


def test_lock_clamped_after_clock_jump(datadir):
    from quickunlock import pin
    s = _store_with_pin()
    s.save_state({"pin_fails": 5, "pin_locked_until": 10.0 ** 9, "pin_hard_locked": False})
    res, remaining = pin.attempt(s, GOOD_PIN, now=1000.0)
    assert res == pin.PinResult.LOCKED and remaining <= pin.MAX_DELAY


def test_corrupt_state_fails_closed_until_password(datadir, monkeypatch):
    from quickunlock import pamauth, pin
    s = _store_with_pin("carol")
    open(os.path.join(s.dir, "state.json"), "w").write("{not json")
    assert pin.attempt(s, GOOD_PIN)[0] == pin.PinResult.HARD_LOCKED
    _pam_env(monkeypatch)
    pamauth.main(["reset"])                             # what a password login runs
    assert pin.attempt(s, GOOD_PIN)[0] == pin.PinResult.OK


def test_damaged_pin_record_is_ignored(datadir):
    from quickunlock import pin
    s = _store_with_pin()
    good = s.pin_record()
    for bad in ({**good, "n": 2 ** 30}, {**good, "n": 1000}, {**good, "alg": "md5"},
                {**good, "salt": "!!"}, {**good, "r": "8"}, {}):
        s.save_pin_record(bad)
        assert pin.attempt(s, GOOD_PIN)[0] == pin.PinResult.BAD_RECORD
    assert s.state()["pin_fails"] == 0


def test_status_has_no_secrets(datadir):
    from quickunlock import paths
    from quickunlock.store import read_public_status
    s = _store_with_pin("bob")
    raw = open(paths.status_file("bob")).read()
    assert "hash" not in raw and "salt" not in raw
    assert read_public_status("bob")["pin_set"] is True
    assert oct(os.stat(s.dir).st_mode & 0o777) == "0o700"


def test_old_voice_settings_are_dropped(datadir):
    from quickunlock.store import UserStore
    s = UserStore("dave")
    s.ensure()
    json.dump({"voice_enabled": True, "microphone": "plughw:0,0", "face_threshold": 0.5},
              open(os.path.join(s.dir, "config.json"), "w"))
    cfg = s.config()
    assert "voice_enabled" not in cfg and "microphone" not in cfg and cfg["face_threshold"] == 0.5


def test_bad_usernames():
    from quickunlock.store import check_user
    for bad in ("../etc", "", "a/b", "Root", "x" * 40):
        with pytest.raises(ValueError):
            check_user(bad)


# ---------------------------------------------------------------- face
def _face_img():
    pytest.importorskip("skimage")
    import cv2
    from skimage import data
    return cv2.cvtColor(data.astronaut(), cv2.COLOR_RGB2BGR)


def test_face_detect_and_match():
    import cv2
    from quickunlock.face import FaceEngine
    img = _face_img()
    eng = FaceEngine()
    f = eng.detect(img)
    assert f is not None
    e1 = eng.embed(img, f)
    # same person, different lighting / scale / slight rotation
    var = cv2.convertScaleAbs(img, alpha=0.8, beta=20)
    var = cv2.resize(var, None, fx=0.95, fy=0.95)
    m = cv2.getRotationMatrix2D((var.shape[1] / 2, var.shape[0] / 2), 8, 1.0)
    var = cv2.warpAffine(var, m, (var.shape[1], var.shape[0]))
    f2 = eng.detect(var)
    e2 = eng.embed(var, f2)
    assert FaceEngine.similarity(e2, e1[None]) > 0.6
    # no face in a blank image
    assert eng.detect(np.full((480, 640, 3), 128, np.uint8)) is None


def test_face_rejects_other_embedding():
    from quickunlock.face import FaceEngine
    rng = np.random.default_rng(1)
    t = rng.standard_normal((3, 128)).astype(np.float32)
    other = rng.standard_normal(128).astype(np.float32)
    other /= np.linalg.norm(other)
    assert FaceEngine.similarity(other, t) < 0.4


# ---------------------------------------------------------------- PAM config
GDM = """#%PAM-1.0
auth    requisite       pam_nologin.so
auth\trequired\tpam_succeed_if.so user != root quiet_success
@include common-auth
auth    optional        pam_gnome_keyring.so
@include common-account
session optional        pam_keyinit.so force revoke
@include common-session
@include common-password
"""


def test_pamconf_roundtrip(tmp_path):
    from quickunlock import pamconf
    f = tmp_path / "gdm-password"
    f.write_text(GDM)
    assert pamconf.install(str(f))
    txt = f.read_text()
    lines = txt.splitlines()
    i_block = lines.index(pamconf.BEGIN)
    i_inc = lines.index("@include common-auth")
    assert lines[i_block - 1].endswith("quiet_success") and i_block < i_inc
    assert lines[i_inc + 1] == pamconf.RBEGIN
    assert " voice" not in txt
    assert not pamconf.install(str(f))                 # idempotent
    assert pamconf.uninstall(str(f))
    assert f.read_text() == GDM
    assert (tmp_path / "gdm-password.quickunlock-backup").read_text() == GDM
    assert not [p for p in os.listdir(tmp_path) if p.startswith(".quickunlock-")]


def test_pamconf_upgrades_old_voice_block():
    from quickunlock import pamconf
    old = GDM.replace("@include common-auth\n", pamconf.BEGIN + "\n"
                      "auth [success=done default=ignore] pam_exec.so quiet stdout /x face\n"
                      "auth [success=done default=ignore] pam_exec.so quiet stdout /x voice\n"
                      "auth [success=done default=ignore] pam_exec.so quiet stdout expose_authtok /x pin\n"
                      + pamconf.END + "\n@include common-auth\n")
    new = pamconf.render(old, entry="/x")
    assert " voice" not in new and new.count(pamconf.BEGIN) == 1


def test_pamconf_check_rejects_bad_stacks():
    from quickunlock import pamconf
    good = pamconf.render(GDM, entry="/x")
    pamconf.check(good, entry="/x")
    swapped = good.replace(" face\n", " pin\n", 1)
    duplicated = good + pamconf.BEGIN + "\n"
    after_password = good.replace("auth\trequired\tpam_succeed_if.so user != root quiet_success",
                                  "auth required pam_unix.so")
    for bad in (swapped, duplicated, after_password):
        with pytest.raises(RuntimeError):
            pamconf.check(bad, entry="/x")


def test_disable_survives_package_upgrade(tmp_path, datadir, monkeypatch, capsys):
    from quickunlock import pamconf
    f = tmp_path / "gdm-password"
    f.write_text(GDM)
    monkeypatch.setattr("quickunlock.paths.PAM_GDM_FILE", str(f))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert pamconf.main(["auto"]) == 0 and pamconf.is_installed(str(f))   # fresh install
    assert pamconf.main(["disable"]) == 0 and not pamconf.is_installed(str(f))
    assert pamconf.main(["auto"]) == 0 and not pamconf.is_installed(str(f))  # upgrade: stays off
    assert pamconf.main(["enable"]) == 0 and pamconf.is_installed(str(f))
    f.write_text(GDM)                                     # gdm3 update replaced the file
    assert pamconf.main(["auto"]) == 0 and pamconf.is_installed(str(f))   # trigger re-applies


# ---------------------------------------------------------------- helper
def test_helper_config_validation():
    from quickunlock.helper import HelperError, _clean_config
    assert _clean_config({"face_threshold": 0.5}) == {"face_threshold": 0.5}
    for bad in ({"face_threshold": 5}, {"camera": "/etc/shadow"}, {"camera": "x;rm -rf"},
                {"nope": 1}, {"pin_enabled": "yes"}, {"voice_enabled": False}):
        with pytest.raises(HelperError):
            _clean_config(bad)


def test_helper_embedding_validation():
    from quickunlock.helper import HelperError, _embeddings
    good = np.eye(128, dtype=np.float32)[:3].tolist()
    assert _embeddings({"embeddings": good}, "face").shape == (3, 128)
    for bad in ([[0.0] * 128], [[1.0] * 64], [[float("nan")] + [0.0] * 127], [[1.0] + [0.0] * 127] * 30):
        with pytest.raises(HelperError):
            _embeddings({"embeddings": bad}, "face")


def test_restricted_helper_only_tightens(datadir):
    from quickunlock.helper import HelperError, run
    from quickunlock.store import UserStore
    s = _store_with_pin("erin")
    run("set-config", {"pin_enabled": True}, "erin")                # full helper: allowed
    allowed = [("set-config", {"pin_enabled": False}),
               ("set-config", {"face_enabled": False, "require_password_after_boot": True}),
               ("set-config", {"face_threshold": 0.60}),
               ("set-config", {"face_timeout": 2.0}),
               ("clear-face", {}), ("status", {})]
    refused = [("set-config", {"pin_enabled": True}),
               ("set-config", {"require_password_after_boot": False}),
               ("set-config", {"face_threshold": 0.30}),
               ("set-config", {"face_timeout": 9.0}),
               ("set-config", {"camera": "/dev/video2"}),
               ("set-config", {"pin_enabled": False, "face_threshold": 0.30}),   # mixed
               ("set-pin", {"pin": "5821"}), ("reset-lockout", {}),
               ("set-face", {"embeddings": np.eye(128)[:1].tolist()}),
               ("verify-face", {"embedding": [1.0] + [0.0] * 127})]
    for cmd, data in refused:
        with pytest.raises(HelperError, match="needs your password"):
            run(cmd, data, "erin", restricted=True)
    assert s.config()["require_password_after_boot"] is True
    for cmd, data in allowed:
        run(cmd, data, "erin", restricted=True)
    assert s.config()["pin_enabled"] is False and s.config()["face_threshold"] == 0.60
    run("clear-pin", {}, "erin", restricted=True)
    run("remove-all", {}, "erin", restricted=True)
    assert UserStore("erin").pin_record() is None


# ---------------------------------------------------------------- PAM entry point
def _pam_env(monkeypatch, user="carol"):
    for k, v in {"PAM_TYPE": "auth", "PAM_SERVICE": "gdm-password", "PAM_USER": user}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("PAM_RHOST", raising=False)


def test_pam_watchdog_kills_a_hung_check(datadir):
    """pam_exec has no timeout: a hang must turn into a quick, non-zero exit."""
    s = _store_with_pin("carol")
    cfg = s.config()
    cfg.update(pin_enabled=True, require_password_after_boot=False)
    s.save_config(cfg)
    code = f"""
import sys, time
sys.path.insert(0, {os.path.join(ROOT, 'src')!r})
from quickunlock import pamauth, paths
paths.DATA_ROOT = {datadir!r}
pamauth.BUDGET = 1
pamauth._pin = lambda *a: time.sleep(60)
sys.exit(pamauth.main(["pin"]))
"""
    env = dict(os.environ, PAM_TYPE="auth", PAM_SERVICE="gdm-password", PAM_USER="carol")
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, "-c", code], input=GOOD_PIN.encode() + b"\0", env=env,
                       timeout=30)
    assert p.returncode == -signal.SIGALRM
    assert time.monotonic() - t0 < 5


def test_pam_pin_unlock_and_reset(datadir, monkeypatch, capsys):
    from quickunlock import pamauth, pin
    s = _store_with_pin("carol")
    cfg = s.config()
    cfg.update(pin_enabled=True, require_password_after_boot=False)
    s.save_config(cfg)
    _pam_env(monkeypatch)
    for _ in range(3):
        pin.attempt(s, "0000", now=0)
    assert s.state()["pin_fails"] == 3
    pamauth.main(["reset"])
    assert s.state()["pin_fails"] == 0
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(GOOD_PIN.encode() + b"\0")))
    assert pamauth.main(["pin"]) == pamauth.OK
    monkeypatch.setenv("PAM_RHOST", "attacker.example")
    assert pamauth.main(["reset"]) == pamauth.SKIP


def test_pam_face_unlock_with_fake_camera(datadir, monkeypatch):
    import cv2
    from quickunlock import face, pamauth
    from quickunlock.store import UserStore
    img = _face_img()
    eng = face.FaceEngine()
    s = UserStore("carol")
    s.save_templates("face", eng.embed(img, eng.detect(img))[None])
    cfg = s.config()
    cfg.update(face_enabled=True, require_password_after_boot=False)
    s.save_config(cfg)

    class FakeCam:
        def __init__(self, dev="auto"):
            pass
        def read(self):
            return cv2.convertScaleAbs(img, alpha=0.9, beta=10)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    monkeypatch.setattr(face, "Camera", FakeCam)
    monkeypatch.setattr(face, "lid_closed", lambda: False)
    _pam_env(monkeypatch)
    assert pamauth.main(["face"]) == pamauth.OK
    # a different "person" must not unlock
    rng = np.random.default_rng(3)
    other = rng.standard_normal((1, 128)).astype(np.float32)
    s.save_templates("face", other / np.linalg.norm(other))
    cfg["face_timeout"] = 1.0
    s.save_config(cfg)
    assert pamauth.main(["face"]) == pamauth.SKIP
    # other PAM services are always refused
    monkeypatch.setenv("PAM_SERVICE", "sudo")
    assert pamauth.main(["face"]) == pamauth.SKIP
