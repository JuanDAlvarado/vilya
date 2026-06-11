"""Wi-Fi Direct (P2P) control via NetworkManager's D-Bus API.

Why this backend exists: NetworkManager forcibly removes any P2P group
interface formed behind its back. When wpa_supplicant signals
GroupStarted, NM wraps the new interface object and, finding no NM
activation for it, drops the wrapper -- whose dispose() calls
RemoveInterface in wpa_supplicant (nm-supplicant-interface.c,
"NMSupplicantInterface is disposing"). The group dies ~100 ms after
forming. So on NM systems, the group must be NM's own.

This is the same approach gnome-network-displays takes. The supplicant
backend (supplicant.py) remains for systems without NM.

The connection profile uses a manual IP (192.168.137.1/24) so NM does
not need dnsmasq; vilya's own DHCP server hands the sink its lease.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import uuid as uuid_mod
from typing import Optional

from dbus_fast import Variant
from dbus_fast.aio import MessageBus
from dbus_fast import BusType

from .supplicant import Group, Peer
from .wfd_ie import build_wfd_ies, parse_device_info

log = logging.getLogger(__name__)

NM_SERVICE = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
NM_IFACE = "org.freedesktop.NetworkManager"
DEVICE_IFACE = "org.freedesktop.NetworkManager.Device"
P2P_DEVICE_IFACE = "org.freedesktop.NetworkManager.Device.WifiP2P"
PEER_IFACE = "org.freedesktop.NetworkManager.WifiP2PPeer"
ACTIVE_IFACE = "org.freedesktop.NetworkManager.Connection.Active"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

NM_DEVICE_TYPE_WIFI_P2P = 30
WPS_METHOD_PBC = 4  # NMSettingWirelessSecurityWpsMethod

# NMActiveConnectionState
NM_ACTIVE_STATE_ACTIVATED = 2
NM_ACTIVE_STATE_DEACTIVATED = 4

ACTIVATION_TIMEOUT = 120  # seconds; includes any accept-tap on the sink


class NMP2PDevice:
    """Drives NetworkManager's Wi-Fi P2P device. API mirrors P2PDevice."""

    def __init__(self, ifname: str = "wlan0") -> None:
        self.ifname = ifname  # parent wifi interface; NM names the P2P dev after it
        self._bus: Optional[MessageBus] = None
        self._nm = None
        self._device_path: Optional[str] = None
        self._p2p = None  # Device.WifiP2P proxy interface
        self._device_props = None

        self.peers: dict[str, Peer] = {}
        self._peer_found = asyncio.Event()
        self._active_path: Optional[str] = None

        # Populated after connect(): our address on the group interface,
        # and the sink's address when we are the P2P client (None when we
        # are GO and the sink must DHCP from us).
        self.local_ip: Optional[str] = None
        self.sink_ip: Optional[str] = None

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    async def open(self) -> None:
        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

        intro = await self._bus.introspect(NM_SERVICE, NM_PATH)
        nm_obj = self._bus.get_proxy_object(NM_SERVICE, NM_PATH, intro)
        self._nm = nm_obj.get_interface(NM_IFACE)

        self._device_path = await self._find_p2p_device()
        log.debug("P2P device: %s", self._device_path)

        intro = await self._bus.introspect(NM_SERVICE, self._device_path)
        dev_obj = self._bus.get_proxy_object(NM_SERVICE, self._device_path, intro)
        self._p2p = dev_obj.get_interface(P2P_DEVICE_IFACE)
        self._device_props = dev_obj.get_interface(PROPS_IFACE)

        self._p2p.on_peer_added(self._on_peer_added)

    async def close(self) -> None:
        if self._bus:
            self._bus.disconnect()

    async def _find_p2p_device(self) -> str:
        want_name = f"p2p-dev-{self.ifname}"
        for path in await self._nm.call_get_devices():
            intro = await self._bus.introspect(NM_SERVICE, path)
            obj = self._bus.get_proxy_object(NM_SERVICE, path, intro)
            props = obj.get_interface(PROPS_IFACE)
            dtype = (await props.call_get(DEVICE_IFACE, "DeviceType")).value
            if dtype != NM_DEVICE_TYPE_WIFI_P2P:
                continue
            name = (await props.call_get(DEVICE_IFACE, "Interface")).value
            if name == want_name:
                return path
        raise RuntimeError(
            f"NetworkManager has no Wi-Fi P2P device for {self.ifname} "
            f"(expected {want_name})"
        )

    async def set_device_name(self, name: str = "vilya") -> None:
        # NM owns P2PDeviceConfig; our name comes from NM's own config.
        pass

    async def set_wfd_ie(self) -> None:
        # The WFD IE rides in the connection profile (wifi-p2p.wfd-ies);
        # NM pushes it to wpa_supplicant during activation. For discovery
        # it is not required -- the sink advertises, we listen.
        pass

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def find(self) -> None:
        self.peers.clear()
        for path in (await self._device_props.call_get(P2P_DEVICE_IFACE, "Peers")).value:
            await self._add_peer(path)
        await self._p2p.call_start_find({})
        log.info("P2P find started (via NetworkManager)")

    async def stop_find(self) -> None:
        try:
            await self._p2p.call_stop_find()
        except Exception as exc:
            log.debug("StopFind: %s", exc)

    async def wait_for_peer(self, name_substring: str, timeout: float = 30) -> Peer:
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            for peer in self.peers.values():
                if name_substring.lower() in peer.name.lower():
                    return peer
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"No peer matching {name_substring!r} found in {timeout}s. "
                    f"Seen: {[p.name for p in self.peers.values()] or 'none'}"
                )
            self._peer_found.clear()
            try:
                await asyncio.wait_for(self._peer_found.wait(), remaining)
            except asyncio.TimeoutError:
                pass

    def _on_peer_added(self, path: str) -> None:
        asyncio.get_event_loop().create_task(self._add_peer(path))

    async def _add_peer(self, path: str) -> None:
        try:
            intro = await self._bus.introspect(NM_SERVICE, path)
            obj = self._bus.get_proxy_object(NM_SERVICE, path, intro)
            props = obj.get_interface(PROPS_IFACE)
            all_props = await props.call_get_all(PEER_IFACE)
        except Exception as exc:
            log.debug("Could not read peer %s: %s", path, exc)
            return

        name = all_props.get("Name", Variant("s", "?")).value
        hw = all_props.get("HwAddress", Variant("s", "")).value
        rtsp_port: Optional[int] = None
        ies_v = all_props.get("WfdIEs")
        if ies_v and ies_v.value:
            info = parse_device_info(bytes(ies_v.value))
            if info:
                rtsp_port = info["rtsp_port"]

        peer = Peer(path=path, name=name, address=hw.lower(), rtsp_port=rtsp_port)
        if path not in self.peers:
            log.info(
                "Peer found: %s (%s)%s",
                peer.name,
                peer.address,
                f" [WFD, RTSP port {rtsp_port}]" if rtsp_port else "",
            )
        self.peers[path] = peer
        self._peer_found.set()

    # ------------------------------------------------------------------
    # Group formation
    # ------------------------------------------------------------------

    async def connect(self, peer: Peer) -> Group:
        settings = {
            "connection": {
                "id": Variant("s", "vilya-p2p"),
                "uuid": Variant("s", str(uuid_mod.uuid4())),
                "type": Variant("s", "wifi-p2p"),
                "autoconnect": Variant("b", False),
                "zone": Variant("s", "trusted"),
            },
            "wifi-p2p": {
                "peer": Variant("s", peer.address.upper()),
                "wps-method": Variant("u", WPS_METHOD_PBC),
                "wfd-ies": Variant("ay", build_wfd_ies()),
            },
            # method=auto: against the Tab we land as P2P *client* (NM's
            # go_intent=7 loses to the Tab), and NM then uses the address
            # the GO assigns during the WPA handshake (EAPOL IP allocation,
            # e.g. 192.168.49.200) or falls back to DHCP client.
            "ipv4": {"method": Variant("s", "auto")},
            "ipv6": {"method": Variant("s", "ignore")},
        }
        options = {
            # Profile evaporates on deactivation; activation dies with us.
            "persist": Variant("s", "volatile"),
            "bind-activation": Variant("s", "dbus-client"),
        }

        log.info("Connecting to %s (%s) via NM...", peer.name, peer.address)
        result = await self._nm.call_add_and_activate_connection2(
            settings, self._device_path, peer.path, options
        )
        _conn_path, active_path, _res = result
        self._active_path = active_path

        await self._wait_activated(active_path)

        ip_iface = (
            await self._device_props.call_get(DEVICE_IFACE, "IpInterface")
        ).value
        if not ip_iface:
            raise RuntimeError("Activation succeeded but no group interface name")

        await self._verify_wfd_ie()
        await self._read_ip_config(ip_iface)
        role = "client" if self.sink_ip else "GO"
        group = Group(
            ifname=ip_iface,
            role=role,
            interface_path="",
            group_path="",
        )
        log.info(
            "Group started: %s (via NM, role=%s, us=%s, sink=%s)",
            group.ifname,
            role,
            self.local_ip,
            self.sink_ip or "pending DHCP",
        )
        return group

    async def _verify_wfd_ie(self) -> None:
        """Confirm NM actually pushed our WFD IE into wpa_supplicant.

        The sink only initiates the RTSP connection if our P2P frames
        carried a source WFD IE, so an empty value here explains a
        'sink never connected' failure. Requires root (which we are).
        """
        try:
            path = "/fi/w1/wpa_supplicant1"
            svc = "fi.w1.wpa_supplicant1"
            intro = await self._bus.introspect(svc, path)
            obj = self._bus.get_proxy_object(svc, path, intro)
            props = obj.get_interface(PROPS_IFACE)
            ies = bytes((await props.call_get(svc, "WFDIEs")).value)
            if ies:
                log.info("wpa_supplicant WFD IE active: %s", ies.hex())
            else:
                log.warning(
                    "wpa_supplicant has NO WFD IE set -- the sink will not "
                    "initiate RTSP. (NM should have pushed wifi-p2p.wfd-ies.)"
                )
        except Exception as exc:
            log.debug("Could not verify WFD IE: %s", exc)

    async def _read_ip_config(self, ifname: str) -> None:
        """Determine our address (and the GO's) on the group interface.

        Asks the kernel directly: NM's Ip4Config D-Bus property can lag
        behind the ACTIVATED state change, but the address is on the
        interface the moment IP configuration finishes.
        """
        for _ in range(20):  # up to ~5 s for the address to land
            out = subprocess.run(
                ["ip", "-j", "addr", "show", "dev", ifname],
                capture_output=True,
                text=True,
            )
            if out.returncode == 0:
                addrs = [
                    a
                    for link in json.loads(out.stdout or "[]")
                    for a in link.get("addr_info", [])
                    if a.get("family") == "inet"
                ]
                if addrs:
                    self.local_ip = addrs[0]["local"]
                    break
            await asyncio.sleep(0.25)
        if not self.local_ip:
            raise RuntimeError(f"No IPv4 address appeared on {ifname}")

        if not self.local_ip.endswith(".1"):
            # We are the P2P client; the GO is .1 by convention (Android
            # always takes x.y.z.1, e.g. 192.168.49.1 on the Tab).
            self.sink_ip = ".".join(self.local_ip.split(".")[:3]) + ".1"

    async def _wait_activated(self, active_path: str) -> None:
        intro = await self._bus.introspect(NM_SERVICE, active_path)
        obj = self._bus.get_proxy_object(NM_SERVICE, active_path, intro)
        active = obj.get_interface(ACTIVE_IFACE)
        props = obj.get_interface(PROPS_IFACE)

        loop = asyncio.get_event_loop()
        fut: asyncio.Future[None] = loop.create_future()

        def on_state(state: int, reason: int) -> None:
            log.debug("Activation state=%d reason=%d", state, reason)
            if state == NM_ACTIVE_STATE_ACTIVATED and not fut.done():
                fut.set_result(None)
            elif state == NM_ACTIVE_STATE_DEACTIVATED and not fut.done():
                fut.set_exception(
                    RuntimeError(f"P2P activation failed (reason={reason})")
                )

        active.on_state_changed(on_state)
        # The state may already be past the signal we subscribed for.
        state = (await props.call_get(ACTIVE_IFACE, "State")).value
        on_state(state, 0)

        try:
            await asyncio.wait_for(fut, ACTIVATION_TIMEOUT)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"P2P activation timed out after {ACTIVATION_TIMEOUT}s"
            ) from None
        finally:
            active.off_state_changed(on_state)

    async def disconnect(self) -> None:
        if not self._active_path:
            return
        try:
            await self._nm.call_deactivate_connection(self._active_path)
        except Exception as exc:
            log.debug("Deactivate: %s", exc)
        finally:
            self._active_path = None
