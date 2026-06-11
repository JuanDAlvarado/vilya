"""WFD (Wi-Fi Display) Information Element construction.

The WFD IE rides inside P2P management frames (probe responses, GO
negotiation, etc.) and is how a peer knows we are a Miracast *source*
rather than a generic laptop. Samsung's Second Screen will not engage
with a peer that does not carry this IE.

Spec reference: Wi-Fi Display Technical Specification v2.1, table 5-3
(WFD Device Information subelement).
"""

from __future__ import annotations

import struct

RTSP_PORT = 7236

# WFD Device Information bitmap (2 bytes), per spec table 5-4:
#   bits 1:0  device type        00 = WFD source
#   bits 5:4  session state      01 = available for WFD session
#   bit  7    also set (0x0080)  -- gnome-network-displays ships 0x0090 and
#             real-world sinks (incl. Samsung) connect to it; match exactly.
DEVINFO_SOURCE_AVAILABLE = 0x0090

SUBELEM_DEVICE_INFO = 0


def build_wfd_ies(
    rtsp_port: int = RTSP_PORT,
    max_throughput_mbps: int = 200,
) -> bytes:
    """Return the WFD subelement blob for wpa_supplicant's WFDIEs property.

    wpa_supplicant wraps this in the vendor IE (OUI 50:6F:9A, type 10)
    and inserts it into all relevant P2P frames.
    """
    body = struct.pack(
        ">HHH", DEVINFO_SOURCE_AVAILABLE, rtsp_port, max_throughput_mbps
    )
    return struct.pack(">BH", SUBELEM_DEVICE_INFO, len(body)) + body


def parse_device_info(ies: bytes) -> dict[str, int]:
    """Parse the Device Information subelement out of a peer's WFD IEs.

    Used to read the sink's advertised RTSP control port. Returns an
    empty dict if no Device Information subelement is present.
    """
    i = 0
    while i + 3 <= len(ies):
        subelem_id = ies[i]
        (length,) = struct.unpack_from(">H", ies, i + 1)
        payload = ies[i + 3 : i + 3 + length]
        if subelem_id == SUBELEM_DEVICE_INFO and len(payload) >= 6:
            dev_info, port, throughput = struct.unpack_from(">HHH", payload)
            return {
                "device_info": dev_info,
                "device_type": dev_info & 0x3,
                "session_available": (dev_info >> 4) & 0x3,
                "rtsp_port": port,
                "max_throughput_mbps": throughput,
            }
        i += 3 + length
    return {}
