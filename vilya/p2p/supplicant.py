"""Wi-Fi Direct (P2P) control via wpa_supplicant's D-Bus interface.

Drives fi.w1.wpa_supplicant1 directly on the system bus, the same
daemon NetworkManager already runs — but for P2P operations only, so
the station connection on wlan0 is left alone.

Requires root: the wpa_supplicant D-Bus policy only allows root to
send method calls.

Flow (mirrors what Windows does on Win+K):
    1. set_wfd_ie()      -- advertise ourselves as a Miracast source
    2. find()            -- P2P social-channel scan; peers stream in
    3. connect(peer)     -- PBC provision discovery + GO negotiation,
                            go_intent=15 so we become Group Owner
    4. GroupStarted      -- yields the new group interface (p2p-wlan0-N)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus

from .wfd_ie import build_wfd_ies, parse_device_info

log = logging.getLogger(__name__)

WPAS_SERVICE = "fi.w1.wpa_supplicant1"
WPAS_PATH = "/fi/w1/wpa_supplicant1"
WPAS_IFACE = "fi.w1.wpa_supplicant1"
IFACE_IFACE = "fi.w1.wpa_supplicant1.Interface"
P2P_IFACE = "fi.w1.wpa_supplicant1.Interface.P2PDevice"
PEER_IFACE = "fi.w1.wpa_supplicant1.Peer"
GROUP_IFACE = "fi.w1.wpa_supplicant1.Group"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

GO_INTENT = 15  # max -- we insist on being Group Owner, like Windows does
GROUP_FORMATION_TIMEOUT = 120  # seconds; includes user tapping "accept" on the sink


@dataclass
class Peer:
    """A discovered P2P peer."""

    path: str  # D-Bus object path
    name: str
    address: str  # MAC, colon-separated
    rtsp_port: Optional[int] = None  # from the peer's WFD IE, if it has one


@dataclass
class Group:
    """A formed P2P group."""

    ifname: str  # e.g. p2p-wlan0-0
    role: str  # "GO" or "client"
    interface_path: str
    group_path: str


def _mac(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


class P2PDevice:
    """Async wrapper around one wpa_supplicant P2P-capable interface."""

    def __init__(self, ifname: str = "wlan0") -> None:
        self.ifname = ifname
        self._bus: Optional[MessageBus] = None
        self._root = None  # proxy for /fi/w1/wpa_supplicant1
        self._iface = None  # proxy for the wlan0 interface object
        self._iface_path: Optional[str] = None

        self.peers: dict[str, Peer] = {}  # path -> Peer
        self._peer_found: asyncio.Event = asyncio.Event()
        self._group_fut: Optional[asyncio.Future] = None
        self._group: Optional[Group] = None
        self._failure: Optional[str] = None

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    async def open(self) -> None:
        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

        intro = await self._bus.introspect(WPAS_SERVICE, WPAS_PATH)
        root_obj = self._bus.get_proxy_object(WPAS_SERVICE, WPAS_PATH, intro)
        self._root = root_obj.get_interface(WPAS_IFACE)

        self._iface_path = await self._root.call_get_interface(self.ifname)
        log.debug("Interface object: %s", self._iface_path)

        intro = await self._bus.introspect(WPAS_SERVICE, self._iface_path)
        iface_obj = self._bus.get_proxy_object(
            WPAS_SERVICE, self._iface_path, intro
        )
        self._iface = iface_obj.get_interface(P2P_IFACE)

        self._iface.on_device_found(self._on_device_found)
        self._iface.on_device_lost(self._on_device_lost)
        self._iface.on_group_started(self._on_group_started)
        self._iface.on_group_formation_failure(self._on_formation_failure)
        self._iface.on_go_negotiation_failure(self._on_go_neg_failure)

    async def close(self) -> None:
        if self._bus:
            self._bus.disconnect()

    async def set_wfd_ie(self) -> None:
        """Advertise WFD source capability in our P2P frames."""
        ies = build_wfd_ies()
        root_props_obj = self._bus.get_proxy_object(
            WPAS_SERVICE,
            WPAS_PATH,
            await self._bus.introspect(WPAS_SERVICE, WPAS_PATH),
        )
        props = root_props_obj.get_interface(PROPS_IFACE)
        await props.call_set(WPAS_IFACE, "WFDIEs", Variant("ay", ies))
        log.info("WFD IE set: %s", ies.hex())

    async def set_device_name(self, name: str = "vilya") -> None:
        """Name shown in the sink's device picker."""
        props_obj = self._bus.get_proxy_object(
            WPAS_SERVICE,
            self._iface_path,
            await self._bus.introspect(WPAS_SERVICE, self._iface_path),
        )
        props = props_obj.get_interface(PROPS_IFACE)
        config = {
            "DeviceName": Variant("s", name),
            # WPS primary device type, 8 bytes: category 1 (Computer),
            # OUI 00:50:F2 type 4 (WPS), subcategory 1 (PC).
            "PrimaryDeviceType": Variant(
                "ay", bytes.fromhex("00010050f2040001")
            ),
        }
        try:
            await props.call_set(
                P2P_IFACE, "P2PDeviceConfig", Variant("a{sv}", config)
            )
        except Exception as exc:
            # Cosmetic (affects how we appear in pickers); never fatal.
            log.warning("Could not set P2P device config: %s", exc)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def find(self) -> None:
        """Start P2P discovery (social channels 1/6/11)."""
        self.peers.clear()
        await self._iface.call_find({"DiscoveryType": Variant("s", "social")})
        log.info("P2P find started")

    async def stop_find(self) -> None:
        await self._iface.call_stop_find()

    async def wait_for_peer(
        self, name_substring: str, timeout: float = 30
    ) -> Peer:
        """Block until a peer whose name contains ``name_substring`` appears."""
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
                pass  # loop re-checks and raises with the peer list

    # ------------------------------------------------------------------
    # Group formation
    # ------------------------------------------------------------------

    async def connect(self, peer: Peer) -> Group:
        """PBC-connect to ``peer`` and become GO. Returns the formed group."""
        loop = asyncio.get_event_loop()
        self._group_fut = loop.create_future()
        self._failure = None

        log.info("Connecting to %s (%s)...", peer.name, peer.address)
        await self._iface.call_connect(
            {
                "peer": Variant("o", peer.path),
                "wps_method": Variant("s", "pbc"),
                "go_intent": Variant("i", GO_INTENT),
                "persistent": Variant("b", False),
                "join": Variant("b", False),
            }
        )
        try:
            group = await asyncio.wait_for(
                self._group_fut, GROUP_FORMATION_TIMEOUT
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Group formation timed out after {GROUP_FORMATION_TIMEOUT}s"
                + (f" ({self._failure})" if self._failure else "")
            ) from None
        if isinstance(group, Exception):
            raise group
        return group

    async def disconnect(self) -> None:
        """Tear down the P2P group, if one was formed.

        Must target the *group's* interface object: calling Disconnect on
        the main interface when no group exists makes wpa_supplicant treat
        wlan0 itself as a group and drops the station connection.
        """
        if self._group is None:
            return
        try:
            intro = await self._bus.introspect(
                WPAS_SERVICE, self._group.interface_path
            )
            obj = self._bus.get_proxy_object(
                WPAS_SERVICE, self._group.interface_path, intro
            )
            await obj.get_interface(P2P_IFACE).call_disconnect()
        except Exception as exc:
            log.debug("P2P disconnect: %s", exc)
        finally:
            self._group = None

    # ------------------------------------------------------------------
    # Signal handlers (sync callbacks from dbus-fast; schedule async work)
    # ------------------------------------------------------------------

    def _on_device_found(self, path: str) -> None:
        asyncio.get_event_loop().create_task(self._add_peer(path))

    async def _add_peer(self, path: str) -> None:
        try:
            intro = await self._bus.introspect(WPAS_SERVICE, path)
            obj = self._bus.get_proxy_object(WPAS_SERVICE, path, intro)
            props = obj.get_interface(PROPS_IFACE)
            name_v = await props.call_get(PEER_IFACE, "DeviceName")
            addr_v = await props.call_get(PEER_IFACE, "DeviceAddress")
        except Exception as exc:
            log.debug("Could not read peer %s: %s", path, exc)
            return
        rtsp_port: Optional[int] = None
        try:
            ies_v = await props.call_get(PEER_IFACE, "IEs")
            info = parse_device_info(bytes(ies_v.value))
            if info:
                rtsp_port = info["rtsp_port"]
        except Exception as exc:
            log.debug("Could not read peer WFD IEs %s: %s", path, exc)
        peer = Peer(
            path=path,
            name=name_v.value,
            address=_mac(addr_v.value),
            rtsp_port=rtsp_port,
        )
        if path not in self.peers:
            log.info(
                "Peer found: %s (%s)%s",
                peer.name,
                peer.address,
                f" [WFD, RTSP port {rtsp_port}]" if rtsp_port else "",
            )
        self.peers[path] = peer
        self._peer_found.set()

    def _on_device_lost(self, path: str) -> None:
        peer = self.peers.pop(path, None)
        if peer:
            log.debug("Peer lost: %s", peer.name)

    def _on_group_started(self, properties: dict) -> None:
        asyncio.get_event_loop().create_task(self._finish_group(properties))

    async def _finish_group(self, properties: dict) -> None:
        role = properties["role"].value
        iface_path = properties["interface_object"].value
        group_path = properties["group_object"].value

        try:
            intro = await self._bus.introspect(WPAS_SERVICE, iface_path)
            obj = self._bus.get_proxy_object(WPAS_SERVICE, iface_path, intro)
            props = obj.get_interface(PROPS_IFACE)
            ifname_v = await props.call_get(IFACE_IFACE, "Ifname")
        except Exception as exc:
            # Group object vanished before we could read it -- it was torn
            # down right after starting (e.g. something bounced the netdev).
            self._fail(f"Group disappeared during startup: {exc}")
            return

        group = Group(
            ifname=ifname_v.value,
            role=role,
            interface_path=iface_path,
            group_path=group_path,
        )
        self._group = group
        log.info("Group started: %s (role=%s)", group.ifname, group.role)
        if self._group_fut and not self._group_fut.done():
            self._group_fut.set_result(group)

    def _on_formation_failure(self, reason: str) -> None:
        self._fail(f"Group formation failure: {reason}")

    def _on_go_neg_failure(self, properties: dict) -> None:
        status = properties.get("status")
        self._fail(f"GO negotiation failed (status={getattr(status, 'value', status)})")

    def _fail(self, msg: str) -> None:
        log.error(msg)
        self._failure = msg
        if self._group_fut and not self._group_fut.done():
            self._group_fut.set_result(RuntimeError(msg))
