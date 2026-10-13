"""Windows side of the GNOME bridge: mirrors every Windows app window into GNOME (running in WSL).

Each app window gets its own connection to gnomebridge.py: its client area is captured with
Windows.Graphics.Capture (PrintWindow polling as fallback) and only the changed rectangles are
streamed; mouse/keyboard/close events coming back are posted to the window as Win32 messages.
New windows (including dialogs) appear, closed ones disappear. The focused one is shown natively
over its mirror (Overlay): real window, real input, no stream in between. Notification-area icons
become buttons in GNOME's top bar (Tray).

    py winbridge.py [port]          needs: pip install numpy; pip install --no-deps windows-capture

ponytail: mirrors not shown natively get PostMessage input, so Ctrl/Alt shortcuts and apps that read
raw input (games, some UWP/Chromium apps) don't react there; the native one has the real keyboard.
"""
import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import socket
import struct
import sys
import threading
import time
import types
import uuid
import winreg
import zlib

import numpy as np

try:
    # windows-capture imports OpenCV only for saving images, which we never do; skip the 40 MB install
    sys.modules.setdefault("cv2", types.ModuleType("cv2"))
    from windows_capture import WindowsCapture  # Windows.Graphics.Capture: frames only when content changes
except ImportError:
    WindowsCapture = None  # fall back to polling with PrintWindow

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32")
kernel32 = ctypes.WinDLL("kernel32")
dwmapi = ctypes.WinDLL("dwmapi")
user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2: real pixel sizes

