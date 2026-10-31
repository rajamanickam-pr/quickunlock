"""Speaker verification ("voice unlock").

Uses a WeSpeaker ResNet-34 speaker-embedding network trained on VoxCeleb.
The convolutional trunk runs in cv2.dnn; statistics pooling and the final
projection are done in numpy (OpenCV 4.x cannot import those ONNX ops).

This recognises *who* is speaking, not *what* is said. A recording of the
user's voice can fool it, so voice unlock is labelled experimental.
"""

import shutil
import subprocess

import numpy as np

from . import paths

SR = 16000


# --------------------------------------------------------------------------
# Kaldi-compatible 80-dim log-mel filterbank (what WeSpeaker was trained on)
# --------------------------------------------------------------------------
_FRAME_LEN, _FRAME_SHIFT, _NFFT, _NMEL = 400, 160, 512, 80
_EPS = np.finfo(np.float32).eps


def _mel(f):
    return 1127.0 * np.log(1.0 + f / 700.0)


def _mel_banks() -> np.ndarray:
    nbins = _NFFT // 2
    low, high = _mel(20.0), _mel(SR / 2)
    delta = (high - low) / (_NMEL + 1)
    freqs = (SR / _NFFT) * np.arange(nbins)
    mels = _mel(freqs)
    banks = np.zeros((_NMEL, nbins + 1), dtype=np.float64)
    for m in range(_NMEL):
        left, center, right = low + m * delta, low + (m + 1) * delta, low + (m + 2) * delta
        up = (mels - left) / (center - left)
        down = (right - mels) / (right - center)
        banks[m, :nbins] = np.maximum(0.0, np.minimum(up, down))
    return banks


_BANKS = _mel_banks()
_WINDOW = 0.54 - 0.46 * np.cos(2 * np.pi * np.arange(_FRAME_LEN) / (_FRAME_LEN - 1))


def fbank(wave: np.ndarray) -> np.ndarray:
    """wave: float samples in int16 scale (-32768..32767). Returns (T, 80)."""
    wave = np.asarray(wave, dtype=np.float64)
    if len(wave) < _FRAME_LEN:
        return np.zeros((0, _NMEL), dtype=np.float32)
    n = 1 + (len(wave) - _FRAME_LEN) // _FRAME_SHIFT
    idx = np.arange(_FRAME_LEN)[None, :] + _FRAME_SHIFT * np.arange(n)[:, None]
    frames = wave[idx]
    frames = frames - frames.mean(axis=1, keepdims=True)                 # remove DC
    frames = np.concatenate([frames[:, :1] - 0.97 * frames[:, :1],
                             frames[:, 1:] - 0.97 * frames[:, :-1]], axis=1)  # pre-emphasis
    frames = frames * _WINDOW
    spec = np.abs(np.fft.rfft(frames, n=_NFFT)) ** 2                     # (n, 257)
    mel = spec @ _BANKS.T
    return np.log(np.maximum(mel, _EPS)).astype(np.float32)


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------
def list_microphones() -> list[tuple[str, str]]:
    """[(alsa_device, label)] from `arecord -l`."""
    if not shutil.which("arecord"):
        return []
    try:
        out = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    mics = []
    for line in out.splitlines():
        # card 1: PCH [HDA Intel PCH], device 0: ALC257 Analog [ALC257 Analog]
        if line.startswith("card ") and ", device " in line:
            try:
                card = line.split(":")[0].split()[1]
                dev = line.split(", device ")[1].split(":")[0]
                label = line.split("[")[1].split("]")[0] + " — " + line.split("[")[2].split("]")[0]
            except IndexError:
                continue
            mics.append((f"plughw:{card},{dev}", label))
    return mics


def record(seconds: float, device: str = "auto") -> np.ndarray:
    """Record mono 16 kHz audio with arecord. Returns int16-scale float array.

    In the user's session "default" goes through PipeWire. At the lock screen
    the helper runs as root without a sound server, so "auto" there falls
    back to the first hardware capture device."""
    devices = [device] if device not in ("auto", "", None) else \
        ["default"] + [d for d, _ in list_microphones()]
    last_err = "no microphone found"
    for dev in devices:
        try:
            p = subprocess.run(["arecord", "-q", "-D", dev, "-f", "S16_LE", "-r", str(SR), "-c", "1",
                                "-t", "raw", "-d", str(max(1, int(round(seconds))))],
                               capture_output=True, timeout=seconds + 5)
        except (OSError, subprocess.TimeoutExpired) as e:
            last_err = str(e)
            continue
        if p.returncode == 0 and len(p.stdout) > SR:
            return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32)
        last_err = p.stderr.decode(errors="replace").strip() or f"arecord failed on {dev}"
    raise RuntimeError(last_err)


def trim_silence(wave: np.ndarray, frame: int = 320) -> np.ndarray:
    """Drop leading/trailing silence using a simple energy gate."""
    if len(wave) < frame * 4:
        return wave
    n = len(wave) // frame
    e = np.sqrt((wave[: n * frame].reshape(n, frame) ** 2).mean(axis=1))
    gate = max(200.0, 0.1 * np.percentile(e, 95))
    voiced = np.where(e > gate)[0]
    if len(voiced) == 0:
        return wave[:0]
    a, b = max(0, voiced[0] - 5), min(n, voiced[-1] + 6)
    return wave[a * frame: b * frame]


def speech_seconds(wave: np.ndarray) -> float:
    return len(trim_silence(wave)) / SR


# --------------------------------------------------------------------------
# Embedding
# --------------------------------------------------------------------------
class VoiceEngine:
    def __init__(self):
        import cv2
        self.net = cv2.dnn.readNetFromONNX(paths.SPEAKER_TRUNK_MODEL)
        head = np.load(paths.SPEAKER_HEAD, allow_pickle=False)
        self.W, self.b, self.mean = head["W"], head["b"], head["mean"]

    def embed(self, wave: np.ndarray) -> np.ndarray:
        speech = trim_silence(wave)
        if len(speech) < SR:                      # need at least ~1 s of speech
            raise ValueError("Not enough speech detected - please speak a bit louder or longer")
        feats = fbank(speech)
        feats = feats - feats.mean(axis=0, keepdims=True)        # CMN
        x = np.ascontiguousarray(feats.T[None, None, :, :], dtype=np.float32)  # 1,1,80,T
        self.net.setInput(x)
        f = self.net.forward()                                    # 1,256,10,T'
        f = f.reshape(f.shape[0], -1, f.shape[-1])
        mu = f.mean(-1)
        n = f.shape[-1]
        sd = np.sqrt(((f - f.mean(-1, keepdims=True)) ** 2).sum(-1) / max(n - 1, 1) + 1e-7)
        z = np.concatenate([mu.reshape(1, -1), sd.reshape(1, -1)], axis=1)
        emb = (z @ self.W.T + self.b - self.mean).flatten().astype(np.float32)
        return emb / (np.linalg.norm(emb) + 1e-9)

    @staticmethod
    def similarity(emb: np.ndarray, templates: np.ndarray) -> float:
        t = templates / (np.linalg.norm(templates, axis=1, keepdims=True) + 1e-9)
        centroid = t.mean(axis=0)
        centroid /= np.linalg.norm(centroid) + 1e-9
        # Average of best single match and the centroid match: more stable.
        return float(0.5 * np.max(t @ emb) + 0.5 * centroid @ emb)
