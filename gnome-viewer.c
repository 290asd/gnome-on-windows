/*
 * GNOME Shell 49+ in one X11 window on WSLg, the way nested mutter showed it up to GNOME 48 (--nested is gone).
 *
 * gnome-shell runs headless; this asks it (ScreenCast RecordVirtual) for a virtual monitor of the window's size and
 * shows the frames GNOME sends over PipeWire. Mutter's buffers (memfd) go to WSLg's X server as they are (MIT-SHM by
 * fd: no copy here), and each goes back to mutter as soon as the server has read it, so GNOME already draws the next
 * frame while this one is shown. Input goes back through mutter's RemoteDesktop D-Bus API, GNOME's pointer image
 * comes as the stream's cursor metadata.
 * mutter-devkit, mutter's own viewer, takes exactly two buffers, copies each, draws it with GTK and only then asks
 * for the next: 13 fps for a full-screen load here, where GNOME 46's --nested got 29.
 *
 *   gnome-viewer WIDTHxHEIGHT[@RATE] SCALE     inside GNOME's D-Bus session, next to gnome-shell --headless
 *
 * ponytail: one monitor, wheel steps only (no smooth touchpad scrolling: XInput 2), no clipboard; mutter-devkit
 * has those if they are ever missed.
 */
#include <linux/input-event-codes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <gio/gio.h>
#include <glib-unix.h>
#include <pipewire/pipewire.h>
#include <spa/buffer/meta.h>
#include <spa/param/video/format-utils.h>
#include <xcb/render.h>
#include <xcb/shm.h>
#include <xcb/xcb.h>
#include <xcb/xcb_renderutil.h>
#include <xcb/xkb.h>

#define RD "org.gnome.Mutter.RemoteDesktop"
#define SC "org.gnome.Mutter.ScreenCast"
#define CURSOR_META_SIZE(w, h) (sizeof(struct spa_meta_cursor) + sizeof(struct spa_meta_bitmap) + (w) * (h) * 4)

static int width, height;
static double rate = 60, scale = 2;
static GMainLoop *loop;
static int status;
static GDBusConnection *bus;
static char *rd_path, *stream_path;

static xcb_connection_t *xc;
static xcb_window_t win;
static xcb_gcontext_t gc;
static uint8_t shm_completion;
static xcb_render_pictformat_t argb32;
static xcb_cursor_t cursor;
static gboolean keys[256];
static unsigned buttons;

static struct pw_loop *pwl;
static struct pw_core *pw_core;
static struct pw_stream *stream;
static struct spa_hook stream_listener;
static struct spa_video_info_raw format;
static struct pw_buffer *shown, *next; /* being read by the X server; newest frame waiting for it */

static void quit(const char *why)
{
    g_printerr("gnome-viewer: %s\n", why);
    status = 1;
    g_main_loop_quit(loop);
}

/* --- frames: PipeWire -> X --- */

static void show_next(void)
{
    if (shown || !next)
        return;
    shown = next;
    next = NULL;
    struct spa_data *d = &shown->buffer->datas[0];
    uint32_t stride = d->chunk->stride > 0 ? (uint32_t)d->chunk->stride : format.size.width * 4;
    xcb_shm_put_image(xc, win, gc, stride / 4, format.size.height, 0, 0, format.size.width, format.size.height, 0, 0,
                      24, XCB_IMAGE_FORMAT_Z_PIXMAP, 1, GPOINTER_TO_UINT(shown->user_data),
                      d->mapoffset + d->chunk->offset);
    xcb_flush(xc);
}