H = wt.HWND
user32.PostMessageW.argtypes = [H, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PrintWindow.argtypes = [H, wt.HDC, wt.UINT]
user32.GetDC.argtypes = [H]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [H, wt.HDC]
user32.GetClientRect.argtypes = [H, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.argtypes = [H, ctypes.POINTER(wt.RECT)]
user32.ChildWindowFromPointEx.argtypes = [H, wt.POINT, wt.UINT]
user32.ChildWindowFromPointEx.restype = H
user32.MapWindowPoints.argtypes = [H, H, ctypes.POINTER(wt.POINT), wt.UINT]
user32.ClientToScreen.argtypes = [H, ctypes.POINTER(wt.POINT)]
user32.GetWindowTextW.argtypes = [H, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [H, wt.LPWSTR, ctypes.c_int]
user32.IsWindow.argtypes = [H]
user32.IsWindowVisible.argtypes = [H]
user32.IsIconic.argtypes = [H]
user32.ShowWindow.argtypes = [H, ctypes.c_int]
user32.GetWindow.argtypes = [H, wt.UINT]
user32.GetWindow.restype = H
user32.GetWindowLongPtrW.argtypes = [H, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [H, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.NotifyWinEvent.argtypes = [wt.DWORD, H, wt.LONG, wt.LONG]
user32.GetWindowThreadProcessId.argtypes = [H, ctypes.POINTER(wt.DWORD)]
user32.SetWindowPos.argtypes = [H, H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetWindowRgn.argtypes = [H, wt.HRGN, wt.BOOL]
user32.mouse_event.argtypes = [wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
gdi32.CreateRectRgn.restype = wt.HRGN
dwmapi.DwmGetWindowAttribute.argtypes = [H, wt.DWORD, ctypes.c_void_p, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.GetApplicationUserModelId.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_uint32), wt.LPWSTR]
kernel32.GetCurrentProcess.restype = wt.HANDLE
kernel32.SetProcessInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
user32.FindWindowExW.argtypes = [H, H, wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowExW.restype = H
user32.GetForegroundWindow.restype = H
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.SetForegroundWindow.argtypes = [H]
user32.SendMessageTimeoutW.argtypes = [H, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT, ctypes.POINTER(ctypes.c_ssize_t)]
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]

# Never mirror: the WSLg windows themselves (GNOME is one of them), shell surfaces, other shells.
SKIP_PROCS = {"msrdc.exe", "seelen-ui.exe", "textinputhost.exe", "shellexperiencehost.exe",
              "startmenuexperiencehost.exe", "searchhost.exe", "lockapp.exe", "yasb.exe"}
SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow"}


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
    _fields_ = [("Version", wt.ULONG), ("ControlMask", wt.ULONG), ("StateMask", wt.ULONG)]


def no_power_throttling():
    """Windows runs a hidden background process like this one on efficiency cores at low clocks (EcoQoS).
    Everything GNOME shows of Windows goes through here, so opt out: full-window updates 15.5 -> 18 fps."""
    state = PROCESS_POWER_THROTTLING_STATE(1, 0x1 | 0x4, 0)  # EXECUTION_SPEED | IGNORE_TIMER_RESOLUTION off
    kernel32.SetProcessInformation(kernel32.GetCurrentProcess(), 4, ctypes.byref(state), ctypes.sizeof(state))


def text(fn, hwnd):
    buf = ctypes.create_unicode_buffer(512)
    fn(hwnd, buf, 512)
    return buf.value


def process_info(hwnd):
    """(lowercase exe path, AppUserModelID) of the window's process; the ID is set for packaged apps only."""
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return "", ""
    buf, size = ctypes.create_unicode_buffer(520), wt.DWORD(520)
    kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
    idbuf, idlen = ctypes.create_unicode_buffer(256), ctypes.c_uint32(256)
    aumid = idbuf.value if kernel32.GetApplicationUserModelId(h, ctypes.byref(idlen), idbuf) == 0 else ""
    kernel32.CloseHandle(h)
    return buf.value.lower(), aumid


def process_name(hwnd):
    return os.path.basename(process_info(hwnd)[0])


def load_apps():
    """Lookup tables from share/windows-apps.json (written by export-apps.ps1)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "share", "windows-apps.json")
    try:
        with open(path, encoding="utf-8") as f:
            apps = json.load(f)
    except (OSError, ValueError):
        return {}, {}, {}, []
    if isinstance(apps, dict):  # PowerShell writes a lone object for a one-app list
        apps = [apps]
    by_appid = {a["appid"].lower(): a["id"] for a in apps}
    by_exe = {a["exe"]: a["id"] for a in apps if a["exe"]}
    by_base = {os.path.basename(a["exe"]): a["id"] for a in apps if a["exe"]}
    by_name = sorted(((a["name"], a["id"]) for a in apps), key=lambda t: -len(t[0]))
    return by_appid, by_exe, by_base, by_name


APPS = load_apps()


def app_for(hwnd, title):
    """GNOME launcher id (windows-<slug>) of the app owning this window, or "" if unknown."""
    by_appid, by_exe, by_base, by_name = APPS
    # Store apps draw in a CoreWindow that belongs to the real app process, not the frame host
    owner = (store_app_window(hwnd) if text(user32.GetClassNameW, hwnd) == "ApplicationFrameWindow" else None) or hwnd
    path, aumid = process_info(owner)
    if aumid.lower() in by_appid:
        return by_appid[aumid.lower()]
    if path in by_exe:
        return by_exe[path]
    if os.path.basename(path) in by_base:  # e.g. Discord runs from app-x.y\Discord.exe, not the stub
        return by_base[os.path.basename(path)]
    for name, app in by_name:  # e.g. Steam's window belongs to steamwebhelper.exe; "x - Notepad++"
        if title == name or title.endswith(" " + name):
            return app
    return ""


def mirrorable(hwnd):
    """App windows and their dialogs; not menus, tooltips, tool windows, shell or hidden/cloaked ones."""
    if not user32.IsWindowVisible(hwnd) or not text(user32.GetWindowTextW, hwnd):
        return False
    style = user32.GetWindowLongPtrW(hwnd, -16)
    ex = user32.GetWindowLongPtrW(hwnd, -20)
    if style & 0x40000000 or ex & 0x08000000:  # WS_CHILD, WS_EX_NOACTIVATE
        return False
    if ex & 0x80 and not ex & 0x40000:  # WS_EX_TOOLWINDOW without WS_EX_APPWINDOW
        return False
    if user32.GetWindow(hwnd, 4) and not style & 0x00C00000:  # owned and no caption: popup/menu
        return False
    cloaked = ctypes.c_int()
    dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
    if cloaked.value:
        return False
    return text(user32.GetClassNameW, hwnd) not in SKIP_CLASSES and process_name(hwnd) not in SKIP_PROCS


def app_windows():
    found = []
    cb = ctypes.WINFUNCTYPE(wt.BOOL, H, wt.LPARAM)(lambda h, _: found.append(h) or True)
    user32.EnumWindows(cb, 0)
    return [h for h in found if mirrorable(h)]


LAUNCH_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gnome-launch.txt")  # appended by win-launch.sh


def launched_from_gnome():
    """AppIDs started from a GNOME launcher in the last 20 s ("<unix time> <AppID>" lines)."""
    try:
        with open(LAUNCH_LOG, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return set()
    now, ids = time.time(), set()
    for line in lines:
        stamp, _, appid = line.partition(" ")
        if appid and stamp.isdigit() and now - int(stamp) < 20:
            ids.add(appid.lower())
    return ids


def focus(hwnd):
    # Windows refuses SetForegroundWindow from a background process unless a key was just pressed;
    # an Alt tap (lands in GNOME, which ignores a lone Alt) lifts that lock.
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)


def gnome_window():
    return user32.FindWindowExW(None, None, "RAIL_WINDOW", f"gnome-shell ({os.environ.get('GNOME_DISTRO', 'Ubuntu-24.04')})")


def keep_behind_gnome(hwnd):
    """A newly mirrored window may be minimized (captures as black) or pop up over fullscreen GNOME: show
    it under GNOME, focus back to GNOME. Without GNOME the windows are the user's again: hands off."""
    gnome = gnome_window()
    if not gnome:
        return
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
    user32.SetWindowPos(hwnd, H(1), 0, 0, 0, 0, 0x1 | 0x2 | 0x10)  # HWND_BOTTOM, NOSIZE | NOMOVE | NOACTIVATE
    if user32.GetForegroundWindow() == hwnd:
        focus(gnome)


uncloak_tried = set()


def uncloak_launched(wanted):
    """Store apps started while GNOME has the foreground stay cloaked (invisible, uncapturable) until
    activated once. Activate just those (once each), then hand the foreground back to GNOME."""
    found = []
    cb = ctypes.WINFUNCTYPE(wt.BOOL, H, wt.LPARAM)(lambda h, _: found.append(h) or True)
    user32.EnumWindows(cb, 0)
    for hwnd in found:
        cloaked = ctypes.c_int()
        dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)
        if not cloaked.value or hwnd in uncloak_tried or text(user32.GetClassNameW, hwnd) != "ApplicationFrameWindow":
            continue
        core = store_app_window(hwnd)
        # ponytail: if that app also has a window on another virtual desktop, Windows switches to it
        if core and process_info(core)[1].lower() in wanted:
            uncloak_tried.add(hwnd)
            focus(hwnd)
            time.sleep(0.5)
            gnome = gnome_window()
            if gnome:
                focus(gnome)


def store_app_window(frame):
    """The CoreWindow of a Store app's frame: its child once shown, a same-titled top-level window before."""
    return (user32.FindWindowExW(frame, None, "Windows.UI.Core.CoreWindow", None)
            or user32.FindWindowExW(None, None, "Windows.UI.Core.CoreWindow", text(user32.GetWindowTextW, frame)))


def client_size(hwnd):
    rc = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    return rc.right, rc.bottom


def capture(hwnd):
    """Client area as top-down BGRA bytes, even when the window is covered (not when minimized)."""
    w, h = client_size(hwnd)
    if w <= 0 or h <= 0:
        return None
    bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    hdc = user32.GetDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bits = ctypes.c_void_p()
    hbm = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(mdc, hbm)
    user32.PrintWindow(hwnd, mdc, 3)  # PW_CLIENTONLY | PW_RENDERFULLCONTENT
    data = ctypes.string_at(bits, w * h * 4)
    gdi32.SelectObject(mdc, old)
    gdi32.DeleteObject(hbm)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(hwnd, hdc)
    return w, h, data


def client_box(hwnd):
    """Client area inside a Windows.Graphics.Capture frame, which covers the window's visible bounds."""
    ext, pt = wt.RECT(), wt.POINT(0, 0)
    dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(ext), ctypes.sizeof(ext))  # DWMWA_EXTENDED_FRAME_BOUNDS
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    w, h = client_size(hwnd)
    return pt.x - ext.left, pt.y - ext.top, w, h


def start_wgc(hwnd, on_frame, interval_ms):
    """Capture hwnd with Windows.Graphics.Capture, at most every interval_ms. on_frame(pixels) gets the
    client area as an (h, w) uint32 view into the capture buffer, valid only during the call (on the
    capture thread). Returns a control with .stop(), or None if unavailable."""
    if WindowsCapture is None:
        return None
    try:
        cap = WindowsCapture(cursor_capture=False, draw_border=False, minimum_update_interval=interval_ms,
                             window_hwnd=int(hwnd))

        @cap.event
        def on_frame_arrived(frame, control):
            x, y, w, h = client_box(hwnd)
            # while a window is being resized the frame can lag behind its new size
            w, h = min(w, frame.width - x), min(h, frame.height - y)
            if x >= 0 and y >= 0 and w > 0 and h > 0:
                on_frame(frame.frame_buffer[y:y + h, x:x + w].view(np.uint32)[..., 0])

        @cap.event
        def on_closed():
            pass

        return cap.start_free_threaded()
    except Exception as e:  # e.g. a window the API refuses; PrintWindow polling still works
        print("Windows.Graphics.Capture unavailable for this window:", e)
        return None


def child_at(hwnd, x, y):
    """Deepest visible child under client point (x, y) and the point in its own coordinates."""
    pt, target = wt.POINT(x, y), hwnd
    while True:
        child = user32.ChildWindowFromPointEx(target, pt, 0x1 | 0x4)  # skip invisible | transparent
        if not child or child == target:
            return target, pt.x, pt.y
        user32.MapWindowPoints(target, child, ctypes.byref(pt), 1)
        target = child


def lparam_xy(x, y):
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def raise_under_gnome(hwnd):
    """Put hwnd right below GNOME: still hidden by it, but above the other mirrored windows. Firefox and
    Chromium apps stop painting while another window fully covers them (they don't count GNOME, whose
    layered window looks transparent to them), so only the topmost maximized one stays awake.
    ponytail: two mirrors visible side by side in GNOME can still cover each other on the Windows side,
    so only the focused one updates; placing the Windows windows apart is the upgrade path."""
    gnome = gnome_window()
    # SetWindowPos after NULL would mean HWND_TOP: in front of GNOME. The native one stays there (Overlay)
    if gnome and hwnd != overlay.hwnd:
        user32.SetWindowPos(hwnd, gnome, 0, 0, 0, 0, 0x1 | 0x2 | 0x10)  # NOSIZE | NOMOVE | NOACTIVATE
        # Chromium only rechecks occlusion on events like this one, not on a z-order change
        user32.NotifyWinEvent(0x800B, hwnd, 0, 0)  # EVENT_OBJECT_LOCATIONCHANGE, OBJID_WINDOW, CHILDID_SELF
    return gnome


def send_under_gnome(top, target, msg, wparam, lparam):
    """Send msg to target with its top-level window right below GNOME and GNOME hit-test transparent.
    Firefox drops wheel messages unless WindowFromPoint at the cursor finds its own window, and the
    cursor is always over fullscreen GNOME. GNOME's window is layered, so WS_EX_TRANSPARENT changes
    nothing on screen; it is click-through only while the app handles the message (~1 ms).
    ponytail: the cursor must also be within the app's real window, true for maximized windows;
    moving the window under the cursor first is the upgrade path."""
    gnome = raise_under_gnome(top)
    if not gnome:
        user32.PostMessageW(target, msg, wparam, lparam)
        return
    ex = user32.GetWindowLongPtrW(gnome, -20) & ~0x20
    user32.SetWindowLongPtrW(gnome, -20, ex | 0x20)  # WS_EX_TRANSPARENT
    try:
        user32.SendMessageTimeoutW(target, msg, wparam, lparam, 0x2, 100, None)  # SMTO_ABORTIFHUNG
    finally:
        user32.SetWindowLongPtrW(gnome, -20, ex)


def draws_own_titlebar(hwnd):
    """True for Chromium/Electron/Firefox/Store-style windows whose client area reaches the top edge.
    Those get no GNOME title bar; classic windows keep it, since their caption isn't captured."""
    wr, pt = wt.RECT(), wt.POINT(0, 0)
    user32.GetWindowRect(hwnd, ctypes.byref(wr))
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.y - wr.top < 12 * user32.GetDpiForWindow(hwnd) / 96


def hit_test(hwnd, x, y):
    """What the app says is under client point (x, y): caption, a caption button, a resize edge, ..."""
    pt = wt.POINT(x, y)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    res = ctypes.c_ssize_t(0)
    ok = user32.SendMessageTimeoutW(hwnd, 0x84, 0, lparam_xy(pt.x, pt.y), 0x2, 100, ctypes.byref(res))  # WM_NCHITTEST
    return ctypes.c_int(res.value).value if ok else 1


# WM_NCHITTEST results GNOME should handle itself: caption, min/max/close buttons, resize edges
TITLEBAR_HITS = {2, 8, 9, 20, 10, 11, 12, 13, 14, 15, 16, 17}


def gnome_in_front():
    """GNOME has the foreground, or the window shown natively inside it (Overlay) does."""
    fg = user32.GetForegroundWindow()
    return bool(fg) and fg in (gnome_window(), overlay.hwnd)


# Top band of a window that draws its own title bar (Chromium tab strips, Store apps) where presses on
# the native window are hit-tested, to give title-bar presses to GNOME (Overlay.mouse_hook). Kept
# narrow: the hit test asks the app, while Windows waits for the hook.
# ponytail: calibration knob, a guess at the tallest common title bar
CAPTION_DIP = 40
OWN_PRESS = 0x57424E47  # dwExtraInfo of the press mouse_hook makes itself, so it lets that one pass


class Overlay:
    """The focused mirror's real window, laid exactly over the mirror and on top of GNOME while nothing
    of GNOME's own covers or moves it (the windows-touchpad extension says when). The app then draws
    through DWM at the display's rate and gets real mouse and keyboard input; nothing goes through the
    capture and the stream. It is clipped to its client area, so GNOME's title bar shows around it.
    Otherwise it goes back under GNOME, where the mirror shows it as before.
    ponytail: only the focused window; the others stay streamed. A killed winbridge.py leaves it
    clipped and on top (start-gnome.ps1 stops GNOME first, which ends it cleanly)."""

    def __init__(self):
        self.hwnd = 0  # the window shown natively now
        self.band = None  # its title-bar band on screen (left, top, right, bottom), see mouse_hook
        self.want = {"o": 0}  # the extension's last word: {o: hwnd, x, y, w, h} of the mirror's frame
        self.areas = {}  # hwnd -> its mirror's content size in GNOME ("area" events)
        # reentrant: mouse_hook's hit test waits in SendMessage, where the next mouse event can call it again
        self.lock = threading.RLock()

    def update(self, want=None):
        """New state from the extension; None checks the last one again."""
        with self.lock:
            if want is not None:
                self.want = want
            old, self.hwnd = self.hwnd, self.place(self.want)
            if old == self.hwnd:
                return
            print("* native:", text(user32.GetWindowTextW, self.hwnd) if self.hwnd else "-")
            if old:
                self.drop(old)
            # the keyboard follows (if GNOME or the old one had it): to the app itself, not through the
            # mirror, or back to GNOME
            gnome, fg = gnome_window(), user32.GetForegroundWindow()
            target = self.hwnd or gnome
            if gnome and fg and fg in (gnome, old) and fg != target:
                focus(target)

    def drop(self, hwnd):
        """Back under GNOME, unclipped (self.hwnd no longer names it)."""
        user32.SetWindowRgn(hwnd, None, True)
        user32.SetWindowPos(hwnd, H(-2), 0, 0, 0, 0, 0x1 | 0x2 | 0x10)  # HWND_NOTOPMOST, NOSIZE | NOMOVE | NOACTIVATE
        raise_under_gnome(hwnd)

    def mouse_hook(self, code, wparam, lparam):
        """Low-level mouse hook: a left press on the native window's own title bar (caption, its buttons,
        edges) belongs to GNOME, as on the mirror. Swallow it, drop the window under GNOME and press
        again: that press lands on the mirror, whose hit test lets GNOME move, maximize, close..."""
        band = self.band
        if code == 0 and wparam == 0x201 and band:  # HC_ACTION, WM_LBUTTONDOWN
            m = MSLLHOOKSTRUCT.from_address(lparam)
            x, y = m.pt.x, m.pt.y
            if m.dwExtraInfo != OWN_PRESS and band[0] <= x < band[2] and band[1] <= y < band[3]:
                with self.lock:
                    hwnd = self.hwnd
                    if hwnd and hit_test(hwnd, x - band[0], y - band[1]) in TITLEBAR_HITS:
                        self.hwnd, self.band = 0, None
                        print("* native: - (title bar press)")
                        self.drop(hwnd)
                        user32.mouse_event(0x2, 0, 0, 0, OWN_PRESS)  # MOUSEEVENTF_LEFTDOWN
                        return 1
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def resized(self, hwnd):
        """The window or its mirror changed size: maybe they agree now."""
        if hwnd == self.want.get("o"):
            self.update()

    def place(self, want):
        """Lay the window over its mirror and return it, or 0 if it can't be shown there now."""
        self.band = None
        hwnd, gnome = want.get("o", 0), gnome_window()
        if not hwnd or not gnome or not user32.IsWindow(hwnd):
            return 0
        cw, ch = client_size(hwnd)
        aw, ah = self.areas.get(hwnd, (0, 0))
        # the mirror follows a resize with a delay (see gnomebridge.py's Mirror.on_size): wait for it
        if abs(aw - cw) > 2 or abs(ah - ch) > 2:
            return 0
        gr, wr, pt = wt.RECT(), wt.RECT(), wt.POINT(0, 0)
        user32.GetWindowRect(gnome, ctypes.byref(gr))
        user32.GetWindowRect(hwnd, ctypes.byref(wr))
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        dx, dy = pt.x - wr.left, pt.y - wr.top  # client area within the window
        # the content fills the bottom of GNOME's frame, under GTK's title bar if it has one
        x, y = gr.left + want["x"] - dx, gr.top + want["y"] + want["h"] - ah - dy
        # only the client area: GNOME's title bar shows around it (the capture sees the clipped window
        # just like the screen, so nothing that should show in the mirror may be cut off)
        if not user32.SetWindowRgn(hwnd, gdi32.CreateRectRgn(dx, dy, dx + cw, dy + ch), True):
            return 0  # e.g. an app running as administrator
        # Topmost: GNOME coming to the front can't cover it then. A plain HWND_TOP would not even get
        # over GNOME: Windows ignores it from this background process while GNOME has the foreground.
        if not user32.SetWindowPos(hwnd, H(-1), x, y, 0, 0, 0x1 | 0x10):  # HWND_TOPMOST, NOSIZE | NOACTIVATE
            user32.SetWindowRgn(hwnd, None, True)
            return 0
        if draws_own_titlebar(hwnd):
            left, top = x + dx, y + dy
            self.band = (left, top, left + cw, top + int(CAPTION_DIP * user32.GetDpiForWindow(hwnd) / 96))
        return hwnd


overlay = Overlay()


BUTTONS = {1: (0x201, 0x202, 0x1), 2: (0x207, 0x208, 0x10), 3: (0x204, 0x205, 0x2)}  # GDK button -> (down, up, MK_)
EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}


class Input:
    def __init__(self, hwnd, own_titlebar, reply, on_input):
        self.hwnd = hwnd
        self.own_titlebar = own_titlebar
        self.reply = reply  # sends a WM_NCHITTEST code back to the viewer
        self.on_input = on_input  # wakes the capture loop: the app is about to repaint
        self.swallowed = False
        self.held = 0
        w, h = client_size(hwnd)
        self.focus = child_at(hwnd, w // 2, h // 2)[0]  # keyboard target until the first click

    def handle(self, ev):
        t = ev["t"]
        self.on_input()  # hover effects count too: the pointer is over this window
        if t in ("down", "up", "move"):
            if t == "down" and ev["b"] == 1 and self.own_titlebar:
                hit = hit_test(self.hwnd, ev["x"], ev["y"])
                if hit in TITLEBAR_HITS:  # GNOME moves/resizes/closes; the app never sees this click
                    self.swallowed = True
                    self.reply(hit)
                    return
            if t == "up" and self.swallowed:
                self.swallowed = False
                return
            target, x, y = child_at(self.hwnd, ev["x"], ev["y"])
            if t == "move":
                user32.PostMessageW(target, 0x200, self.held, lparam_xy(x, y))
                return
            down, up, mk = BUTTONS.get(ev["b"], BUTTONS[1])
            if t == "down":
                self.held |= mk
                self.focus = target
                user32.PostMessageW(target, down, self.held, lparam_xy(x, y))
            else:
                self.held &= ~mk
                user32.PostMessageW(target, up, self.held, lparam_xy(x, y))
        elif t == "wheel":
            target, _, _ = child_at(self.hwnd, ev["x"], ev["y"])
            pt = wt.POINT(ev["x"], ev["y"])
            user32.ClientToScreen(self.hwnd, ctypes.byref(pt))  # WM_MOUSEWHEEL wants screen coords
            delta = int(-ev["dy"] * 120) & 0xFFFF
            send_under_gnome(self.hwnd, target, 0x20A, delta << 16, lparam_xy(pt.x, pt.y))
        elif t == "char":
            for c in ev["c"]:
                user32.PostMessageW(self.focus, 0x102, ord(c), 1)  # WM_CHAR
        elif t == "vk":
            vk = ev["vk"]
            lp = 1 | (user32.MapVirtualKeyW(vk, 0) << 16) | ((1 << 24) if vk in EXTENDED else 0)
            user32.PostMessageW(self.focus, 0x100, vk, lp)  # WM_KEYDOWN
            user32.PostMessageW(self.focus, 0x101, vk, lp | (3 << 30))  # WM_KEYUP
        elif t == "focus":  # GNOME focused/unfocused the mirror: lets apps show or hide their caret
            if ev["on"]:  # e.g. switched to its workspace: wake it up if another window covered it
                raise_under_gnome(self.hwnd)
            user32.PostMessageW(self.hwnd, 0x0006, 1 if ev["on"] else 0, 0)  # WM_ACTIVATE
            user32.PostMessageW(self.focus, 0x0007 if ev["on"] else 0x0008, 0, 0)  # WM_SETFOCUS / KILLFOCUS
        elif t == "close":
            user32.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE: app may ask to save, that dialog gets mirrored too
        elif t == "resize":
            wr, cr = wt.RECT(), wt.RECT()
            user32.GetWindowRect(self.hwnd, ctypes.byref(wr))
            user32.GetClientRect(self.hwnd, ctypes.byref(cr))
            extra_w = (wr.right - wr.left) - cr.right
            extra_h = (wr.bottom - wr.top) - cr.bottom
            user32.SetWindowPos(self.hwnd, None, 0, 0, ev["w"] + extra_w, ev["h"] + extra_h,
                                0x2 | 0x4 | 0x10)  # NOMOVE | NOZORDER | NOACTIVATE
            overlay.resized(self.hwnd)
        elif t == "area":  # the mirror's content size in GNOME, right away (resize waits for the drag's end)
            overlay.areas[self.hwnd] = (ev["w"], ev["h"])
            overlay.resized(self.hwnd)


hid = ctypes.WinDLL("hid")
HIDP_OK = 0x00110000
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
hid.HidP_GetValueCaps.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(wt.USHORT), ctypes.c_void_p]
hid.HidP_GetUsageValue.argtypes = [ctypes.c_int, wt.USHORT, wt.USHORT, wt.USHORT, ctypes.POINTER(wt.ULONG),
                                   ctypes.c_void_p, ctypes.c_char_p, wt.ULONG]
hid.HidP_GetUsages.argtypes = [ctypes.c_int, wt.USHORT, wt.USHORT, ctypes.POINTER(wt.USHORT),
                               ctypes.POINTER(wt.ULONG), ctypes.c_void_p, ctypes.c_char_p, wt.ULONG]
user32.GetRawInputDeviceInfoW.argtypes = [wt.HANDLE, wt.UINT, ctypes.c_void_p, ctypes.POINTER(wt.UINT)]
user32.GetRawInputData.argtypes = [ctypes.c_void_p, wt.UINT, ctypes.c_void_p, ctypes.POINTER(wt.UINT), wt.UINT]
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, H, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
user32.CreateWindowExW.restype = H


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT), ("dwFlags", wt.DWORD), ("hwndTarget", H)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", wt.USHORT), ("UsagePage", wt.USHORT), ("InputReportByteLength", wt.USHORT),
                ("OutputReportByteLength", wt.USHORT), ("FeatureReportByteLength", wt.USHORT),
                ("Reserved", wt.USHORT * 17), ("NumberLinkCollectionNodes", wt.USHORT),
                ("NumberInputButtonCaps", wt.USHORT), ("NumberInputValueCaps", wt.USHORT),
                ("rest", wt.USHORT * 7)]


class HIDP_VALUE_CAPS(ctypes.Structure):
    _fields_ = [("UsagePage", wt.USHORT), ("ReportID", ctypes.c_ubyte), ("IsAlias", ctypes.c_ubyte),
                ("BitField", wt.USHORT), ("LinkCollection", wt.USHORT), ("LinkUsage", wt.USHORT),
                ("LinkUsagePage", wt.USHORT), ("flags", ctypes.c_ubyte * 6), ("BitSize", wt.USHORT),
                ("ReportCount", wt.USHORT), ("Reserved2", wt.USHORT * 5), ("UnitsExp", wt.ULONG),
                ("Units", wt.ULONG), ("LogicalMin", wt.LONG), ("LogicalMax", wt.LONG),
                ("PhysicalMin", wt.LONG), ("PhysicalMax", wt.LONG), ("Usage", wt.USHORT), ("rest", wt.USHORT * 7)]


def physical_mm(caps):
    """Size of a HID value's physical range in mm (precision touchpads report cm or inches)."""
    exp = caps.UnitsExp - 16 if caps.UnitsExp > 7 else caps.UnitsExp  # a signed nibble
    size = (caps.PhysicalMax - caps.PhysicalMin) * 10.0 ** exp
    return size * (25.4 if caps.Units & 0xF == 3 else 10)  # 0x13 inch, 0x11 cm


class Touchpad:
    """HID layout of one precision touchpad: per finger a link collection with tip switch, contact id,
    x and y. report() turns input reports into frames {contact id: (x mm, y mm)} of the fingers down."""

    def __init__(self, hdev):
        size = wt.UINT()
        user32.GetRawInputDeviceInfoW(hdev, 0x20000005, None, ctypes.byref(size))  # RIDI_PREPARSEDDATA
        self.pre = ctypes.create_string_buffer(size.value)
        user32.GetRawInputDeviceInfoW(hdev, 0x20000005, self.pre, ctypes.byref(size))
        caps = HIDP_CAPS()
        hid.HidP_GetCaps(self.pre, ctypes.byref(caps))
        n = wt.USHORT(caps.NumberInputValueCaps)
        values = (HIDP_VALUE_CAPS * n.value)()
        hid.HidP_GetValueCaps(0, values, ctypes.byref(n), self.pre)  # HidP_Input
        self.links, self.mm = [], {}
        for c in values[:n.value]:
            if c.UsagePage == 1 and c.Usage in (0x30, 0x31):  # X, Y
                self.mm[c.Usage] = physical_mm(c) / max(c.LogicalMax - c.LogicalMin, 1)
                if c.Usage == 0x30:
                    self.links.append(c.LinkCollection)
        self.frame, self.left = None, 0

    def value(self, report, page, link, usage):
        v = wt.ULONG()
        ok = hid.HidP_GetUsageValue(0, page, link, usage, ctypes.byref(v), self.pre, report, len(report))
        return v.value if ok == HIDP_OK else None

    def report(self, report):
        count = self.value(report, 0x0D, 0, 0x54)  # contact count: set in a frame's first report
        if count:
            self.frame, self.left = {}, count
        if self.frame is None:
            return None
        for link in self.links:  # a frame may span several reports ("hybrid" touchpads)
            if self.left <= 0:
                break
            self.left -= 1
            usages, n = (wt.USHORT * 16)(), wt.ULONG(16)
            hid.HidP_GetUsages(0, 0x0D, link, usages, ctypes.byref(n), self.pre, report, len(report))
            if 0x42 in usages[:n.value]:  # tip switch: this finger is on the pad
                self.frame[self.value(report, 0x0D, link, 0x51)] = (
                    self.value(report, 1, link, 0x30) * self.mm[0x30], self.value(report, 1, link, 0x31) * self.mm[0x31])
        if self.left > 0:
            return None
        frame, self.frame = self.frame, None
        return frame


def touchpad_loop(on_frame):
    """Read precision touchpads as raw input, also while GNOME is in front: on_frame(frame) per touchpad
    frame (see Touchpad). Runs a message loop, so call it in its own thread."""
    hwnd = user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, H(-3), None, None, None)  # HWND_MESSAGE
    dev = RAWINPUTDEVICE(0x0D, 0x05, 0x100, hwnd)  # digitizer page, touch pad; RIDEV_INPUTSINK
    if not user32.RegisterRawInputDevices(ctypes.byref(dev), 1, ctypes.sizeof(dev)):
        print("no touchpad gestures: RegisterRawInputDevices failed")
        return
    pads, msg = {}, wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == 0xFF:  # WM_INPUT
            size = wt.UINT()
            user32.GetRawInputData(msg.lParam, 0x10000003, None, ctypes.byref(size), 24)  # RID_INPUT
            buf = ctypes.create_string_buffer(size.value)
            user32.GetRawInputData(msg.lParam, 0x10000003, buf, ctypes.byref(size), 24)
            hdev = struct.unpack_from("<Q", buf, 8)[0]
            pad = pads.get(hdev) or pads.setdefault(hdev, Touchpad(hdev))
            each, count = struct.unpack_from("<II", buf, 24)  # RAWHID after the 24-byte header
            for i in range(count):
                frame = pad.report(buf.raw[32 + i * each:32 + (i + 1) * each])
                if frame is not None:
                    on_frame(frame)
        user32.DispatchMessageW(ctypes.byref(msg))


