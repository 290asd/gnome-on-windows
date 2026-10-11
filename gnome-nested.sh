#!/bin/bash
# Real GNOME Shell as a nested compositor inside one WSLg window, sized to the whole screen.
# Linux apps started with WAYLAND_DISPLAY=gnome-nested appear inside it.

# Right after the WSL VM boots WSLg reports a 640x480 placeholder screen, then switches to the real
# one when Windows connects; nested mutter dies on that switch, so wait for the real size first.
screen_size() { xrandr --display :0 2>/dev/null | awk '/ connected/ {split($3, a, "+"); print a[1]; exit}'; }
for _ in $(seq 60); do
    SIZE=$(screen_size)
    [ -n "$SIZE" ] && [ "$SIZE" != "640x480" ] && break
    sleep 0.5
done
export MUTTER_DEBUG_DUMMY_MODE_SPECS="${1:-${SIZE:-1920x1200}}"
export MUTTER_DEBUG_DUMMY_MONITOR_SCALES="${GNOME_SCALE:-2}"  # integer scale keeps it sharp
export MESA_SHADER_CACHE_DISABLE=true   # mixing d3d12/llvmpipe in one cache blacked out GNOME before
# Software rendering only. d3d12 (the Windows GPU) leaves nested mutter's window blank in WSLg, via
# EGL and GLX alike, and every attempt hung the GPU (LiveKernelEvent 141) until Windows froze.
export GALLIUM_DRIVER=llvmpipe
# llvmpipe defaults to one thread per core (22 here); syncing them for every frame cost more than
# it saved: 4 threads measured 160 % CPU vs 250 % for the same load
export LP_NUM_THREADS="${GNOME_LP_THREADS:-4}"
export XDG_SESSION_TYPE=wayland
export XDG_CURRENT_DESKTOP=GNOME
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
# GNOME-only launchers ("Exit GNOME"); kept out of ~/.local so WSLg doesn't add them to Windows' Start menu
export XDG_DATA_DIRS="$(dirname "$(readlink -f "$0")")/share:${XDG_DATA_DIRS:-/usr/share/ubuntu:/usr/local/share:/usr/share:/var/lib/snapd/desktop}"

win_pos() { DISPLAY=:0 xwininfo -id "$1" 2>/dev/null | awk '/Absolute upper-left X/ {x=$NF} /Absolute upper-left Y/ {y=$NF} END {print x "," y}'; }

fullscreen_when_shown() {
    for _ in $(seq 60); do
        sleep 0.5
        # WSLg's window manager publishes no client list, so look the window up in the X tree
        id=$(DISPLAY=:0 xwininfo -root -tree 2>/dev/null | awk '/"gnome-shell"/ {print $1; exit}')
        [ -n "$id" ] && break
    done
    [ -z "$id" ] && return
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
        exec gnome-shell --nested --wayland --no-x11 --wayland-display=gnome-nested'
    rc=$?
    # 0 / 143 (SIGTERM from "Exit GNOME" or the stop script) = asked to quit; a long run = was in use
    if [ $rc -eq 0 ] || [ $rc -eq 143 ] || [ $(( $(date +%s) - started )) -gt 20 ]; then
        break
    fi
    sleep 3
done
