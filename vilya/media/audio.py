"""Session audio routing: send desktop audio to the sink, not the speakers.

Capturing the speakers' monitor leaves the laptop playing too (at a
head start, since the Tab decodes ~150 ms later). Instead we create a
null sink, make it the default output for the duration of the session,
and capture *its* monitor: the laptop is silent, the Tab is the audio
device, and the laptop's volume keys control the null sink -- which
scales the captured signal, so they effectively control the Tab.

Previous default is restored (and the module unloaded) on teardown.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

SINK_NAME = "vilya_cast"


def _pactl(*args: str) -> str:
    res = subprocess.run(
        ["pactl", *args], capture_output=True, text=True, check=True
    )
    return res.stdout.strip()


@dataclass
class AudioRoute:
    module_id: str
    previous_default: str

    @property
    def monitor(self) -> str:
        return f"{SINK_NAME}.monitor"


def _unload_stale() -> None:
    """Remove leftover vilya sinks from crashed sessions."""
    try:
        for line in _pactl("list", "short", "modules").splitlines():
            if SINK_NAME in line:
                _pactl("unload-module", line.split()[0])
                log.debug("Unloaded stale audio module %s", line.split()[0])
    except subprocess.CalledProcessError:
        pass


def setup() -> Optional[AudioRoute]:
    """Create the cast sink and make it the default. None on failure."""
    try:
        _unload_stale()
        previous = _pactl("get-default-sink")
        module_id = _pactl(
            "load-module",
            "module-null-sink",
            f"sink_name={SINK_NAME}",
            "sink_properties=device.description=vilya-cast",
        )
        _pactl("set-default-sink", SINK_NAME)
        log.info(
            "Audio routed to the sink device (was: %s); laptop is silent, "
            "volume keys control the cast",
            previous,
        )
        return AudioRoute(module_id=module_id, previous_default=previous)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        log.warning("Audio routing failed (%s); falling back to speaker monitor", exc)
        return None


def teardown(route: AudioRoute) -> None:
    try:
        _pactl("set-default-sink", route.previous_default)
    except subprocess.CalledProcessError as exc:
        log.warning("Could not restore default sink: %s", exc)
    try:
        _pactl("unload-module", route.module_id)
    except subprocess.CalledProcessError as exc:
        log.warning("Could not unload cast sink: %s", exc)
    log.info("Audio routing restored to %s", route.previous_default)
