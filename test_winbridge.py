"""Checks the frame-diff protocol: patches applied the way gnomebridge.py does must rebuild the frame.

    py test_winbridge.py
"""
import struct
import zlib

import numpy as np

import winbridge
from winbridge import BAND, changed_rects


def frame_patch(cur, prev):
    """winbridge.frame_patch with its buffers joined, i.e. the bytes that go over the socket."""
    parts, prev = winbridge.frame_patch(cur, prev)
    return parts and b"".join(parts), prev


def apply(buf, msg):
    """gnomebridge.py's PTCH handling (Mirror.apply_patch / apply_rects), minus GTK."""
    kind, payload = msg[:4], msg[8:]
    w, h, n = struct.unpack_from("<III", payload)
    rects = [struct.unpack_from("<IIII", payload, 12 + 16 * i) for i in range(n)]
    pixels, stride, off = payload[12 + 16 * n:], w * 4, 0
    if kind == b"PTCH":
        pixels = zlib.decompress(pixels)
    buf = bytearray(buf) if buf is not None and len(buf) == w * h * 4 else bytearray(w * h * 4)
    for y, r, x, c in rects:
        for row in range(y, y + r):
            p = row * stride + x * 4
            buf[p:p + c * 4] = pixels[off:off + c * 4]
            off += c * 4
    return bytes(buf)


rng = np.random.default_rng(1)
h, w = 50, 40  # height not a multiple of BAND, to hit the short last band
a = rng.integers(0, 2**32, (h, w), dtype=np.uint32)

assert changed_rects(a, None) == [(0, h, 0, w)]
assert changed_rects(a, a.copy()) == []

b = a.copy()
b[BAND + 1, 5] ^= 1          # band 16..31, column 5
b[2 * BAND, 30] ^= 1         # band 32..47 touches it: merged, columns 5..30
b[h - 1, 7] ^= 1             # short last band 48..49 touches too
assert changed_rects(b, a) == [(BAND, h - BAND, 5, 26)], changed_rects(b, a)

c = a.copy()
c[0, 0] ^= 1                 # band 0..15, column 0
c[3 * BAND, 39] ^= 1         # band 48..49, not adjacent, last column
assert changed_rects(c, a) == [(0, BAND, 0, 1), (3 * BAND, h - 3 * BAND, 39, 1)]

# end to end, including prev being kept in sync and a resize
msg, prev = frame_patch(a, None)
screen = apply(None, msg)
for nxt in (b, c, a):
    msg, prev = frame_patch(nxt, prev)
    screen = apply(screen, msg)
    assert screen == nxt.tobytes() and (prev == nxt).all()
assert frame_patch(a, prev) == (None, prev)  # nothing changed: nothing sent
big = rng.integers(0, 2**32, (h + 3, w + 1), dtype=np.uint32)
msg, prev = frame_patch(big, prev)
assert msg[:4] == b"PTCH" and apply(screen, msg) == big.tobytes()

# big changes go uncompressed
huge = rng.integers(0, 2**32, (700, 500), dtype=np.uint32)  # 1.4 MB
msg, prev = frame_patch(huge, None)
assert msg[:4] == b"PTCR" and apply(None, msg) == huge.tobytes()

# tray: what Shell_NotifyIcon sends the taskbar -> what GNOME is told; Explorer's callback parameters; PNGs
import base64, ctypes, threading
def tray_data(nim, flags, hicon=0, tip="", state=0, info="", version=0, guid=b""):
    return winbridge.TRAY_DATA.pack(0x34753423, nim, 956, 0x1234, 7, flags, 0x8001, hicon,
                                    tip.encode("utf-16-le"), state, 1, info.encode("utf-16-le"),
                                    version, b"", 0, guid, 0)
