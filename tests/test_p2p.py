"""Tests for the P2P support modules (WFD IE builder, DHCP codec)."""

import socket
import struct

from vilya.p2p import dhcp
from vilya.p2p.wfd_ie import build_wfd_ies, parse_device_info


# ---------------------------------------------------------------------------
# WFD IE
# ---------------------------------------------------------------------------

class TestWFDIE:
    def test_default_blob(self):
        # Byte-identical to gnome-network-displays' known-working IE:
        # id=0, len=0x0006, dev_info 0x0090, RTSP 7236, 200 Mbps.
        assert build_wfd_ies() == bytes.fromhex("00000600901c4400c8")

    def test_roundtrip(self):
        info = parse_device_info(build_wfd_ies(rtsp_port=7236))
        assert info["device_type"] == 0  # source
        assert info["session_available"] == 1
        assert info["rtsp_port"] == 7236
        assert info["max_throughput_mbps"] == 200

    def test_parse_skips_unknown_subelements(self):
        # Unknown subelement (id 6, len 0x0002, 2-byte body), then device info.
        blob = bytes.fromhex("060002beef") + build_wfd_ies()
        assert parse_device_info(blob)["rtsp_port"] == 7236

    def test_parse_empty(self):
        assert parse_device_info(b"") == {}

    def test_parse_truncated(self):
        assert parse_device_info(bytes.fromhex("0006")) == {}


# ---------------------------------------------------------------------------
# DHCP codec
# ---------------------------------------------------------------------------

def make_request(msg_type: int, mac: bytes, xid: int = 0x1234,
                 requested_ip: str | None = None) -> bytes:
    head = struct.pack("BBBB", dhcp.BOOTREQUEST, 1, 6, 0)
    head += struct.pack(">I", xid)
    head += struct.pack(">HH", 0, 0x8000)  # broadcast flag
    head += socket.inet_aton("0.0.0.0") * 4
    head += mac + b"\x00" * (16 - len(mac))
    head += b"\x00" * 192
    head += dhcp.MAGIC_COOKIE
    opts = bytes([dhcp.OPT_MSG_TYPE, 1, msg_type])
    if requested_ip:
        opts += bytes([dhcp.OPT_REQUESTED_IP, 4]) + socket.inet_aton(requested_ip)
    opts += bytes([dhcp.OPT_END])
    return head + opts


MAC = bytes.fromhex("aabbcc445566")


class TestDHCPCodec:
    def test_parse_discover(self):
        pkt = dhcp.parse_packet(make_request(dhcp.DISCOVER, MAC))
        assert pkt.op == dhcp.BOOTREQUEST
        assert pkt.msg_type == dhcp.DISCOVER
        assert pkt.mac == "aa:bb:cc:44:55:66"
        assert pkt.xid == 0x1234

    def test_parse_rejects_garbage(self):
        try:
            dhcp.parse_packet(b"\x01\x02\x03")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")

    def test_offer_roundtrip(self):
        req = dhcp.parse_packet(make_request(dhcp.DISCOVER, MAC))
        reply = dhcp.parse_packet(dhcp.build_reply(req, dhcp.OFFER, "192.168.137.50"))
        assert reply.op == dhcp.BOOTREPLY
        assert reply.msg_type == dhcp.OFFER
        assert reply.yiaddr == "192.168.137.50"
        assert reply.xid == req.xid
        assert reply.chaddr == MAC
        assert reply.options[dhcp.OPT_SERVER_ID] == socket.inet_aton(dhcp.SERVER_IP)
        assert reply.options[dhcp.OPT_NETMASK] == socket.inet_aton(dhcp.NETMASK)


class TestDHCPServerLogic:
    def _server(self, leases: list) -> dhcp.DHCPServer:
        srv = dhcp.DHCPServer("test0", on_lease=lambda m, i: leases.append((m, i)))
        # Substitute the transport with a capture stub; no real socket.
        sent: list[bytes] = []

        class FakeTransport:
            def sendto(self, payload, addr):
                sent.append(payload)

            def close(self):
                pass

        srv._transport = FakeTransport()
        srv._sent = sent  # type: ignore[attr-defined]
        return srv

    def test_discover_then_request_yields_lease(self):
        leases: list = []
        srv = self._server(leases)

        srv.datagram_received(make_request(dhcp.DISCOVER, MAC), ("0.0.0.0", 68))
        offer = dhcp.parse_packet(srv._sent[0])
        assert offer.msg_type == dhcp.OFFER
        ip = offer.yiaddr

        srv.datagram_received(
            make_request(dhcp.REQUEST, MAC, requested_ip=ip), ("0.0.0.0", 68)
        )
        ack = dhcp.parse_packet(srv._sent[1])
        assert ack.msg_type == dhcp.ACK
        assert ack.yiaddr == ip
        assert leases == [("aa:bb:cc:44:55:66", ip)]

    def test_same_mac_keeps_same_ip(self):
        srv = self._server([])
        srv.datagram_received(make_request(dhcp.DISCOVER, MAC), ("0.0.0.0", 68))
        srv.datagram_received(make_request(dhcp.DISCOVER, MAC), ("0.0.0.0", 68))
        a = dhcp.parse_packet(srv._sent[0]).yiaddr
        b = dhcp.parse_packet(srv._sent[1]).yiaddr
        assert a == b

    def test_foreign_request_naks(self):
        srv = self._server([])
        srv.datagram_received(
            make_request(dhcp.REQUEST, MAC, requested_ip="10.0.0.99"),
            ("0.0.0.0", 68),
        )
        assert dhcp.parse_packet(srv._sent[0]).msg_type == dhcp.NAK
