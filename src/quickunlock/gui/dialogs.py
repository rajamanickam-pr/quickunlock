"""Enrollment dialogs: PIN and face."""

import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gdk, Gtk  # noqa: E402

FACE_STEPS = [
    "Look straight at the camera",
    "Turn your head slightly to the left",
    "Turn your head slightly to the right",
    "Tilt your head slightly up",
    "Tilt your head slightly down",
    "Look straight at the camera again",
]


def _dialog(title: str, child: Gtk.Widget, width=420, height=-1) -> Adw.Dialog:
    d = Adw.Dialog(title=title, content_width=width, content_height=height)
    tv = Adw.ToolbarView()
    tv.add_top_bar(Adw.HeaderBar())
    tv.set_content(child)
    d.set_child(tv)
    return d


# ------------------------------------------------------------------ PIN
class PinDialog:
    def __init__(self, parent, on_done):
        self.on_done = on_done
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=12, margin_bottom=24, margin_start=24, margin_end=24)
        box.append(Gtk.Label(label="Your PIN unlocks this computer at the lock screen. "
                                   "Use 4–12 digits that are hard to guess: no 1234, 2580, "
                                   "1212 or years. Your password still works too.",
                             wrap=True, xalign=0, css_classes=["dim-label"]))
        grp = Adw.PreferencesGroup()
        self.pin1 = Adw.PasswordEntryRow(title="New PIN")
        self.pin2 = Adw.PasswordEntryRow(title="Confirm PIN")
        for e in (self.pin1, self.pin2):
            e.set_input_purpose(Gtk.InputPurpose.PIN)
            e.connect("changed", self._changed)
            grp.add(e)
        self.pin2.connect("entry-activated", lambda *_: self._save())
        box.append(grp)
        self.err = Gtk.Label(css_classes=["error"], xalign=0, wrap=True, visible=False)
        box.append(self.err)
        self.btn = Gtk.Button(label="Set PIN", css_classes=["suggested-action", "pill"],
                              halign=Gtk.Align.CENTER, sensitive=False)
        self.btn.connect("clicked", lambda *_: self._save())
        box.append(self.btn)
        self.dialog = _dialog("Set up a PIN", box)
        self.dialog.present(parent)

    def _changed(self, *_):
        a, b = self.pin1.get_text(), self.pin2.get_text()
        msg = ""
        if a and not a.isdigit():
            msg = "Digits only, please."
        elif b and a != b:
            msg = "The PINs don't match."
        self.err.set_label(msg)
        self.err.set_visible(bool(msg))
        self.btn.set_sensitive(not msg and len(a) >= 4 and a == b)

    def _save(self):
        if not self.btn.get_sensitive():
            return
        from ..pin import validate
        try:
            validate(self.pin1.get_text())
        except ValueError as e:
            self.err.set_label(str(e))
            self.err.set_visible(True)
            return
        self.dialog.close()
        self.on_done(self.pin1.get_text())


# ------------------------------------------------------------------ camera view
def _frame_texture(frame_bgr):
    import cv2
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    rgb = cv2.flip(rgb, 1)  # mirror, like a selfie camera
    h, w = rgb.shape[:2]
    return Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8, GLib.Bytes.new(rgb.tobytes()), w * 3)


class CameraWorker:
    """Reads the camera in a thread, runs detection, posts frames to the UI."""

    def __init__(self, device, on_frame, on_error):
        self.device, self.on_frame, self.on_error = device, on_frame, on_error
        self.stop_flag = threading.Event()
        self.engine = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        import cv2
        from ..face import Camera, FaceEngine
        try:
            self.engine = FaceEngine()
            cam = Camera(self.device)
        except Exception as e:
            GLib.idle_add(self.on_error, str(e))
            return
        with cam:
            while not self.stop_flag.is_set():
                frame = cam.read()
                if frame is None:
                    time.sleep(0.05)
                    continue
                face = self.engine.detect(frame)
                emb = None
                view = frame.copy()
                if face is not None:
                    emb = self.engine.embed(frame, face)
                    x, y, w, h = [int(v) for v in face[:4]]
                    cv2.rectangle(view, (x, y), (x + w, y + h), (80, 200, 120), 3)
                GLib.idle_add(self.on_frame, _frame_texture(view), emb)
                time.sleep(0.03)

    def stop(self):
        self.stop_flag.set()


class FaceDialog:
    """Enroll (mode='enroll') or test (mode='test') face recognition."""

    def __init__(self, parent, device, on_done, mode="enroll"):
        self.on_done, self.mode = on_done, mode
        self.samples, self.step, self.last_capture = [], 0, 0.0
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=6, margin_bottom=24, margin_start=24, margin_end=24)
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, height_request=300,
                                   css_classes=["card"])
        self.picture.set_overflow(Gtk.Overflow.HIDDEN)
        box.append(self.picture)
        self.title = Gtk.Label(label="Starting camera…", css_classes=["title-3"], wrap=True)
        box.append(self.title)
        self.hint = Gtk.Label(label="Make sure your face is well lit.", wrap=True,
                              css_classes=["dim-label"])
        box.append(self.hint)
        self.progress = Gtk.ProgressBar(visible=(mode == "enroll"))
        box.append(self.progress)
        self.dialog = _dialog("Set up face unlock" if mode == "enroll" else "Test face unlock",
                              box, width=520)
        self.dialog.connect("closed", lambda *_: self.worker.stop())
        self.worker = CameraWorker(device, self._frame, self._error)
        self.dialog.present(parent)

    def _error(self, msg):
        self.title.set_label("Camera problem")
        self.hint.set_label(msg + "\nCheck that no other app is using the camera.")

    def _frame(self, texture, emb):
        self.picture.set_paintable(texture)
        if self.worker.stop_flag.is_set():
            return
        if self.mode == "test":
            self.title.set_label("Look at the camera")
            if emb is not None:
                self.worker.stop()
                self.dialog.close()
                self.on_done([emb.tolist()])
            return
        self.title.set_label(FACE_STEPS[self.step])
        if emb is None:
            self.hint.set_label("No face found – move closer or improve the lighting.")
            return
        self.hint.set_label("Hold still…")
        now = time.monotonic()
        if now - self.last_capture < 0.8:
            return
        # take two samples per pose
        self.samples.append(emb.tolist())
        self.last_capture = now
        self.progress.set_fraction(len(self.samples) / (2 * len(FACE_STEPS)))
        if len(self.samples) % 2 == 0:
            self.step += 1
        if self.step >= len(FACE_STEPS):
            self.worker.stop()
            self.title.set_label("All done!")
            self.hint.set_label("Saving…")
            GLib.timeout_add(400, self._finish)

    def _finish(self):
        self.dialog.close()
        self.on_done(self.samples)
        return False