# libinput reports gesture motion as from a 1000 dpi device; GNOME's swipe distances assume that.
# ponytail: calibration knob, lower it if GNOME's swipes react to too little finger travel
GESTURE_UNITS_PER_MM = 1000 / 25.4


class Swipes:
    """libinput-style swipes of 3+ fingers from touchpad frames: send(phase, fingers, dx, dy) with phase
    begin / update / end, for swipes that start while GNOME is in front."""

    def __init__(self, send):
        self.send, self.fingers, self.active, self.last = send, 0, False, {}

    def frame(self, contacts):
        n = len(contacts)
        if self.fingers and n != self.fingers:  # a finger lifted or landed: that swipe is over
            if self.active:
                self.send("end", self.fingers, 0.0, 0.0)
            self.fingers = 0
        if not self.fingers and n >= 3:
            self.fingers = n
            self.active = gnome_in_front() and self.send("begin", n, 0.0, 0.0)
        elif self.fingers and self.active:
            both = contacts.keys() & self.last.keys()
            if both:
                dx = sum(contacts[i][0] - self.last[i][0] for i in both) / len(both) * GESTURE_UNITS_PER_MM
                dy = sum(contacts[i][1] - self.last[i][1] for i in both) / len(both) * GESTURE_UNITS_PER_MM
                if dx or dy:
                    self.active = self.send("update", n, dx, dy)
        self.last = contacts


