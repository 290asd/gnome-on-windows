#!/usr/bin/env python3
"""GNOME side of the Wi-Fi and Bluetooth bridge: NetworkManager and BlueZ, as much of them as GNOME uses,
run on Windows' own Wi-Fi and Bluetooth (winradios.py on Windows does the work).

GNOME's quick settings and its Settings panels find Wi-Fi and Bluetooth through NetworkManager and BlueZ on
the system bus. WSL has no radios, so this takes their bus names (setup-radios.sh allows that, once),
answers with what winradios.py reports and passes every action on to it. GNOME's Bluetooth toggle also
needs GNOME Settings Daemon's rfkill service on the session bus, so that one is here too.

    python3 gnomeradios.py [port]      gnome-nested.sh starts it, inside GNOME's session bus

ponytail: one Wi-Fi device and one Bluetooth adapter, no wired devices, VPNs or hotspots; GNOME can't
edit a saved Wi-Fi network's settings (Windows keeps them), only connect, disconnect and forget it.
"""
import hashlib
import json
import socket
import sys
import threading
import time
import uuid

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

PROPS, OM = "org.freedesktop.DBus.Properties", "org.freedesktop.DBus.ObjectManager"
o, u = dbus.ObjectPath, dbus.UInt32


def ao(paths):
    return dbus.Array(paths, signature="o")


def strs(items):
    return dbus.Array(items, signature="s")


def error(name, message):
    return dbus.exceptions.DBusException(message, name=name)


