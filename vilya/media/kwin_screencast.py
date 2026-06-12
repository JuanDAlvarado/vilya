"""KWin virtual output creation via the zkde_screencast_unstable_v1
Wayland protocol.

This is how the Tab becomes an *extended* display: KWin creates a brand
new virtual monitor (Plasma treats it exactly like a plugged-in screen)
and streams its content as a PipeWire node we feed to the encoder. The
same mechanism krfb-virtualmonitor uses; there is no D-Bus equivalent.

We speak the Wayland wire protocol directly over the session socket --
it is a simple length-prefixed binary framing, and we need exactly one
interface, so a client library dependency isn't warranted.

Wire format (little-endian):
    message  = object_id:u32, (size:u16 << 16 | opcode:u16):u32, args
    string   = len:u32 (includes NUL), bytes, NUL, pad to 4
    fixed    = signed 24.8 fixed point
    new_id   = u32 (client-allocated, counting up from 2)

The virtual output lives exactly as long as this Wayland connection:
closing the socket removes the monitor from Plasma.

Protocol reference: plasma-wayland-protocols,
src/protocols/zkde-screencast-unstable-v1.xml (LGPL-2.1-or-later).
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
from typing import Optional

log = logging.getLogger(__name__)

SCREENCAST_INTERFACE = "zkde_screencast_unstable_v1"
BIND_VERSION_MAX = 5  # v6 deprecates the 'created' event we rely on

# Core object ids / opcodes
WL_DISPLAY = 1
WL_DISPLAY_REQ_SYNC = 0
WL_DISPLAY_REQ_GET_REGISTRY = 1
WL_DISPLAY_EVT_ERROR = 0
WL_REGISTRY_REQ_BIND = 0
WL_REGISTRY_EVT_GLOBAL = 0
WL_CALLBACK_EVT_DONE = 0

# zkde_screencast_unstable_v1 requests
ZKDE_REQ_DESTROY = 2
ZKDE_REQ_STREAM_VIRTUAL_OUTPUT = 3  # since version 2

# zkde_screencast_stream_unstable_v1
STREAM_REQ_CLOSE = 0
STREAM_EVT_CLOSED = 0
STREAM_EVT_CREATED = 1
STREAM_EVT_FAILED = 2

POINTER_EMBEDDED = 2  # cursor rendered into the stream when over this output


def encode_string(s: str) -> bytes:
    raw = s.encode("utf-8") + b"\x00"
    pad = (-len(raw)) % 4
    return struct.pack("<I", len(raw)) + raw + b"\x00" * pad


def decode_string(data: bytes, offset: int) -> tuple[str, int]:
    """Return (string, next_offset)."""
    (length,) = struct.unpack_from("<I", data, offset)
    start = offset + 4
    s = data[start : start + length - 1].decode("utf-8", errors="replace")
    return s, start + length + ((-length) % 4)


def encode_message(object_id: int, opcode: int, args: bytes = b"") -> bytes:
    size = 8 + len(args)
    return struct.pack("<II", object_id, (size << 16) | opcode) + args


def to_fixed(value: float) -> int:
    return int(value * 256)


class KWinVirtualOutput:
    """Creates (and owns) one KWin virtual output streamed over PipeWire."""

    def __init__(self) -> None:
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._next_id = 2
        self.node_id: Optional[int] = None
        self._stream_obj: Optional[int] = None
        self._screencast_obj: Optional[int] = None

    def _new_id(self) -> int:
        oid = self._next_id
        self._next_id += 1
        return oid

    async def open(
        self,
        width: int,
        height: int,
        name: str = "vilya",
        scale: float = 1.0,
    ) -> int:
        """Create the virtual output; returns the PipeWire node id."""
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        display = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        if not runtime_dir:
            raise RuntimeError("XDG_RUNTIME_DIR not set; no Wayland session?")
        path = os.path.join(runtime_dir, display)

        self._reader, self._writer = await asyncio.open_unix_connection(path)

        # Discover globals: get_registry + sync barrier.
        registry = self._new_id()
        sync_cb = self._new_id()
        self._writer.write(
            encode_message(
                WL_DISPLAY, WL_DISPLAY_REQ_GET_REGISTRY, struct.pack("<I", registry)
            )
            + encode_message(
                WL_DISPLAY, WL_DISPLAY_REQ_SYNC, struct.pack("<I", sync_cb)
            )
        )
        await self._writer.drain()

        screencast_global: Optional[tuple[int, int]] = None  # (name, version)
        async for obj, opcode, body in self._events():
            if obj == registry and opcode == WL_REGISTRY_EVT_GLOBAL:
                (gname,) = struct.unpack_from("<I", body, 0)
                iface, off = decode_string(body, 4)
                (version,) = struct.unpack_from("<I", body, off)
                if iface == SCREENCAST_INTERFACE:
                    screencast_global = (gname, version)
            elif obj == sync_cb and opcode == WL_CALLBACK_EVT_DONE:
                break

        if screencast_global is None:
            raise RuntimeError(
                f"Compositor does not expose {SCREENCAST_INTERFACE} "
                "(KWin/Plasma required)"
            )

        gname, gversion = screencast_global
        version = min(gversion, BIND_VERSION_MAX)
        if gversion < 2:
            raise RuntimeError(
                "KWin screencast protocol too old for virtual outputs"
            )

        # registry.bind(name, interface, version, new_id)
        self._screencast_obj = self._new_id()
        bind_args = (
            struct.pack("<I", gname)
            + encode_string(SCREENCAST_INTERFACE)
            + struct.pack("<II", version, self._screencast_obj)
        )
        # stream_virtual_output(new_id stream, string name, int w, int h,
        #                       fixed scale, uint pointer)
        self._stream_obj = self._new_id()
        stream_args = (
            struct.pack("<I", self._stream_obj)
            + encode_string(name)
            + struct.pack(
                "<iiiI", width, height, to_fixed(scale), POINTER_EMBEDDED
            )
        )
        self._writer.write(
            encode_message(registry, WL_REGISTRY_REQ_BIND, bind_args)
            + encode_message(
                self._screencast_obj, ZKDE_REQ_STREAM_VIRTUAL_OUTPUT, stream_args
            )
        )
        await self._writer.drain()

        async for obj, opcode, body in self._events():
            if obj == self._stream_obj:
                if opcode == STREAM_EVT_CREATED:
                    (self.node_id,) = struct.unpack_from("<I", body, 0)
                    log.info(
                        "KWin virtual output %dx%d created (PipeWire node %d)",
                        width,
                        height,
                        self.node_id,
                    )
                    return self.node_id
                if opcode == STREAM_EVT_FAILED:
                    error, _ = decode_string(body, 0)
                    raise RuntimeError(f"KWin refused virtual output: {error}")
                if opcode == STREAM_EVT_CLOSED:
                    raise RuntimeError("KWin closed the stream during setup")
        raise RuntimeError("Wayland connection closed during setup")

    async def _events(self):
        """Yield (object_id, opcode, body) for each inbound message."""
        assert self._reader is not None
        while True:
            try:
                header = await self._reader.readexactly(8)
            except asyncio.IncompleteReadError:
                return
            obj, size_op = struct.unpack("<II", header)
            size = size_op >> 16
            opcode = size_op & 0xFFFF
            body = await self._reader.readexactly(size - 8)
            if obj == WL_DISPLAY and opcode == WL_DISPLAY_EVT_ERROR:
                _, off = struct.unpack_from("<I", body, 0), 4
                (code,) = struct.unpack_from("<I", body, 4)
                message, _ = decode_string(body, 8)
                raise RuntimeError(f"Wayland error {code}: {message}")
            yield obj, opcode, body

    async def close(self) -> None:
        """Tear down the stream; Plasma removes the virtual monitor."""
        if self._writer is None:
            return
        try:
            if self._stream_obj:
                self._writer.write(
                    encode_message(self._stream_obj, STREAM_REQ_CLOSE)
                )
            if self._screencast_obj:
                self._writer.write(
                    encode_message(self._screencast_obj, ZKDE_REQ_DESTROY)
                )
            await self._writer.drain()
        except Exception as exc:
            log.debug("Wayland close: %s", exc)
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except Exception:
            pass
        self._writer = None
        log.info("Virtual output removed")
