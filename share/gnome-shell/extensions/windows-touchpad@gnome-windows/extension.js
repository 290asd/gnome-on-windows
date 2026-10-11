// Feeds touchpad swipes read on Windows (winbridge.py) to GNOME's own swipe handling, as if they came
// from a local touchpad: overview, app grid and workspace switching follow the fingers as usual.
// WSLg forwards no touchpad gestures, and nested mutter reads no input devices of its own.
// Also presses Super, which winbridge.py keeps away from Windows while GNOME is in front.
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const PORT = 47801; // winbridge.py's port + 1

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
        // nested mutter turns these into XTest key presses on the WSLg X server, which sends them back
        // to GNOME's window: there they combine with the other keys, which arrive through RDP
        this._keyboard = Clutter.get_default_backend().get_default_seat()
            .create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        // the first such key press after mutter starts is lost: spend it on a lone Shift
        this._warmUp = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 3000, () => {
            for (const state of [Clutter.KeyState.PRESSED, Clutter.KeyState.RELEASED])
                this._keyboard.notify_key(GLib.get_monotonic_time(), 42, state); // KEY_LEFTSHIFT
            this._warmUp = 0;
            return GLib.SOURCE_REMOVE;
        });
        this._connections = new Set();
        this._service = new Gio.SocketService();
        const loopback = Gio.InetAddress.new_loopback(Gio.SocketFamily.IPV4);
        this._service.add_address(new Gio.InetSocketAddress({address: loopback, port: PORT}),
            Gio.SocketType.STREAM, Gio.SocketProtocol.TCP, null);
        this._service.connect('incoming', (service, connection) => {
            this._connections.add(connection);
            this._readLine(new Gio.DataInputStream({base_stream: connection.get_input_stream()}), connection);
            return true;
        });
        this._service.start();
    }

    disable() {
        if (this._warmUp)
            GLib.source_remove(this._warmUp);
        this._service.stop();
        this._service.close();
        this._service = null;
        this._connections.forEach(c => c.close(null));
        this._connections = null;
        this._keyboard = null;
    }

    _readLine(stream, connection) {
        stream.read_line_async(GLib.PRIORITY_DEFAULT, null, (s, res) => {
            let line = null;
            try {
                [line] = s.read_line_finish_utf8(res);
                if (line !== null)
                    this._feed(JSON.parse(line));
            } catch (e) {
                logError(e, 'windows-touchpad');
            }
            if (line === null || !this._connections?.has(connection)) {
                this._connections?.delete(connection);
                connection.close(null);
                return;
            }
            this._readLine(stream, connection);
        });
    }

    _feed(msg) {
        if ('k' in msg) { // {k: evdev key code, down}
            this._keyboard.notify_key(GLib.get_monotonic_time(), msg.k,
                msg.down ? Clutter.KeyState.PRESSED : Clutter.KeyState.RELEASED);
        } else { // {p: begin / update / end, n: fingers, dx, dy}
            this._swipe(msg);
        }
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
