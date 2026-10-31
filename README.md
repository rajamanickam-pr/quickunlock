# QuickUnlock

**Windows Hello-style sign-in options for Ubuntu.** QuickUnlock lets you unlock the GNOME lock screen with:

- **A PIN**: 4–12 digits, with brute-force lockout. Easy-to-guess PINs are refused.
- **Your face**: works with any webcam, and IR cameras are supported. See the security notes before you turn it on.

Your password always keeps working. QuickUnlock only changes the GDM login and lock screen. **`sudo`, `su`, SSH, polkit prompts, TTY login and disk encryption still need your real password.**

Everything runs locally on the CPU. Nothing is uploaded anywhere.

Target: Ubuntu 26.04 LTS (GNOME, GDM, OpenCV 4.10). Ubuntu 24.10 and newer should also work. Ubuntu 24.04 does not, because its OpenCV is 4.6.

> **Not a GNOME Settings panel.** GNOME Settings has no way for other apps to add pages to it, so QuickUnlock is its own app, **Sign-in Options**. It appears in the app grid and when you search Activities for "PIN", "face" or "unlock".

---

## Install

```bash
sudo apt install ./quickunlock_0.2.0_all.deb
```

Then open **Sign-in Options** from the app grid.

The package adds a marked block to `/etc/pam.d/gdm-password` and keeps a backup next to it. Removing the package takes the block out again.

```bash
quickunlock-pam-setup status          # installed / not installed
sudo quickunlock-pam-setup disable    # off-switch; stays off across package upgrades
sudo quickunlock-pam-setup enable
```

`/etc/pam.d/gdm-password` belongs to the `gdm3` package. If a `gdm3` update ships a new version of that file, `apt` asks whether to keep your modified copy. Either answer is safe. If you take the new file, QuickUnlock puts its lines back automatically, unless you switched it off with `disable`.

## How it behaves at the lock screen

1. **Face** (if enabled): "Looking for your face…" for up to 3 seconds. If your face matches, the screen unlocks.
2. **Password box**: type **either your PIN or your password**.

If a step fails, it simply falls through to the next one. A crash in QuickUnlock can never lock you out, and neither can a hang: every QuickUnlock check is killed after a fixed time limit (10 s for the PIN), and the password box takes over.

**After a restart, you sign in with your password once.** You can turn this off in Options. It's on by default for two reasons:

- Your GNOME keyring (Wi-Fi, browser and app passwords) is encrypted with your password and can't be opened with a PIN or face.
- Windows Hello behaves the same way after updates.

### PIN rules

4–12 digits. These are refused because they are the first things anyone tries:

- one repeated digit (`1111`), runs (`1234`, `9876`, `8901`)
- repeating patterns (`1212`, `123123`, `1122`)
- years 1950–2029 (`1987`, `2001`)
- keypad lines and other very common picks (`2580`, `1470`, `1004`, `147258`, …)

### PIN lockout

| Wrong PINs in a row | Result |
|---|---|
| 1–2 | nothing |
| 3, 4, 5 … | PIN locked for 30 s, 60 s, 120 s … (max 15 min) |
| 10 | PIN disabled until you sign in with your password |

Signing in with your password resets the counter. Each attempt is counted *before* the PIN is checked, and attempts are serialised with a lock file. An attempt that is interrupted, or one running in parallel with another, can't get a free guess. If the counter file is ever damaged, the PIN is treated as disabled until your next password sign-in.

## Security notes (please read)