/* GNOME's pointer image as the window's X cursor (premultiplied ARGB, like mutter's sprites) */
static void set_cursor(const struct spa_meta_bitmap *bitmap, int hot_x, int hot_y)
{
    int w = bitmap->size.width, h = bitmap->size.height;
    gboolean hidden = !w || !h;
    if (hidden)
        w = h = 1;
    uint8_t *pixels = g_malloc0((size_t)w * h * 4);
    const uint8_t *src = SPA_PTROFF(bitmap, bitmap->offset, const uint8_t);
    for (int y = 0; !hidden && y < h; y++) {
        for (int x = 0; x < w; x++) {
            const uint8_t *s = src + y * bitmap->stride + x * 4;
            uint8_t *d = pixels + (y * w + x) * 4; /* B G R A: X's ARGB32 in memory */
            switch (bitmap->format) {
            case SPA_VIDEO_FORMAT_RGBA: d[0] = s[2], d[1] = s[1], d[2] = s[0], d[3] = s[3]; break;
            case SPA_VIDEO_FORMAT_BGRA: memcpy(d, s, 4); break;
            case SPA_VIDEO_FORMAT_ARGB: d[0] = s[3], d[1] = s[2], d[2] = s[1], d[3] = s[0]; break;
            case SPA_VIDEO_FORMAT_ABGR: d[0] = s[1], d[1] = s[2], d[2] = s[3], d[3] = s[0]; break;
            default: g_free(pixels); return;
            }
        }
    }
    xcb_pixmap_t pixmap = xcb_generate_id(xc);
    xcb_gcontext_t pixmap_gc = xcb_generate_id(xc);
    xcb_render_picture_t picture = xcb_generate_id(xc);
    xcb_cursor_t c = xcb_generate_id(xc);
    xcb_create_pixmap(xc, 32, pixmap, win, w, h);
    xcb_create_gc(xc, pixmap_gc, pixmap, 0, NULL);
    xcb_put_image(xc, XCB_IMAGE_FORMAT_Z_PIXMAP, pixmap, pixmap_gc, w, h, 0, 0, 0, 32, w * h * 4, pixels);
    xcb_render_create_picture(xc, picture, pixmap, argb32, 0, NULL);
    xcb_render_create_cursor(xc, c, picture, CLAMP(hot_x, 0, w - 1), CLAMP(hot_y, 0, h - 1));
    xcb_change_window_attributes(xc, win, XCB_CW_CURSOR, &c);
    if (cursor)
        xcb_free_cursor(xc, cursor);
    cursor = c;
    xcb_render_free_picture(xc, picture);
    xcb_free_gc(xc, pixmap_gc);
    xcb_free_pixmap(xc, pixmap);
    xcb_flush(xc);
    g_free(pixels);
}

static void on_process(void *data)
{
    struct pw_buffer *b;
    while ((b = pw_stream_dequeue_buffer(stream))) {
        struct spa_meta_cursor *mc = spa_buffer_find_meta_data(b->buffer, SPA_META_Cursor, sizeof(*mc));
        if (mc && spa_meta_cursor_is_valid(mc) && mc->bitmap_offset) /* a new image; positions need nothing */
            set_cursor(SPA_PTROFF(mc, mc->bitmap_offset, struct spa_meta_bitmap), mc->hotspot.x, mc->hotspot.y);
        if (!b->buffer->datas[0].chunk->size || !b->user_data) { /* cursor only */
            pw_stream_queue_buffer(stream, b);
            continue;
        }
        if (next) /* superseded before the X server got to it */
            pw_stream_queue_buffer(stream, next);
        next = b;
    }
    show_next();
}

static void on_add_buffer(void *data, struct pw_buffer *b)
{
    struct spa_data *d = &b->buffer->datas[0];
    if (d->type != SPA_DATA_MemFd)
        return;
    xcb_shm_seg_t seg = xcb_generate_id(xc);
    xcb_shm_attach_fd(xc, seg, dup(d->fd), 1); /* the X server maps mutter's buffer itself */
    b->user_data = GUINT_TO_POINTER(seg);
}

static void on_remove_buffer(void *data, struct pw_buffer *b)
{
    if (b == shown)
        shown = NULL;
    if (b == next)
        next = NULL;
    if (b->user_data)
        xcb_shm_detach(xc, GPOINTER_TO_UINT(b->user_data));
    xcb_flush(xc);
}

