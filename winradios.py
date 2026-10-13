"""Windows side of the Wi-Fi and Bluetooth bridge: GNOME's Wi-Fi and Bluetooth settings (gnomeradios.py in
WSL stands in for NetworkManager and BlueZ there) work on Windows' own radios through this.

Wi-Fi through the native Wi-Fi API (wlanapi): the networks in sight, Windows' saved profiles, connecting
with a new password. Both radios on and off through Windows.Devices.Radios. Bluetooth devices through
device enumeration (paired ones always, nearby ones while GNOME searches), pairing with GNOME asking the
PIN questions, and audio devices connected and disconnected the way Windows' own Settings does it.

    py winradios.py [port]      needs: pip install winrt-runtime winrt-Windows.Devices.Radios
                                       winrt-Windows.Devices.Enumeration winrt-Windows.Foundation
                                       winrt-Windows.Foundation.Collections
"""
import asyncio
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import sys
import time
from xml.sax.saxutils import escape

from winrt.system import unbox_boolean, unbox_guid, unbox_string, unbox_uint16
from winrt.windows.devices.enumeration import (DeviceInformation, DeviceInformationKind, DevicePairingKinds,
                                               DevicePairingResultStatus, DeviceUnpairingResultStatus)
from winrt.windows.devices.radios import Radio, RadioAccessStatus, RadioKind, RadioState

H = wt.HANDLE
wlanapi = ctypes.WinDLL("wlanapi")
ole32 = ctypes.OleDLL("ole32")  # HRESULT failures raise OSError
wininet = ctypes.WinDLL("wininet")


class GUID(ctypes.Structure):
    _fields_ = [("a", wt.DWORD), ("b", wt.WORD), ("c", wt.WORD), ("d", ctypes.c_ubyte * 8)]


def guid(text):
    g = GUID()
    ole32.CLSIDFromString(text, ctypes.byref(g))
    return g


# --- Wi-Fi ---

class DOT11_SSID(ctypes.Structure):
    _fields_ = [("len", wt.ULONG), ("ssid", ctypes.c_ubyte * 32)]


class WLAN_INTERFACE_INFO(ctypes.Structure):
    _fields_ = [("guid", GUID), ("desc", wt.WCHAR * 256), ("state", ctypes.c_int)]


class WLAN_AVAILABLE_NETWORK(ctypes.Structure):
    _fields_ = [("profile", wt.WCHAR * 256), ("ssid", DOT11_SSID), ("bss_type", ctypes.c_int), ("bssids", wt.ULONG),
                ("connectable", wt.BOOL), ("not_connectable_reason", wt.DWORD), ("phys", wt.ULONG),
                ("phy", ctypes.c_int * 8), ("more_phys", wt.BOOL), ("signal", wt.ULONG), ("secure", wt.BOOL),
                ("auth", ctypes.c_int), ("cipher", ctypes.c_int), ("flags", wt.DWORD), ("reserved", wt.DWORD)]


class WLAN_CONNECTION_PARAMETERS(ctypes.Structure):
    _fields_ = [("mode", ctypes.c_int), ("profile", wt.LPCWSTR), ("ssid", ctypes.c_void_p), ("bssids", ctypes.c_void_p),
                ("bss_type", ctypes.c_int), ("flags", wt.DWORD)]


class WLAN_NOTIFICATION_DATA(ctypes.Structure):
    _fields_ = [("source", wt.DWORD), ("code", wt.DWORD), ("guid", GUID), ("size", wt.DWORD), ("data", ctypes.c_void_p)]


class WLAN_CONNECTION_NOTIFICATION_DATA(ctypes.Structure):
    _fields_ = [("mode", ctypes.c_int), ("profile", wt.WCHAR * 256), ("ssid", DOT11_SSID), ("bss_type", ctypes.c_int),
                ("secure", wt.BOOL), ("reason", wt.DWORD)]