class Obj(dbus.service.Object):
    """An object with properties, props = {interface: {name: D-Bus typed value}}."""

    def __init__(self, conn, path, props):
        super().__init__(conn, path)
        self.path, self.props = path, props

    @dbus.service.method(PROPS, in_signature="ss", out_signature="v")
    def Get(self, iface, name):
        return self.props[iface][name]

    @dbus.service.method(PROPS, in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return self.props.get(iface, {})

    @dbus.service.method(PROPS, in_signature="ssv")
    def Set(self, iface, name, value):
        self.set(name, value)

    def set(self, name, value):
        raise error("org.freedesktop.DBus.Error.PropertyReadOnly", f"{name} is read-only")

    @dbus.service.signal(PROPS, signature="sa{sv}as")
    def PropertiesChanged(self, iface, changed, invalidated):
        pass

    def update(self, iface, **values):
        changed = {k: v for k, v in values.items() if self.props[iface].get(k) != v}
        if changed:
            self.props[iface].update(changed)
            self.PropertiesChanged(iface, changed, strs([]))


class Root(dbus.service.Object):
    """The object manager: clients load every object from it and follow additions and removals."""

    def __init__(self, conn, path):
        super().__init__(conn, path)
        self.objects = {}

    @dbus.service.method(OM, out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        return {path: obj.props for path, obj in self.objects.items()}

    @dbus.service.signal(OM, signature="oa{sa{sv}}")
    def InterfacesAdded(self, path, interfaces):
        pass

    @dbus.service.signal(OM, signature="oas")
    def InterfacesRemoved(self, path, interfaces):
        pass

    def add(self, obj):
        self.objects[obj.path] = obj
        self.InterfacesAdded(obj.path, obj.props)
        return obj

    def remove(self, obj):
        del self.objects[obj.path]
        self.InterfacesRemoved(obj.path, strs(obj.props))
        obj.remove_from_connection()


class Windows:
    """The connection from winradios.py: state reports in, commands out (each answered by a reply), PIN
    questions in and their answers out."""

    def __init__(self, port):
        self.sock, self.calls, self.ids = None, {}, 0
        self.on_state = self.on_ask = None
        threading.Thread(target=self.serve, args=(port,), daemon=True).start()

    def send(self, **msg):
        try:
            self.sock.sendall((json.dumps(msg) + "\n").encode())
            return True
        except (OSError, AttributeError):
            return False

    def call(self, op, done=None, **args):
        """Run op on Windows; done(None, or what went wrong) gets the answer."""
        self.ids += 1
        if done:
            self.calls[self.ids] = done
        if not self.send(op=op, id=self.ids, **args):
            self.calls.pop(self.ids, None)
            if done:
                done("Windows' side (winradios.py) is not running")

    def serve(self, port):
        srv = socket.create_server(("127.0.0.1", port))
        while True:
            conn, _ = srv.accept()
            GLib.idle_add(self.connected, conn)
            try:
                for line in conn.makefile("r", encoding="utf-8"):
                    GLib.idle_add(self.received, json.loads(line))
            except (OSError, ValueError):
                pass
            GLib.idle_add(self.lost, conn)

    def connected(self, conn):
        print("winradios.py connected")
        self.sock = conn

    def received(self, msg):
        if msg["t"] == "state":
            self.on_state(msg)
        elif msg["t"] == "reply":
            done = self.calls.pop(msg["id"], None)
            if done:
                done(msg["err"])
        elif msg["t"] == "ask":
            self.on_ask(msg)

    def lost(self, conn):
        if self.sock is not conn:
            return
        print("winradios.py went away")
        self.sock = None
        for done in self.calls.values():
            done("winradios.py went away")
        self.calls.clear()
        self.on_state({})  # nothing known: no Wi-Fi, no Bluetooth


# --- NetworkManager ---

NM = "org.freedesktop.NetworkManager"
NMP = "/org/freedesktop/NetworkManager"
DEVICE, WIRELESS = NM + ".Device", NM + ".Device.Wireless"
AP, ACTIVE = NM + ".AccessPoint", NM + ".Connection.Active"
SETTINGS, CONNECTION = NM + ".Settings", NM + ".Settings.Connection"
AGENT = NM + ".SecretAgent"

# security (winradios.py's names) -> the access point's Flags (privacy), WpaFlags, RsnFlags
PSK, EAP, SAE, OWE = 0x100, 0x200, 0x400, 0x800
TKIP, CCMP = 0x4 | 0x40, 0x8 | 0x80  # pairwise and group cipher
AP_FLAGS = {"none": (0, 0, 0), "wep": (1, 0, 0), "wpa": (1, PSK | TKIP, 0), "wpa2": (1, 0, PSK | CCMP),
            "sae": (1, 0, SAE | CCMP), "owe": (1, 0, OWE | CCMP), "wpa-eap": (1, EAP | TKIP, 0),
            "wpa2-eap": (1, 0, EAP | CCMP), "wpa3-eap": (1, 0, EAP | CCMP)}
KEY_MGMT = {"wep": "none", "wpa": "wpa-psk", "wpa2": "wpa-psk", "sae": "sae", "owe": "owe", "wpa-eap": "wpa-eap",
            "wpa2-eap": "wpa-eap", "wpa3-eap": "wpa-eap"}
PERSONAL = {"wep", "wpa", "wpa2", "sae"}  # networks GNOME asks the password of itself
PERMISSIONS = ["enable-disable-network", "enable-disable-wifi", "sleep-wake", "network-control",
               "wifi.share.protected", "wifi.share.open", "settings.modify.own", "settings.modify.system",
               "settings.modify.hostname", "reload", "wifi.scan"]
# NetworkManager's device states, and the reasons used here
UNAVAILABLE, DISCONNECTED, CONFIG, NEED_AUTH, ACTIVATED, FAILED = 20, 30, 50, 60, 100, 120
NO_SECRETS = 7
ACTIVATING, ACTIVE_ON, DEACTIVATED = 1, 2, 4


def boottime_ms():
    return int(time.clock_gettime(time.CLOCK_BOOTTIME) * 1000)


def ssid_name(ssid):
    """A network's name for its new Windows profile: the SSID as text, or in hex if it isn't text."""
    try:
        return bytes.fromhex(ssid).decode()
    except UnicodeDecodeError:
        return ssid


def bssid(ssid, sec):
    """A made-up, steady access point address: Windows lists networks, not their access points, and libnm
    matches no saved connection to an access point without an address."""
    return ":".join(f"{b:02X}" for b in b"\x02" + hashlib.md5(f"{ssid} {sec}".encode()).digest()[:5])


def password(secrets):
    """The password or key in secrets from GNOME, or None."""
    wsec, eap = secrets.get("802-11-wireless-security", {}), secrets.get("802-1x", {})
    secret = wsec.get("psk") or wsec.get("wep-key0") or eap.get("password")
    return str(secret) if secret else None


class NMManager(Obj):
    def __init__(self, nm):
        self.nm = nm
        super().__init__(nm.conn, NMP, {NM: {
            "Devices": ao([]), "AllDevices": ao([]), "Checkpoints": ao([]), "NetworkingEnabled": True,
            "WirelessEnabled": False, "WirelessHardwareEnabled": True, "WwanEnabled": False,
            "WwanHardwareEnabled": False, "WimaxEnabled": False, "WimaxHardwareEnabled": False,
            "RadioFlags": u(0), "ActiveConnections": ao([]), "PrimaryConnection": o("/"),
            "PrimaryConnectionType": "", "Metered": u(0), "ActivatingConnection": o("/"), "Startup": False,
            "Version": "1.46.0", "Capabilities": dbus.Array([], signature="u"), "State": u(20),
            "Connectivity": u(1), "ConnectivityCheckAvailable": False, "ConnectivityCheckEnabled": False,
            "ConnectivityCheckUri": "", "GlobalDnsConfiguration": dbus.Dictionary({}, signature="sv")}})

    @dbus.service.method(NM, out_signature="ao")
    def GetDevices(self):
        return self.props[NM]["Devices"]

    @dbus.service.method(NM, out_signature="ao")
    def GetAllDevices(self):
        return self.props[NM]["Devices"]

    @dbus.service.method(NM, out_signature="a{ss}")
    def GetPermissions(self):
        return {f"{NM}.{p}": "yes" for p in PERMISSIONS}

    @dbus.service.method(NM, out_signature="u")
    def state(self):
        return self.props[NM]["State"]

    @dbus.service.method(NM, out_signature="u")
    def CheckConnectivity(self):
        return self.props[NM]["Connectivity"]

    @dbus.service.method(NM, in_signature="ooo", out_signature="o", async_callbacks=("ok", "err"))
    def ActivateConnection(self, conn, device, ap, ok, err):
        self.nm.activate(conn, {}, ap, lambda c, a: ok(a), err)

    @dbus.service.method(NM, in_signature="a{sa{sv}}oo", out_signature="oo", async_callbacks=("ok", "err"))
    def AddAndActivateConnection(self, settings, device, ap, ok, err):
        self.nm.activate(None, settings, ap, ok, err)

    @dbus.service.method(NM, in_signature="a{sa{sv}}ooa{sv}", out_signature="ooa{sv}", async_callbacks=("ok", "err"))
    def AddAndActivateConnection2(self, settings, device, ap, options, ok, err):
        self.nm.activate(None, settings, ap, lambda c, a: ok(c, a, dbus.Dictionary({}, signature="sv")), err)

    @dbus.service.method(NM, in_signature="o", async_callbacks=("ok", "err"))
    def DeactivateConnection(self, active, ok, err):
        self.nm.disconnect(ok, err)

    @dbus.service.signal(NM, signature="u")
    def StateChanged(self, state):
        pass

    @dbus.service.signal(NM, signature="o")
    def DeviceAdded(self, path):
        pass

    @dbus.service.signal(NM, signature="o")
    def DeviceRemoved(self, path):
        pass

    def set(self, name, value):
        if name != "WirelessEnabled":
            super().set(name, value)
        self.nm.win.call("wifi-power", on=bool(value))


class NMSettings(Obj):
    def __init__(self, nm):
        super().__init__(nm.conn, NMP + "/Settings", {SETTINGS: {
            "Connections": ao([]), "Hostname": socket.gethostname(), "CanModify": True, "VersionId": dbus.UInt64(0)}})

    @dbus.service.method(SETTINGS, out_signature="ao")
    def ListConnections(self):
        return self.props[SETTINGS]["Connections"]

    @dbus.service.method(SETTINGS, in_signature="a{sa{sv}}", out_signature="o")
    def AddConnection(self, settings):
        raise error(NM + ".Settings.Failed", "Windows keeps the Wi-Fi networks: connect to it instead")

    @dbus.service.signal(SETTINGS, signature="o")
    def NewConnection(self, path):
        pass

    @dbus.service.signal(SETTINGS, signature="o")
    def ConnectionRemoved(self, path):
        pass


class NMAgentManager(Obj):
    """Where GNOME Shell registers its secret agent, which asks the user for Wi-Fi passwords."""

    def __init__(self, nm):
        self.nm = nm
        super().__init__(nm.conn, NMP + "/AgentManager", {NM + ".AgentManager": {}})

    @dbus.service.method(NM + ".AgentManager", in_signature="s", sender_keyword="sender")
    def Register(self, identifier, sender):
        self.nm.agents[sender] = identifier

    @dbus.service.method(NM + ".AgentManager", in_signature="su", sender_keyword="sender")
    def RegisterWithCapabilities(self, identifier, capabilities, sender):
        self.nm.agents[sender] = identifier

    @dbus.service.method(NM + ".AgentManager", sender_keyword="sender")
    def Unregister(self, sender):
        self.nm.agents.pop(sender, None)


class NMDevice(Obj):
    def __init__(self, nm, path):
        self.nm = nm
        super().__init__(nm.conn, path, {
            DEVICE: {"Udi": "/sys/class/net/wlan0", "Path": "", "Interface": "wlan0", "IpInterface": "wlan0",
                     "Driver": "winradios", "DriverVersion": "", "FirmwareVersion": "", "Capabilities": u(1),
                     "Ip4Address": u(0), "State": u(DISCONNECTED), "StateReason": dbus.Struct((u(DISCONNECTED), u(0))),
                     "ActiveConnection": o("/"), "Ip4Config": o("/"), "Dhcp4Config": o("/"), "Ip6Config": o("/"),
                     "Dhcp6Config": o("/"), "Managed": True, "Autoconnect": True, "FirmwareMissing": False,
                     "NmPluginMissing": False, "DeviceType": u(2), "AvailableConnections": ao([]),
                     "PhysicalPortId": "", "Mtu": u(1500), "Metered": u(0),
                     "LldpNeighbors": dbus.Array([], signature="a{sv}"), "Real": True, "Ip4Connectivity": u(0),
                     "Ip6Connectivity": u(0), "InterfaceFlags": u(0x10001), "HwAddress": "", "Ports": ao([])},
            # WEP, TKIP, CCMP, WPA, RSN, 2.4 and 5 GHz: everything Windows can join
            WIRELESS: {"HwAddress": "", "PermHwAddress": "", "Mode": u(2), "Bitrate": u(0), "AccessPoints": ao([]),
                       "ActiveAccessPoint": o("/"), "WirelessCapabilities": u(0x73F), "LastScan": dbus.Int64(-1)}})

    @dbus.service.method(DEVICE, async_callbacks=("ok", "err"))
    def Disconnect(self, ok, err):
        self.nm.disconnect(ok, err)

    @dbus.service.method(WIRELESS, out_signature="ao")
    def GetAccessPoints(self):
        return self.props[WIRELESS]["AccessPoints"]

    @dbus.service.method(WIRELESS, out_signature="ao")
    def GetAllAccessPoints(self):
        return self.props[WIRELESS]["AccessPoints"]

    @dbus.service.method(WIRELESS, in_signature="a{sv}")
    def RequestScan(self, options):
        self.nm.win.call("wifi-scan")

    @dbus.service.signal(DEVICE, signature="uuu")
    def StateChanged(self, new, old, reason):
        pass

    def set_state(self, state, reason=0):
        old = self.props[DEVICE]["State"]
        if state != old:
            self.update(DEVICE, State=u(state), StateReason=dbus.Struct((u(state), u(reason))))
            self.StateChanged(u(state), old, u(reason))


class NMConnection(Obj):
    """A Windows profile of a network in sight, as a saved NetworkManager connection."""

    def __init__(self, nm, path, name, net):
        self.nm, self.name, self.net = nm, name, net
        sec, key_mgmt = net["sec"], KEY_MGMT.get(net["sec"])
        self.uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "gnome-windows-wifi:" + name))
        self.settings = {"connection": {"id": name, "uuid": self.uuid, "type": "802-11-wireless"},
                         "802-11-wireless": {"ssid": dbus.ByteArray(bytes.fromhex(net["ssid"])),
                                             "mode": "infrastructure"},
                         "ipv4": {"method": "auto"}, "ipv6": {"method": "auto"}}
        if key_mgmt:
            self.settings["802-11-wireless-security"] = {"key-mgmt": key_mgmt}
        if key_mgmt == "wpa-eap":
            self.settings["802-1x"] = {"eap": strs(["peap"]), "phase2-auth": "mschapv2"}
        if sec == "wep":
            self.settings["802-11-wireless-security"]["wep-key-type"] = u(1)
        super().__init__(nm.conn, path, {CONNECTION: {"Unsaved": False, "Flags": u(0), "Filename": ""}})

    @dbus.service.method(CONNECTION, out_signature="a{sa{sv}}")
    def GetSettings(self):
        return self.settings

    @dbus.service.method(CONNECTION, in_signature="s", out_signature="a{sa{sv}}")
    def GetSecrets(self, setting):
        return dbus.Dictionary({}, signature="sa{sv}")  # Windows doesn't hand out saved passwords

    @dbus.service.method(CONNECTION, async_callbacks=("ok", "err"))
    def Delete(self, ok, err):
        self.nm.win.call("wifi-forget", lambda e: err(error(NM + ".Settings.Failed", e)) if e else ok(),
                         profile=self.name)

    @dbus.service.method(CONNECTION, in_signature="a{sa{sv}}")
    def Update(self, settings):
        raise error(NM + ".Settings.Failed", "Windows keeps this network's settings: change them there")

    @dbus.service.signal(CONNECTION)
    def Removed(self):
        pass