class GnomeShell:
    """JSON lines with the GNOME Shell extension windows-touchpad@gnome-windows: swipes, the Super key and
    tray icons out, where the focused mirror is and tray clicks (for on_line) in. Out goes through a thread
    of its own: the keyboard hook has to return at once, or every key press on Windows waits."""

    def __init__(self, port, on_line):
        self.port, self.on_line, self.out, self.sock = port, on_line, queue.Queue(), None
        threading.Thread(target=self.send_loop, daemon=True).start()
        threading.Thread(target=self.read_loop, daemon=True).start()

    def __call__(self, **msg):
        self.out.put((json.dumps(msg) + "\n").encode())
        return True

    def do(self, fn):
        """Run fn() on the sending thread, in order with the messages."""
        self.out.put(fn)

    def send_loop(self):
        while True:
            item, sock = self.out.get(), self.sock
            if callable(item):
                item()
            elif sock:  # not connected: GNOME is (re)starting, this one is lost
                try:
                    sock.sendall(item)
                except OSError:
                    pass

    def read_loop(self):
        """Stay connected, also across GNOME restarts."""
        while True:
            try:
                sock = socket.create_connection(("localhost", self.port), timeout=0.5)
            except OSError:
                time.sleep(1)
                continue
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # every swipe update counts
            self.sock = sock
            try:
                for line in sock.makefile("r", encoding="utf-8"):
                    self.on_line(json.loads(line))
            except (OSError, ValueError):
                pass
            self.sock = None
            sock.close()
            self.on_line({"o": 0})  # GNOME is gone: nothing to show natively
            time.sleep(1)


