"""UIBC (User Input Back Channel) server and Generic Input parser.

WFD spec section 4.11: after negotiating wfd_uibc_capability, the sink
opens a TCP connection to the source's advertised UIBC port and sends
input events. We support the Generic category (raw touch/mouse events;
HIDC is the alternative nobody needs here).

Packet layout (big-endian):
    octet 0:  version (bits 7..5), T = timestamp present (bit 4)
    octet 1:  input category (bits 3..0): 0 = GENERIC, 1 = HIDC
    octets 2-3: total packet length
    [octets 4-5: timestamp, only if T]
    payload: Generic Input Body Format IE:
        ID (1), length (2), then per-type fields.

Generic IE types:
    0 touch down / 1 touch up / 2 touch move:
        num_pointers (1), then per pointer: id (1), x (2), y (2)
    3 key down / 4 key up: key codes (ASCII-ish) -- logged, not injected yet
    5 zoom, 6 vscroll, 7 hscroll, 8 rotate -- logged, not injected yet

X/Y are scaled to the negotiated WFD display resolution, which equals
our VideoMode dimensions -- so for the extended display they map 1:1
onto the virtual output.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)

UIBC_PORT = 7239

CATEGORY_GENERIC = 0

GENERIC_TOUCH_DOWN = 0
GENERIC_TOUCH_UP = 1
GENERIC_TOUCH_MOVE = 2
GENERIC_KEY_DOWN = 3
GENERIC_KEY_UP = 4
GENERIC_VSCROLL = 6


@dataclass
class TouchEvent:
    kind: int  # GENERIC_TOUCH_DOWN / _UP / _MOVE
    x: int
    y: int
    pointer_id: int = 0


def parse_packet(data: bytes) -> Optional[TouchEvent]:
    """Parse one UIBC packet; returns a TouchEvent or None (non-touch)."""
    if len(data) < 4:
        return None
    timestamp_present = bool(data[0] & 0x10)
    category = data[1] & 0x0F
    offset = 6 if timestamp_present else 4
    if category != CATEGORY_GENERIC or len(data) < offset + 3:
        return None

    ie_id = data[offset]
    # ie length: data[offset+1:offset+3]; body follows
    body = data[offset + 3 :]

    if ie_id in (GENERIC_TOUCH_DOWN, GENERIC_TOUCH_UP, GENERIC_TOUCH_MOVE):
        if len(body) < 6:
            return None
        # num_pointers = body[0]; first pointer only (single touch v1)
        pointer_id = body[1]
        x, y = struct.unpack_from(">HH", body, 2)
        return TouchEvent(kind=ie_id, x=x, y=y, pointer_id=pointer_id)

    log.debug("UIBC non-touch IE id=%d body=%s", ie_id, body[:16].hex())
    return None


class UIBCServer:
    """Listens for the sink's UIBC connection; emits TouchEvents."""

    def __init__(
        self,
        on_touch: Callable[[TouchEvent], None],
        port: int = UIBC_PORT,
    ) -> None:
        self.on_touch = on_touch
        self.port = port
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_client, "0.0.0.0", self.port
        )
        log.info("UIBC listening on %d", self.port)

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        log.info("UIBC: sink connected from %s", peer)
        buf = b""
        try:
            while True:
                chunk = await reader.read(2048)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 4:
                    (length,) = struct.unpack_from(">H", buf, 2)
                    if length < 4:
                        log.warning(
                            "UIBC bad length %d, dropping buffer: %s",
                            length,
                            buf[:24].hex(),
                        )
                        buf = b""
                        break
                    if len(buf) < length:
                        break
                    packet, buf = buf[:length], buf[length:]
                    log.debug("UIBC pkt: %s", packet.hex())
                    event = parse_packet(packet)
                    if event:
                        self.on_touch(event)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        log.info("UIBC: sink disconnected")

    def stop(self) -> None:
        if self._server:
            self._server.close()