nim, n = winbridge.parse_tray(tray_data(1, winbridge.NIF_TIP, tip="Hei 😀", version=4))
assert nim == 1 and n["key"] == f"{0x1234}:7" and n["tip"] == "Hei 😀" and n["version"] == 4, n
assert winbridge.parse_tray(tray_data(0, 0x4, tip="vanha")[:160])[1]["tip"] == "vanha"  # NOTIFYICONDATA V1
guid = bytes.fromhex("3d429e69da73ba42b94e881ff8672467")                                  # Discord's, as sent
assert winbridge.parse_tray(tray_data(0, 0x20, guid=guid))[1]["key"] == "{699E423D-73DA-42BA-B94E-881FF8672467}"
assert winbridge.parse_tray(b"\0" * 40) is None
tray_sent, real_icon_png = [], winbridge.icon_png
winbridge.icon_png = lambda h: b"png%d" % h
tray = winbridge.Tray.__new__(winbridge.Tray)  # no window, no thread
tray.shell, tray.icons, tray.sent, tray.lock = lambda **m: tray_sent.append(m), {}, {}, threading.RLock()
tray.pinned = set()
key, b64 = f"{0x1234}:7", base64.b64encode(b"png5").decode()
shown = {"tray": key, "icon": b64, "tip": "Sync", "pinned": False}
tray.update(*winbridge.parse_tray(tray_data(0, 0x7, hicon=5, tip="Sync\r\n2 files")))      # added: name, unpinned
tray.update(*winbridge.parse_tray(tray_data(1, 0x2 | 0x4, hicon=5, tip="Sync\r\n2 files")))  # same again
tray.pinned = {("", 7)}                                                                    # (exe of 0x1234: none)
tray.publish(key)                                                                          # pinned in Windows
tray.update(*winbridge.parse_tray(tray_data(1, 0x10, info="Valmis")))                      # balloon
tray.update(*winbridge.parse_tray(tray_data(1, 0x8, state=1)))                             # hidden
tray.update(*winbridge.parse_tray(tray_data(1, 0x8, state=0)))                             # shown again
tray.icons[key]["exe"] = "explorer.exe"                                                    # Explorer's own
tray.publish(key)
tray.update(*winbridge.parse_tray(tray_data(2, 0)))                                        # deleted
assert tray_sent == [shown, {**shown, "pinned": True}, {"tray": key, "title": "Sync", "text": "Valmis"},
                     {"tray": key}, {**shown, "pinned": True}, {"tray": key}], tray_sent
