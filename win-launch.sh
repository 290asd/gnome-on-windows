#!/bin/sh
# Launches a Windows app through the Windows shell; winbridge.py then brings its window into GNOME.
# Called from the generated GNOME launchers as: win-launch.sh <base64 of the Start-menu AppID>
id=$(printf %s "$1" | base64 -d) || exit 1
[ -n "$id" ] || exit 1
# tells winbridge.py this app was started from GNOME, so it may un-hide its Store-app window
echo "$(date +%s) $id" >> "$(dirname "$(readlink -f "$0")")/gnome-launch.txt"
exec /mnt/c/Windows/explorer.exe "shell:AppsFolder\\$id"
