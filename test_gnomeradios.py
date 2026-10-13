#!/usr/bin/env python3
"""Checks gnomeradios.py joining a new Wi-Fi network the way GNOME Shell does it: no saved profile, so Windows
needs a password, Shell's agent asks for it, and the second try carries it. A stand-in plays winradios.py.

    dbus-run-session -- python3 test_gnomeradios.py      (in WSL; the session bus doubles as the system bus)
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time

os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = os.environ["DBUS_SESSION_BUS_ADDRESS"]
import dbus  # noqa: E402
import dbus.service  # noqa: E402
from dbus.mainloop.glib import DBusGMainLoop  # noqa: E402
from gi.repository import GLib  # noqa: E402

PORT = 47899
NM, NMP = "org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager"
NET = {"ssid": b"cafe".hex(), "sec": "wpa2", "signal": 70, "profile": "", "connected": False}


class Agent(dbus.service.Object):
    """GNOME Shell's password dialog, with the user typing hunter22."""
    asked = []

    @dbus.service.method(NM + ".SecretAgent", in_signature="a{sa{sv}}osasu", out_signature="a{sa{sv}}")
    def GetSecrets(self, connection, path, setting, hints, flags):
        self.asked.append((str(setting), str(connection[setting]["key-mgmt"])))
        return {setting: {"psk": "hunter22"}}


def stand_in(win, passwords):
    """winradios.py without a saved profile for the network: "secrets" until a password comes."""
    def send(**msg):
        win.sendall((json.dumps(msg) + "\n").encode())

    def state(net):
        send(t="state", online=True, wifi={"on": True, "busy": False, "scans": 0, "nets": [net]}, bt=None)

    state(NET)
    for line in win.makefile("r"):
        msg = json.loads(line)
        if msg["op"] == "wifi-connect":
            passwords.append(msg["password"])
            if msg["password"] is None:
                send(t="reply", id=msg["id"], err="secrets")
            else:
                state(dict(NET, profile=msg["name"], connected=True))
                send(t="reply", id=msg["id"], err=None)


def main():
    DBusGMainLoop(set_as_default=True)
    radios = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                            "gnomeradios.py"), str(PORT)])
    try:
        for _ in range(50):
            try:
                win = socket.create_connection(("127.0.0.1", PORT))
                break
            except OSError:
                time.sleep(0.1)
        passwords = []
        threading.Thread(target=stand_in, args=(win, passwords), daemon=True).start()
        bus = dbus.SessionBus()
        while not bus.name_has_owner(NM) or not dbus.Interface(bus.get_object(NM, NMP), NM).GetDevices():
            time.sleep(0.1)
        Agent(bus, NMP + "/SecretAgent")
        dbus.Interface(bus.get_object(NM, NMP + "/AgentManager"), NM + ".AgentManager").RegisterWithCapabilities("test", 0)
        device = bus.get_object(NM, NMP + "/Devices/1")
        props = dbus.Interface(device, "org.freedesktop.DBus.Properties")
        [ap] = props.Get(NM + ".Device.Wireless", "AccessPoints")
        loop = GLib.MainLoop()
        dbus.Interface(bus.get_object(NM, NMP), NM).AddAndActivateConnection(
            {}, device.object_path, ap, reply_handler=lambda *_: None, error_handler=lambda e: loop.quit())
        GLib.timeout_add(100, lambda: props.Get(NM + ".Device", "State") != 100 or loop.quit())
        GLib.timeout_add(5000, loop.quit)
        loop.run()

        assert Agent.asked == [("802-11-wireless-security", "wpa-psk")], Agent.asked
        assert passwords == [None, "hunter22"], passwords
        assert props.Get(NM + ".Device", "State") == 100  # activated
        manager = dbus.Interface(bus.get_object(NM, NMP), "org.freedesktop.DBus.Properties")
        assert manager.Get(NM, "PrimaryConnection") != "/"
        print("ok")
    finally:
        radios.terminate()


if __name__ == "__main__":
    main()
