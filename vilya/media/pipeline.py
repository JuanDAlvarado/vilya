"""GStreamer media pipeline for the WFD RTP stream.

Runs gst-launch-1.0 as a subprocess (no PyGObject dependency for the
prototype). Video-only for now: H.264 constrained-baseline level 3.1 at
1280x720p30 -- matching what we declare in M4 -- muxed into MPEG-TS,
RTP-payloaded (PT 33), and sent to the sink's negotiated UDP port.

Audio (LPCM 48 kHz) is deferred; see docs/todo.md Phase 3.
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
from typing import Optional

log = logging.getLogger(__name__)

WIDTH = 1280
HEIGHT = 720
FPS = 30
BITRATE_KBPS = 8000

# x264 in constrained-baseline needs no B-frames; zerolatency also forces
# this, but be explicit. key-int-max=FPS gives one IDR per second so the
# sink can join/recover quickly.
ENCODE_TAIL = (
    f"videoconvert ! video/x-raw,format=I420 "
    f"! x264enc tune=zerolatency speed-preset=superfast bframes=0 "
    f"bitrate={BITRATE_KBPS} key-int-max={FPS} "
    f"! video/x-h264,profile=constrained-baseline,level=(string)3.1 "
    f"! h264parse config-interval=1 "
    f"! mpegtsmux alignment=7 "
    f"! rtpmp2tpay "
    f"! udpsink name=vilya-rtp sync=false"
)

REQUIRED_ELEMENTS = [
    "x264enc",  # gst-plugins-ugly
    "mpegtsmux",  # gst-plugins-bad
    "rtpmp2tpay",  # gst-plugins-good
    "udpsink",  # gst-plugins-good
    "h264parse",  # gst-plugins-bad
]


def missing_elements() -> list[str]:
    """Return GStreamer elements we need but the system lacks."""
    missing = []
    for element in REQUIRED_ELEMENTS:
        res = subprocess.run(
            ["gst-inspect-1.0", "--exists", element], capture_output=True
        )
        if res.returncode != 0:
            missing.append(element)
    return missing


def build_pipeline(
    sink_host: str,
    sink_port: int,
    source: str = "screen",
    pipewire_fd: Optional[int] = None,
    pipewire_node: Optional[int] = None,
) -> str:
    """Return the gst-launch pipeline description string."""
    caps = f"video/x-raw,width={WIDTH},height={HEIGHT},framerate={FPS}/1"
    if source == "test":
        head = (
            f"videotestsrc is-live=true pattern=smpte ! {caps} "
            f"! timeoverlay font-desc=\"Sans 36\" "
        )
    elif source == "screen":
        if pipewire_fd is None or pipewire_node is None:
            raise ValueError("screen source requires pipewire fd and node id")
        head = (
            f"pipewiresrc fd={pipewire_fd} path={pipewire_node} "
            f"do-timestamp=true "
            f"! videorate ! videoscale ! videoconvert ! {caps} "
        )
    else:
        raise ValueError(f"Unknown source {source!r}")

    tail = ENCODE_TAIL.replace(
        "udpsink name=vilya-rtp",
        f"udpsink name=vilya-rtp host={sink_host} port={sink_port}",
    )
    return head + "! " + tail


class MediaPipeline:
    """Owns the gst-launch-1.0 subprocess."""

    def __init__(
        self,
        sink_host: str,
        sink_port: int,
        source: str = "screen",
        pipewire_fd: Optional[int] = None,
        pipewire_node: Optional[int] = None,
    ) -> None:
        self.description = build_pipeline(
            sink_host, sink_port, source, pipewire_fd, pipewire_node
        )
        self._pipewire_fd = pipewire_fd
        self._proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        if self._proc:
            return
        gst = shutil.which("gst-launch-1.0")
        if not gst:
            raise RuntimeError("gst-launch-1.0 not found")
        cmd = [gst, "-q"] + shlex.split(self.description)
        log.info("Starting media pipeline: %s", self.description)
        pass_fds = [self._pipewire_fd] if self._pipewire_fd is not None else []
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            pass_fds=pass_fds,
        )

    def poll(self) -> Optional[int]:
        """Return the exit code if the pipeline died, else None."""
        if not self._proc:
            return None
        code = self._proc.poll()
        if code is not None and code != 0:
            err = (self._proc.stderr.read() if self._proc.stderr else b"").decode(
                errors="replace"
            )
            log.error("Pipeline exited %d: %s", code, err.strip()[-500:])
        return code

    def stop(self) -> None:
        if not self._proc:
            return
        log.info("Stopping media pipeline")
        self._proc.terminate()
        try:
            self._proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None
