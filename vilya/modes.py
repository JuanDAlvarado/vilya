"""WFD video mode definitions shared by the RTSP layer and the pipeline.

A mode couples what we promise in M4 (profile/level/CEA bits) with what
the encoder actually produces -- these must agree or the sink's decoder
chokes. CEA resolution bits per WFD spec table 5-9; profile bitmap:
0x01 CBP, 0x02 CHP; level bitmap: 0x01=3.1 0x02=3.2 0x04=4.0 0x08=4.1
0x10=4.2.

The Tab S8+ under-advertises in M3 (CBP L3.1, 480p only) but accepted
CHP L4.2 @ 1080p in M4 during handshake testing -- trust the hardware,
not the advertisement.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VideoMode:
    name: str
    width: int
    height: int
    fps: int
    cea_bit: int
    profile_bits: int  # WFD profile bitmap value for M4
    level_bits: int  # WFD level bitmap value for M4
    gst_profile: str  # x264enc output caps profile
    bitrate_kbps: int

    @property
    def m4_video_formats(self) -> str:
        return (
            f"wfd_video_formats: 00 00 {self.profile_bits:02X} "
            f"{self.level_bits:02X} {self.cea_bit:08X} 00000000 00000000 "
            f"00 0000 0000 00 none none"
        )


MODES: dict[str, VideoMode] = {
    "720p30": VideoMode(
        "720p30", 1280, 720, 30, 0x00000020, 0x01, 0x01,
        "constrained-baseline", 8000,
    ),
    "1080p30": VideoMode(
        "1080p30", 1920, 1080, 30, 0x00000080, 0x02, 0x10, "high", 14000,
    ),
    "1080p60": VideoMode(
        "1080p60", 1920, 1080, 60, 0x00000100, 0x02, 0x10, "high", 20000,
    ),
}
