#!/bin/sh
# Once, in WSL: lets gnomeradios.py stand in for NetworkManager and BlueZ, so that GNOME's Wi-Fi and
# Bluetooth settings work on Windows' own radios.
#   sudo sh setup-radios.sh
set -e
user=${SUDO_USER:?run it with sudo, as the user who runs GNOME}

# WSL's own network needs no NetworkManager (WSL sets it up), and its bus name is gnomeradios.py's now
systemctl disable --now NetworkManager.service NetworkManager-wait-online.service 2>/dev/null || true
systemctl mask NetworkManager.service

cat >/etc/dbus-1/system.d/gnome-windows-radios.conf <<EOF
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<!-- gnomeradios.py (gnome-windows): NetworkManager and BlueZ for GNOME, run on Windows' Wi-Fi and Bluetooth -->
<busconfig>
  <policy user="$user">
    <allow own="org.freedesktop.NetworkManager"/>
    <allow own="org.bluez"/>
    <!-- asking GNOME for Wi-Fi passwords and Bluetooth PINs -->
    <allow send_interface="org.freedesktop.NetworkManager.SecretAgent"/>
    <allow send_interface="org.bluez.Agent1"/>
  </policy>
</busconfig>
EOF

# GNOME's network menu reacts only if polkit lets the user control the network, and polkit doesn't count
# a session started by wsl.exe as an active local one
cat >/etc/polkit-1/rules.d/50-gnome-windows.rules <<EOF
polkit.addRule(function (action, subject) {
    if (action.id.indexOf("org.freedesktop.NetworkManager.") === 0 && subject.user === "$user")
        return polkit.Result.YES;
});
EOF
echo "Done. Restart GNOME: start-gnome.ps1"