def hkcu(key, name, default=None):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return default


def appearance():
    """Windows' dark mode and desktop wallpaper, for the extension to set in GNOME. v changes with the
    file, also when a slideshow or Spotlight rewrites the same one."""
    look = {"dark": hkcu(r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "AppsUseLightTheme", 1) == 0}
    # SPI_GETDESKWALLPAPER, not the registry value: that can be stale, and under the py launcher's app
    # container it is a virtualized one. Empty: a solid color, left alone
    buf = ctypes.create_unicode_buffer(1024)
    user32.SystemParametersInfoW(0x73, len(buf), buf, 0)
    path = buf.value
    if path and not os.path.exists(path):  # the original was deleted: Windows still shows its own copy
        path = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Themes\TranscodedWallpaper")
    try:
        st = os.stat(path)
    except OSError:
        return look
    # size too: a half-written file may already have its final mtime
    look["v"] = f"{st.st_mtime_ns}-{st.st_size}"
    look["wallpaper"] = f"/mnt/{path[0].lower()}{path[2:]}".replace("\\", "/")  # ponytail: default WSL automount
    return look


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE

SUPER_KEYS = {0x5B: 125, 0x5C: 126}  # VK_LWIN / VK_RWIN -> evdev KEY_LEFTMETA / KEY_RIGHTMETA
# time for GNOME's window to get the keyboard over RDP after it comes to the front.
# ponytail: calibration knob, a guess; Super pressed on a native window does nothing if too short
FOCUS_SETTLE_S = 0.1
# GNOME shortcuts taken from the window shown natively: their keys (Tab, Left, Up, Right, Down) and
# modifiers (left/right Shift and Ctrl, left Alt), VK -> evdev. Not right Alt: on many layouts it is
# AltGr, which Windows reports as right Alt plus a left Ctrl.
SHORTCUT_KEYS = {0x09: 15, 0x25: 105, 0x26: 103, 0x27: 106, 0x28: 108}
MODIFIERS = {0xA0: 42, 0xA1: 54, 0xA2: 29, 0xA3: 97, 0xA4: 56}


class GnomeKeys:
    """Keys that belong to GNOME, taken from Windows by a low-level keyboard hook.

    The Windows keys as GNOME's Super: pressed while GNOME (or a window shown natively in it, see
    Overlay) is in front, they never reach Windows (no Start menu, no Win+X shortcuts) but are pressed
    in GNOME by the extension. The other keys still go through RDP and combine with it there, so
    Super+A, Super+arrows and so on work too.

    Alt+Tab and Ctrl+Alt+arrows (with Shift too) pressed while the native window has the keyboard:
    the keyboard moves to GNOME, and the extension presses the modifiers held, then the key, there.
    Until those modifiers are released the shortcut's keys keep going that way: Alt+Tab+Tab walks
    GNOME's switcher."""

    def __init__(self, shell):
        self.shell, self.held = shell, set()
        self.mods = set()  # modifiers pressed in GNOME for a shortcut, until they are released
        self.taps = set()  # shortcut keys whose press was swallowed, so their release is too

    def hook(self, code, wparam, lparam):
        k = KBDLLHOOKSTRUCT.from_address(lparam)
        if code == 0 and not k.flags & 0x10:  # HC_ACTION, not injected
            down = wparam in (0x100, 0x104)  # WM_KEYDOWN, WM_SYSKEYDOWN
            if self.super_key(k.vkCode, down) or self.shortcut(k.vkCode, down):
                return 1  # swallowed: Windows never sees it
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def to_gnome(self):
        """The native window (Overlay) has the keyboard: hand it to GNOME first, off this hook, and
        only then press, since the press reaches GNOME through its focus."""
        gnome = gnome_window()
        self.shell.do(lambda: (focus(gnome), time.sleep(FOCUS_SETTLE_S)))

    def super_key(self, vk, down):
        evdev = SUPER_KEYS.get(vk)
        if not evdev:
            return False
        if down and vk not in self.held and gnome_in_front():
            self.held.add(vk)
            if user32.GetForegroundWindow() != gnome_window():
                self.to_gnome()
            self.shell(k=evdev, down=True)
            return True
        if vk in self.held:  # its auto-repeats and release (even if GNOME lost the front since)
            if not down:
                self.held.discard(vk)
                self.shell(k=evdev, down=False)
            return True
        return False

    def shortcut(self, vk, down):
        if vk in self.mods and not down:  # Windows saw the press, so it gets the release too
            self.mods.discard(vk)
            self.shell(k=MODIFIERS[vk], down=False)
            return False
        evdev = SHORTCUT_KEYS.get(vk)
        if not evdev:
            return False
        if not down:
            swallowed = vk in self.taps
            self.taps.discard(vk)
            return swallowed
        if not self.mods:
            held = {m for m in MODIFIERS if user32.GetAsyncKeyState(m) & 0x8000}
            native = overlay.hwnd and user32.GetForegroundWindow() == overlay.hwnd
            if not native or 0xA4 not in held or (vk != 0x09 and not held & {0xA2, 0xA3}):
                return False
            self.mods = held
            self.to_gnome()
            for m in sorted(held):  # Shift, Ctrl, Alt
                self.shell(k=MODIFIERS[m], down=True)
        self.taps.add(vk)
        self.shell(k=evdev, down=True)  # a tap: nothing stays pressed in GNOME if its release goes astray
        self.shell(k=evdev, down=False)
        return True


def hook_loop(kind, hook):
    """Run hook(code, wparam, lparam) as a low-level keyboard (13) or mouse (14) hook. The hook is called
    from this thread's message loop, so call it in a thread of its own."""
    proc = HOOKPROC(hook)
    if not user32.SetWindowsHookExW(kind, proc, kernel32.GetModuleHandleW(None), 0):
        print(f"SetWindowsHookExW({kind}) failed:", "no Super key or GNOME shortcuts" if kind == 13 else "title bars of native windows don't move them")
        return
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        pass


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, H, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