static void on_param_changed(void *data, uint32_t id, const struct spa_pod *param)
{
    if (id != SPA_PARAM_Format || !param || spa_format_video_raw_parse(param, &format) < 0)
        return;
    g_print("gnome-viewer: %ux%u frames\n", format.size.width, format.size.height);
    uint8_t buf[1024];
    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buf, sizeof(buf));
    const struct spa_pod *params[3];
    /* more than two: one with the X server, one waiting for it, and GNOME still has one to draw into */
    params[0] = spa_pod_builder_add_object(&b, SPA_TYPE_OBJECT_ParamBuffers, SPA_PARAM_Buffers,
        SPA_PARAM_BUFFERS_buffers, SPA_POD_CHOICE_RANGE_Int(4, 2, 8),
        SPA_PARAM_BUFFERS_dataType, SPA_POD_Int(1 << SPA_DATA_MemFd));
    params[1] = spa_pod_builder_add_object(&b, SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
        SPA_PARAM_META_type, SPA_POD_Id(SPA_META_Header),
        SPA_PARAM_META_size, SPA_POD_Int(sizeof(struct spa_meta_header)));
    params[2] = spa_pod_builder_add_object(&b, SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
        SPA_PARAM_META_type, SPA_POD_Id(SPA_META_Cursor),
        SPA_PARAM_META_size, SPA_POD_CHOICE_RANGE_Int(CURSOR_META_SIZE(384, 384), CURSOR_META_SIZE(1, 1),
                                                      CURSOR_META_SIZE(384, 384)));
    pw_stream_update_params(stream, params, 3);
}

static void on_state_changed(void *data, enum pw_stream_state old, enum pw_stream_state state, const char *error)
{
    if (state == PW_STREAM_STATE_ERROR)
        quit(error ? error : "stream error");
}

static const struct pw_stream_events stream_events = {
    PW_VERSION_STREAM_EVENTS,
    .state_changed = on_state_changed,
    .param_changed = on_param_changed,
    .add_buffer = on_add_buffer,
    .remove_buffer = on_remove_buffer,
    .process = on_process,
};

static void on_stream_added(GDBusConnection *c, const char *sender, const char *path, const char *iface,
                            const char *signal, GVariant *args, gpointer data)
{
    uint32_t node;
    g_variant_get(args, "(u)", &node);
    stream = pw_stream_new(pw_core, "gnome-viewer",
                           pw_properties_new(PW_KEY_MEDIA_TYPE, "Video", PW_KEY_MEDIA_CATEGORY, "Capture",
                                             PW_KEY_MEDIA_ROLE, "Screen", NULL));
    pw_stream_add_listener(stream, &stream_listener, &stream_events, NULL);
    uint8_t buf[1024];
    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buf, sizeof(buf));
    const struct spa_pod *params[1];
    params[0] = spa_pod_builder_add_object(&b, SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat,
        SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_video),
        SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
        SPA_FORMAT_VIDEO_format, SPA_POD_CHOICE_ENUM_Id(3, SPA_VIDEO_FORMAT_BGRx, SPA_VIDEO_FORMAT_BGRx,
                                                        SPA_VIDEO_FORMAT_BGRA),
        SPA_FORMAT_VIDEO_size, SPA_POD_CHOICE_RANGE_Rectangle(&SPA_RECTANGLE(width, height), &SPA_RECTANGLE(1, 1),
                                                              &SPA_RECTANGLE(16384, 16384)),
        SPA_FORMAT_VIDEO_framerate, SPA_POD_Fraction(&SPA_FRACTION(0, 1)),
        SPA_FORMAT_VIDEO_maxFramerate, SPA_POD_CHOICE_RANGE_Fraction(&SPA_FRACTION((int)(rate + 0.5), 1),
                                                                     &SPA_FRACTION(0, 1),
                                                                     &SPA_FRACTION((int)(rate + 0.5), 1)));
    /* not the driver: GNOME draws on its own clock and the frames overlap with showing them */
    if (pw_stream_connect(stream, PW_DIRECTION_INPUT, node, PW_STREAM_FLAG_AUTOCONNECT, params, 1) < 0)
        quit("can't connect to GNOME's PipeWire stream");
}

/* --- D-Bus: sessions and input --- */

static GVariant *call(const char *name, const char *path, const char *iface, const char *method, GVariant *args,
                      const char *reply)
{
    GError *error = NULL;
    GVariant *r = g_dbus_connection_call_sync(bus, name, path, iface, method, args,
                                              reply ? G_VARIANT_TYPE(reply) : NULL, G_DBUS_CALL_FLAGS_NONE, -1,
                                              NULL, &error);
    if (!r) {
        quit(error->message);
        g_error_free(error);
    }
    return r;
}

static void rd_notify(const char *method, GVariant *args)
{
    if (rd_path)
        g_dbus_connection_call(bus, RD, rd_path, RD ".Session", method, args, NULL, G_DBUS_CALL_FLAGS_NONE, -1,
                               NULL, NULL, NULL);
    else
        g_variant_unref(g_variant_ref_sink(args));
}

