# QuickUnlock

**Windows Hello-style sign-in options for Ubuntu.** QuickUnlock lets you unlock the GNOME lock screen with:

- **A PIN**: 4–12 digits, with brute-force lockout.
- **Your face**: works with any webcam, and IR cameras are supported.
- **Your voice**: speaker recognition. This one is *experimental*.

Your password always keeps working. QuickUnlock only changes the GDM login and lock screen. **`sudo`, `su`, SSH, polkit prompts, TTY login and disk encryption still need your real password.**

Everything runs locally on the CPU. Nothing is uploaded anywhere.

Target: Ubuntu 26.04 LTS (GNOME, GDM, OpenCV 4.10). Ubuntu 24.10 and newer should also work. Ubuntu 24.04 does not, because its OpenCV is 4.6.

---

## Install

```bash
sudo apt install ./quickunlock_0.1.0_all.deb
```

Then open **Sign-in Options** from the app grid. You can also search for "PIN", "face" or "unlock".

The package adds a marked block to `/etc/pam.d/gdm-password` and keeps a backup next to it. Removing the package takes the block out again.

```bash
quickunlock-pam-setup status          # installed / not installed
sudo quickunlock-pam-setup disable    # emergency off-switch
sudo quickunlock-pam-setup enable
```

## How it behaves at the lock screen

1. **Face** (if enabled): "Looking for your face…" for up to 3 seconds. If your face matches, the screen unlocks.
2. **Voice** (if enabled): "Say your voice phrase now…", then it records for 3 seconds.
3. **Password box**: type **either your PIN or your password**.

If a step fails, it simply falls through to the next one. A crash in QuickUnlock can never lock you out. The worst case is that the shortcut is skipped.

**After a restart, you sign in with your password once.** You can turn this off in Options. It's on by default for two reasons:

- Your GNOME keyring (Wi-Fi, browser and app passwords) is encrypted with your password and can't be opened with a PIN or face.
- Windows Hello behaves the same way after updates.

### PIN lockout

| Wrong PINs in a row | Result |
|---|---|
| 1–2 | nothing |
| 3, 4, 5 … | PIN locked for 30 s, 60 s, 120 s … (max 15 min) |
| 10 | PIN disabled until you sign in with your password |

Signing in with your password resets the counter.

## Security notes (please read)

- **PIN**: the PIN is stored as a scrypt hash in a root-only file. Short PINs are only safe because of the lockout above.
- **Face**: an ordinary RGB webcam **can be fooled by a good photo or video of you**. Use an IR camera if you have one (select it under *Camera*). Some IR emitters need [`linux-enable-ir-emitter`](https://github.com/EmixamPP/linux-enable-ir-emitter). You can raise *Face strictness* for fewer false accepts.
- **Voice**: speaker recognition identifies *who* is talking, not *what* is said. **A recording of your voice can unlock it.** Treat it as a convenience feature for low-risk machines.
- **Templates are protected**: face and voice data live in `/var/lib/quickunlock/users/<you>/`, which only root can read. Changing them requires your password (polkit). Another program in your session can't quietly add someone else's face.
- **Local only**: remote logins (`PAM_RHOST` set) and every PAM service other than `gdm-password` are refused.

## Architecture

```
┌──────────────────────────┐   pkexec (asks your password)  ┌─────────────────────────────┐
│ quickunlock-settings     │ ─────────────────────────────▶ │ quickunlock-helper (root)   │
│ GTK4 / libadwaita        │   JSON on stdin                │ validates + stores PIN hash, │
│ camera + mic run here    │                                │ face/voice embeddings        │
└──────────────────────────┘                                └──────────────┬──────────────┘
                                                                           │ /var/lib/quickunlock
┌──────────────────────────┐   pam_exec.so (root)           ┌──────────────▼──────────────┐
│ GDM login / lock screen  │ ─────────────────────────────▶ │ quickunlock-pam face|voice| │
│ /etc/pam.d/gdm-password  │   exit 0 = unlock              │ pin|reset                    │
└──────────────────────────┘                                └─────────────────────────────┘
```

PAM stack after installation:

```
auth requisite pam_nologin.so
auth required  pam_succeed_if.so user != root quiet_success
# >>> quickunlock >>>
auth [success=done default=ignore] pam_exec.so quiet stdout /usr/libexec/quickunlock/quickunlock-pam face
auth [success=done default=ignore] pam_exec.so quiet stdout /usr/libexec/quickunlock/quickunlock-pam voice
auth [success=done default=ignore] pam_exec.so quiet stdout expose_authtok /usr/libexec/quickunlock/quickunlock-pam pin
# <<< quickunlock <<<
@include common-auth
# >>> quickunlock-reset >>>
auth optional pam_exec.so quiet /usr/libexec/quickunlock/quickunlock-pam reset
# <<< quickunlock-reset <<<
auth optional pam_gnome_keyring.so
```

The `pin` line receives whatever was typed into the password field. If that isn't the PIN, `pam_unix` reuses the same text as your password, so one box handles both. The `reset` line only runs after a successful password check.

### Recognition

| | Model | Runtime | Match rule |
|---|---|---|---|
| Face | YuNet detector + SFace (128-d) | `cv2.dnn` | cosine ≥ 0.45 in 2 frames |
| Voice | WeSpeaker ResNet-34 (256-d) | trunk in `cv2.dnn`, pooling in numpy | cosine ≥ 0.60 |

Models are bundled. See [MODELS.md](MODELS.md) for licenses.

## Development

```bash
# unit tests (no root, camera or mic needed)
pip install pytest scikit-image
QUICKUNLOCK_MODELS=data/models python3 -m pytest tests

# run the settings app from the source tree as root, with a scratch data dir
sudo QUICKUNLOCK_DATA=/tmp/qudata QUICKUNLOCK_MODELS=$PWD/data/models QUICKUNLOCK_USER=$USER \
     python3 -c "import sys; sys.path.insert(0,'src'); from quickunlock.gui.app import main; main()"

# build the .deb
./build-deb.sh            # -> build/quickunlock_0.1.0_all.deb
```

**Test PAM safely**: keep a root shell open (`sudo -i`) before you change anything. Then try it with `pamtester`:

```bash
sudo apt install pamtester
pamtester gdm-password $USER authenticate
```

## Publishing checklist

- [ ] Replace `your-name` in the URLs (`app.py`, `.policy`, `control`, metainfo) with your GitHub account.
- [ ] Push to GitHub and attach `build/quickunlock_*_all.deb` to a release.
- [ ] Optional: build a source package and upload it to a Launchpad PPA (`ppa:your-name/quickunlock`) so users get updates through `apt`.
- [ ] Flatpak and Snap are **not** possible for this app, because sandboxed apps cannot change system PAM settings.

## Uninstall

```bash
sudo apt remove quickunlock      # removes the PAM lines, keeps your data
sudo apt purge quickunlock       # also deletes /var/lib/quickunlock
```

## License

MIT. Bundled models have their own licenses, listed in [MODELS.md](MODELS.md).
