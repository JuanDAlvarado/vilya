"""Absolute pointer injection via /dev/uinput, pure stdlib.

Creates a virtual absolute-pointer device (like a drawing tablet or a
VM mouse): KWin maps its coordinate space onto the union of all
outputs, so a touch aimed at the extended screen is just an offset into
that union. The caller owns the coordinate transform; this module only
speaks the kernel's uinput API (ioctls + input_event writes).

Requires write access to /dev/uinput (see the udev rule in docs).
"""

from __future__ import annotations

import fcntl
import logging
import os
import struct
import time

log = logging.getLogger(__name__)

# --- ioctl plumbing (asm-generic/ioctl.h) ---------------------------------

_IOC_WRITE = 1


def _ioc(direction: int, ioc_type: str, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord(ioc_type) << 8) | nr


def _iow(ioc_type: str, nr: int, size: int) -> int:
    return _ioc(_IOC_WRITE, ioc_type, nr, size)


def _io(ioc_type: str, nr: int) -> int:
    return (ord(ioc_type) << 8) | nr


# --- uinput / input constants (linux/uinput.h, linux/input-event-codes.h) --

UI_SET_EVBIT = _iow("U", 100, 4)
UI_SET_KEYBIT = _iow("U", 101, 4)
UI_SET_ABSBIT = _iow("U", 103, 4)
UI_DEV_SETUP = _iow("U", 3, 92)  # struct uinput_setup
UI_ABS_SETUP = _iow("U", 4, 28)  # struct uinput_abs_setup
UI_DEV_CREATE = _io("U", 1)
UI_DEV_DESTROY = _io("U", 2)

EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03
SYN_REPORT = 0
ABS_X = 0x00
ABS_Y = 0x01
BTN_LEFT = 0x110

BUS_VIRTUAL = 0x06

# struct input_event on 64-bit: timeval (2x long) + type u16 + code u16 + value s32
_EVENT_FMT = "llHHi"


class AbsolutePointer:
    """A uinput absolute pointer covering ``width`` x ``height`` units."""

    def __init__(self, width: int, height: int, name: str = "vilya-touch"):
        self.width = width
        self.height = height
        self._fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self._fd, UI_SET_EVBIT, EV_KEY)
            fcntl.ioctl(self._fd, UI_SET_KEYBIT, BTN_LEFT)
            fcntl.ioctl(self._fd, UI_SET_EVBIT, EV_ABS)
            fcntl.ioctl(self._fd, UI_SET_ABSBIT, ABS_X)
            fcntl.ioctl(self._fd, UI_SET_ABSBIT, ABS_Y)

            for axis, maximum in ((ABS_X, width - 1), (ABS_Y, height - 1)):
                # struct uinput_abs_setup: u16 code, (2 pad), input_absinfo
                # { s32 value,min,max,fuzz,flat,resolution }
                buf = struct.pack(
                    "HxxiiiiiI", axis, 0, 0, maximum, 0, 0, 0
                )
                fcntl.ioctl(self._fd, UI_ABS_SETUP, buf)

            # struct uinput_setup: input_id {u16 bus,vendor,product,version},
            # char name[80], u32 ff_effects_max
            setup = struct.pack(
                "HHHH80sI",
                BUS_VIRTUAL,
                0x1209,  # pid.codes open-source vendor space
                0x0001,
                1,
                name.encode(),
                0,
            )
            fcntl.ioctl(self._fd, UI_DEV_SETUP, setup)
            fcntl.ioctl(self._fd, UI_DEV_CREATE)
        except Exception:
            os.close(self._fd)
            raise
        log.info(
            "uinput pointer created: %s (%dx%d units)", name, width, height
        )

    def _emit(self, etype: int, code: int, value: int) -> None:
        now = time.time()
        sec = int(now)
        usec = int((now - sec) * 1e6)
        os.write(self._fd, struct.pack(_EVENT_FMT, sec, usec, etype, code, value))

    def _syn(self) -> None:
        self._emit(EV_SYN, SYN_REPORT, 0)

    def move(self, x: int, y: int) -> None:
        self._emit(EV_ABS, ABS_X, max(0, min(self.width - 1, x)))
        self._emit(EV_ABS, ABS_Y, max(0, min(self.height - 1, y)))
        self._syn()

    def press(self, x: int, y: int) -> None:
        self._emit(EV_ABS, ABS_X, max(0, min(self.width - 1, x)))
        self._emit(EV_ABS, ABS_Y, max(0, min(self.height - 1, y)))
        self._emit(EV_KEY, BTN_LEFT, 1)
        self._syn()

    def release(self) -> None:
        self._emit(EV_KEY, BTN_LEFT, 0)
        self._syn()

    def close(self) -> None:
        try:
            fcntl.ioctl(self._fd, UI_DEV_DESTROY)
        finally:
            os.close(self._fd)
        log.info("uinput pointer destroyed")
