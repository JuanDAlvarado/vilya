"""Minimal DHCP server for the P2P group interface.

Replaces dnsmasq: as Group Owner we must hand the sink an IP address
before it can reach our RTSP port. Implements just DISCOVER/OFFER and
REQUEST/ACK on one interface with a tiny static pool — all a single
Miracast sink ever needs.

Subnet matches what Windows ICS uses (192.168.137.0/24), the same
layout observed in the working Win->Tab capture.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import struct
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)

SERVER_IP = "192.168.137.1"
POOL_START = "192.168.137.50"
NETMASK = "255.255.255.0"
LEASE_SECONDS = 3600

DHCP_SERVER_PORT = 67
DHCP_CLIENT_PORT = 68
MAGIC_COOKIE = b"\x63\x82\x53\x63"

# BOOTP op codes
BOOTREQUEST = 1
BOOTREPLY = 2

# Option 53 message types
DISCOVER = 1
OFFER = 2
REQUEST = 3
ACK = 5
NAK = 6

OPT_NETMASK = 1
OPT_ROUTER = 3
OPT_REQUESTED_IP = 50
OPT_LEASE_TIME = 51
OPT_MSG_TYPE = 53
OPT_SERVER_ID = 54
OPT_END = 255


@dataclass
class DHCPPacket:
    op: int
    xid: int
    flags: int
    ciaddr: str
    yiaddr: str
    chaddr: bytes  # 6-byte MAC
    options: dict[int, bytes]

    @property
    def mac(self) -> str:
        return ":".join(f"{b:02x}" for b in self.chaddr)

    @property
    def msg_type(self) -> Optional[int]:
        t = self.options.get(OPT_MSG_TYPE)
        return t[0] if t else None


def parse_packet(data: bytes) -> DHCPPacket:
    if len(data) < 240 or data[236:240] != MAGIC_COOKIE:
        raise ValueError("Not a DHCP packet")
    op, _htype, hlen, _hops = struct.unpack_from("BBBB", data, 0)
    (xid,) = struct.unpack_from(">I", data, 4)
    (flags,) = struct.unpack_from(">H", data, 10)
    ciaddr = socket.inet_ntoa(data[12:16])
    yiaddr = socket.inet_ntoa(data[16:20])
    chaddr = data[28 : 28 + min(hlen, 6)]

    options: dict[int, bytes] = {}
    i = 240
    while i < len(data):
        code = data[i]
        if code == OPT_END:
            break
        if code == 0:  # pad
            i += 1
            continue
        if i + 1 >= len(data):
            break
        length = data[i + 1]
        options[code] = data[i + 2 : i + 2 + length]
        i += 2 + length

    return DHCPPacket(
        op=op,
        xid=xid,
        flags=flags,
        ciaddr=ciaddr,
        yiaddr=yiaddr,
        chaddr=chaddr,
        options=options,
    )


def build_reply(
    req: DHCPPacket,
    msg_type: int,
    yiaddr: str,
    server_ip: str = SERVER_IP,
    lease: int = LEASE_SECONDS,
) -> bytes:
    head = struct.pack(
        "BBBB", BOOTREPLY, 1, 6, 0
    )  # op, htype=ethernet, hlen=6, hops
    head += struct.pack(">I", req.xid)
    head += struct.pack(">HH", 0, req.flags)  # secs, flags (echo broadcast bit)
    head += socket.inet_aton("0.0.0.0")  # ciaddr
    head += socket.inet_aton(yiaddr)  # yiaddr
    head += socket.inet_aton(server_ip)  # siaddr
    head += socket.inet_aton("0.0.0.0")  # giaddr
    head += req.chaddr + b"\x00" * (16 - len(req.chaddr))  # chaddr
    head += b"\x00" * 64  # sname
    head += b"\x00" * 128  # file
    head += MAGIC_COOKIE

    opts = bytes([OPT_MSG_TYPE, 1, msg_type])
    opts += bytes([OPT_SERVER_ID, 4]) + socket.inet_aton(server_ip)
    opts += bytes([OPT_LEASE_TIME, 4]) + struct.pack(">I", lease)
    opts += bytes([OPT_NETMASK, 4]) + socket.inet_aton(NETMASK)
    opts += bytes([OPT_ROUTER, 4]) + socket.inet_aton(server_ip)
    opts += bytes([OPT_END])
    return head + opts


class DHCPServer(asyncio.DatagramProtocol):
    """One-interface DHCP server. Calls ``on_lease(mac, ip)`` on each ACK."""

    def __init__(
        self,
        ifname: str,
        on_lease: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.ifname = ifname
        self.on_lease = on_lease
        self.leases: dict[str, str] = {}  # mac -> ip
        self._next_ip = ipaddress.IPv4Address(POOL_START)
        self._transport: Optional[asyncio.DatagramTransport] = None

    async def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_BINDTODEVICE,
            self.ifname.encode() + b"\x00",
        )
        sock.bind(("0.0.0.0", DHCP_SERVER_PORT))
        sock.setblocking(False)

        loop = asyncio.get_event_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: self, sock=sock
        )
        log.info("DHCP server listening on %s", self.ifname)

    def stop(self) -> None:
        if self._transport:
            self._transport.close()

    def _allocate(self, mac: str) -> str:
        if mac in self.leases:
            return self.leases[mac]
        ip = str(self._next_ip)
        self._next_ip += 1
        self.leases[mac] = ip
        return ip

    # -- DatagramProtocol callbacks ------------------------------------

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        try:
            req = parse_packet(data)
        except ValueError:
            return
        if req.op != BOOTREQUEST:
            return

        if req.msg_type == DISCOVER:
            ip = self._allocate(req.mac)
            log.info("DHCP DISCOVER from %s -> offering %s", req.mac, ip)
            self._send(build_reply(req, OFFER, ip))
        elif req.msg_type == REQUEST:
            ip = self._allocate(req.mac)
            requested = req.options.get(OPT_REQUESTED_IP)
            if requested and socket.inet_ntoa(requested) != ip:
                log.info("DHCP REQUEST for foreign IP from %s -> NAK", req.mac)
                self._send(build_reply(req, NAK, "0.0.0.0"))
                return
            log.info("DHCP ACK %s -> %s", req.mac, ip)
            self._send(build_reply(req, ACK, ip))
            if self.on_lease:
                self.on_lease(req.mac, ip)

    def _send(self, payload: bytes) -> None:
        # Always broadcast: the client has no usable unicast address yet.
        assert self._transport is not None
        self._transport.sendto(
            payload, ("255.255.255.255", DHCP_CLIENT_PORT)
        )
