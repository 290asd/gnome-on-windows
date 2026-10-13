# GNOME on Windows

Real GNOME Shell (Ubuntu 24.04 in WSL2/WSLg) running fullscreen on Windows 11, with your Windows apps
inside it as ordinary GNOME windows: in the dash and app grid with their own icons, on any workspace,
moved, tiled and closed by GNOME, while the apps themselves keep running on Windows.

**Status: 0.1 beta.** It works day to day on one laptop; expect rough edges on others.

## What you get

- Every Windows app window (dialogs included) mirrored into GNOME, input sent back to it.
- The focused window shown natively: the real Windows window is laid over its mirror, so it draws at
  the display's refresh rate with no added latency and takes real mouse and keyboard input (the
  keyboard as soon as GNOME focuses it).
- Windows Start-menu apps as GNOME launchers (`export-apps.ps1`).
- GNOME's own three-finger touchpad gestures (overview, app grid, workspaces), fed from the
  Windows touchpad.
- The Windows key as GNOME's Super key while GNOME is in front (Super, Super+A, Super+arrows, ...).
- Windows' desktop wallpaper and dark mode in GNOME, following them when they change.
- Windows' notification-area (tray) icons in GNOME's top bar, as on Windows: the ones pinned to the
  taskbar (Settings → Personalization → Taskbar → Other system tray icons) as icons, the rest in a ▾
  menu by name. Click them as on Windows, and the app's own menu opens right there; their balloons
  show up as GNOME notifications.
- GNOME's Wi-Fi and Bluetooth menus and Settings panels working on Windows' own radios: turn them on
  and off, join Wi-Fi networks (GNOME asks for the password), pair Bluetooth devices, connect and
  disconnect headphones.

## Requirements

- Windows 11 with WSL2 and WSLg, and a distro named `Ubuntu-24.04`:
  ```
  sudo apt install gnome-shell python3-gi python3-gi-cairo gir1.2-gtk-3.0 wmctrl x11-utils x11-xserver-utils
  ```
- Python 3 on Windows (the `py` launcher), with:
  ```
  py -m pip install numpy
  py -m pip install --no-deps windows-capture
  py -m pip install winrt-runtime winrt-Windows.Devices.Radios winrt-Windows.Devices.Enumeration winrt-Windows.Foundation winrt-Windows.Foundation.Collections
  ```

## Setup

1. Clone the repository anywhere on the Windows side.
2. Export the Start-menu apps as GNOME launchers (re-run after installing apps):
   ```
   powershell -ExecutionPolicy Bypass -File export-apps.ps1
   ```
3. For Wi-Fi and Bluetooth in GNOME, once, in WSL (this stops WSL's NetworkManager, which has no
   radios to manage there, and lets `gnomeradios.py` take its place):
   ```
   sudo sh setup-radios.sh
   ```
4. For the touchpad gestures, stop Windows from using them itself: Settings → Bluetooth & devices →
   Touchpad → Three-finger gestures and Four-finger gestures → Swipes: **Nothing**.
5. Optional, for a 120 Hz display: set it to 120 Hz (Settings → System → Display → Advanced display),
   and raise WSLg's 60 Hz cap (GNOME follows it) in `C:\ProgramData\Microsoft\WSL\.wslgconfig`
   (as administrator), then `wsl --shutdown`:
   ```
   [system-distro-env]
   WESTON_RDP_MONITOR_REFRESH_RATE=120
   ```
6. Start (or restart) everything:
   ```
   powershell -ExecutionPolicy Bypass -File start-gnome.ps1
   ```
   Stop with `start-gnome.ps1 stop`, or the *Exit GNOME* launcher inside GNOME.

## How it works

| Piece | Side | Job |
|---|---|---|
| `gnome-nested.sh` | WSL | Nested GNOME Shell in one fullscreen WSLg window (software rendering) |
| `winbridge.py` | Windows | Captures each app window (Windows.Graphics.Capture) and streams the changed pixels; posts mouse and keyboard input back; reads touchpad swipes and the Windows key; lays the focused window natively over its mirror; reports the wallpaper and dark mode; takes the apps' tray icons in front of Explorer (passing each on to it) and clicks them |
| `gnomebridge.py` | WSL | One GTK window per Windows window: draws the frames, sends input back |
| `share/gnome-shell/extensions/windows-touchpad@gnome-windows` | GNOME | Feeds the swipes to GNOME's own gesture handling and presses Super; tells winbridge.py where the focused mirror is while nothing of GNOME's covers it; sets Windows' wallpaper and dark mode; shows the tray icons and their balloons |
| `export-apps.ps1`, `win-launch.sh` | both | Start-menu apps as GNOME launchers |
| `gnomeradios.py` | WSL | NetworkManager and BlueZ on the system bus (and GNOME Settings Daemon's rfkill on the session bus), as far as GNOME uses them, passing everything on to winradios.py |
| `winradios.py` | Windows | Runs that on Windows' Wi-Fi (native Wi-Fi API) and Bluetooth (WinRT device enumeration and pairing; audio devices through their driver, like Windows' Settings); reports their state |
| `setup-radios.sh` | WSL | Once, as root: NetworkManager off, D-Bus and polkit rules for gnomeradios.py |

## Known limitations

- Only the focused window is native. The others are mirrors: their input is posted as window
  messages (apps reading raw input, such as games, and some Ctrl/Alt shortcuts don't react), and
  GNOME renders in software, so a maximized mirror updates at roughly 15–30 fps.
- Of GNOME's own shortcuts, only Super (and Super+anything), Alt+Tab and Ctrl+Alt+arrows reach GNOME
  from the native window; others (Alt+F4, Alt+Space, ...) go to the app.
- GNOME notification banners can end up under the native window.
- Two mirrors side by side in GNOME can cover each other on the Windows side; then only the focused
  one keeps updating.
- Assumes the distro name `Ubuntu-24.04`, Linux user id 1000 and GNOME Shell 46.
- Wi-Fi: GNOME joins personal (password) and open networks, and any network Windows has saved;
  an enterprise (802.1X) network has to be joined once in Windows' own settings. A saved network's
  settings can't be edited from GNOME (only connected, disconnected, forgotten). No wired, VPN or
  hotspot entries. Windows lists Wi-Fi networks to desktop apps only with location access (Settings →
  Privacy & security → Location → Let desktop apps access your location).
- Bluetooth: only audio devices connect and disconnect on request, as in Windows; mice, keyboards and
  the like connect by themselves.
- Tray: at most 6 pinned icons in the top bar (more go in the ▾ menu), without tooltips. Explorer's
  own (battery, volume, network) are left out: GNOME shows those itself. An app running as
  administrator shows its icon only after its next change. Apps that place a panel of their own next
  to their icon (rather than a menu) open it at the bottom right, where Explorer's hidden icon is.

## Tests

```
py test_winbridge.py
```
In WSL:
```
dbus-run-session -- python3 test_gnomeradios.py
```

## License

MIT, see [LICENSE](LICENSE).
