"""Face detection + recognition using OpenCV's YuNet and SFace models.

Both models ship with the package (MIT / Apache-2.0) and run on the CPU
through cv2.dnn, so the only runtime dependency is python3-opencv.
"""

import glob
import os
import time

import cv2
import numpy as np

from . import paths

MIN_FACE_PX = 64          # ignore faces smaller than this (too far / photo of a photo)
DETECT_SCORE = 0.85


def list_cameras() -> list[str]:
    """Video capture devices, e.g. ['/dev/video0', '/dev/video2']."""
    out = []
    for dev in sorted(glob.glob("/dev/video*"), key=lambda p: int(p[10:]) if p[10:].isdigit() else 99):
        idx_file = f"/sys/class/video4linux/{os.path.basename(dev)}/index"
        try:
            # index 0 is the capture node; higher indexes are metadata nodes
            if os.path.exists(idx_file) and open(idx_file).read().strip() != "0":
                continue
        except OSError:
            pass
        out.append(dev)
    return out


def camera_label(dev: str) -> str:
    try:
        name = open(f"/sys/class/video4linux/{os.path.basename(dev)}/name").read().strip()
    except OSError:
        name = "Camera"
    ir = " (infrared)" if "ir" in name.lower().split() or "infrared" in name.lower() else ""
    return f"{name}{ir} — {dev}"


class Camera:
    def __init__(self, device: str = "auto"):
        self.cap = None
        candidates = list_cameras() if device in ("auto", "", None) else [device]
        for dev in candidates:
            cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                ok, _ = cap.read()
                if ok:
                    self.cap, self.device = cap, dev
                    return
            cap.release()
        raise RuntimeError("No working camera found")

    def read(self):
        ok, frame = self.cap.read()
        if not ok:
            return None
        if frame.ndim == 2 or frame.shape[2] == 1:   # IR cameras deliver grey frames
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        return frame

    def close(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FaceEngine:
    def __init__(self):
        major, minor = (int(x) for x in cv2.__version__.split(".")[:2])
        if (major, minor) < (4, 8):
            raise RuntimeError("OpenCV 4.8 or newer is required (Ubuntu 24.10+)")
        self.detector = cv2.FaceDetectorYN.create(paths.FACE_DETECT_MODEL, "", (320, 320),
                                                  DETECT_SCORE, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(paths.FACE_RECOG_MODEL, "")

    def detect(self, frame):
        """Return the largest face (YuNet row of 15 floats) or None."""
        h, w = frame.shape[:2]
        self.detector.setInputSize((w, h))
        _, faces = self.detector.detect(frame)
        if faces is None or len(faces) == 0:
            return None
        face = max(faces, key=lambda f: f[2] * f[3])
        if min(face[2], face[3]) < MIN_FACE_PX:
            return None
        return face

    def embed(self, frame, face) -> np.ndarray:
        if face is None or len(face) < 14:
            raise ValueError("no face")
        crop = self.recognizer.alignCrop(frame, face)
        feat = self.recognizer.feature(crop).flatten().astype(np.float32)
        return feat / (np.linalg.norm(feat) + 1e-9)

    @staticmethod
    def similarity(emb: np.ndarray, templates: np.ndarray) -> float:
        t = templates / (np.linalg.norm(templates, axis=1, keepdims=True) + 1e-9)
        return float(np.max(t @ emb))

    def verify_stream(self, camera: Camera, templates: np.ndarray, threshold: float,
                      timeout: float, needed: int = 2) -> tuple[bool, float]:
        """Look at the camera until `needed` frames match or time runs out.

        Requiring several matching frames makes a single lucky frame (or a
        quick flash of a photo) less likely to unlock."""
        deadline = time.monotonic() + timeout
        hits, best = 0, -1.0
        while time.monotonic() < deadline:
            frame = camera.read()
            if frame is None:
                continue
            face = self.detect(frame)
            if face is None:
                continue
            score = self.similarity(self.embed(frame, face), templates)
            best = max(best, score)
            if score >= threshold:
                hits += 1
                if hits >= needed:
                    return True, best
        return False, best


def lid_closed() -> bool:
    for f in glob.glob("/proc/acpi/button/lid/*/state"):
        try:
            if "closed" in open(f).read():
                return True
        except OSError:
            pass
    return False
