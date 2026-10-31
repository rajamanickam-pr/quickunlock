"""QuickUnlock settings window ("Sign-in options")."""

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .. import APP_ID, __version__  # noqa: E402
from . import backend  # noqa: E402
from .dialogs import FaceDialog, PinDialog, VoiceDialog  # noqa: E402


class MethodRow(Adw.ExpanderRow):
    """One sign-in method: icon, title, status, on/off switch and actions."""

    def __init__(self, icon, title, on_toggle):
        super().__init__(title=title)
        self.add_prefix(Gtk.Image(icon_name=icon, pixel_size=24))
        self.switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._handler = self.switch.connect("notify::active", lambda s, _: on_toggle(s.get_active()))
        self.add_suffix(self.switch)
        self.actions = Gtk.Box(spacing=8, margin_top=10, margin_bottom=10, margin_start=12,
                               margin_end=12, halign=Gtk.Align.END)
        holder = Adw.PreferencesRow(activatable=False)
        holder.set_child(self.actions)
        self.add_row(holder)

    def button(self, label, cb, style=None):
        b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
        if style:
            b.add_css_class(style)
        b.connect("clicked", lambda *_: cb())
        self.actions.append(b)
        return b

    def set_state(self, configured: bool, enabled: bool, subtitle: str):
        self.set_subtitle(subtitle)
        with self.switch.handler_block(self._handler):
            self.switch.set_active(enabled)
        self.switch.set_sensitive(configured)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Sign-in Options", default_width=640,
                         default_height=760)
        self.status = backend.status()
        self.toasts = Adw.ToastOverlay()
        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        menu = Gio.Menu()
        menu.append("About QuickUnlock", "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        tv.add_top_bar(header)
        self.banner = Adw.Banner(title="Lock-screen integration is turned off on this computer",
                                 button_label="Turn On")
        self.banner.connect("button-clicked", self._enable_pam)
        tv.add_top_bar(self.banner)
        self.toasts.set_child(self._build_page())
        tv.set_content(self.toasts)
        self.set_content(tv)
        self.refresh()

    # ------------------------------------------------------------- layout
    def _build_page(self):
        page = Adw.PreferencesPage()

        intro = Adw.PreferencesGroup(
            title="Ways to unlock",
            description="Unlock the lock screen with a PIN, your face or your voice. "
                        "Your password always works and is still needed for sudo, "
                        "software updates and the first sign-in after a restart.")
        page.add(intro)

        self.pin_row = MethodRow("dialog-password-symbolic", "PIN", lambda on: self._toggle("pin", on))
        self.pin_set_btn = self.pin_row.button("Set up", self._set_pin, "suggested-action")
        self.pin_unlock_btn = self.pin_row.button("Re-enable PIN", self._reset_lockout)
        self.pin_rm_btn = self.pin_row.button("Remove", lambda: self._remove("pin"), "destructive-action")
        intro.add(self.pin_row)

        self.face_row = MethodRow("camera-web-symbolic", "Face recognition",
                                  lambda on: self._toggle("face", on))
        self.face_set_btn = self.face_row.button("Set up", self._enroll_face, "suggested-action")
        self.face_test_btn = self.face_row.button("Test", self._test_face)
        self.face_rm_btn = self.face_row.button("Remove", lambda: self._remove("face"), "destructive-action")
        intro.add(self.face_row)

        self.voice_row = MethodRow("audio-input-microphone-symbolic", "Voice (experimental)",
                                   lambda on: self._toggle("voice", on))
        self.voice_set_btn = self.voice_row.button("Set up", self._enroll_voice, "suggested-action")
        self.voice_test_btn = self.voice_row.button("Test", self._test_voice)
        self.voice_rm_btn = self.voice_row.button("Remove", lambda: self._remove("voice"), "destructive-action")
        intro.add(self.voice_row)

        # options
        opts = Adw.PreferencesGroup(title="Options")
        self.boot_row = Adw.SwitchRow(
            title="Require password after restart",
            subtitle="Recommended. Keeps your keyring and saved passwords unlocked correctly.")
        self.boot_row.connect("notify::active", lambda r, _: self._set_cfg(
            {"require_password_after_boot": r.get_active()}))
        opts.add(self.boot_row)

        from ..face import camera_label, list_cameras
        self.cameras = ["auto"] + list_cameras()
        self.cam_row = Adw.ComboRow(title="Camera", model=Gtk.StringList.new(
            ["Automatic"] + [camera_label(c) for c in self.cameras[1:]]))
        self.cam_row.connect("notify::selected", lambda r, _: self._set_cfg(
            {"camera": self.cameras[r.get_selected()]}))
        opts.add(self.cam_row)

        from ..voice import list_microphones
        self.mics = [("auto", "Automatic")] + list_microphones()
        self.mic_row = Adw.ComboRow(title="Microphone", model=Gtk.StringList.new(
            [label for _, label in self.mics]))
        self.mic_row.connect("notify::selected", lambda r, _: self._set_cfg(
            {"microphone": self.mics[r.get_selected()][0]}))
        opts.add(self.mic_row)
        page.add(opts)

        adv = Adw.PreferencesGroup(title="Advanced")
        exp = Adw.ExpanderRow(title="Recognition settings",
                              subtitle="Higher strictness = fewer false unlocks, more retries")
        self.face_strict = Adw.SpinRow.new_with_range(0.30, 0.80, 0.01)
        self.face_strict.set_title("Face strictness")
        self.face_strict.set_digits(2)
        self.face_timeout = Adw.SpinRow.new_with_range(1, 10, 0.5)
        self.face_timeout.set_title("Seconds to look for a face")
        self.face_timeout.set_digits(1)
        self.voice_strict = Adw.SpinRow.new_with_range(0.30, 0.90, 0.01)
        self.voice_strict.set_title("Voice strictness")
        self.voice_strict.set_digits(2)
        for row, key in ((self.face_strict, "face_threshold"), (self.face_timeout, "face_timeout"),
                         (self.voice_strict, "voice_threshold")):
            row.connect("notify::value", self._spin_changed, key)
            exp.add_row(row)
        adv.add(exp)
        rm = Adw.ActionRow(title="Remove all QuickUnlock data",
                           subtitle="Deletes your PIN, face and voice data from this computer")
        b = Gtk.Button(label="Remove All", css_classes=["destructive-action"], valign=Gtk.Align.CENTER)
        b.connect("clicked", lambda *_: self._remove_all())
        rm.add_suffix(b)
        adv.add(rm)
        page.add(adv)
        self._spin_timer = None
        return page

    # ------------------------------------------------------------- state
    def refresh(self, *_):
        s = self.status = backend.status()
        cfg = s["config"]
        self.banner.set_revealed(not s.get("pam_installed", True))

        if s["pin_set"] and s.get("pin_hard_locked"):
            sub = "Locked after too many wrong attempts"
        else:
            sub = ("On" if s["pin_enabled"] else "Off") if s["pin_set"] else "Not set up"
        self.pin_row.set_state(s["pin_set"], s["pin_enabled"], sub)
        self.pin_set_btn.set_label("Change PIN" if s["pin_set"] else "Set up")
        self.pin_unlock_btn.set_visible(bool(s.get("pin_hard_locked")))
        self.pin_rm_btn.set_visible(s["pin_set"])

        for kind, row, setb, testb, rmb in (
                ("face", self.face_row, self.face_set_btn, self.face_test_btn, self.face_rm_btn),
                ("voice", self.voice_row, self.voice_set_btn, self.voice_test_btn, self.voice_rm_btn)):
            n = s[f"{kind}_samples"]
            sub = ("On" if s[f"{kind}_enabled"] else "Off") + f" · {n} samples" if n else "Not set up"
            row.set_state(n > 0, s[f"{kind}_enabled"], sub)
            setb.set_label("Set up again" if n else "Set up")
            testb.set_visible(n > 0)
            rmb.set_visible(n > 0)

        self._quiet = True
        self.boot_row.set_active(cfg["require_password_after_boot"])
        cam = cfg.get("camera", "auto")
        self.cam_row.set_selected(self.cameras.index(cam) if cam in self.cameras else 0)
        mic = cfg.get("microphone", "auto")
        ids = [m for m, _ in self.mics]
        self.mic_row.set_selected(ids.index(mic) if mic in ids else 0)
        self.face_strict.set_value(cfg["face_threshold"])
        self.face_timeout.set_value(cfg["face_timeout"])
        self.voice_strict.set_value(cfg["voice_threshold"])
        self._quiet = False

    def toast(self, msg):
        self.toasts.add_toast(Adw.Toast(title=msg, timeout=4))

    def _helper(self, cmd, data=None, ok_msg=None):
        def done(res):
            if res.get("error"):
                if not res.get("cancelled"):
                    self.toast(res["error"])
            elif ok_msg:
                self.toast(ok_msg)
            self.refresh()
        backend.call_helper(cmd, data, done)

    # ------------------------------------------------------------- actions
    def _toggle(self, kind, on):
        self._helper("set-config", {f"{kind}_enabled": on})

    def _set_cfg(self, upd):
        if not getattr(self, "_quiet", True):
            self._helper("set-config", upd)

    def _spin_changed(self, row, _pspec, key):
        if getattr(self, "_quiet", True):
            return
        if self._spin_timer:
            GLib.source_remove(self._spin_timer)

        def fire():
            self._spin_timer = None
            self._helper("set-config", {key: round(row.get_value(), 2)})
            return False
        self._spin_timer = GLib.timeout_add(700, fire)

    def _set_pin(self):
        PinDialog(self, lambda p: self._helper("set-pin", {"pin": p}, "PIN saved"))

    def _reset_lockout(self):
        self._helper("reset-lockout", None, "PIN re-enabled")

    def _enroll_face(self):
        FaceDialog(self, self.status["config"].get("camera", "auto"),
                   lambda embs: self._helper("set-face", {"embeddings": embs}, "Face unlock is ready"))

    def _test_face(self):
        def got(embs):
            backend.call_helper("verify-face", {"embedding": embs[0]}, self._show_test)
        FaceDialog(self, self.status["config"].get("camera", "auto"), got, mode="test")

    def _enroll_voice(self):
        cfg = self.status["config"]
        VoiceDialog(self, cfg.get("microphone", "auto"), cfg.get("voice_seconds", 3.0),
                    lambda embs: self._helper("set-voice", {"embeddings": embs}, "Voice unlock is ready"))

    def _test_voice(self):
        cfg = self.status["config"]

        def got(embs):
            backend.call_helper("verify-voice", {"embedding": embs[0]}, self._show_test)
        VoiceDialog(self, cfg.get("microphone", "auto"), cfg.get("voice_seconds", 3.0), got, mode="test")

    def _show_test(self, res):
        if res.get("error"):
            self.toast(res["error"])
            return
        verdict = "Match – this would unlock" if res["match"] else "No match – this would not unlock"
        self.toast(f"{verdict} (score {res['score']:.2f}, needs {res['threshold']:.2f})")

    def _confirm(self, heading, body, label, cb):
        d = Adw.AlertDialog(heading=heading, body=body)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", label)
        d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
        d.connect("response", lambda _d, r: r == "ok" and cb())
        d.present(self)

    def _remove(self, kind):
        names = {"pin": "PIN", "face": "face data", "voice": "voice data"}
        self._confirm(f"Remove {names[kind]}?", "You can set it up again at any time.", "Remove",
                      lambda: self._helper(f"clear-{kind}", None, f"{names[kind].capitalize()} removed"))

    def _remove_all(self):
        self._confirm("Remove all QuickUnlock data?",
                      "Your PIN, face and voice data will be deleted. You will unlock with your password.",
                      "Remove All", lambda: self._helper("remove-all", None, "All data removed"))

    def _enable_pam(self, *_):
        def done(res):
            if not res.get("ok") and res.get("error"):
                self.toast(res["error"])
            self.refresh()
        backend.enable_pam(done)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._about)
        self.add_action(about)

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()

    def _about(self, *_):
        Adw.AboutDialog(application_name="QuickUnlock", application_icon=APP_ID, version=__version__,
                        developer_name="QuickUnlock contributors", license_type=Gtk.License.MIT_X11,
                        website="https://github.com/your-name/quickunlock",
                        comments="PIN, face and voice unlock for the GNOME lock screen.").present(
            self.props.active_window)


def main(argv=None):
    return App().run(argv or sys.argv)