- **PIN**: the PIN is stored as a scrypt hash in a root-only file. Short PINs are only safe because of the lockout and the PIN rules above.
- **Face**: an ordinary RGB webcam **can be fooled by a good photo or video of you**. QuickUnlock has no liveness check. Use an IR camera if you have one (select it under *Camera*). Some IR emitters need [`linux-enable-ir-emitter`](https://github.com/EmixamPP/linux-enable-ir-emitter). You can raise *Face strictness* for fewer false accepts. If your laptop only has a normal webcam, use the PIN.
- **Changes need your password, every time.** Setting or changing a PIN, enrolling a face, turning a method on, lowering strictness or changing the camera asks for your password each time, with no 5-minute grace period. Changes that only make unlocking *harder* need no password: turning a method off, removing data, raising strictness, or requiring the password after restart. A separate helper does those changes, and it refuses anything else.
- **Templates are protected**: PIN hashes and face data live in `/var/lib/quickunlock/users/<you>/`, which only root can read. Another program in your session can't quietly add someone else's face or PIN.
- **Audit log**: every change made through the app is logged to the system journal (`journalctl -t quickunlock-helper`). Unlocks and failures are logged under `quickunlock`.
- **Local only**: remote logins (`PAM_RHOST` set) and every PAM service other than `gdm-password` are refused.

## Architecture

```
┌──────────────────────────┐  pkexec, password every time  ┌─────────────────────────────┐
│ quickunlock-settings     │ ────────────────────────────▶ │ quickunlock-helper (root)   │
│ GTK4 / libadwaita        │                               │ PIN hash, face templates    │
│ camera runs here         │  pkexec, no password          ├─────────────────────────────┤
│                          │ ────────────────────────────▶ │ quickunlock-helper-restrict │
└──────────────────────────┘  JSON on stdin                │ only "make it harder"       │
                                                           └──────────────┬──────────────┘
┌──────────────────────────┐   pam_exec.so (root)          ┌──────────────▼──────────────┐
│ GDM login / lock screen  │ ────────────────────────────▶ │ quickunlock-pam             │
│ /etc/pam.d/gdm-password  │   exit 0 = unlock             │ face | pin | reset          │
└──────────────────────────┘                               └─────────────────────────────┘
```

PAM stack after installation:

```
auth requisite pam_nologin.so
auth required  pam_succeed_if.so user != root quiet_success
# >>> quickunlock >>>
auth [success=done default=ignore] pam_exec.so quiet stdout /usr/libexec/quickunlock/quickunlock-pam face
auth [success=done default=ignore] pam_exec.so quiet stdout expose_authtok /usr/libexec/quickunlock/quickunlock-pam pin
# <<< quickunlock <<<
@include common-auth
# >>> quickunlock-reset >>>
auth optional pam_exec.so quiet /usr/libexec/quickunlock/quickunlock-pam reset
# <<< quickunlock-reset <<<
auth optional pam_gnome_keyring.so
```

The `pin` line receives whatever was typed into the password field. If that isn't the PIN, `pam_unix` reuses the same text as your password, so one box handles both. The `reset` line only runs after a successful password check. Before writing the file, `quickunlock-pam-setup` checks that the result has exactly this shape, and it writes the file atomically.

### Face recognition

| Model | Runtime | Match rule |
|---|---|---|
| YuNet detector + SFace (128-d) | `cv2.dnn` | cosine ≥ 0.45 in 2 frames |

Models are bundled. See [MODELS.md](MODELS.md) for licenses.

## Development

```bash
# tests (no root or camera needed; test_pam_stack.py drives the real libpam)
pip install pytest scikit-image opencv-python-headless numpy
QUICKUNLOCK_MODELS=data/models python3 -m pytest tests

# run the settings app from the source tree as root, with a scratch data dir
sudo QUICKUNLOCK_DATA=/tmp/qudata QUICKUNLOCK_MODELS=$PWD/data/models QUICKUNLOCK_USER=$USER \
     python3 -c "import sys; sys.path.insert(0,'src'); from quickunlock.gui.app import main; main()"

# build the .deb
./build-deb.sh            # -> build/quickunlock_0.2.0_all.deb
```

`tests/test_pam_stack.py` loads the real PAM modules from a temporary config directory (`pam_start_confdir`). It checks that the PIN unlocks, that the password is never asked twice, that the lockout message reaches the screen, and that a hung QuickUnlock falls through to the password. It never tries a real password.

**Test PAM safely**: keep a root shell open (`sudo -i`) before you change anything. Then try it with `pamtester`:

```bash
sudo apt install pamtester
pamtester gdm-password $USER authenticate
```

## Changes in 0.2.0

- Voice unlock removed. Speaker recognition could be fooled by a recording.
- Common and patterned PINs are refused.
- PIN attempts are counted before checking and serialised. Damaged state fails closed. Out-of-range lock times are clamped.
- A time limit kills any QuickUnlock check that hangs.
- Polkit: password every time for loosening changes, no password for tightening changes, no 5-minute reuse.
- `disable` survives upgrades. A `gdm3` update that replaces `gdm-password` gets the lines re-applied.
- The PAM file is validated before writing, and the write is atomic and durable.

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