class COPYDATASTRUCT(ctypes.Structure):
    _fields_ = [("dwData", ctypes.c_size_t), ("cbData", wt.DWORD), ("lpData", ctypes.c_void_p)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD), ("yHotspot", wt.DWORD), ("hbmMask", wt.HBITMAP),
                ("hbmColor", wt.HBITMAP)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", wt.LONG), ("bmWidth", wt.LONG), ("bmHeight", wt.LONG), ("bmWidthBytes", wt.LONG),
                ("bmPlanes", wt.WORD), ("bmBitsPixel", wt.WORD), ("bmBits", ctypes.c_void_p)]


user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wt.ATOM
user32.DefWindowProcW.argtypes = [H, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.ChangeWindowMessageFilterEx.argtypes = [H, wt.UINT, wt.DWORD, ctypes.c_void_p]
user32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
user32.RegisterWindowMessageW.restype = wt.UINT
user32.GetIconInfo.argtypes = [wt.HANDLE, ctypes.POINTER(ICONINFO)]
user32.AllowSetForegroundWindow.argtypes = [wt.DWORD]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
gdi32.GetObjectW.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p]
gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]

TASKBAR_CREATED = user32.RegisterWindowMessageW("TaskbarCreated")  # "add your icons again", from a new taskbar
# What Shell_NotifyIcon sends the taskbar (WM_COPYDATA, dwData 1): a signature, the NIM_ message, then
# NOTIFYICONDATAW with 32-bit handles, the same for 32- and 64-bit apps
TRAY_DATA = struct.Struct("<II 6I 256s 2I 512s I 128s I 16s I")
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_STATE, NIF_INFO, NIF_GUID = 0x1, 0x2, 0x4, 0x8, 0x10, 0x20


def parse_tray(data):
    """(NIM_ message, the icon's fields) from the data of a tray WM_COPYDATA, or None if it isn't one."""
    data = bytes(data[:TRAY_DATA.size]).ljust(TRAY_DATA.size, b"\0")  # older apps send less
    (sig, nim, _, hwnd, uid, flags, callback, hicon, tip, state, state_mask, info, version, info_title, _,
     guid, _) = TRAY_DATA.unpack(data)
    if sig != 0x34753423:
        return None

    def wstr(b):
        return b.decode("utf-16-le", "replace").split("\0", 1)[0]
    # a GUID as the registry writes it (NotifyIconSettings' IconGuid)
    key = f"{{{str(uuid.UUID(bytes_le=guid)).upper()}}}" if flags & NIF_GUID else f"{hwnd}:{uid}"
    return nim, {"key": key, "hwnd": hwnd, "uid": uid,
                 "flags": flags, "callback": callback, "hicon": hicon, "tip": wstr(tip), "state": state,
                 "state_mask": state_mask, "info": wstr(info), "version": version, "info_title": wstr(info_title)}


def png(w, h, rgba):
    """RGBA bytes as a PNG file: GNOME's St.Icon takes encoded images only."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\0" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))  # filter 0 on each row
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def icon_png(hicon):
    """An icon as PNG, or None. Right away, while the handle is good: the app may destroy the icon as
    soon as it has handed it over.
    ponytail: black-and-white icons (no color bitmap) are skipped; DrawIconEx is the upgrade path."""
    info = ICONINFO()
    if not hicon or not user32.GetIconInfo(hicon, ctypes.byref(info)):
        return None
    try:
        bm = BITMAP()
        if not info.hbmColor or not gdi32.GetObjectW(info.hbmColor, ctypes.sizeof(bm), ctypes.byref(bm)):
            return None
        w, h = bm.bmWidth, bm.bmHeight
        hdc, planes = user32.GetDC(None), []
        for hbm in (info.hbmColor, info.hbmMask):
            bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
            buf = ctypes.create_string_buffer(w * h * 4)
            gdi32.GetDIBits(hdc, hbm, 0, h, buf, ctypes.byref(bmi), 0)  # DIB_RGB_COLORS
            planes.append(np.frombuffer(buf.raw, np.uint8).reshape(h, w, 4))
        user32.ReleaseDC(None, hdc)
    finally:
        gdi32.DeleteObject(info.hbmColor)
        gdi32.DeleteObject(info.hbmMask)
    color, mask = planes
    rgba = color[..., [2, 1, 0, 3]].copy()  # BGRA -> RGBA
    if not rgba[..., 3].any():  # an icon without alpha: its mask (white) says what's transparent
        rgba[..., 3] = np.where(mask[..., 0] == 0, 255, 0)
    return png(w, h, rgba.tobytes())


def callback_params(version, uid, event, x, y):
    """wParam, lParam of a tray icon's callback message as Explorer sends them: for NOTIFYICON_VERSION_4 the
    point to show a menu at and the event with the icon's id, before that the id and the event."""
    if version >= 4:
        return lparam_xy(x, y), ((uid & 0xFFFF) << 16) | event
    return uid, event


def shows_popup(pid):
    """Process pid has a menu or a popup like it on screen: a visible top-level window that isn't mirrored."""
    found = []

    def check(h, _):
        p, r = wt.DWORD(), wt.RECT()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if (p.value == pid and user32.IsWindowVisible(h) and user32.GetWindowRect(h, ctypes.byref(r))
                and r.right > r.left and r.bottom > r.top and not mirrorable(h)):
            found.append(h)
        return not found
    user32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, H, wt.LPARAM)(check), 0)
    return bool(found)


def give_back_focus(pid):
    """A click on a tray icon lets its app take the foreground, for its menu. Once the menu is closed without
    opening a window, the app's hidden window keeps the foreground and keys reach nothing: hand it back to
    GNOME (or the window shown natively in it). New windows sort themselves out (keep_behind_gnome).
    ponytail: polls for up to a minute; a WinEvent hook on foreground changes is the upgrade path."""
    for i in range(240):
        time.sleep(0.25)
        gnome, fg = gnome_window(), user32.GetForegroundWindow()
        if not gnome:
            return
        if fg in (gnome, overlay.hwnd):
            if i >= 8:  # back (or the app never took it)
                return
        elif not user32.IsWindowVisible(fg) and not shows_popup(pid):
            focus(overlay.hwnd or gnome)
            return


def icon_name(icon):
    """A tray icon's name: its tooltip's first line (the rest is often a status), else its program's."""
    return icon["tip"].split("\n")[0].strip() or os.path.splitext(icon["exe"])[0]


def pinned_icons():
    """The tray icons Windows shows on its taskbar rather than in its overflow (Settings → Personalization →
    Taskbar → Other system tray icons): their GUIDs, and (exe name, id) for those without one."""
    root, found = r"Control Panel\NotifyIconSettings", set()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root) as k:
            names = [winreg.EnumKey(k, i) for i in range(winreg.QueryInfoKey(k)[0])]
    except OSError:
        return found
    for name in names:
        key = rf"{root}\{name}"
        if hkcu(key, "IsPromoted") == 1:
            # the path may start with a known folder's GUID ({7C5A40EF-...}\Steam\steam.exe): the name is enough
            exe = hkcu(key, "ExecutablePath", "").rsplit("\\", 1)[-1].lower()
            found.add(hkcu(key, "IconGuid", "").upper() or (exe, hkcu(key, "UID")))
    return found


