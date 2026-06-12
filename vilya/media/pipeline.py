"""GStreamer media pipeline for the WFD RTP stream.

Runs gst-launch-1.0 as a subprocess (no PyGObject dependency for the
prototype). Video-only for now: H.264 per the negotiated VideoMode,
muxed into MPEG-TS, RTP-payloaded (PT 33), sent to the sink's UDP port.

Latency discipline (the first cut accumulated ~7 s):
- leaky queues drop stale frames instead of queueing them, so a slow
  frame costs one frame, not permanent added delay
- videoconvert is multithreaded
- x264 runs zerolatency with a small VBV buffer (300 ms ceiling)
- udpsink sync=false pushes packets the moment they exist

Audio (LPCM 48 kHz) is deferred; see docs/todo.md Phase 3.
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
from typing import Optional

from ..modes import MODES, VideoMode

log = logging.getLogger(__name__)

DEFAULT_MODE = MODES["1080p30"]

# Bounded, lossy decoupling: never let latency build up in the pipeline.
_LEAKY_Q = "queue max-size-buffers=2 leaky=downstream"

REQUIRED_ELEMENTS = [
    "x264enc",  # gst-plugins-ugly
    "mpegtsmux",  # gst-plugins-bad
    "rtpmp2tpay",  # gst-plugins-good
    "udpsink",  # gst-plugins-good
    "h264parse",  # gst-plugins-bad
]


def missing_elements(audio: bool = False) -> list[str]:
    """Return GStreamer elements we need but the system lacks."""
    missing = []
    for element in REQUIRED_ELEMENTS + (["fdkaacenc", "pulsesrc"] if audio else []):
        res = subprocess.run(
            ["gst-inspect-1.0", "--exists", element], capture_output=True
        )
        if res.returncode != 0:
            missing.append(element)
    return missing


def default_monitor() -> Optional[str]:
    """PulseAudio/PipeWire monitor source of the default output."""
    res = subprocess.run(
        ["pactl", "get-default-sink"], capture_output=True, text=True
    )
    sink = res.stdout.strip()
    return f"{sink}.monitor" if res.returncode == 0 and sink else None


def build_pipeline(
    sink_host: str,
    sink_port: int,
    source: str = "screen",
    pipewire_fd: Optional[int] = None,
    pipewire_node: Optional[int] = None,
    mode: VideoMode = DEFAULT_MODE,
    audio_monitor: Optional[str] = None,
) -> str:
    """Return the gst-launch pipeline description string.

    With ``audio_monitor`` set, desktop audio is captured from that
    Pulse/PipeWire monitor source, AAC-encoded, and muxed into the same
    transport stream (the Tab advertises AAC 48 kHz stereo in M3).
    """
    caps = (
        f"video/x-raw,format=I420,width={mode.width},height={mode.height},"
        f"framerate={mode.fps}/1"
    )
    # Screen capture is variable-rate by nature (KWin sends on damage).
    # No videorate and no fixed framerate cap: regulating to constant
    # rate makes each frame wait for its successor (up to keepalive-time
    # = 500 ms of added latency). H.264-in-TS is timestamp-driven; sinks
    # handle VFR fine.
    vfr_caps = f"video/x-raw,format=I420,width={mode.width},height={mode.height}"
    if source == "test":
        head = (
            f"videotestsrc is-live=true pattern=smpte ! {caps} "
            f"! timeoverlay font-desc=\"Sans 36\" "
        )
    elif source == "screen":
        if pipewire_node is None:
            raise ValueError("screen source requires a pipewire node id")
        # fd is the portal's private PipeWire connection; KWin-native
        # virtual-output nodes live on the default connection (no fd).
        fd_prop = f"fd={pipewire_fd} " if pipewire_fd is not None else ""
        # Convert to I420 before scaling: half the bytes of BGRx, and at
        # native panel resolution videoscale becomes a passthrough.
        # keepalive-time: KWin only sends frames on screen damage; a
        # static desktop would starve the TS stream and the sink drops
        # the session after ~20 s of silence. Resend the last frame
        # every 500 ms when idle.
        head = (
            f"pipewiresrc {fd_prop}path={pipewire_node} "
            f"do-timestamp=true keepalive-time=500 "
            f"! {_LEAKY_Q} "
            f"! videoconvert n-threads=4 ! video/x-raw,format=I420 "
            f"! videoscale ! {vfr_caps} "
        )
    else:
        raise ValueError(f"Unknown source {source!r}")

    tail = (
        f"! {_LEAKY_Q} "
        f"! x264enc tune=zerolatency speed-preset=superfast bframes=0 "
        f"bitrate={mode.bitrate_kbps} key-int-max={mode.fps} "
        f"vbv-buf-capacity=300 "
        f"! video/x-h264,profile={mode.gst_profile} "
        f"! h264parse config-interval=1 "
        f"! mux. "
        f"mpegtsmux name=mux alignment=7 "
        f"! rtpmp2tpay "
        f"! udpsink host={sink_host} port={sink_port} sync=false"
    )
    audio = ""
    if audio_monitor:
        audio = (
            f" pulsesrc device={audio_monitor} "
            f"! audio/x-raw,rate=48000,channels=2 "
            f"! audioconvert ! audioresample "
            f"! fdkaacenc bitrate=192000 ! aacparse "
            f"! queue max-size-time=200000000 leaky=downstream "
            f"! mux."
        )
    return head + tail + audio


class MediaPipeline:
    """Owns the gst-launch-1.0 subprocess."""

    def __init__(
        self,
        sink_host: str,
        sink_port: int,
        source: str = "screen",
        pipewire_fd: Optional[int] = None,
        pipewire_node: Optional[int] = None,
        mode: VideoMode = DEFAULT_MODE,
        audio_monitor: Optional[str] = None,
    ) -> None:
        self.description = build_pipeline(
            sink_host,
            sink_port,
            source,
            pipewire_fd,
            pipewire_node,
            mode,
            audio_monitor,
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
