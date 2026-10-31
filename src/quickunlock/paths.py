"""Filesystem locations. Every path can be overridden with an environment
variable so the test-suite can run without root."""

import os

DATA_ROOT = os.environ.get("QUICKUNLOCK_DATA", "/var/lib/quickunlock")
MODEL_DIR = os.environ.get("QUICKUNLOCK_MODELS", "/usr/share/quickunlock/models")
LIBEXEC = os.environ.get("QUICKUNLOCK_LIBEXEC", "/usr/libexec/quickunlock")
PAM_GDM_FILE = os.environ.get("QUICKUNLOCK_PAM_FILE", "/etc/pam.d/gdm-password")

HELPER = os.path.join(LIBEXEC, "quickunlock-helper")
HELPER_RESTRICT = os.path.join(LIBEXEC, "quickunlock-helper-restrict")
PAM_ENTRY = os.path.join(LIBEXEC, "quickunlock-pam")

FACE_DETECT_MODEL = os.path.join(MODEL_DIR, "face_detection_yunet_2023mar.onnx")
FACE_RECOG_MODEL = os.path.join(MODEL_DIR, "face_recognition_sface_2021dec.onnx")


def user_dir(user: str) -> str:
    """Root-only directory holding a user's secrets (PIN hash, templates)."""
    return os.path.join(DATA_ROOT, "users", user)


def status_file(user: str) -> str:
    """World-readable, secret-free summary the settings app can read."""
    return os.path.join(DATA_ROOT, "status", user + ".json")


def pam_disabled_marker() -> str:
    """Exists while an admin has run `quickunlock-pam-setup disable`."""
    return os.path.join(DATA_ROOT, "pam-disabled")
