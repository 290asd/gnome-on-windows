// Feeds touchpad swipes read on Windows (winbridge.py) to GNOME's own swipe handling, as if they came
// from a local touchpad: overview, app grid and workspace switching follow the fingers as usual.
// WSLg forwards no touchpad gestures, and nested mutter reads no input devices of its own.
// Also presses Super, which winbridge.py keeps away from Windows while GNOME is in front, and tells
// winbridge.py where the focused mirror is, so it can show the real Windows window there.
// And sets Windows' dark mode and desktop wallpaper in GNOME, following them when they change.
// And shows Windows' notification-area (tray) icons in the top bar, their clicks going back to the apps.
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const PORT = 47801; // winbridge.py's port + 1

// Tray icons Windows pins to its taskbar that get a top-bar button; the others go in the tray menu.
// ponytail: a count, not measured; leaves room for GNOME's own indicators at 1440 logical px wide.
// Measuring the panel's free width is the upgrade path.
const TRAY_IN_BAR = 6;

const PHASES = {
    begin: Clutter.TouchpadGesturePhase.BEGIN,
    update: Clutter.TouchpadGesturePhase.UPDATE,
    end: Clutter.TouchpadGesturePhase.END,
};

// The SwipeTrackers that listen to touchpad swipes, in the order they connect to the stage.
// ponytail: private fields of GNOME Shell 46; check these paths when the shell version changes.
function swipeTrackers() {
    const controls = Main.overview._overview?._controls;
    return [
        Main.wm._workspaceAnimation?._swipeTracker,
        controls?._workspacesDisplay?._swipeTracker,
        controls?._appDisplay?._swipeTracker,
        Main.overview._swipeTracker,
    ].filter(t => t);
}

export default class WindowsTouchpad {
    enable() {
        // GNOME 46's nested mutter turns these into XTest key presses on the WSLg X server, which sends them
        // back to GNOME's window: there they combine with the other keys, which arrive through RDP. From 49 on
        // (devkit) they go straight into mutter's own seat, next to the viewer's keys.
        // Clutter.get_default_backend() is gone in GNOME 51
        const backend = global.stage.context?.get_backend() ?? Clutter.get_default_backend();
        this._keyboard = backend.get_default_seat()
            .create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        // the first such key press after mutter starts is lost: spend it on a lone Shift
        this._warmUp = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 3000, () => {
            for (const state of [Clutter.KeyState.PRESSED, Clutter.KeyState.RELEASED])
                this._keyboard.notify_key(GLib.get_monotonic_time(), 42, state); // KEY_LEFTSHIFT
            this._warmUp = 0;
            return GLib.SOURCE_REMOVE;
        });
        this._connections = new Set();
        this._trayIcons = new Map(); // tray icon id -> {gicon, tip, pinned, connection, button or item}
        // Windows' overflow: the tray icons without a button of their own, left of those
        this._trayMenu = new PanelMenu.Button(0, 'Windows tray');
        this._trayMenu.add_child(new St.Icon({icon_name: 'pan-down-symbolic', style_class: 'system-status-icon'}));
        Main.panel.addToStatusArea('windows-tray', this._trayMenu);
        this._trayMenu.container.hide();
        this._service = new Gio.SocketService();
        const loopback = Gio.InetAddress.new_loopback(Gio.SocketFamily.IPV4);
        this._service.add_address(new Gio.InetSocketAddress({address: loopback, port: PORT}),
            Gio.SocketType.STREAM, Gio.SocketProtocol.TCP, null);
        this._service.connect('incoming', (service, connection) => {
            this._connections.add(connection);
            this._readLine(new Gio.DataInputStream({base_stream: connection.get_input_stream()}), connection);
            if (this._overlay)
                this._write(connection, this._overlay);
            return true;
        });
        this._service.start();

