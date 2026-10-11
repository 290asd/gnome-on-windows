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
front = [1]
winbridge.user32 = types.SimpleNamespace(GetForegroundWindow=lambda: front[0], CallNextHookEx=lambda *a: 0)
keys, superkey = [], winbridge.SuperKey(lambda **m: keys.append(m))
def key(vk, down, injected=False):
    k = winbridge.KBDLLHOOKSTRUCT(vk, 0, 0x10 if injected else 0, 0, 0)
    return superkey.hook(0, 0x100 if down else 0x101, winbridge.ctypes.addressof(k))
assert [key(0x5B, True), key(0x5B, True), key(0x41, True), key(0x41, False), key(0x5B, False)] == [1, 1, 0, 0, 1]
assert keys == [{"k": 125, "down": True}, {"k": 125, "down": False}], keys  # auto-repeat not resent
assert key(0x5C, True, injected=True) == 0 and keys[2:] == []             # our own injected keys pass
front[0] = 2                                                                # Windows app in front
assert [key(0x5B, True), key(0x5B, False)] == [0, 0] and keys[2:] == []
front[0] = 1
key(0x5B, True); front[0] = 2                                               # front changes while held
assert key(0x5B, False) == 1 and keys[-1] == {"k": 125, "down": False}
print("ok")
