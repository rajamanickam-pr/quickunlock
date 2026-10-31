"""Unit tests that run without root, a camera or a microphone.

    QUICKUNLOCK_MODELS=data/models python3 -m pytest tests
"""

import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("QUICKUNLOCK_MODELS", os.path.join(ROOT, "data", "models"))


@pytest.fixture()
def datadir(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setattr("quickunlock.paths.DATA_ROOT", d)
    yield d
    shutil.rmtree(d)


# ---------------------------------------------------------------- PIN
def test_pin_validation():
    from quickunlock import pin
    for bad in ("12", "abcd", "1111", "1234", "4321", "1234567890123", "12 34"):
        with pytest.raises(ValueError):
            pin.validate(bad)
    pin.validate("2580")
    pin.validate("917364")


def test_pin_attempts_and_lockout(datadir):
    from quickunlock import pin
    from quickunlock.store import UserStore
    s = UserStore("alice")
    s.save_pin_record(pin.make_record("2580"))
    assert pin.attempt(s, "2580", now=0)[0] == pin.PinResult.OK
    assert pin.attempt(s, "1111", now=0)[0] == pin.PinResult.WRONG
    assert pin.attempt(s, "1112", now=0)[0] == pin.PinResult.WRONG
    res, wait = pin.attempt(s, "1113", now=0)        # 3rd failure -> delay
    assert res == pin.PinResult.LOCKED and wait == 30
    assert pin.attempt(s, "2580", now=10)[0] == pin.PinResult.LOCKED   # even right PIN
    assert pin.attempt(s, "2580", now=31)[0] == pin.PinResult.OK       # counter resets
    t = 100.0
    for _ in range(9):
        res, wait = pin.attempt(s, "0000", now=t)
        t += wait + 1
    assert pin.attempt(s, "0000", now=t)[0] == pin.PinResult.HARD_LOCKED
    assert pin.attempt(s, "2580", now=t + 10_000)[0] == pin.PinResult.HARD_LOCKED


def test_status_has_no_secrets(datadir):
    from quickunlock import pin, paths
    from quickunlock.store import UserStore, read_public_status
    s = UserStore("bob")
    s.save_pin_record(pin.make_record("2580"))
    raw = open(paths.status_file("bob")).read()
    assert "hash" not in raw and "salt" not in raw
    assert read_public_status("bob")["pin_set"] is True
    assert oct(os.stat(s.dir).st_mode & 0o777) == "0o700"


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


# ---------------------------------------------------------------- voice
def test_fbank_shape():
    from quickunlock.voice import fbank
    w = np.random.default_rng(0).standard_normal(16000).astype(np.float32) * 1000
    assert fbank(w).shape == (98, 80)


def _espeak(text, voice):
    if not shutil.which("espeak-ng"):
        pytest.skip("espeak-ng not installed")
    out = subprocess.run(["espeak-ng", "-v", voice, "-s", "150", "--stdout", text],
                         capture_output=True, check=True).stdout
    import wave, io
    with wave.open(io.BytesIO(out)) as w:
        sr = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32)
    import cv2
    return cv2.resize(a[None], (int(len(a) * 16000 / sr), 1))[0]


def test_voice_same_speaker_scores_higher():
    from quickunlock.voice import VoiceEngine
    eng = VoiceEngine()
    phrases = ["my voice is my password, please let me in",
               "the quick brown fox jumps over the lazy dog today",
               "open the computer for me right now please"]
    a = np.stack([eng.embed(_espeak(p, "en-us+m3")) for p in phrases])
    same = eng.embed(_espeak("unlock my laptop, it is me again", "en-us+m3"))
    other = eng.embed(_espeak("unlock my laptop, it is me again", "en-gb-x-rp+f4"))
    s_same, s_other = VoiceEngine.similarity(same, a), VoiceEngine.similarity(other, a)
    print("same", s_same, "other", s_other)
    assert s_same > s_other + 0.1


def test_voice_rejects_silence():
    from quickunlock.voice import VoiceEngine
    with pytest.raises(ValueError):
        VoiceEngine().embed(np.zeros(16000 * 3, np.float32))


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
    assert not pamconf.install(str(f))                 # idempotent
    assert pamconf.uninstall(str(f))
    assert f.read_text() == GDM
    assert (tmp_path / "gdm-password.quickunlock-backup").read_text() == GDM


# ---------------------------------------------------------------- helper validation
def test_helper_config_validation():
    from quickunlock.helper import HelperError, _clean_config
    assert _clean_config({"face_threshold": 0.5}) == {"face_threshold": 0.5}
    for bad in ({"face_threshold": 5}, {"camera": "/etc/shadow"}, {"microphone": "x;rm -rf"},
                {"nope": 1}, {"pin_enabled": "yes"}):
        with pytest.raises(HelperError):
            _clean_config(bad)


def test_helper_embedding_validation():
    from quickunlock.helper import HelperError, _embeddings
    good = np.eye(128, dtype=np.float32)[:3].tolist()
    assert _embeddings({"embeddings": good}, "face").shape == (3, 128)
    for bad in ([[0.0] * 128], [[1.0] * 64], [[float("nan")] + [0.0] * 127], [[1.0] + [0.0] * 127] * 30):
        with pytest.raises(HelperError):
            _embeddings({"embeddings": bad}, "face")


# ---------------------------------------------------------------- PAM entry point
def _pam_env(monkeypatch, user="carol"):
    for k, v in {"PAM_TYPE": "auth", "PAM_SERVICE": "gdm-password", "PAM_USER": user}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("PAM_RHOST", raising=False)


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