static void on_closed(GDBusConnection *c, const char *sender, const char *path, const char *iface,
                      const char *signal, GVariant *args, gpointer data)
{
    g_main_loop_quit(loop); /* GNOME ended the session: it is quitting */
}

static void start(GDBusConnection *c, const char *name, const char *owner, gpointer data)
{
    GVariant *r, *v;
    if (rd_path || !(r = call(RD, "/org/gnome/Mutter/RemoteDesktop", RD, "CreateSession", NULL, "(o)")))
        return;
    g_variant_get(r, "(o)", &rd_path);
    g_variant_unref(r);
    if (!(r = call(RD, rd_path, "org.freedesktop.DBus.Properties", "Get",
                   g_variant_new("(ss)", RD ".Session", "SessionId"), "(v)")))
        return;
    g_variant_get(r, "(v)", &v);
    GVariantBuilder props;
    g_variant_builder_init(&props, G_VARIANT_TYPE_VARDICT);
    g_variant_builder_add(&props, "{sv}", "remote-desktop-session-id", v);
    g_variant_unref(r);
    char *sc_path;
    if (!(r = call(SC, "/org/gnome/Mutter/ScreenCast", SC, "CreateSession", g_variant_new("(a{sv})", &props), "(o)")))
        return;
    g_variant_get(r, "(o)", &sc_path);
    g_variant_unref(r);

    GVariantBuilder mode, modes;
    g_variant_builder_init(&mode, G_VARIANT_TYPE_VARDICT);
    g_variant_builder_add(&mode, "{sv}", "size", g_variant_new("(uu)", width, height));
    g_variant_builder_add(&mode, "{sv}", "refresh-rate", g_variant_new_double(rate));
    g_variant_builder_add(&mode, "{sv}", "preferred-scale", g_variant_new_double(scale));
    g_variant_builder_add(&mode, "{sv}", "is-preferred", g_variant_new_boolean(TRUE));
    g_variant_builder_init(&modes, G_VARIANT_TYPE("aa{sv}"));
    g_variant_builder_add_value(&modes, g_variant_builder_end(&mode));
    g_variant_builder_init(&props, G_VARIANT_TYPE_VARDICT);
    g_variant_builder_add(&props, "{sv}", "cursor-mode", g_variant_new_uint32(2)); /* metadata */
    g_variant_builder_add(&props, "{sv}", "is-platform", g_variant_new_boolean(TRUE));
    g_variant_builder_add(&props, "{sv}", "modes", g_variant_builder_end(&modes));
    if (!(r = call(SC, sc_path, SC ".Session", "RecordVirtual", g_variant_new("(a{sv})", &props), "(o)")))
        return;
    g_variant_get(r, "(o)", &stream_path);
    g_variant_unref(r);
    g_free(sc_path);

    g_dbus_connection_signal_subscribe(bus, SC, SC ".Stream", "PipeWireStreamAdded", stream_path, NULL,
                                       G_DBUS_SIGNAL_FLAGS_NONE, on_stream_added, NULL, NULL);
    g_dbus_connection_signal_subscribe(bus, RD, RD ".Session", "Closed", rd_path, NULL, G_DBUS_SIGNAL_FLAGS_NONE,
                                       on_closed, NULL, NULL);
    if ((r = call(RD, rd_path, RD ".Session", "Start", NULL, NULL)))
        g_variant_unref(r);
}

static void vanished(GDBusConnection *c, const char *name, gpointer data)
{
    if (rd_path)
        g_main_loop_quit(loop); /* gnome-shell is gone */
}

static void key(int keycode, gboolean down)
{
    if (keycode < 8 || keycode > 255 || keys[keycode] == down)
        return; /* auto-repeat: GNOME repeats held keys itself */
    keys[keycode] = down;
    rd_notify("NotifyKeyboardKeycode", g_variant_new("(ub)", keycode - 8, down));
}

static void button(int b, gboolean down)
{
    static const int evdev[] = {[1] = BTN_LEFT, [2] = BTN_MIDDLE, [3] = BTN_RIGHT, [8] = BTN_SIDE, [9] = BTN_EXTRA};
    if (b >= 4 && b <= 7) { /* wheel steps: up, down, left, right */
        if (down)
            rd_notify("NotifyPointerAxisDiscrete", g_variant_new("(ui)", b >= 6, b % 2 ? 1 : -1));
        return;
    }
    if (b < 1 || b > 9 || !evdev[b] || !!(buttons & 1u << b) == down)
        return;
    buttons ^= 1u << b;
    rd_notify("NotifyPointerButton", g_variant_new("(ib)", evdev[b], down));
}

