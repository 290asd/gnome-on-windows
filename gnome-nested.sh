#!/bin/bash
# Real GNOME Shell as a nested compositor inside one WSLg window, sized to the whole screen.
# Linux apps started with WAYLAND_DISPLAY=gnome-nested appear inside it.

# Right after the WSL VM boots WSLg reports a 640x480 placeholder screen, then switches to the real
# one when Windows connects; nested mutter dies on that switch, so wait for the real size first.
screen_size() { xrandr --display :0 2>/dev/null | awk '/ connected/ {split($3, a, "+"); print a[1]; exit}'; }
# WSLg's refresh rate (the starred mode), 60 Hz unless WESTON_RDP_MONITOR_REFRESH_RATE is set in
# .wslgconfig; GNOME's frame clock runs at whatever its dummy mode says
screen_rate() { xrandr --display :0 2>/dev/null | awk '{for (i = 2; i <= NF; i++) if ($i ~ /\*/) {sub(/[*+]+/, "", $i); print $i; exit}}'; }
for _ in $(seq 60); do
    SIZE=$(screen_size)
    [ -n "$SIZE" ] && [ "$SIZE" != "640x480" ] && break
    sleep 0.5
done
RATE=$(screen_rate)
export MUTTER_DEBUG_DUMMY_MODE_SPECS="${1:-${SIZE:-1920x1200}@${RATE:-60}}"
export MUTTER_DEBUG_DUMMY_MONITOR_SCALES="${GNOME_SCALE:-2}"  # integer scale keeps it sharp
export MESA_SHADER_CACHE_DISABLE=true   # mixing d3d12/llvmpipe in one cache blacked out GNOME before
# GPU rendering on the NVIDIA dGPU through d3d12. The Intel Arc (Mesa's default adapter) hangs the
# GPU under gnome-shell (LiveKernelEvent 141) within a minute, which has frozen Windows: first with a
# blank window, and on 2026-10-13 with Intel driver 32.0.101.9034 and Mesa 25.2.8 while drawing fine
# (glxgears doesn't). Exported so GTK4 apps started inside GNOME use the NVIDIA too. No NVIDIA GPU:
# software rendering. GNOME_GALLIUM=llvmpipe forces software, GNOME_GPU picks another adapter.
/usr/lib/wsl/lib/nvidia-smi -L >/dev/null 2>&1 || GNOME_GALLIUM="${GNOME_GALLIUM:-llvmpipe}"
export GALLIUM_DRIVER="${GNOME_GALLIUM:-d3d12}"
export MESA_D3D12_DEFAULT_ADAPTER_NAME="${GNOME_GPU:-NVIDIA}"
# llvmpipe defaults to one thread per core (22 here); syncing them for every frame cost more than
# it saved: 4 threads measured 160 % CPU vs 250 % for the same load
export LP_NUM_THREADS="${GNOME_LP_THREADS:-4}"
export XDG_SESSION_TYPE=wayland
export XDG_CURRENT_DESKTOP=GNOME
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export GNOME_WINDOWS_DIR="$(dirname "$(readlink -f "$0")")"
# GNOME 49 dropped --nested (mutter's X11 backend, removed in 50). From 49 on, --devkit: headless mutter whose
# virtual monitor mutter-devkit (GTK 4, package mutter-dev-bin) shows in a window, frames over PipeWire (the
# user's pipewire + wireplumber services), input over libei. mutter starts that viewer with GNOME's starting
# environment: a Wayland display that doesn't exist (WSLg's wayland-0 is GTK's default even when unset) makes it
# an X11 window, which fullscreen_when_shown can fullscreen (it has no fullscreen of its own, and a WSLg Wayland
# window can't be made fullscreen from outside); its GDK scale becomes the monitor's, its size follows the window.
if [ "$(gnome-shell --version | grep -o '[0-9]\+' | head -1)" -ge 49 ]; then
    export SHELL_CMD="env WAYLAND_DISPLAY=devkit-viewer-on-x11 GDK_SCALE=${GNOME_SCALE:-2} gnome-shell --devkit"
else
    export SHELL_CMD="gnome-shell --nested"
fi
# GNOME-only launchers ("Exit GNOME"); kept out of ~/.local so WSLg doesn't add them to Windows' Start menu
export XDG_DATA_DIRS="$GNOME_WINDOWS_DIR/share:${XDG_DATA_DIRS:-/usr/share/ubuntu:/usr/local/share:/usr/share:/var/lib/snapd/desktop}"

