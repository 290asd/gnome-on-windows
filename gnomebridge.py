#!/usr/bin/env python3
"""GNOME side of the bridge: shows each mirrored Windows window as a normal GTK window in GNOME.

winbridge.py (Windows) opens one connection per app window; this draws the frames it sends and
sends mouse/keyboard/close back. The GNOME window lives as long as the Windows window does.

    GDK_BACKEND=wayland WAYLAND_DISPLAY=gnome-nested python3 gnomebridge.py [port]
"""
import ctypes
import json
import socket
import struct
import sys
import threading
import time
import zlib

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
import cairo
from gi.repository import Gdk, GLib, Gtk

# GDK key name -> Windows virtual-key code, for keys that aren't plain text
VK = {"Return": 0x0D, "KP_Enter": 0x0D, "BackSpace": 0x08, "Tab": 0x09, "Escape": 0x1B, "Delete": 0x2E,
      "Insert": 0x2D, "Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28, "Home": 0x24, "End": 0x23,
      "Page_Up": 0x21, "Page_Down": 0x22}


# Per-window Wayland app_id, so GNOME ties each mirror to its Windows app's launcher (icon, name,
# dash). GTK3 only sets one app_id per process and has no Python binding for this, hence ctypes.
_gdk = ctypes.CDLL("libgdk-3.so.0")
_gdk.gdk_wayland_window_set_application_id.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
_gdk.gdk_wayland_window_set_dbus_properties_libgtk_only.argtypes = [ctypes.c_void_p] + [ctypes.c_char_p] * 6
ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
ctypes.pythonapi.PyCapsule_GetPointer.argtypes = [ctypes.py_object, ctypes.c_char_p]


def tag_window(widget, app_id, hwnd):
    ptr = ctypes.pythonapi.PyCapsule_GetPointer(widget.get_window().__gpointer__, None)
    if app_id:
        _gdk.gdk_wayland_window_set_application_id(ptr, app_id.encode())
    # The Windows handle, as GTK's D-Bus window path: the windows-touchpad extension reads it to tell
    # winbridge.py where this mirror is, so it can show the real window there (its Overlay). GNOME Shell
    # then looks for window actions at that path on the given bus name, and crashes if there is none:
    # the bus itself always answers (that it has none)
    _gdk.gdk_wayland_window_set_dbus_properties_libgtk_only(
        ptr, app_id.encode() or None, None, None, f"/hwnd/{hwnd}".encode(), None, b"org.freedesktop.DBus")


def apply_rects(buf, w, rects, pixels):
    """Write PTCH rectangles (y, rows, x, cols) of BGRA pixels into a w-wide frame buffer."""
    stride, off = w * 4, 0
    for y, r, x, c in rects:
        if x == 0 and c == w:  # full-width: one copy
            buf[y * stride:(y + r) * stride] = pixels[off:off + r * stride]
            off += r * stride
            continue
        seg = c * 4
        for row in range(y, y + r):
            p = row * stride + x * 4
            buf[p:p + seg] = pixels[off:off + seg]
            off += seg


NO_HEIGHT = Gtk.CssProvider()
NO_HEIGHT.load_from_data(b"* { min-height: 0; }")  # Adwaita gives title bars 46 px at least

# WM_NCHITTEST resize edges -> GDK edges
EDGES = {10: Gdk.WindowEdge.WEST, 11: Gdk.WindowEdge.EAST, 12: Gdk.WindowEdge.NORTH,
         13: Gdk.WindowEdge.NORTH_WEST, 14: Gdk.WindowEdge.NORTH_EAST, 15: Gdk.WindowEdge.SOUTH,
         16: Gdk.WindowEdge.SOUTH_WEST, 17: Gdk.WindowEdge.SOUTH_EAST}