class Tray:
    """Windows' notification-area icons in GNOME's top bar (the extension draws them): those Windows pins to
    its taskbar as buttons, the others in a menu, as Windows' overflow.

    Apps hand their icons to the "Shell_TrayWnd" window FindWindow finds first, the top one in z-order: a
    hidden one of ours in front of Explorer's gets them all, and passes each on to Explorer unchanged, so
    Windows' own tray stays as it was, also once we're gone. A click on a button in GNOME becomes what
    Explorer would send the app, which then opens its own menu (right under the button: GNOME covers the
    whole screen) or a window.
    ponytail: Shell_NotifyIconGetRect still answers with Explorer's hidden icon, so popups that place
    themselves next to their icon open at the bottom right; answering with the GNOME button is the upgrade path."""

    def __init__(self, shell):
        self.shell, self.hwnd, self.conn = shell, None, None
        self.icons = {}  # key -> {hwnd, exe, uid, callback, version, hidden, png, tip}
        self.sent = {}  # key -> (png, name, pinned) GNOME shows now
        self.pinned = set()  # see pinned_icons()
        self.lock = threading.RLock()  # the window's thread, GNOME's clicks and the main loop
        self.left = (None, 0.0)  # (key, time) of the last left press, for double clicks
        self.fronted = 0.0
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        self.proc = WNDPROC(self.wndproc)  # kept for as long as the window lives
        inst = kernel32.GetModuleHandleW(None)
        user32.RegisterClassW(ctypes.byref(WNDCLASSW(lpfnWndProc=self.proc, hInstance=inst,
                                                     lpszClassName="Shell_TrayWnd")))
        # hidden, but not message-only: FindWindow skips those. WS_EX_TOOLWINDOW | WS_EX_TOPMOST, WS_POPUP
        self.hwnd = user32.CreateWindowExW(0x88, "Shell_TrayWnd", None, 0x80000000, 0, 0, 0, 0, None, None, inst, None)
        user32.ChangeWindowMessageFilterEx(self.hwnd, 0x4A, 1, None)  # WM_COPYDATA from lower integrity too, as Explorer
        self.to_front()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.DispatchMessageW(ctypes.byref(msg))

    def to_front(self):
        """In front of Explorer's tray window, then every app adds its icons again, as for a new taskbar: to us.
        ponytail: what changed while Explorer's was in front reaches us only this way, at most every 5 s, and
        an app that ignores TaskbarCreated shows up with its next change."""
        self.fronted = time.monotonic()
        user32.SetWindowPos(self.hwnd, H(-1), 0, 0, 0, 0, 0x1 | 0x2 | 0x10)  # HWND_TOPMOST, NOSIZE | NOMOVE | NOACTIVATE
        if user32.FindWindowExW(None, None, "Shell_TrayWnd", None) == self.hwnd:
            user32.PostMessageW(H(0xFFFF), TASKBAR_CREATED, 0, 0)  # HWND_BROADCAST

    def wndproc(self, hwnd, msg, wparam, lparam):
        if msg != 0x4A:  # WM_COPYDATA; the rest is the window's own, not Explorer's business (WM_DESTROY...)
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        # Explorer's by name: another Shell_TrayWnd (a second winbridge.py) would pass it back to us
        explorer, res = None, ctypes.c_ssize_t(0)
        while (explorer := user32.FindWindowExW(None, explorer, "Shell_TrayWnd", None)) and process_name(explorer) != "explorer.exe":
            pass
        if explorer:
            user32.SendMessageTimeoutW(explorer, 0x4A, wparam, lparam, 0x2, 1000, ctypes.byref(res))  # SMTO_ABORTIFHUNG
        cds = COPYDATASTRUCT.from_address(lparam)
        parsed = cds.dwData == 1 and cds.lpData and parse_tray(ctypes.string_at(cds.lpData, cds.cbData))
        if not parsed:  # app bars, icon positions: Explorer answers
            return res.value
        self.update(*parsed)
        return 1

    def update(self, nim, n):
        key = n["key"]
        with self.lock:
            if nim == 2:  # NIM_DELETE
                self.icons.pop(key, None)
            elif nim == 4:  # NIM_SETVERSION
                if key in self.icons:
                    self.icons[key]["version"] = n["version"]
            elif nim in (0, 1):  # NIM_ADD, NIM_MODIFY (also of one added before we were here)
                icon = self.icons.setdefault(key, {"hwnd": 0, "exe": "", "uid": 0, "callback": 0, "version": 0,
                                                   "hidden": False, "png": None, "tip": ""})
                if n["hwnd"] and n["hwnd"] != icon["hwnd"]:
                    icon["hwnd"], icon["uid"] = n["hwnd"], n["uid"]
                    icon["exe"] = os.path.basename(process_info(n["hwnd"])[0])
                if n["flags"] & NIF_MESSAGE:
                    icon["callback"] = n["callback"]
                if n["flags"] & NIF_ICON:
                    icon["png"] = icon_png(n["hicon"])
                if n["flags"] & NIF_TIP:
                    icon["tip"] = n["tip"]
                if n["flags"] & NIF_STATE and n["state_mask"] & 1:  # NIS_HIDDEN
                    icon["hidden"] = bool(n["state"] & 1)
                if n["flags"] & NIF_INFO and n["info"]:  # a balloon
                    self.shell(tray=key, title=n["info_title"] or icon_name(icon), text=n["info"])
            self.publish(key)

    def publish(self, key):
        """Tell GNOME about this icon if that changed: its picture, name and whether Windows pins it to the
        taskbar, or that it's gone. Not Explorer's own (battery, volume, network): GNOME shows those itself."""
        icon = self.icons.get(key)
        now = None
        if icon and icon["png"] and not icon["hidden"] and icon["exe"] != "explorer.exe":
            now = (icon["png"], icon_name(icon), key in self.pinned or (icon["exe"], icon["uid"]) in self.pinned)
        if now == self.sent.get(key):
            return
        if now:
            self.sent[key] = now
            self.shell(tray=key, icon=base64.b64encode(now[0]).decode(), tip=now[1], pinned=now[2])
        else:
            del self.sent[key]
            self.shell(tray=key)

    def tick(self):
        """From the main loop: stay in front of Explorer's tray window, drop the icons of apps that died, follow
        what Windows pins, and show them all to a GNOME that (re)connected."""
        if not self.hwnd:
            return
        if (user32.FindWindowExW(None, None, "Shell_TrayWnd", None) != self.hwnd
                and time.monotonic() - self.fronted > 5):
            print("tray: another tray window came to the front, back in front of it")
            self.to_front()
        pinned = pinned_icons()
        with self.lock:
            self.pinned = pinned
            if self.shell.sock is not self.conn:
                self.conn, self.sent = self.shell.sock, {}
            for key, icon in list(self.icons.items()):
                if not user32.IsWindow(icon["hwnd"]):
                    del self.icons[key]
            for key in self.icons.keys() | self.sent.keys():
                self.publish(key)

    def click(self, m):
        """A press or release on an icon's button in GNOME: what Explorer's tray would send its app."""
        key, b, down = m["tray"], m["b"], m["down"]
        with self.lock:
            icon = dict(self.icons.get(key) or {})
        if not icon.get("callback") or b not in (1, 2, 3):
            return
        if down:
            events = [{1: 0x201, 2: 0x207, 3: 0x204}[b]]  # WM_LBUTTONDOWN, WM_MBUTTONDOWN, WM_RBUTTONDOWN
            if b == 1:
                last, self.left = self.left, (key, time.monotonic())
                if last[0] == key and self.left[1] - last[1] < user32.GetDoubleClickTime() / 1000:
                    events, self.left = [0x203], (None, 0.0)  # WM_LBUTTONDBLCLK
        else:
            # the app opens its menu or a window now, which takes the foreground: lift Windows' lock on it
            # (focus()'s Alt tap) and hand the app the right
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(icon["hwnd"], ctypes.byref(pid))
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 2, 0)
            user32.AllowSetForegroundWindow(pid.value)
            # WM_xBUTTONUP, from version 3 on followed by NIN_SELECT / WM_CONTEXTMENU, where most apps act
            events = {1: [0x202, 0x400], 2: [0x208], 3: [0x205, 0x7B]}[b][:None if icon["version"] >= 3 else 1]
            threading.Thread(target=give_back_focus, args=(pid.value,), daemon=True).start()
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))  # on the button: GNOME covers the whole screen
        for event in events:
            user32.PostMessageW(icon["hwnd"], icon["callback"],
                                *callback_params(icon["version"], icon["uid"], event, pt.x, pt.y))


def message(kind, payload):
    return kind + struct.pack("<I", len(payload)) + payload


BAND = 16  # rows per comparison band