win_pos() { DISPLAY=:0 xwininfo -id "$1" 2>/dev/null | awk '/Absolute upper-left X/ {x=$NF} /Absolute upper-left Y/ {y=$NF} END {print x "," y}'; }

fullscreen_when_shown() {
    for _ in $(seq 60); do
        sleep 0.5
        # WSLg's window manager publishes no client list, so look the window up in the X tree
        id=$(DISPLAY=:0 xwininfo -root -tree 2>/dev/null | awk '/"gnome-shell"|"Mutter Development Kit"/ {print $1; exit}')
        [ -n "$id" ] && break
    done
    [ -z "$id" ] && return
    # the devkit viewer's window, under nested GNOME's name: winbridge.py finds GNOME by it
    DISPLAY=:0 wmctrl -i -r "$id" -N gnome-shell
    # WSLg sometimes parks the fullscreen X window at -32768,-32768 (when fullscreen is requested
    # while it is still placing the window, or later when the foreground changes). It still shows on
    # Windows, but every click and key misses GNOME. Keep checking for the whole session and redo it.
    sleep 2
    while pos=$(win_pos "$id") && [ "$pos" != "," ]; do
        if [ "$pos" != "0,0" ] && ! DISPLAY=:0 xprop -id "$id" WM_STATE 2>/dev/null | grep -q Iconic; then
            echo "GNOME window at $pos, (re)applying fullscreen"
            DISPLAY=:0 wmctrl -i -r "$id" -b remove,fullscreen
            sleep 1
            DISPLAY=:0 wmctrl -i -r "$id" -b add,fullscreen
        fi
        sleep 2
    done
}

# ponytail: retry blindly when GNOME dies within 20 s (WSLg still settling); a proper fix would
# wait for a WSLg readiness signal instead.
for attempt in 1 2 3; do
    echo "--- attempt $attempt, ${MUTTER_DEBUG_DUMMY_MODE_SPECS} @ ${MUTTER_DEBUG_DUMMY_MONITOR_SCALES}x"
    fullscreen_when_shown &
    started=$(date +%s)
    # --no-x11: WSLg's /tmp/.X11-unix lacks the sticky bit, so mutter can't start its own Xwayland
    # hot corner off: nested mutter has no pointer barriers, and the fallback re-opens the overview
    # every time the scene under a resting pointer changes, trapping you in it
    # Ctrl+Alt+Up opens the overview (frees it from switch-to-workspace-up, a no-op with horizontal
    # workspaces), Ctrl+Alt+Left/Right switch workspaces. Windows handles touchpad gestures itself,
    # so its Advanced gestures settings map 3-finger swipes to these shortcuts. It injects the arrows
    # without the extended-key flag, so they reach GNOME as keypad arrows: bind KP_* too.
    # The windows-touchpad extension (share/gnome-shell/extensions) turns touchpad swipes that
    # winbridge.py reads on Windows into GNOME's own swipe gestures.
    dbus-run-session -- bash -c 'gsettings set org.gnome.desktop.interface enable-hot-corners false
        gnome-extensions enable windows-touchpad@gnome-windows
        gsettings set org.gnome.desktop.wm.keybindings switch-to-workspace-up "[]"
        gsettings set org.gnome.shell.keybindings toggle-overview "[\"<Control><Alt>Up\", \"<Control><Alt>KP_Up\"]"
        gsettings set org.gnome.desktop.wm.keybindings switch-to-workspace-left \
            "[\"<Super>Page_Up\", \"<Super><Alt>Left\", \"<Control><Alt>Left\", \"<Control><Alt>KP_Left\"]"
        gsettings set org.gnome.desktop.wm.keybindings switch-to-workspace-right \
            "[\"<Super>Page_Down\", \"<Super><Alt>Right\", \"<Control><Alt>Right\", \"<Control><Alt>KP_Right\"]"
        # Wi-Fi and Bluetooth for GNOME, run on the Windows radios; needs this session bus, ends with it
        python3 -u "$GNOME_WINDOWS_DIR/gnomeradios.py" &
        exec $SHELL_CMD --wayland --no-x11 --wayland-display=gnome-nested'
    rc=$?
    # 0 / 143 (SIGTERM from "Exit GNOME" or the stop script) = asked to quit; a long run = was in use
    if [ $rc -eq 0 ] || [ $rc -eq 143 ] || [ $(( $(date +%s) - started )) -gt 20 ]; then
        break
    fi
    sleep 3
done