class NMActive(Obj):
    def __init__(self, nm, path, conn, ap):
        self.name = conn.name
        super().__init__(nm.conn, path, {ACTIVE: {
            "Connection": o(conn.path), "SpecificObject": o(ap), "Id": conn.name, "Uuid": conn.uuid,
            "Type": "802-11-wireless", "Devices": ao([nm.device.path]), "State": u(ACTIVATING), "StateFlags": u(0),
            "Default": False, "Ip4Config": o("/"), "Dhcp4Config": o("/"), "Default6": False, "Ip6Config": o("/"),
            "Dhcp6Config": o("/"), "Vpn": False, "Controller": o("/"), "Master": o("/")}})

    @dbus.service.signal(ACTIVE, signature="uu")
    def StateChanged(self, state, reason):
        pass

    def set_state(self, state, reason=0):
        if state != self.props[ACTIVE]["State"]:
            self.update(ACTIVE, State=u(state), Default=state == ACTIVE_ON)
            self.StateChanged(u(state), u(reason))


class NetworkManager:
    """NetworkManager with one Wi-Fi device: Windows' Wi-Fi. Its networks in sight are access points, their
    Windows profiles saved connections, the one Windows is connected to the active connection."""

    def __init__(self, win):
        self.win = win
        self.conn = dbus.SystemBus(private=True)
        self.root = Root(self.conn, "/org/freedesktop")
        self.manager = self.root.add(NMManager(self))
        self.settings = self.root.add(NMSettings(self))
        self.root.add(NMAgentManager(self))
        self.agents = {}  # bus name -> identifier of the registered secret agents
        self.device, self.aps, self.conns, self.active = None, {}, {}, None
        self.wifi, self.online, self.scans, self.n = None, False, None, 0
        self.pending = None  # the network GNOME is connecting to: {name, net, state}
        self.name = dbus.service.BusName(NM, self.conn, do_not_queue=True)  # last: clients look as soon as it shows up

    def new_path(self, kind):
        self.n += 1
        return f"{NMP}/{kind}/{self.n}"

    def sync(self, wifi, online):
        """Bring the objects in line with Windows' Wi-Fi state (None: no Wi-Fi) and GNOME's connection
        attempt, if any."""
        self.wifi, self.online = wifi, online
        if not wifi:
            for objects in (self.aps, self.conns):
                for obj in objects.values():
                    self.root.remove(obj)
                objects.clear()
            self.pending = None
            self.set_active(None, 3)  # device disconnected
            if self.device:
                self.root.remove(self.device)
                self.manager.DeviceRemoved(self.device.path)
                self.device = None
        else:
            if not self.device:
                self.device = self.root.add(NMDevice(self, NMP + "/Devices/1"))
                self.manager.DeviceAdded(self.device.path)
            self.sync_wifi(wifi)
        path = self.active.path if self.active else "/"
        activated = bool(self.active) and not self.pending
        state = 70 if online else 40 if self.pending else 20  # connected (global) / connecting / disconnected
        old_state = self.manager.props[NM]["State"]
        self.manager.update(
            NM, Devices=ao([self.device.path] if self.device else []), AllDevices=ao([self.device.path] if self.device else []),
            WirelessEnabled=bool(wifi and wifi["on"]), RadioFlags=u(1 if wifi else 0),
            ActiveConnections=ao([path] if self.active else []),
            PrimaryConnection=o(path if activated else "/"),
            PrimaryConnectionType="802-11-wireless" if activated else "",
            ActivatingConnection=o(path if self.pending else "/"), State=u(state),
            Connectivity=u(4 if online else 1))  # full / none: WSL's network is Windows'
        if state != old_state:
            self.manager.StateChanged(u(state))

    def sync_wifi(self, wifi):
        nets = {(n["ssid"], n["sec"]): n for n in wifi["nets"]}
        for key in self.aps.keys() - nets.keys():
            self.root.remove(self.aps.pop(key))
        for key, net in nets.items():
            strength = dbus.Byte(min(net["signal"], 100))
            if key not in self.aps:
                flags, wpa, rsn = AP_FLAGS[net["sec"]]
                self.aps[key] = self.root.add(Obj(self.conn, self.new_path("AccessPoint"), {AP: {
                    "Flags": u(flags), "WpaFlags": u(wpa), "RsnFlags": u(rsn),
                    "Ssid": dbus.ByteArray(bytes.fromhex(net["ssid"])), "Frequency": u(0), "HwAddress": bssid(*key),
                    "Mode": u(2), "MaxBitrate": u(0), "Bandwidth": u(20), "Strength": strength,
                    "LastSeen": dbus.Int32(boottime_ms() // 1000)}}))
            elif self.aps[key].props[AP]["Strength"] != strength:
                self.aps[key].update(AP, Strength=strength, LastSeen=dbus.Int32(boottime_ms() // 1000))

        # saved connections: the Windows profiles of the networks in sight, and the one being joined
        profiles = {n["profile"]: n for n in wifi["nets"] if n["profile"]}
        if self.pending:
            profiles.setdefault(self.pending["name"], self.pending["net"])
        for name in self.conns.keys() - profiles.keys():
            conn = self.conns.pop(name)
            conn.Removed()
            self.settings.ConnectionRemoved(conn.path)
            self.root.remove(conn)
        for name, net in profiles.items():
            if name not in self.conns:
                self.conns[name] = self.root.add(NMConnection(self, self.new_path("Settings"), name, net))
                self.settings.NewConnection(self.conns[name].path)
        conn_paths = ao(sorted(c.path for c in self.conns.values()))
        self.settings.update(SETTINGS, Connections=conn_paths)

        # the active connection: the network being joined, else the one Windows is connected to
        connected = next((n for n in wifi["nets"] if n["connected"] and n["profile"]), None)
        name = self.pending["name"] if self.pending else connected and connected["profile"]
        if self.active and self.active.name != name:
            self.set_active(None, DEACTIVATED)
        if name and not self.active:
            net = self.pending["net"] if self.pending else connected
            ap = self.aps.get((net["ssid"], net["sec"]))
            self.active = self.root.add(NMActive(self, self.new_path("ActiveConnection"), self.conns[name],
                                                 ap.path if ap else "/"))
        if self.active:
            self.active.set_state(ACTIVATING if self.pending else ACTIVE_ON)
        ap = self.active and self.active.props[ACTIVE]["SpecificObject"]

        if self.pending:
            state = self.pending["state"]
        else:
            state = ACTIVATED if self.active else DISCONNECTED if wifi["on"] else UNAVAILABLE
        self.device.set_state(state)
        if wifi["scans"] != self.scans:  # GNOME waits for LastScan to change after asking for a scan
            self.scans = wifi["scans"]
            self.device.update(WIRELESS, LastScan=dbus.Int64(boottime_ms()))
        self.device.update(DEVICE, ActiveConnection=o(self.active.path if self.active else "/"),
                           AvailableConnections=conn_paths)
        self.device.update(WIRELESS, AccessPoints=ao(sorted(a.path for a in self.aps.values())),
                           ActiveAccessPoint=o(ap or "/"))

    def set_active(self, active, reason):
        if self.active:
            self.active.set_state(DEACTIVATED, reason)
            self.root.remove(self.active)
        self.active = active

    def activate(self, conn_path, settings, ap_path, ok, err):
        """Join a network: a saved one (conn_path) or the one access point ap_path is (settings may hold its
        password). ok(connection path, active connection path) once it is under way."""
        fail = lambda message: err(error(NM + ".UnknownConnection", message))  # noqa: E731
        if not self.device or not self.wifi["on"]:
            return fail("Wi-Fi is off")
        if self.pending:
            return fail("already connecting to a network")
        if conn_path:
            name = next((n for n, c in self.conns.items() if c.path == conn_path), None)
            net = name and next((n for n in self.wifi["nets"] if n["profile"] == name), None)
        else:  # the access point, or a network of that name
            key = next((k for k, a in self.aps.items() if a.path == ap_path), None)
            ssid = bytes(settings.get("802-11-wireless", {}).get("ssid", b"")).hex()
            net = next((n for n in self.wifi["nets"] if (n["ssid"], n["sec"]) == key or n["ssid"] == ssid), None)
            name = net and (net["profile"] or ssid_name(net["ssid"]))
        if not net:
            return fail("that network is not in sight")
        if self.active and self.active.name == name:
            return ok(o(self.conns[name].path), o(self.active.path))
        self.pending = {"name": name, "net": net, "state": CONFIG}
        self.sync(self.wifi, self.online)
        ok(o(self.conns[name].path), o(self.active.path))
        given = password(settings)
        self.attempt(given, asked=given is not None)

    def attempt(self, secret, asked):
        """Have Windows join the pending network; ask GNOME for the password if it needs one (or if the saved
        one fails), once."""
        p = self.pending
        sec = p["net"]["sec"]

        def done(e):
            if not e:
                return self.finish(None)
            print(f"connecting to {p['name']}: {e}")
            if not asked and sec in PERSONAL:  # no password yet ("secrets"), or the saved one failed
                return self.ask_secrets(request_new=e != "secrets")
            self.finish(1)

        p["state"] = CONFIG
        self.sync(self.wifi, self.online)
        self.win.call("wifi-connect", done, ssid=p["net"]["ssid"], sec=sec, name=p["name"], password=secret)

    def ask_secrets(self, request_new):
        """GNOME Shell's agent shows its password dialog; flags: allow interaction, (and ask anew)."""
        if not self.agents:
            return self.finish(NO_SECRETS)
        self.pending["state"] = NEED_AUTH
        self.sync(self.wifi, self.online)
        conn = self.conns[self.pending["name"]]
        setting = "802-1x" if "802-1x" in conn.settings else "802-11-wireless-security"

        def got(secrets):
            secret = password(secrets)
            self.attempt(secret, asked=True) if secret else self.finish(NO_SECRETS)

        self.conn.call_async(list(self.agents)[-1], NMP + "/SecretAgent", AGENT, "GetSecrets", "a{sa{sv}}osasu",
                             (conn.settings, o(conn.path), setting, strs([]), u(1 | (2 if request_new else 0))),
                             got, lambda e: self.finish(NO_SECRETS), timeout=600)

    def finish(self, reason):
        """The attempt is over: joined (reason None), or failed."""
        p, self.pending = self.pending, None
        if not p or not self.device:  # Wi-Fi went away meanwhile
            return
        if reason is None:  # show it joined now, not only on Windows' next report
            for net in self.wifi["nets"]:
                net["connected"] = (net["ssid"], net["sec"]) == (p["net"]["ssid"], p["net"]["sec"])
                if net["connected"]:
                    net["profile"] = p["name"]
        else:
            self.device.set_state(FAILED, reason)
            self.set_active(None, 9 if reason == NO_SECRETS else 1)
        self.sync(self.wifi, self.online)

    def disconnect(self, ok, err):
        self.win.call("wifi-disconnect", lambda e: err(error(NM + ".Failed", e)) if e else ok())


# --- BlueZ ---

BLUEZ, ADAPTER_PATH = "org.bluez", "/org/bluez/hci0"
ADAPTER, DEVICE1, AGENT1 = "org.bluez.Adapter1", "org.bluez.Device1", "org.bluez.Agent1"
RFKILL = "org.gnome.SettingsDaemon.Rfkill"
AUDIO = ["0000110b-0000-1000-8000-00805f9b34fb", "0000110e-0000-1000-8000-00805f9b34fb",  # A2DP sink, AVRCP
         "0000111e-0000-1000-8000-00805f9b34fb"]  # hands-free
HID, HOGP = "00001124-0000-1000-8000-00805f9b34fb", "00001812-0000-1000-8000-00805f9b34fb"


def device_kind(cls, appearance):
    """(icon, service UUIDs) from a classic device's class or an LE device's appearance. GNOME shows only
    devices with services it can connect (audio, input) in its Bluetooth menu."""
    major, minor = cls >> 8 & 0x1F, cls >> 2 & 0x3F
    if major == 4:
        return {1: "audio-headset", 2: "audio-headset", 5: "audio-speakers", 6: "audio-headphones"}.get(
            minor, "audio-card"), AUDIO
    if major == 5:  # peripheral: keyboard, pointing device, both, or neither (gamepads and such)
        return {1: "input-keyboard", 2: "input-mouse", 3: "input-keyboard"}.get(minor >> 4, "input-gaming"), [HID]
    category, sub = appearance >> 6, appearance & 0x3F
    if category == 0x0F:  # human interface device
        return {1: "input-keyboard", 2: "input-mouse"}.get(sub, "input-gaming"), [HOGP]
    if category in (0x21, 0x25):  # audio sink, wearable audio
        return "audio-headphones", AUDIO
    return {1: "computer", 2: "phone"}.get(major, "phone" if category == 1 else "computer" if category == 2 else ""), []


def bluez_error(e):
    return error("org.bluez.Error.Failed", e)


class BluezManager(Obj):
    """/org/bluez: where GNOME Settings registers its pairing agent."""

    def __init__(self, bluez):
        self.bluez = bluez
        super().__init__(bluez.conn, "/org/bluez", {"org.bluez.AgentManager1": {}})

    @dbus.service.method("org.bluez.AgentManager1", in_signature="os", sender_keyword="sender")
    def RegisterAgent(self, path, capability, sender):
        self.bluez.agent = (sender, path)

    @dbus.service.method("org.bluez.AgentManager1", in_signature="o")
    def UnregisterAgent(self, path):
        if self.bluez.agent and self.bluez.agent[1] == path:
            self.bluez.agent = None

    @dbus.service.method("org.bluez.AgentManager1", in_signature="o")
    def RequestDefaultAgent(self, path):
        pass


class BluezAdapter(Obj):
    def __init__(self, bluez):
        self.bluez = bluez
        super().__init__(bluez.conn, ADAPTER_PATH, {ADAPTER: {
            "Address": "", "AddressType": "public", "Name": "", "Alias": "", "Class": u(0x10C), "Powered": False,
            "PowerState": "off", "Discoverable": False, "DiscoverableTimeout": u(180), "Pairable": True,
            "PairableTimeout": u(0), "Discovering": False, "UUIDs": strs([]), "Modalias": "",
            "Roles": strs(["central"])}})

    @dbus.service.method(ADAPTER)
    def StartDiscovery(self):
        self.bluez.win.call("bt-discover", on=True)

    @dbus.service.method(ADAPTER)
    def StopDiscovery(self):
        self.bluez.win.call("bt-discover", on=False)

    @dbus.service.method(ADAPTER, in_signature="a{sv}")
    def SetDiscoveryFilter(self, options):
        pass

    @dbus.service.method(ADAPTER, out_signature="as")
    def GetDiscoveryFilters(self):
        return strs([])

    @dbus.service.method(ADAPTER, in_signature="o", async_callbacks=("ok", "err"))
    def RemoveDevice(self, path, ok, err):
        addr = path.rpartition("/dev_")[2].replace("_", ":")
        self.bluez.win.call("bt-remove", lambda e: err(bluez_error(e)) if e else ok(), addr=addr)

    def set(self, name, value):
        if name == "Powered":
            self.bluez.win.call("bt-power", on=bool(value))
        elif name in ("Alias", "Discoverable", "Pairable", "DiscoverableTimeout", "PairableTimeout"):
            self.update(ADAPTER, **{name: value})  # nothing on Windows: GNOME just sees it taken
        else:
            super().set(name, value)


class BluezDevice(Obj):
    def __init__(self, bluez, path, addr):
        self.bluez, self.addr = bluez, addr
        super().__init__(bluez.conn, path, {DEVICE1: {}})

    def run(self, op, ok, err, **args):
        self.bluez.win.call(op, lambda e: err(bluez_error(e)) if e else ok(), addr=self.addr, **args)

    @dbus.service.method(DEVICE1, async_callbacks=("ok", "err"))
    def Connect(self, ok, err):
        self.run("bt-connect", ok, err, on=True)

    @dbus.service.method(DEVICE1, async_callbacks=("ok", "err"))
    def Disconnect(self, ok, err):
        self.run("bt-connect", ok, err, on=False)

    @dbus.service.method(DEVICE1, in_signature="s", async_callbacks=("ok", "err"))
    def ConnectProfile(self, profile, ok, err):
        self.run("bt-connect", ok, err, on=True)

    @dbus.service.method(DEVICE1, in_signature="s", async_callbacks=("ok", "err"))
    def DisconnectProfile(self, profile, ok, err):
        self.run("bt-connect", ok, err, on=False)

    @dbus.service.method(DEVICE1, async_callbacks=("ok", "err"))
    def Pair(self, ok, err):
        self.run("bt-pair", ok, err)

    @dbus.service.method(DEVICE1)
    def CancelPairing(self):
        pass

    def set(self, name, value):
        if name not in ("Trusted", "Alias", "Blocked", "WakeAllowed"):
            super().set(name, value)
        self.update(DEVICE1, **{name: value})  # nothing on Windows: GNOME just sees it taken


class Rfkill(Obj):
    """GNOME Settings Daemon's rfkill service, for Bluetooth only: GNOME's Bluetooth toggle shows only when
    it is there, and turns Bluetooth on and off through it."""

    def __init__(self, bluez, session):
        self.bluez = bluez
        super().__init__(session, "/org/gnome/SettingsDaemon/Rfkill", {RFKILL: {
            "AirplaneMode": False, "HardwareAirplaneMode": False, "HasAirplaneMode": False,
            "ShouldShowAirplaneMode": False, "BluetoothAirplaneMode": False, "BluetoothHardwareAirplaneMode": False,
            "BluetoothHasAirplaneMode": False}})
        self.name = dbus.service.BusName(RFKILL, session, do_not_queue=True)  # owned while referenced

    def set(self, name, value):
        if name == "BluetoothAirplaneMode":
            self.bluez.win.call("bt-power", on=not value)
        elif name != "AirplaneMode":  # no airplane mode (HasAirplaneMode false): ignored
            super().set(name, value)


class BlueZ:
    """BlueZ with one adapter: Windows' Bluetooth. Its paired devices, and while GNOME searches, those nearby."""

    def __init__(self, win, session):
        self.win = win
        self.conn = dbus.SystemBus(private=True)
        self.root = Root(self.conn, "/")
        self.root.add(BluezManager(self))
        self.agent = None  # (bus name, path) of GNOME Settings' pairing agent
        self.adapter, self.devices = None, {}
        self.rfkill = Rfkill(self, session)
        self.name = dbus.service.BusName(BLUEZ, self.conn, do_not_queue=True)

    def sync(self, bt):
        if not bt:
            for dev in self.devices.values():
                self.root.remove(dev)
            self.devices.clear()
            if self.adapter:
                self.root.remove(self.adapter)
                self.adapter = None
            self.rfkill.update(RFKILL, BluetoothHasAirplaneMode=False)
            return
        if not self.adapter:
            self.adapter = self.root.add(BluezAdapter(self))
        on = bt["on"]
        self.adapter.update(ADAPTER, Address=bt["addr"], Name=bt["name"], Alias=bt["name"], Powered=on,
                            PowerState="on" if on else "off", Discovering=bt["discovering"])
        self.rfkill.update(RFKILL, BluetoothHasAirplaneMode=True, BluetoothAirplaneMode=not on)
        devs = {d["addr"]: d for d in bt["devs"]}
        for addr in self.devices.keys() - devs.keys():
            self.root.remove(self.devices.pop(addr))
        for addr, d in devs.items():
            icon, uuids = device_kind(d["cls"], d["appearance"])
            props = {"Address": addr, "AddressType": "public", "Name": d["name"], "Alias": d["name"], "Icon": icon,
                     "Paired": d["paired"], "Bonded": d["paired"], "Trusted": d["paired"], "Blocked": False,
                     "LegacyPairing": False, "Connected": d["connected"], "ServicesResolved": d["connected"],
                     "UUIDs": strs(uuids), "Adapter": o(ADAPTER_PATH)}
            if d["cls"]:
                props["Class"] = u(d["cls"])
            if d["appearance"]:
                props["Appearance"] = dbus.UInt16(d["appearance"])
            if addr in self.devices:
                self.devices[addr].update(DEVICE1, **props)
            else:
                dev = BluezDevice(self, f"{ADAPTER_PATH}/dev_{addr.replace(':', '_')}", addr)
                dev.props[DEVICE1] = props
                self.devices[addr] = self.root.add(dev)

    def ask(self, msg):
        """A PIN question from Windows' pairing, for GNOME Settings' agent: compare a passkey, type one in,
        or show one to type on the device."""
        answer = lambda ok, pin="": self.win.send(op="answer", id=msg["id"], ok=ok, pin=pin)  # noqa: E731
        dev = self.devices.get(msg["addr"])
        if not self.agent or not dev:
            return answer(False)
        owner, path = self.agent

        def call(method, signature, args, reply):
            self.conn.call_async(owner, path, AGENT1, method, signature, args, reply, lambda e: answer(False),
                                 timeout=90)

        if msg["kind"] == "confirm":
            call("RequestConfirmation", "ou", (o(dev.path), u(int(msg["pin"]))), lambda: answer(True))
        elif msg["kind"] == "pin":
            call("RequestPinCode", "o", (o(dev.path),), lambda pin: answer(True, str(pin)))
        else:  # display: Windows goes on as soon as it is shown
            call("DisplayPinCode", "os", (o(dev.path), msg["pin"]), lambda: None)
            answer(True)


def main():
    DBusGMainLoop(set_as_default=True)
    win = Windows(int(sys.argv[1]) if len(sys.argv) > 1 else 47802)
    session = dbus.SessionBus()
    session.set_exit_on_disconnect(True)  # GNOME's session is over: let the next one have the bus names
    try:
        nm, bluez = NetworkManager(win), BlueZ(win, session)
    except dbus.exceptions.DBusException as e:
        sys.exit(f"gnomeradios.py: {e.get_dbus_message()}\n  NetworkManager still running? Run: sudo sh setup-radios.sh")
    win.on_state = lambda msg: (nm.sync(msg.get("wifi"), msg.get("online", False)), bluez.sync(msg.get("bt")))
    win.on_ask = bluez.ask
    print("NetworkManager and BlueZ for GNOME, waiting for winradios.py")
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
