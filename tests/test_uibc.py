"""Tests for the UIBC packet parser."""

import struct

from vilya.input.uibc import (
    GENERIC_TOUCH_DOWN,
    GENERIC_TOUCH_MOVE,
    GENERIC_TOUCH_UP,
    parse_packet,
)


def make_touch(ie_id: int, x: int, y: int, timestamp: bool = False) -> bytes:
    body = bytes([1, 0]) + struct.pack(">HH", x, y)  # 1 pointer, id 0
    ie = bytes([ie_id]) + struct.pack(">H", len(body)) + body
    header_len = 6 if timestamp else 4
    total = header_len + len(ie)
    pkt = bytes([0x10 if timestamp else 0x00, 0x00]) + struct.pack(">H", total)
    if timestamp:
        pkt += struct.pack(">H", 1234)
    return pkt + ie


class TestParse:
    def test_touch_down(self):
        ev = parse_packet(make_touch(GENERIC_TOUCH_DOWN, 960, 600))
        assert ev is not None
        assert ev.kind == GENERIC_TOUCH_DOWN
        assert (ev.x, ev.y) == (960, 600)

    def test_touch_move_with_timestamp(self):
        ev = parse_packet(make_touch(GENERIC_TOUCH_MOVE, 10, 20, timestamp=True))
        assert ev is not None
        assert ev.kind == GENERIC_TOUCH_MOVE
        assert (ev.x, ev.y) == (10, 20)

    def test_touch_up(self):
        ev = parse_packet(make_touch(GENERIC_TOUCH_UP, 0, 0))
        assert ev is not None
        assert ev.kind == GENERIC_TOUCH_UP

    def test_hidc_ignored(self):
        pkt = bytes([0x00, 0x01]) + struct.pack(">H", 8) + b"\x00" * 4
        assert parse_packet(pkt) is None

    def test_short_packet(self):
        assert parse_packet(b"\x00\x00") is None

    def test_non_touch_ie_ignored(self):
        body = bytes([0x41, 0x41])  # key code
        ie = bytes([3]) + struct.pack(">H", len(body)) + body  # KEY_DOWN
        pkt = bytes([0x00, 0x00]) + struct.pack(">H", 4 + len(ie)) + ie
        assert parse_packet(pkt) is None