static void release_all(void) /* the window lost the keyboard: nothing may stay held in GNOME */
{
    for (int k = 0; k < 256; k++)
        key(k, FALSE);
    for (int b = 1; b <= 9; b++)
        button(b, FALSE);
}

/* --- X --- */

static void handle(xcb_generic_event_t *e)
{
    uint8_t type = e->response_type & 0x7f;
    if (type == shm_completion) { /* the X server has read the frame: GNOME may draw into that buffer again */
        if (shown)
            pw_stream_queue_buffer(stream, shown);
        shown = NULL;
        show_next();
        return;
    }
    switch (type) {
    case 0: {
        xcb_generic_error_t *err = (xcb_generic_error_t *)e;
        g_printerr("gnome-viewer: X error %d (request %d.%d)\n", err->error_code, err->major_code, err->minor_code);
        break;
    }
    case XCB_MOTION_NOTIFY: {
        xcb_motion_notify_event_t *m = (xcb_motion_notify_event_t *)e;
        if (stream_path) /* GNOME's logical pixels */
            rd_notify("NotifyPointerMotionAbsolute",
                      g_variant_new("(sdd)", stream_path, m->event_x / scale, m->event_y / scale));
        break;
    }
    case XCB_BUTTON_PRESS:
    case XCB_BUTTON_RELEASE:
        button(((xcb_button_press_event_t *)e)->detail, type == XCB_BUTTON_PRESS);
        break;
    case XCB_KEY_PRESS:
    case XCB_KEY_RELEASE:
        key(((xcb_key_press_event_t *)e)->detail, type == XCB_KEY_PRESS);
        break;
    case XCB_FOCUS_OUT:
        release_all();
        break;
    case XCB_CLIENT_MESSAGE: /* WM_DELETE_WINDOW, the only protocol set */
        g_main_loop_quit(loop);
        break;
    }
}

static void drain_x(void)
{
    xcb_generic_event_t *e;
    while ((e = xcb_poll_for_event(xc))) {
        handle(e);
        free(e);
    }
    if (xcb_connection_has_error(xc))
        quit("lost the X server");
}

static gboolean on_x(gint fd, GIOCondition condition, gpointer data)
{
    drain_x();
    return G_SOURCE_CONTINUE;
}

static gboolean on_pipewire(gint fd, GIOCondition condition, gpointer data)
{
    pw_loop_iterate(pwl, 0);
    drain_x(); /* writing may have read events into xcb's queue without the socket staying readable */
    return G_SOURCE_CONTINUE;
}

static xcb_atom_t atom(const char *name)
{
    xcb_intern_atom_reply_t *r = xcb_intern_atom_reply(xc, xcb_intern_atom(xc, 0, strlen(name), name), NULL);
    xcb_atom_t a = r ? r->atom : XCB_NONE;
    free(r);
    return a;
}