        this._overlay = null; // last overlay line sent
        this._grabbed = false;
        this._grabIds = [
            global.display.connect('grab-op-begin', () => (this._grabbed = true)),
            global.display.connect('grab-op-end', () => (this._grabbed = false)),
        ];
        this._frameId = global.stage.connect('before-update', () => this._reportOverlay());
        // From 49 on, GNOME's window is a screen cast to mutter-devkit: GNOME shows it as screen sharing, with a
        // stop button that would end it. ponytail: hides Linux apps' own screen sharing too; tell the handles
        // apart if that ever matters.
        Main.panel.statusArea.screenSharing?.container.hide();
    }

    disable() {
        Main.panel.statusArea.screenSharing?.container.show();
        global.stage.disconnect(this._frameId);
        this._grabIds.forEach(id => global.display.disconnect(id));
        if (this._warmUp)
            GLib.source_remove(this._warmUp);
        this._service.stop();
        this._service.close();
        this._service = null;
        this._connections.forEach(c => c.close(null));
        this._connections = null;
        this._trayIcons.forEach((t, id) => this._dropTrayIcon(id));
        this._trayMenu.destroy();
        this._trayIcons = this._trayMenu = null;
        this._keyboard = null;
    }

    _readLine(stream, connection) {
        stream.read_line_async(GLib.PRIORITY_DEFAULT, null, (s, res) => {
            let line = null;
            try {
                [line] = s.read_line_finish_utf8(res);
                if (line !== null)
                    this._feed(JSON.parse(line), connection);
            } catch (e) {
                logError(e, 'windows-touchpad');
            }
            if (line === null || !this._connections?.has(connection)) {
                this._connections?.delete(connection);
                connection.close(null);
                if (this._trayIcons) { // winbridge.py sends them again
                    for (const [id, t] of this._trayIcons) {
                        if (t.connection === connection)
                            this._dropTrayIcon(id);
                    }
                    this._layoutTray();
                }
                return;
            }
            this._readLine(stream, connection);
        });
    }

    // Checked every frame, sent when it changes: {o: Windows handle, x, y, w, h} of the focused mirror's
    // frame while nothing of GNOME's own covers it or moves it, else {o: 0}. winbridge.py's Overlay then
    // shows the real window in its place.
    _reportOverlay() {
        const line = `${JSON.stringify(this._overlayState())}\n`;
        if (line === this._overlay)
            return;
        this._overlay = line;
        this._connections.forEach(c => this._write(c, line));
    }

    _overlayState() {
        const win = global.display.focus_window;
        // gnomebridge.py tags each mirror with its Windows handle
        const hwnd = Number(win?.get_gtk_window_object_path()?.match(/^\/hwnd\/(\d+)$/)?.[1]);
        const actor = win?.get_compositor_private();
        // ponytail: _switchData is private to GNOME Shell 46's workspace switch animation
        if (!hwnd || !actor || Main.overview.visible || Main.modalCount > 0 || this._grabbed ||
            Main.wm._workspaceAnimation?._switchData || win.minimized ||
            !win.located_on_workspace(global.workspace_manager.get_active_workspace()) ||
            // open/close/minimize/maximize animations
            actor.opacity !== 255 || actor.scale_x !== 1 || actor.scale_y !== 1 ||
            actor.translation_x !== 0 || actor.translation_y !== 0)
            return {o: 0};
        const r = win.get_frame_rect();
        // sw: GNOME's width in the same units (GNOME 46 lays out in physical pixels, 49+ in logical ones)
        return {o: hwnd, x: r.x, y: r.y, w: r.width, h: r.height, sw: global.stage.width};
    }

    _write(connection, line) {
        try { // a blocking write, but winbridge.py always reads and these are a few bytes on a change
            connection.get_output_stream().write_all(new TextEncoder().encode(line), null);
        } catch (e) {
            logError(e, 'windows-touchpad');
        }
    }

    _feed(msg, connection) {
        if ('tray' in msg) {
            this._tray(msg, connection);
        } else if ('dark' in msg) { // {dark, wallpaper: its path under /mnt, v: its version}
            this._appearance(msg);
        } else if ('k' in msg) { // {k: evdev key code, down}
            this._keyboard.notify_key(GLib.get_monotonic_time(), msg.k,
                msg.down ? Clutter.KeyState.PRESSED : Clutter.KeyState.RELEASED);
        } else { // {p: begin / update / end, n: fingers, dx, dy}
            this._swipe(msg);
        }
    }

    // A Windows notification-area icon (winbridge.py's Tray): {tray: id, icon: base64 PNG, tip: its name,
    // pinned} shows or updates it, {tray: id, title, text} is one of its balloons, {tray: id} alone removes it.
    _tray(msg, connection) {
        if ('title' in msg) {
            Main.notify(msg.title, msg.text);
            return;
        }
        if (msg.icon) {
            const gicon = Gio.BytesIcon.new(GLib.Bytes.new(GLib.base64_decode(msg.icon)));
            this._trayIcons.set(msg.tray,
                {...this._trayIcons.get(msg.tray), gicon, tip: msg.tip, pinned: msg.pinned, connection});
        } else {
            this._dropTrayIcon(msg.tray);
        }
        this._layoutTray();
    }

    _dropTrayIcon(id) {
        const t = this._trayIcons.get(id);
        t?.button?.destroy();
        t?.item?.destroy();
        this._trayIcons.delete(id);
    }

    // As on Windows: the icons pinned to its taskbar (Settings → Personalization → Taskbar → Other system
    // tray icons) as top-bar buttons, TRAY_IN_BAR at most; the others in the tray menu, with their names.
    _layoutTray() {
        let room = TRAY_IN_BAR;
        for (const [id, t] of this._trayIcons) {
            const inBar = t.pinned && room-- > 0;
            if (inBar && !t.button) {
                t.item?.destroy();
                t.item = null;
                t.button = this._trayButton(id);
            } else if (!inBar && !t.item) {
                t.button?.destroy();
                t.button = null;
                t.item = new PopupMenu.PopupImageMenuItem('', null);
                t.item.connect('activate', (item, event) => { // the menu closes; a click on the icon
                    const b = event?.type() === Clutter.EventType.BUTTON_RELEASE ? event.get_button() : 1;
                    this._trayClick(id, b, true);
                    this._trayClick(id, b, false);
                });
                this._trayMenu.menu.addMenuItem(t.item);
            }
            if (t.button) {
                t.button._icon.gicon = t.gicon;
                t.button.accessible_name = t.tip;
            } else {
                t.item.setIcon(t.gicon);
                t.item.label.text = t.tip;
            }
        }
        this._trayMenu.container.visible = this._trayMenu.menu.numMenuItems > 0;
    }

    _trayButton(id) {
        const button = new PanelMenu.Button(0, '', true);
        button._icon = new St.Icon({style_class: 'system-status-icon'});
        button.add_child(button._icon);
        for (const [signal, down] of [['button-press-event', true], ['button-release-event', false]]) {
            button.connect(signal, (actor, event) => {
                this._trayClick(id, event.get_button(), down);
                return Clutter.EVENT_STOP;
            });
        }
        Main.panel.addToStatusArea(`windows-tray-${id}`, button, 1); // right of the tray menu
        return button;
    }

    // To winbridge.py's Tray, which sends the app what Explorer would: it answers on Windows, with its own
    // menu right where the pointer is (GNOME covers the whole screen) or a window
    _trayClick(id, b, down) {
        const t = this._trayIcons.get(id);
        if (t)
            this._write(t.connection, `${JSON.stringify({tray: id, b, down})}\n`);
    }

    _appearance({dark, wallpaper, v}) {
        const iface = new Gio.Settings({schema_id: 'org.gnome.desktop.interface'});
        // libadwaita and GTK 4 follow color-scheme; GTK 3 (the mirrors' title bars) only the theme name
        const theme = iface.get_string('gtk-theme').replace(/-dark$/, '') + (dark ? '-dark' : '');
        for (const [key, value] of [['color-scheme', dark ? 'prefer-dark' : 'default'], ['gtk-theme', theme]]) {
            if (iface.get_string(key) !== value)
                iface.set_string(key, value);
        }
        if (wallpaper)
            this._setWallpaper(wallpaper, v);
    }

    // GNOME shows a copy on the Linux side, under a new name for each version: it reloads a background
    // only when the URI changes or a file monitor reports a change, and /mnt reports none made on Windows
    _setWallpaper(path, v) {
        const bg = new Gio.Settings({schema_id: 'org.gnome.desktop.background'});
        const dir = Gio.File.new_for_path(GLib.build_filenamev([GLib.get_user_cache_dir(), 'gnome-windows']));
        const dest = dir.get_child(`wallpaper-${v}`);
        this._wallpaper = v;
        if (bg.get_string('picture-uri') === dest.get_uri())
            return; // reconnected, nothing new
        GLib.mkdir_with_parents(dir.get_path(), 0o755);
        Gio.File.new_for_path(path).copy_async(dest, Gio.FileCopyFlags.OVERWRITE, GLib.PRIORITY_DEFAULT,
            null, null, (src, res) => {
                try {
                    src.copy_finish(res);
                } catch (e) {
                    logError(e, 'windows-touchpad');
                    return;
                }
                if (this._wallpaper !== v) { // a newer one came while copying
                    dest.delete_async(GLib.PRIORITY_DEFAULT, null, null);
                    return;
                }
                const old = Gio.File.new_for_uri(bg.get_string('picture-uri'));
                bg.set_string('picture-uri', dest.get_uri());
                bg.set_string('picture-uri-dark', dest.get_uri());
                if (old.get_parent()?.equal(dir))
                    old.delete_async(GLib.PRIORITY_DEFAULT, null, null);
            });
    }

    // One swipe event in the shape TouchpadSwipeGesture._handleEvent reads from a Clutter event.
    _swipe({p, n, dx, dy}) {
        const [x, y] = global.get_pointer();
        const time = Math.floor(GLib.get_monotonic_time() / 1000) % 2 ** 32;
        const event = {
            type: () => Clutter.EventType.TOUCHPAD_SWIPE,
            get_gesture_phase: () => PHASES[p],
            get_touchpad_gesture_finger_count: () => n,
            get_time: () => time,
            get_coords: () => [x, y],
            get_gesture_motion_delta_unaccelerated: () => [dx, dy],
        };
        // like the stage's captured-event handlers: the first one that takes the swipe stops the rest
        for (const tracker of swipeTrackers()) {
            if (tracker._touchpadGesture._handleEvent(global.stage, event) === Clutter.EVENT_STOP)
                break;
        }
    }
}