def changed_rects(cur, prev):
    """Rectangles (y, rows, x, cols) where pixel array cur differs from prev: one per changed band of
    BAND rows, narrowed to its changed columns, adjacent bands merged. Everything if prev doesn't fit."""
    h, w = cur.shape
    if prev is None or prev.shape != cur.shape:
        return [(0, h, 0, w)]
    neq = cur != prev
    rows = np.flatnonzero(neq.any(axis=1))
    if not rows.size:
        return []
    rects = []
    for y in range(int(rows[0]) // BAND * BAND, int(rows[-1]) + 1, BAND):
        cols = np.flatnonzero(neq[y:y + BAND].any(axis=0))
        if not cols.size:
            continue
        r, x0, x1 = min(BAND, h - y), int(cols[0]), int(cols[-1]) + 1
        if rects and rects[-1][0] + rects[-1][1] == y:  # touches the previous band: grow it
            py, pr, px, pc = rects[-1]
            x0, x1 = min(x0, px), max(x1, px + pc)
            rects[-1] = (py, pr + r, x0, x1 - x0)
        else:
            rects.append((y, r, x0, x1 - x0))
    return rects


# bytes of changed pixels. zlib runs ~150 MB/s, the socket ~700 MB/s: compressing a 1 MB patch cost
# ~7 ms on each end against ~1.5 ms to send it raw. ponytail: latency knob, raise it if bandwidth bites
COMPRESS_BELOW = 1 << 16


def frame_patch(cur, prev):
    """PTCH (zlib) / PTCR (raw) message with only the changed rectangles of cur, as a list of buffers
    to send in order, and prev updated to match cur. cur may be a view into the capture buffer:
    nothing is copied but the changed pixels, and the buffers stay valid after it is reused. Big
    changes (video, scrolling) go raw: zlib took ~130 ms per 20 MB frame on both ends, while the WSL
    socket moves ~700 MB/s. They are not joined into one message: that took 3-4 more 20 MB copies."""
    rects = changed_rects(cur, prev)
    if not rects:
        return None, prev
    if prev is None or prev.shape != cur.shape:
        prev = np.empty_like(cur)
    h, w = cur.shape
    head = struct.pack("<III", w, h, len(rects)) + b"".join(struct.pack("<IIII", *r) for r in rects)
    pixels = []
    for y, r, x, c in rects:
        part = cur[y:y + r, x:x + c].copy()  # sent later by another thread, when cur's buffer is reused
        prev[y:y + r, x:x + c] = part
        pixels.append(part)
    size = sum(p.nbytes for p in pixels)
    if size < COMPRESS_BELOW:
        return [message(b"PTCH", head + zlib.compress(b"".join(pixels), 1))], prev
    return [b"PTCR" + struct.pack("<I", len(head) + size) + head, *pixels], prev


# Capture interval for every window. A lower rate for unfocused ones cost more than it saved: switching
# meant restarting WGC, ~150 ms without frames on every focus change, and scrolling a window under
# the pointer without focusing it ran at that low rate. WGC counts the interval from when the last
# frame was handled and frames come on vsync, so 33 ms gave ~18 fps: 8 lets a frame through on
# the first vsync after its handling, at 120 Hz too.
FRAME_MS = 8


def pixels_of(frame):
    """(w, h, bgra bytes) from capture() as an (h, w) uint32 array, like the WGC frames."""
    w, h, data = frame
    return np.frombuffer(data, np.uint32).reshape(h, w)


class Session(threading.Thread):
    """One mirrored window: frames and title out, input in, until the window or the viewer goes away."""

    def __init__(self, hwnd, port):
        super().__init__(daemon=True)
        self.hwnd, self.port = hwnd, port
        self.stopped = threading.Event()
        self.wake = threading.Event()
        self.input_at = 0.0
        self.prev = None  # last frame sent, (h, w) uint32
        self.changed_at = 0.0
        self.push_lock = threading.Lock()
        self.outbox = queue.Queue(maxsize=1)  # changes on their way to the viewer

    def push(self, pixels):
        """Queue what changed since the last frame for the sender thread. Runs on the WGC capture thread
        (or the polling loop), which can diff the next frame while this one is still being sent."""
        with self.push_lock:
            parts, self.prev = frame_patch(pixels, self.prev)
            if parts:
                self.changed_at = time.monotonic()
                try:
                    self.outbox.put(parts, timeout=1)  # waits while the frame before is still being sent
                except queue.Full:  # viewer stuck: this change is lost, so the next frame is sent whole
                    self.prev = None

    def send_frames(self, send):
        while (parts := self.outbox.get()) is not None:
            try:
                send(*parts)
            except OSError:
                self.wake.set()  # viewer gone: let the session loop notice and end
                return

    def poke(self):
        """Input reached the window: capture right away and at full rate for a while."""
        self.input_at = time.monotonic()
        self.wake.set()

    def run(self):
        hwnd = self.hwnd
        try:
            sock = socket.create_connection(("localhost", self.port))
        except OSError:
            return  # retried every half second while GNOME is (re)starting: touch nothing before this
        # Nagle held back small input replies and each frame's last segment until an ACK (tens of ms)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        keep_behind_gnome(hwnd)
        title = text(user32.GetWindowTextW, hwnd)
        w, h = client_size(hwnd)
        app = app_for(hwnd, title)
        own_titlebar = draws_own_titlebar(hwnd)
        hello = {"title": title, "w": w, "h": h, "app": app, "own_titlebar": own_titlebar, "hwnd": hwnd}
        sock.sendall((json.dumps(hello) + "\n").encode())
        print(f"+ {title}  [{app or 'no app match'}]{'  (own title bar)' if own_titlebar else ''}")
        lock = threading.Lock()  # frames (this thread) and hit replies (reader thread) share the socket

        def send(*parts):
            with lock:
                for part in parts:
                    sock.sendall(part)

        threading.Thread(target=self.send_frames, args=(send,), daemon=True).start()
        inp = Input(hwnd, own_titlebar, lambda hit: send(message(b"HITT", struct.pack("<i", hit))), self.poke)
        reader = threading.Thread(target=self.read_events, args=(sock, inp), daemon=True)
        reader.start()
        first = capture(hwnd)  # a picture right away; WGC then only reports changes
        if first:
            self.push(pixels_of(first))
        wgc = start_wgc(hwnd, self.push, FRAME_MS)
        try:
            while not self.stopped.is_set() and reader.is_alive() and user32.IsWindow(hwnd):
                new_title = text(user32.GetWindowTextW, hwnd)
                if new_title != title:
                    title = new_title
                    send(message(b"TITL", title.encode()))
                if hwnd == overlay.want.get("o") and hwnd != overlay.hwnd:
                    overlay.update()  # e.g. no longer minimized, or resized by the app: may fit now
                if wgc:
                    # frames arrive by themselves; check the title twice a second
                    self.wake.wait(0.5)
                    self.wake.clear()
                else:
                    busy = time.monotonic() - max(self.changed_at, self.input_at) < 2
                    # idle windows are polled slowly; input wakes the loop at once (no 200 ms click lag)
                    self.wake.wait(1 / 30 if busy else 1 / 5)
                    self.wake.clear()
                    frame = capture(hwnd)
                    if frame:
                        self.push(pixels_of(frame))
        except OSError:
            pass
        if wgc:
            wgc.stop()
        try:
            # close() alone waits for the reader thread's makefile() to let go, so the GNOME window
            # would stay (showing the closing app's last, black frame); shutdown() ends it now
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        try:
            self.outbox.put_nowait(None)  # sender idle: let it end (a busy one fails on the closed socket)
        except queue.Full:
            pass
        overlay.areas.pop(hwnd, None)
        print(f"- {title}")

    def read_events(self, sock, inp):
        try:
            for line in sock.makefile("r", encoding="utf-8"):
                try:
                    inp.handle(json.loads(line))
                except (KeyError, ValueError) as e:
                    print("bad event:", line.strip(), e)
        except OSError:
            pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 47800
    sessions = {}
    # the log is cp1252 when redirected: a window title with U+200E crashed its session on every retry
    sys.stdout.reconfigure(errors="replace")
    no_power_throttling()
    # Windows waits for the low-level hooks on every key and mouse event, and they need the GIL that
    # the capture threads hold: hand it over within 1 ms, not the default 5
    sys.setswitchinterval(0.001)
    print(f"mirroring app windows -> localhost:{port}")
    shell = GnomeShell(port + 1, lambda m: tray.click(m) if "tray" in m else overlay.update(m))
    tray = Tray(shell)
    swipes =Swipes(lambda phase, fingers, dx, dy: shell(p=phase, n=fingers, dx=dx, dy=dy))
    threading.Thread(target=touchpad_loop, args=(swipes.frame,), daemon=True).start()
    threading.Thread(target=hook_loop, args=(13, GnomeKeys(shell).hook), daemon=True).start()  # WH_KEYBOARD_LL
    threading.Thread(target=hook_loop, args=(14, overlay.mouse_hook), daemon=True).start()  # WH_MOUSE_LL
    gnome_seen, last_look = time.monotonic(), None
    while True:
        if gnome_window():
            gnome_seen = time.monotonic()
        elif time.monotonic() - gnome_seen > 10:  # (not up yet at start, or) closed
            print("GNOME is gone, exiting")
            return
        wanted = launched_from_gnome()
        if wanted:
            uncloak_launched(wanted)
        current = set(app_windows())
        for hwnd in current - sessions.keys():
            sessions[hwnd] = Session(hwnd, port)
            sessions[hwnd].start()
        for hwnd in list(sessions):
            if hwnd not in current or not sessions[hwnd].is_alive():
                sessions.pop(hwnd).stopped.set()
        look = (appearance(), shell.sock)  # the socket too: sent again after GNOME (re)connects
        if look != last_look and shell.sock:
            shell(**look[0])
            last_look = look
        tray.tick()
        time.sleep(0.5)


if __name__ == "__main__":
    main()