static gboolean init_x(void)
{
    xc = xcb_connect(NULL, NULL);
    if (xcb_connection_has_error(xc)) {
        g_printerr("gnome-viewer: can't open the X display\n");
        return FALSE;
    }
    xcb_shm_query_version_reply_t *shm = xcb_shm_query_version_reply(xc, xcb_shm_query_version(xc), NULL);
    gboolean fds = shm && (shm->major_version > 1 || shm->minor_version >= 2);
    free(shm);
    if (!fds) {
        g_printerr("gnome-viewer: the X server can't share memory by file descriptor (MIT-SHM 1.2)\n");
        return FALSE;
    }
    shm_completion = xcb_get_extension_data(xc, &xcb_shm_id)->first_event + XCB_SHM_COMPLETION;
    xcb_render_pictforminfo_t *f = xcb_render_util_find_standard_format(xcb_render_util_query_formats(xc),
                                                                        XCB_PICT_STANDARD_ARGB_32);
    argb32 = f ? f->id : 0;
    /* held keys repeat as presses only: key() can drop them, GNOME repeats by itself */
    free(xcb_xkb_use_extension_reply(xc, xcb_xkb_use_extension(xc, 1, 0), NULL));
    free(xcb_xkb_per_client_flags_reply(xc, xcb_xkb_per_client_flags(xc, XCB_XKB_ID_USE_CORE_KBD,
        XCB_XKB_PER_CLIENT_FLAG_DETECTABLE_AUTO_REPEAT, XCB_XKB_PER_CLIENT_FLAG_DETECTABLE_AUTO_REPEAT, 0, 0, 0),
        NULL));

    xcb_screen_t *screen = xcb_setup_roots_iterator(xcb_get_setup(xc)).data;
    win = xcb_generate_id(xc);
    uint32_t values[] = {XCB_BACK_PIXMAP_NONE,
                         XCB_EVENT_MASK_KEY_PRESS | XCB_EVENT_MASK_KEY_RELEASE | XCB_EVENT_MASK_BUTTON_PRESS |
                             XCB_EVENT_MASK_BUTTON_RELEASE | XCB_EVENT_MASK_POINTER_MOTION |
                             XCB_EVENT_MASK_FOCUS_CHANGE};
    xcb_create_window(xc, XCB_COPY_FROM_PARENT, win, screen->root, 0, 0, width, height, 0,
                      XCB_WINDOW_CLASS_INPUT_OUTPUT, screen->root_visual, XCB_CW_BACK_PIXMAP | XCB_CW_EVENT_MASK,
                      values);
    /* the name WSLg gives the Windows window, "gnome-shell (<distro>)": winbridge.py finds GNOME by it */
    const char name[] = "gnome-shell", class[] = "gnome-shell\0gnome-shell";
    xcb_change_property(xc, XCB_PROP_MODE_REPLACE, win, XCB_ATOM_WM_NAME, XCB_ATOM_STRING, 8, strlen(name), name);
    xcb_change_property(xc, XCB_PROP_MODE_REPLACE, win, atom("_NET_WM_NAME"), atom("UTF8_STRING"), 8,
                        strlen(name), name);
    xcb_change_property(xc, XCB_PROP_MODE_REPLACE, win, XCB_ATOM_WM_CLASS, XCB_ATOM_STRING, 8, sizeof(class), class);
    xcb_atom_t delete_window = atom("WM_DELETE_WINDOW"), fullscreen = atom("_NET_WM_STATE_FULLSCREEN");
    xcb_change_property(xc, XCB_PROP_MODE_REPLACE, win, atom("WM_PROTOCOLS"), XCB_ATOM_ATOM, 32, 1, &delete_window);
    xcb_change_property(xc, XCB_PROP_MODE_REPLACE, win, atom("_NET_WM_STATE"), XCB_ATOM_ATOM, 32, 1, &fullscreen);
    gc = xcb_generate_id(xc);
    xcb_create_gc(xc, gc, win, 0, NULL);
    xcb_map_window(xc, win);
    xcb_flush(xc);
    return TRUE;
}

int main(int argc, char **argv)
{
    if (argc < 3 || sscanf(argv[1], "%dx%d@%lf", &width, &height, &rate) < 2 || width <= 0 || height <= 0 ||
        (scale = g_ascii_strtod(argv[2], NULL)) <= 0) {
        g_printerr("usage: %s WIDTHxHEIGHT[@RATE] SCALE\n", argv[0]);
        return 2;
    }
    pw_init(&argc, &argv);
    loop = g_main_loop_new(NULL, FALSE);
    if (!init_x())
        return 1;
    pwl = pw_loop_new(NULL);
    pw_core = pw_context_connect(pw_context_new(pwl, NULL, 0), NULL, 0);
    if (!pw_core) {
        g_printerr("gnome-viewer: can't connect to PipeWire\n");
        return 1;
    }
    pw_loop_enter(pwl);
    GError *error = NULL;
    if (!(bus = g_bus_get_sync(G_BUS_TYPE_SESSION, NULL, &error))) {
        g_printerr("gnome-viewer: %s\n", error->message);
        return 1;
    }
    g_unix_fd_add(xcb_get_file_descriptor(xc), G_IO_IN, on_x, NULL);
    g_unix_fd_add(pw_loop_get_fd(pwl), G_IO_IN, on_pipewire, NULL);
    /* GNOME may still be starting: begin once its screen cast API is there, end when it goes */
    g_bus_watch_name_on_connection(bus, SC, G_BUS_NAME_WATCHER_FLAGS_NONE, start, vanished, NULL, NULL);
    drain_x();
    g_main_loop_run(loop);
    return status;
}