def recv_exact(conn, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("window closed")
        buf += chunk
    return buf


class Mirror(Gtk.Window):
    def __init__(self, conn, hello):
        super().__init__(title=hello["title"])
        self.conn = conn
        self.frame = None
        self.pending_resize = None
        self.area_size = None  # last size sent as an "area" event
        self.press = None  # last left press, used when Windows answers that it hit the app's title bar
        self.caption_at = 0.0
        if hello.get("own_titlebar"):
            # The app's own top edge acts as the title bar (see on_hit). An empty, zero-height title bar
            # rather than no decorations: GTK's resize borders (and shadow) stay around the window.
            bar = Gtk.Box()
            bar.get_style_context().add_provider(NO_HEIGHT, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.set_titlebar(bar)

        area = Gtk.DrawingArea(can_focus=True)
        area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK
                        | Gdk.EventMask.SMOOTH_SCROLL_MASK | Gdk.EventMask.KEY_PRESS_MASK)
        area.connect("draw", self.on_draw)
        area.connect("button-press-event", lambda w, e: self.on_button(w, e, "down"))
        area.connect("button-release-event", lambda w, e: self.on_button(w, e, "up"))
        area.connect("motion-notify-event", self.on_motion)
        area.connect("scroll-event", self.on_scroll)
        area.connect("key-press-event", self.on_key)
        area.connect("size-allocate", self.on_size)
        self.area = area
        self.add(area)
        # one Windows pixel per screen pixel: sharp, at the cost of not matching Windows' 175 % size
        s = self.get_scale_factor()
        self.set_default_size(max(hello["w"] // s, 50), max(hello["h"] // s, 50))
        # after map: the Wayland toplevel only exists once the window is shown
        self.connect_after("map", lambda w: tag_window(w, hello.get("app", ""), hello.get("hwnd", 0)))
        self.connect("delete-event", self.on_close)
        self.connect("focus-in-event", lambda *_: self.send(t="focus", on=True))
        self.connect("focus-out-event", lambda *_: self.send(t="focus", on=False))
        threading.Thread(target=self.recv_loop, daemon=True).start()

    def send(self, **ev):
        try:
            self.conn.sendall((json.dumps(ev) + "\n").encode())
        except OSError:
            pass

    def px(self, v):
        return int(v * self.get_scale_factor())

    # --- from Windows ---
    def recv_loop(self):
        try:
            while True:
                head = recv_exact(self.conn, 8)
                kind, n = bytes(head[:4]), struct.unpack("<I", head[4:])[0]
                payload = recv_exact(self.conn, n)
                if kind in (b"PTCH", b"PTCR"):  # changed rectangles, zlib-compressed / raw
                    w, h, n = struct.unpack_from("<III", payload)
                    rects = [struct.unpack_from("<IIII", payload, 12 + 16 * i) for i in range(n)]
                    pixels = payload[12 + 16 * n:]
                    if kind == b"PTCH":
                        pixels = zlib.decompress(pixels)
                    # ahead of GTK's redraw (HIGH_IDLE + 20): the default idle priority missed a frame
                    GLib.idle_add(self.apply_patch, w, h, rects, pixels, priority=GLib.PRIORITY_HIGH_IDLE)
                elif kind == b"TITL":
                    GLib.idle_add(self.set_title, payload.decode())
                elif kind == b"HITT":
                    GLib.idle_add(self.on_hit, struct.unpack("<i", payload)[0])
        except (ConnectionError, OSError, zlib.error):
            pass
        GLib.idle_add(self.destroy)

    def apply_patch(self, w, h, rects, pixels):
        """Copy the changed rectangles into the frame and redraw only those areas."""
        if not self.frame or self.frame[:2] != (w, h):
            self.frame = (w, h, bytearray(w * h * 4))
            self.area.queue_draw()
        apply_rects(self.frame[2], w, rects, pixels)
        s = self.get_scale_factor()
        for y, r, x, c in rects:
            self.area.queue_draw_area(x // s, y // s, c // s + 2, r // s + 2)

    def on_draw(self, widget, cr):
        if not self.frame:
            return
        w, h, data = self.frame
        surface = cairo.ImageSurface.create_for_data(data, cairo.FORMAT_RGB24, w, h, w * 4)
        s = self.get_scale_factor()
        cr.scale(1 / s, 1 / s)
        cr.set_source_surface(surface, 0, 0)
        cr.paint()

    # --- to Windows ---
    def on_close(self, *_):
        self.send(t="close")  # the Windows app decides; we disappear when its window does
        return True

    def on_button(self, widget, e, kind):
        if e.type == Gdk.EventType._2BUTTON_PRESS and time.monotonic() - self.caption_at < 1:
            self.toggle_maximize()  # double-click on the app's title bar
            return
        if e.type != Gdk.EventType.BUTTON_PRESS and kind == "down":
            return  # ignore GTK's synthetic double/triple-click events
        widget.grab_focus()
        if kind == "down" and e.button == 1:
            self.press = (e.button, int(e.x_root), int(e.y_root), e.time, time.monotonic())
        self.send(t=kind, b=e.button, x=self.px(e.x), y=self.px(e.y))

    def toggle_maximize(self):
        self.unmaximize() if self.is_maximized() else self.maximize()

    def on_hit(self, hit):
        """Windows says the last press landed on the app's title bar area: let GNOME do the window action."""
        if not self.press or time.monotonic() - self.press[4] > 0.5:
            return
        button, x, y, stamp, _ = self.press
        if hit == 2:  # caption: move (button is still held)
            self.caption_at = time.monotonic()
            self.begin_move_drag(button, x, y, stamp)
        elif hit == 8:
            self.iconify()
        elif hit == 9:
            self.toggle_maximize()
        elif hit == 20:
            self.send(t="close")
        elif hit in EDGES:
            self.begin_resize_drag(EDGES[hit], button, x, y, stamp)

    def on_motion(self, widget, e):
        self.send(t="move", x=self.px(e.x), y=self.px(e.y))  # GTK already merges motion per frame

    def on_scroll(self, widget, e):
        ok, dx, dy = e.get_scroll_deltas()
        if not ok:
            dy = {Gdk.ScrollDirection.UP: -1, Gdk.ScrollDirection.DOWN: 1}.get(e.direction, 0)
        if dy:
            self.send(t="wheel", dy=dy, x=self.px(e.x), y=self.px(e.y))

    def on_key(self, widget, e):
        name = Gdk.keyval_name(e.keyval) or ""
        if name in VK:
            self.send(t="vk", vk=VK[name])
            return True
        u = Gdk.keyval_to_unicode(e.keyval)
        if u and not e.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK):
            self.send(t="char", c=chr(u))
            return True
        return False

    def on_size(self, widget, alloc):
        size = (self.px(alloc.width), self.px(alloc.height))
        if size != self.area_size:  # where winbridge.py's Overlay lays the real window
            self.area_size = size
            self.send(t="area", w=size[0], h=size[1])
        # debounce: resize the real window once the user stops dragging
        if self.pending_resize:
            GLib.source_remove(self.pending_resize)
        self.pending_resize = GLib.timeout_add(250, self.flush_resize, alloc.width, alloc.height)

    def flush_resize(self, w, h):
        self.pending_resize = None
        if self.frame and (abs(self.px(w) - self.frame[0]) > 2 or abs(self.px(h) - self.frame[1]) > 2):
            self.send(t="resize", w=self.px(w), h=self.px(h))
        return False


def serve(port):
    srv = socket.create_server(("127.0.0.1", port))
    print(f"waiting for winbridge.py on port {port} ...")
    while True:
        conn, _ = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # input events go out at once
        hello = b""
        try:
            while not hello.endswith(b"\n"):
                hello += recv_exact(conn, 1)
            info = json.loads(hello)
        except (ConnectionError, ValueError):
            conn.close()
            continue
        GLib.idle_add(lambda c=conn, h=info: Mirror(c, h).show_all())


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 47800
    threading.Thread(target=serve, args=(port,), daemon=True).start()
    Gtk.main()


if __name__ == "__main__":
    main()