assert tray.icons == {} and tray.sent == {}
winbridge.icon_png = real_icon_png
assert winbridge.callback_params(4, 7, 0x7B, 10, 20) == (20 << 16 | 10, 7 << 16 | 0x7B)    # WM_CONTEXTMENU
assert winbridge.callback_params(3, 7, 0x7B, 10, 20) == (7, 0x7B)
p = winbridge.png(3, 2, bytes(range(24)))
idat = struct.unpack(">I", p[33:37])[0]
assert p[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", p[16:24]) == (3, 2)
assert zlib.crc32(p[12:29]) == struct.unpack(">I", p[29:33])[0]
assert zlib.decompress(p[41:41 + idat]) == b"\0" + bytes(range(12)) + b"\0" + bytes(range(12, 24))
winbridge.user32.LoadIconW.restype = ctypes.c_void_p
app_icon = winbridge.icon_png(winbridge.user32.LoadIconW(None, ctypes.c_void_p(32512)))   # IDI_APPLICATION
assert app_icon[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", app_icon[16:24])[0] >= 16

# touchpad frames -> swipe begin/update/end, as GNOME's swipe handling expects them
import types
winbridge.gnome_window = lambda: 1
winbridge.user32 = types.SimpleNamespace(GetForegroundWindow=lambda: 1)  # GNOME in front
sent = []
swipes = winbridge.Swipes(lambda *ev: sent.append(ev) or True)
K = winbridge.GESTURE_UNITS_PER_MM
swipes.frame({1: (10, 10), 2: (20, 10)})                                  # two fingers: scrolling, not ours
swipes.frame({1: (10, 10), 2: (20, 10), 3: (30, 10)})                     # third finger lands
swipes.frame({1: (11, 8), 2: (21, 8), 3: (31, 8)})                        # all move 1 mm right, 2 mm up
swipes.frame({1: (11, 8), 2: (21, 8), 3: (31, 8), 4: (40, 8)})            # fourth finger: a new swipe
swipes.frame({1: (11, 8)})                                                # lifted
assert sent == [("begin", 3, 0.0, 0.0), ("update", 3, K, -2 * K), ("end", 3, 0.0, 0.0),
                ("begin", 4, 0.0, 0.0), ("end", 4, 0.0, 0.0)], sent

# Windows key: GNOME's Super while GNOME is in front, untouched otherwise
front, pressed = [1], set()
winbridge.user32 = types.SimpleNamespace(GetForegroundWindow=lambda: front[0], CallNextHookEx=lambda *a: 0,
                                         GetAsyncKeyState=lambda vk: -0x8000 if vk in pressed else 0)
class Shell(list):  # GnomeShell: messages, and "focus" where the keyboard moves to GNOME first
    def __call__(self, **m): self.append(m)
    def do(self, fn): self.append("focus")
keys = Shell()
gnomekeys = winbridge.GnomeKeys(keys)
def key(vk, down, injected=False):
    if not injected:
        (pressed.add if down else pressed.discard)(vk)
    k = winbridge.KBDLLHOOKSTRUCT(vk, 0, 0x10 if injected else 0, 0, 0)
    return gnomekeys.hook(0, 0x100 if down else 0x101, winbridge.ctypes.addressof(k))
assert [key(0x5B, True), key(0x5B, True), key(0x41, True), key(0x41, False), key(0x5B, False)] == [1, 1, 0, 0, 1]
assert keys == [{"k": 125, "down": True}, {"k": 125, "down": False}], keys  # auto-repeat not resent
assert key(0x5C, True, injected=True) == 0 and keys[2:] == []             # our own injected keys pass
front[0] = 2                                                                # Windows app in front
assert [key(0x5B, True), key(0x5B, False)] == [0, 0] and keys[2:] == []
front[0] = 1
key(0x5B, True); front[0] = 2                                               # front changes while held
assert key(0x5B, False) == 1 and keys[-1] == {"k": 125, "down": False}

# Alt+Tab, Ctrl+Alt+arrows on the native window: to GNOME, modifiers released on both sides
def tap(k): return [{"k": k, "down": True}, {"k": k, "down": False}]
winbridge.overlay.hwnd = front[0] = 2                                       # native window in front
del keys[:]
assert [key(0xA4, True), key(0x09, True), key(0x09, False), key(0x09, True), key(0x09, False)] == [0, 1, 1, 1, 1]
assert key(0xA4, False) == 0                                                # Windows saw Alt go down
assert keys == ["focus", {"k": 56, "down": True}, *tap(15), *tap(15), {"k": 56, "down": False}], keys
del keys[:]
assert [key(0xA2, True), key(0xA4, True), key(0x27, True), key(0x27, False), key(0xA4, False), key(0xA2, False)] == [0, 0, 1, 1, 0, 0]
assert keys == ["focus", {"k": 29, "down": True}, {"k": 56, "down": True}, *tap(106),
                {"k": 56, "down": False}, {"k": 29, "down": False}], keys
del keys[:]
assert [key(0x09, True), key(0x09, False)] == [0, 0]                        # Tab alone
assert [key(0xA2, True), key(0xA5, True), key(0x25, True), key(0x25, False), key(0xA5, False), key(0xA2, False)] == [0] * 6  # AltGr+Left
front[0] = 1                                                                # GNOME in front: RDP carries it
assert [key(0xA4, True), key(0x09, True), key(0x09, False), key(0xA4, False)] == [0] * 4
assert keys == [], keys

# Windows' look for GNOME: dark mode, wallpaper as a WSL path with a version that follows the file
import os, tempfile
reg = {}
winbridge.hkcu = lambda key, name, default=None: reg.get(name, default)
def spi(action, n, buf, flags): buf.value = reg.get("Wallpaper", "")  # SPI_GETDESKWALLPAPER
winbridge.user32 = types.SimpleNamespace(SystemParametersInfoW=spi)
assert winbridge.appearance() == {"dark": False}                            # defaults: light, no wallpaper
with tempfile.TemporaryDirectory() as d:
    pic, themes = os.path.join(d, "uusi kuva.jpg"), os.path.join(d, r"Microsoft\Windows\Themes")
    os.makedirs(themes)
    open(pic, "wb").write(b"1")
    open(os.path.join(themes, "TranscodedWallpaper"), "wb").write(b"2")
    os.environ["APPDATA"] = d
    reg.update(AppsUseLightTheme=0, Wallpaper=pic)
    look = winbridge.appearance()
    assert look["dark"] and look["wallpaper"] == f"/mnt/{d[0].lower()}{d[2:]}/uusi kuva.jpg".replace("\\", "/"), look
    open(pic, "wb").write(b"12")                                            # rewritten in place
    assert winbridge.appearance()["v"] != look["v"]
    os.remove(pic)                                                          # original gone: Windows' copy
    assert winbridge.appearance()["wallpaper"].endswith("/Microsoft/Windows/Themes/TranscodedWallpaper")
    reg["Wallpaper"] = ""                                                   # solid color
    assert winbridge.appearance() == {"dark": True}

# GNOME gone: windows are left alone (any user32 call would raise here)
winbridge.gnome_window, winbridge.user32 = lambda: None, types.SimpleNamespace()
winbridge.keep_behind_gnome(5)
print("ok")