NOTIFY = ctypes.WINFUNCTYPE(None, ctypes.POINTER(WLAN_NOTIFICATION_DATA), ctypes.c_void_p)
GUIDP = ctypes.POINTER(GUID)
wlanapi.WlanOpenHandle.argtypes = [wt.DWORD, ctypes.c_void_p, ctypes.POINTER(wt.DWORD), ctypes.POINTER(H)]
wlanapi.WlanEnumInterfaces.argtypes = [H, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
wlanapi.WlanGetAvailableNetworkList.argtypes = [H, GUIDP, wt.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
wlanapi.WlanScan.argtypes = [H, GUIDP, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
wlanapi.WlanConnect.argtypes = [H, GUIDP, ctypes.POINTER(WLAN_CONNECTION_PARAMETERS), ctypes.c_void_p]
wlanapi.WlanDisconnect.argtypes = [H, GUIDP, ctypes.c_void_p]
wlanapi.WlanSetProfile.argtypes = [H, GUIDP, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.BOOL, ctypes.c_void_p,
                                   ctypes.POINTER(wt.DWORD)]
wlanapi.WlanDeleteProfile.argtypes = [H, GUIDP, wt.LPCWSTR, ctypes.c_void_p]
wlanapi.WlanRegisterNotification.argtypes = [H, wt.DWORD, wt.BOOL, NOTIFY, ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.POINTER(wt.DWORD)]
wlanapi.WlanFreeMemory.argtypes = [ctypes.c_void_p]


def wlan_items(ptr, cls):
    """Copies of the items of a WLAN_*_LIST (a count, an index, then the items), freeing the list."""
    n = wt.DWORD.from_address(ptr).value
    items = [cls.from_buffer_copy(item) for item in (cls * n).from_address(ptr + 8)]
    wlanapi.WlanFreeMemory(ptr)
    return items


# DOT11_AUTH_ALGORITHM -> the security, in gnomeradios.py's terms
AUTH = {1: "none", 2: "wep", 3: "wpa-eap", 4: "wpa", 6: "wpa2-eap", 7: "wpa2", 8: "wpa3-eap", 9: "sae", 10: "owe",
        11: "wpa3-eap"}
# security -> profile XML authentication and encryption, for the networks a password (or nothing) joins
PROFILE = {"none": ("open", "none"), "wep": ("open", "WEP"), "wpa": ("WPAPSK", "TKIP"), "wpa2": ("WPA2PSK", "AES"),
           "sae": ("WPA3SAE", "AES"), "owe": ("OWE", "AES")}


def profile_xml(name, ssid, sec, password):
    auth, enc = PROFILE[sec]
    key = ""
    if password:
        kind = "networkKey" if sec == "wep" or len(password) == 64 else "passPhrase"
        key = (f"<sharedKey><keyType>{kind}</keyType><protected>false</protected>"
               f"<keyMaterial>{escape(password)}</keyMaterial></sharedKey>")
    return ('<?xml version="1.0"?><WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">'
            f"<name>{escape(name)}</name><SSIDConfig><SSID><hex>{ssid.upper()}</hex></SSID></SSIDConfig>"
            "<connectionType>ESS</connectionType><connectionMode>auto</connectionMode><MSM><security>"
            f"<authEncryption><authentication>{auth}</authentication><encryption>{enc}</encryption>"
            f"<useOneX>false</useOneX></authEncryption>{key}</security></MSM></WLANProfile>")


class Wifi:
    """The first Wi-Fi interface. on_change() is called from a wlanapi thread on its notifications
    (scans, connecting, disconnecting)."""

    def __init__(self, on_change):
        self.h, self.on_change = H(), on_change
        self.scans = 0  # finished scans: gnomeradios.py tells GNOME a scan ended when this changes
        self.waiting = None  # (profile, done(reason)) of the connection attempt under way
        self.notify = NOTIFY(self.notified)  # kept referenced: wlanapi calls it
        wlanapi.WlanOpenHandle(2, None, ctypes.byref(wt.DWORD()), ctypes.byref(self.h))
        wlanapi.WlanRegisterNotification(self.h, 0x8, True, self.notify, None, None, None)  # ACM notifications
        self.do(wlanapi.WlanScan, None, None, None)  # until a scan of its own, a new client sees just its network

    def notified(self, data, _):
        d = data.contents
        if d.code in (7, 8):  # scan complete / failed
            self.scans += 1
        elif d.code == 10 and self.waiting:  # connection complete (or not: then the reason says why)
            c = WLAN_CONNECTION_NOTIFICATION_DATA.from_address(d.data)
            if c.profile == self.waiting[0]:
                self.waiting[1](c.reason)
        self.on_change()

    def interface(self):
        """(GUID, WLAN_INTERFACE_STATE) of the first Wi-Fi interface, or (None, 0)."""
        ptr = ctypes.c_void_p()
        if wlanapi.WlanEnumInterfaces(self.h, None, ctypes.byref(ptr)):
            return None, 0
        items = wlan_items(ptr.value, WLAN_INTERFACE_INFO)
        return (items[0].guid, items[0].state) if items else (None, 0)

    def state(self):
        """{busy, scans, nets: [{ssid (hex), sec, signal (%), profile, connected}]}, None without Wi-Fi."""
        guid_, st = self.interface()
        if not guid_:
            return None
        ptr, nets = ctypes.c_void_p(), {}
        if not wlanapi.WlanGetAvailableNetworkList(self.h, ctypes.byref(guid_), 0, None, ctypes.byref(ptr)):
            for n in wlan_items(ptr.value, WLAN_AVAILABLE_NETWORK):
                sec = "wep" if n.auth == 1 and n.secure else AUTH.get(n.auth)
                if not sec or n.bss_type != 1 or not n.ssid.len:  # infrastructure networks with a name only
                    continue
                ssid = bytes(n.ssid.ssid[:n.ssid.len]).hex()
                net = {"ssid": ssid, "sec": sec, "signal": n.signal, "profile": n.profile,
                       "connected": bool(n.flags & 1)}
                old = nets.get((ssid, sec))
                # listed once per saved profile and once without one: keep the one in use, or a saved one
                if not old or (net["connected"], bool(net["profile"])) > (old["connected"], bool(old["profile"])):
                    nets[ssid, sec] = net
        return {"busy": st in (3, 5, 6, 7), "scans": self.scans, "nets": list(nets.values())}

    def do(self, fn, *args):
        """wlanapi's fn(handle, interface, *args) on the interface: None, or what went wrong."""
        guid_, _ = self.interface()
        if not guid_:
            return "no Wi-Fi"
        err = fn(self.h, ctypes.byref(guid_), *args)
        return ctypes.FormatError(err) if err else None

    async def connect(self, ssid, sec, name, password):
        """Join network ssid (hex) with its saved profile, or with password as profile name. Returns None,
        "secrets" when it needs a password first, or why it failed."""
        guid_, _ = self.interface()
        if not guid_:
            return "no Wi-Fi"
        saved = next((n["profile"] for n in self.state()["nets"]
                      if n["ssid"] == ssid and n["sec"] == sec and n["profile"]), "")
        new = password is not None or not saved
        if new:
            if sec not in PROFILE:
                return "joining an enterprise network the first time works only in Windows' own settings"
            if password is None and sec not in ("none", "owe"):
                return "secrets"
            reason = wt.DWORD()
            if err := wlanapi.WlanSetProfile(self.h, ctypes.byref(guid_), 0, profile_xml(name, ssid, sec, password),
                                             None, True, None, ctypes.byref(reason)):
                return f"{ctypes.FormatError(err)} (reason {reason.value})"
        profile = name if new else saved
        loop = asyncio.get_running_loop()
        result = loop.create_future()
        self.waiting = (profile, lambda r: loop.call_soon_threadsafe(lambda: result.done() or result.set_result(r)))
        try:
            params = WLAN_CONNECTION_PARAMETERS(0, profile, None, None, 1, 0)  # by profile, infrastructure
            reason = wlanapi.WlanConnect(self.h, ctypes.byref(guid_), ctypes.byref(params), None) or \
                await asyncio.wait_for(result, 30)
        except TimeoutError:
            reason = "timed out"
        finally:
            self.waiting = None
        if not reason:
            return None
        if new:  # e.g. a wrong password: don't keep it
            wlanapi.WlanDeleteProfile(self.h, ctypes.byref(guid_), profile, None)
        return f"connecting failed ({reason:#x})" if isinstance(reason, int) else reason


def online():
    """Windows has some network connection (Wi-Fi or not); WSL's network goes through it."""
    return bool(wininet.InternetGetConnectedState(ctypes.byref(wt.DWORD()), 0))


async def set_radio(radio, on):
    if radio is None:
        return "no such radio"
    status = await radio.set_state_async(RadioState.ON if on else RadioState.OFF)
    return None if status == RadioAccessStatus.ALLOWED else f"Windows refused: {status.name}"


# --- Bluetooth ---

AEP = "System.Devices.Aep."
CLASSIC, LE = "{e0cbf06c-cd8b-4647-bb8a-263b43f0f974}", "{bb7bb05e-5972-42b5-94fc-76eaa7084d49}"
YES, NO = "System.StructuredQueryType.Boolean#True", "System.StructuredQueryType.Boolean#False"
PAIRED = f'({AEP}ProtocolId:="{CLASSIC}" OR {AEP}ProtocolId:="{LE}") AND {AEP}IsPaired:={YES}'
# classic devices answer only an inquiry; LE ones advertise
NEARBY = (f'(({AEP}ProtocolId:="{CLASSIC}" AND {AEP}Bluetooth.IssueInquiry:={YES}) OR {AEP}ProtocolId:="{LE}")'
          f' AND {AEP}IsPaired:={NO}')
PROPS = {"DeviceAddress": unbox_string, "IsConnected": unbox_boolean, "ContainerId": unbox_guid,
         "ProtocolId": unbox_guid, "Bluetooth.Cod.Major": unbox_uint16, "Bluetooth.Cod.Minor": unbox_uint16,
         "Bluetooth.Le.Appearance": unbox_uint16}


def prop(di, name):
    value = di.properties.get(AEP + name)
    try:
        return None if value is None else PROPS[name](value)
    except (TypeError, OSError):  # a different type than expected: not worth failing over
        return None


class Watch:
    """A running DeviceWatcher's devices (association endpoints), id -> DeviceInformation. Its events come on
    WinRT threads: changes are applied on the asyncio loop, then on_change() is called."""

    def __init__(self, aqs, loop, on_change):
        self.devs = {}

        def later(apply):
            return lambda _, arg: loop.call_soon_threadsafe(lambda: (apply(arg), on_change()))

        self.watcher = DeviceInformation.create_watcher_with_kind_aqs_filter_and_additional_properties(
            aqs, [AEP + p for p in PROPS], DeviceInformationKind.ASSOCIATION_ENDPOINT)
        self.watcher.add_added(later(lambda di: self.devs.__setitem__(di.id, di)))
        self.watcher.add_updated(later(lambda u: u.id in self.devs and self.devs[u.id].update(u)))
        self.watcher.add_removed(later(lambda u: self.devs.pop(u.id, None)))
        self.watcher.start()

    def stop(self):
        self.watcher.stop()


class Bluetooth:
    def __init__(self, loop, on_change):
        self.loop, self.on_change = loop, on_change
        self.paired = Watch(PAIRED, loop, on_change)
        self.nearby = None  # while GNOME searches for devices

    def devices(self):
        """All known devices, the paired ones last (they win on a duplicate address)."""
        return [*(self.nearby.devs.values() if self.nearby else ()), *self.paired.devs.values()]

    def find(self, addr):
        return next((di for di in reversed(self.devices()) if (prop(di, "DeviceAddress") or "").upper() == addr), None)

    def state(self, radio):
        if radio is None:
            return None
        devs, adapter = {}, "00:00:00:00:00:00"
        for di in self.devices():
            addr = (prop(di, "DeviceAddress") or "").upper()
            if not addr or not di.name:  # LE devices often advertise no name: nothing to show for those
                continue
            # an AEP id is "Bluetooth#Bluetooth<adapter address>-<device address>" (or BluetoothLE#...)
            adapter = (re.search(r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})-", di.id) or [0, adapter])[1].upper()
            major, minor = prop(di, "Bluetooth.Cod.Major"), prop(di, "Bluetooth.Cod.Minor")
            devs[addr] = {"addr": addr, "name": di.name, "paired": di.pairing.is_paired,
                          "connected": bool(prop(di, "IsConnected")),
                          "cls": (major << 8 | minor << 2) if major is not None and minor is not None else 0,
                          "appearance": prop(di, "Bluetooth.Le.Appearance") or 0,
                          "le": str(prop(di, "ProtocolId")) == LE.strip("{}")}
        return {"on": radio.state == RadioState.ON, "addr": adapter, "name": os.environ.get("COMPUTERNAME", "Windows"),
                "discovering": self.nearby is not None, "devs": list(devs.values())}

    def discover(self, on):
        if on and not self.nearby:
            self.nearby = Watch(NEARBY, self.loop, self.on_change)
        elif not on and self.nearby:
            self.nearby.stop()
            self.nearby = None

    async def pair(self, addr, ask):
        """Pair with Windows' own pairing; PIN questions go to ask(kind, addr, pin) -> (ok, pin)."""
        di = self.find(addr)
        if not di:
            return "device not found"

        def requested(_, args):  # on a WinRT thread
            kind, pin = args.pairing_kind, args.pin
            if kind == DevicePairingKinds.CONFIRM_ONLY:
                args.accept()
                return
            deferral = args.get_deferral()

            async def answer():
                try:
                    ok, reply = await ask(kind, addr, pin)
                    if ok:
                        args.accept_with_pin(reply) if kind == DevicePairingKinds.PROVIDE_PIN else args.accept()
                finally:
                    deferral.complete()

            asyncio.run_coroutine_threadsafe(answer(), self.loop)

        custom = di.pairing.custom
        token = custom.add_pairing_requested(requested)
        try:
            result = await custom.pair_async(DevicePairingKinds.CONFIRM_ONLY | DevicePairingKinds.DISPLAY_PIN |
                                             DevicePairingKinds.PROVIDE_PIN | DevicePairingKinds.CONFIRM_PIN_MATCH)
        finally:
            custom.remove_pairing_requested(token)
        ok = result.status in (DevicePairingResultStatus.PAIRED, DevicePairingResultStatus.ALREADY_PAIRED)
        return None if ok else f"pairing failed: {result.status.name}"

    async def remove(self, addr):
        di = self.find(addr)
        if not di:
            return "device not found"
        result = await di.pairing.unpair_async()
        ok = result.status in (DeviceUnpairingResultStatus.UNPAIRED, DeviceUnpairingResultStatus.ALREADY_UNPAIRED)
        return None if ok else f"removing failed: {result.status.name}"

    async def connect(self, addr, on):
        di = self.find(addr)
        if not di:
            return "device not found"
        if await asyncio.to_thread(bt_audio, str(prop(di, "ContainerId")), on):
            return None
        return "Windows connects only audio devices on request; others connect by themselves"


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wt.DWORD)]


class KSPROPERTY(ctypes.Structure):
    _fields_ = [("set", GUID), ("id", wt.ULONG), ("flags", wt.ULONG)]


ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
ole32.PropVariantClear.argtypes = [ctypes.c_void_p]
CLSID_MMDeviceEnumerator = guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IDeviceTopology = guid("{2A07407E-6497-4A18-9787-32F79BD0D98F}")
IID_IKsControl = guid("{28F54685-06FD-11D2-B27A-00A0C9223196}")
KSPROPSETID_BtAudio = guid("{7FA06C40-B8F6-4C7E-8556-E8C33A12E54D}")
PKEY_Device_ContainerId = PROPERTYKEY(guid("{8C7ED206-3F8A-4827-B3AB-AE9E1FAEFC6C}"), 2)


def com(obj, index, *args):
    """Call method number index of COM interface pointer obj."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *[ctypes.c_void_p] * len(args))(vtbl[index])(obj, *args)


def bt_audio(container, connect):
    """Connect or disconnect a Bluetooth audio device the way Windows' Settings does: ask the audio driver
    behind each of the device's sound endpoints (found by its container id) for a one-shot reconnect or
    disconnect (KSPROPSETID_BtAudio). Returns how many drivers took it; 0 for a device without audio."""
    ole32.CoInitializeEx(None, 0)  # a worker thread of its own: multithreaded apartment
    held, done, target = [], set(), guid("{%s}" % container)

    def get(obj, index, *args):  # an interface pointer out of a method, released at the end
        out = ctypes.c_void_p()
        com(obj, index, *args, ctypes.byref(out))
        held.append(out)
        return out

    try:
        enum = ctypes.c_void_p()
        ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, 0x17,
                               ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(enum))
        held.append(enum)
        endpoints, n = get(enum, 3, 2, 0xF), wt.UINT()  # EnumAudioEndpoints(eAll, any state)
        com(endpoints, 3, ctypes.byref(n))
        for i in range(n.value):
            try:
                endpoint = get(endpoints, 4, i)
                pv = (ctypes.c_byte * 24)()  # PROPVARIANT
                com(get(endpoint, 4, 0), 5, ctypes.byref(PKEY_Device_ContainerId), ctypes.byref(pv))
                ours = (ctypes.c_ushort.from_buffer(pv).value == 72 and  # VT_CLSID
                        bytes(GUID.from_address(ctypes.c_void_p.from_buffer(pv, 8).value)) == bytes(target))
                ole32.PropVariantClear(ctypes.byref(pv))
                if not ours:
                    continue
                # the endpoint's connector leads to the driver's filter; its IKsControl takes the request
                connector = get(get(endpoint, 3, ctypes.byref(IID_IDeviceTopology), 0x17, None), 4, 0)
                filter_id = ctypes.c_wchar_p()
                com(connector, 10, ctypes.byref(filter_id))  # GetDeviceIdConnectedTo
                fid = filter_id.value
                ole32.CoTaskMemFree(filter_id)
                if fid in done:
                    continue
                ks = get(get(enum, 5, ctypes.c_wchar_p(fid)), 3, ctypes.byref(IID_IKsControl), 0x17, None)
                request = KSPROPERTY(KSPROPSETID_BtAudio, 0 if connect else 1, 1)  # ONESHOT_RECONNECT / _DISCONNECT, GET
                com(ks, 3, ctypes.byref(request), ctypes.sizeof(request), None, 0, ctypes.byref(wt.ULONG()))
                done.add(fid)
            except OSError:  # e.g. an endpoint left over from an earlier driver: try the others
                continue
    finally:
        for obj in reversed(held):
            if obj:
                com(obj, 2)  # Release
    return len(done)


# --- the link to gnomeradios.py ---

PIN_QUESTIONS = {DevicePairingKinds.CONFIRM_PIN_MATCH: "confirm", DevicePairingKinds.DISPLAY_PIN: "display",
                 DevicePairingKinds.PROVIDE_PIN: "pin"}


class Bridge:
    """JSON lines with gnomeradios.py: the radios' state out whenever it changes (checked at least every
    2 s), commands in, each answered with a reply; PIN questions out, their answers in."""

    def __init__(self, port):
        self.port, self.writer = port, None
        self.asks, self.n_asks = {}, 0  # PIN questions waiting for their answers
        self.tasks = set()  # asyncio keeps only weak references to running tasks

    async def run(self):
        loop = asyncio.get_running_loop()
        self.changed = asyncio.Event()
        poke = lambda: loop.call_soon_threadsafe(self.changed.set)  # noqa: E731
        await Radio.request_access_async()
        self.radios = {r.kind: r for r in await Radio.get_radios_async()}
        for r in self.radios.values():
            r.add_state_changed(lambda *_: poke())
        self.wifi = Wifi(poke)
        self.bt = Bluetooth(loop, poke)
        print(f"Wi-Fi and Bluetooth -> localhost:{self.port}")
        last = time.monotonic()
        while time.monotonic() - last < 10:  # gnomeradios.py (GNOME) gone for 10 s: done
            try:
                reader, self.writer = await asyncio.open_connection("localhost", self.port)
            except OSError:
                await asyncio.sleep(1)
                continue
            print("connected")
            pusher = asyncio.create_task(self.push())
            try:
                while line := await reader.readline():
                    task = asyncio.create_task(self.handle(json.loads(line)))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
            except (OSError, ValueError) as e:
                print("connection lost:", e)
            pusher.cancel()
            self.writer.close()
            self.writer, last = None, time.monotonic()
            self.bt.discover(False)
        print("GNOME is gone, exiting")

    def send(self, **msg):
        if self.writer:
            self.writer.write((json.dumps(msg) + "\n").encode())

    def state(self):
        wifi = self.wifi.state()
        if wifi:
            radio = self.radios.get(RadioKind.WI_FI)
            wifi["on"] = radio is None or radio.state == RadioState.ON
        return {"online": online(), "wifi": wifi, "bt": self.bt.state(self.radios.get(RadioKind.BLUETOOTH))}

    async def push(self):
        sent = None
        while True:
            self.changed.clear()
            state = self.state()
            if state != sent:
                self.send(t="state", **state)
                sent = state
            try:
                await asyncio.wait_for(self.changed.wait(), 2)
            except TimeoutError:
                pass

    async def ask(self, kind, addr, pin):
        if kind not in PIN_QUESTIONS:
            return False, ""
        self.n_asks += 1
        n = self.n_asks
        self.asks[n] = asyncio.get_running_loop().create_future()
        self.send(t="ask", id=n, kind=PIN_QUESTIONS[kind], addr=addr, pin=pin)
        try:
            return await asyncio.wait_for(self.asks[n], 90)
        except TimeoutError:
            return False, ""
        finally:
            self.asks.pop(n, None)

    async def handle(self, msg):
        if msg["op"] == "answer":
            future = self.asks.get(msg["id"])
            if future and not future.done():
                future.set_result((bool(msg.get("ok")), msg.get("pin", "")))
            return
        try:
            err = await self.run_op(msg)
        except Exception as e:  # WinRT and COM failures: GNOME shows them, this keeps running
            err = f"{type(e).__name__}: {e}"
        if err:
            print(f"{msg['op']}: {err}")
        self.send(t="reply", id=msg["id"], err=err)
        self.changed.set()

    async def run_op(self, msg):
        match msg["op"]:
            case "wifi-power":
                return await set_radio(self.radios.get(RadioKind.WI_FI), msg["on"])
            case "wifi-scan":
                return self.wifi.do(wlanapi.WlanScan, None, None, None)
            case "wifi-connect":
                return await self.wifi.connect(msg["ssid"], msg["sec"], msg["name"], msg.get("password"))
            case "wifi-disconnect":
                return self.wifi.do(wlanapi.WlanDisconnect, None)
            case "wifi-forget":
                return self.wifi.do(wlanapi.WlanDeleteProfile, msg["profile"], None)
            case "bt-power":
                return await set_radio(self.radios.get(RadioKind.BLUETOOTH), msg["on"])
            case "bt-discover":
                return self.bt.discover(msg["on"])
            case "bt-pair":
                return await self.bt.pair(msg["addr"], self.ask)
            case "bt-remove":
                return await self.bt.remove(msg["addr"])
            case "bt-connect":
                return await self.bt.connect(msg["addr"], msg["on"])
        return f"unknown command {msg['op']}"


if __name__ == "__main__":
    asyncio.run(Bridge(int(sys.argv[1]) if len(sys.argv) > 1 else 47802).run())
