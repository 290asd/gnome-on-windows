# GNOME on Windows

Real GNOME Shell (Ubuntu 24.04 in WSL2/WSLg) running fullscreen on Windows 11, with your Windows apps
inside it as ordinary GNOME windows: in the dash and app grid with their own icons, on any workspace,
moved, tiled and closed by GNOME, while the apps themselves keep running on Windows.

**Status: 0.1 beta.** It works day to day on one laptop; expect rough edges on others.

## What you get

- Every Windows app window (dialogs included) mirrored into GNOME, input sent back to it.
- Windows Start-menu apps as GNOME launchers (`export-apps.ps1`).
- GNOME's own three-finger touchpad gestures (overview, app grid, workspaces), fed from the
  Windows touchpad.
- The Windows key as GNOME's Super key while GNOME is in front (Super, Super+A, Super+arrows, ...).

## Requirements

- Windows 11 with WSL2 and WSLg, and a distro named `Ubuntu-24.04`:
  ```
  sudo apt install gnome-shell python3-gi python3-gi-cairo gir1.2-gtk-3.0 wmctrl x11-utils x11-xserver-utils
  ```
- Python 3 on Windows (the `py` launcher), with:
  ```
  py -m pip install numpy
  py -m pip install --no-deps windows-capture
  ```

## Setup

1. Clone the repository anywhere on the Windows side.
2. Export the Start-menu apps as GNOME launchers (re-run after installing apps):
   ```
   powershell -ExecutionPolicy Bypass -File export-apps.ps1
   ```
3. For the touchpad gestures, stop Windows from using them itself: Settings → Bluetooth & devices →
   Touchpad → Three-finger gestures and Four-finger gestures → Swipes: **Nothing**.
4. Start (or restart) everything:
   ```
   powershell -ExecutionPolicy Bypass -File start-gnome.ps1
   ```
   Stop with `start-gnome.ps1 stop`, or the *Exit GNOME* launcher inside GNOME.

## How it works

| Piece | Side | Job |
|---|---|---|
| `gnome-nested.sh` | WSL | Nested GNOME Shell in one fullscreen WSLg window (software rendering) |
| `winbridge.py` | Windows | Captures each app window (Windows.Graphics.Capture) and streams the changed pixels; posts mouse and keyboard input back; reads touchpad swipes and the Windows key |
| `gnomebridge.py` | WSL | One GTK window per Windows window: draws the frames, sends input back |
| `share/gnome-shell/extensions/windows-touchpad@gnome-windows` | GNOME | Feeds the swipes to GNOME's own gesture handling and presses Super |
| `export-apps.ps1`, `win-launch.sh` | both | Start-menu apps as GNOME launchers |

## Known limitations

- Input is posted as window messages: apps reading raw input (games) and some Ctrl/Alt shortcuts
  don't react.
- GNOME renders in software, so maximized windows update at roughly 15–30 fps.
- Two mirrors side by side in GNOME can cover each other on the Windows side; then only the focused
  one keeps updating.
- Assumes the distro name `Ubuntu-24.04`, Linux user id 1000 and GNOME Shell 46.

## Tests

```
py test_winbridge.py
```

## License

MIT, see [LICENSE](LICENSE).
