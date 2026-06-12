"""Tests for video modes and the GStreamer pipeline description builder."""

import shlex

import pytest

from vilya.media.pipeline import build_pipeline
from vilya.modes import MODES


class TestVideoModes:
    def test_m4_line_1080p30(self):
        # CHP (02) level 4.2 (10), CEA bit 7 -- the combination the Tab
        # accepted during handshake testing.
        assert MODES["1080p30"].m4_video_formats == (
            "wfd_video_formats: 00 00 02 10 00000080 00000000 00000000 "
            "00 0000 0000 00 none none"
        )

    def test_m4_line_720p30(self):
        assert MODES["720p30"].m4_video_formats == (
            "wfd_video_formats: 00 00 01 01 00000020 00000000 00000000 "
            "00 0000 0000 00 none none"
        )

    def test_modes_consistent(self):
        for mode in MODES.values():
            assert mode.width > 0 and mode.height > 0 and mode.fps > 0
            # Exactly one of the WFD resolution tables must be used.
            assert (mode.cea_bit != 0) != (mode.vesa_bit != 0)
            assert mode.bitrate_kbps >= 4000


class TestBuildPipeline:
    def test_test_source(self):
        desc = build_pipeline("192.168.49.1", 19000, source="test")
        assert desc.startswith("videotestsrc")
        assert "host=192.168.49.1" in desc
        assert "port=19000" in desc
        assert "rtpmp2tpay" in desc
        assert "mpegtsmux name=mux alignment=7" in desc

    def test_mode_drives_encoder(self):
        desc = build_pipeline(
            "10.0.0.1", 19000, source="test", mode=MODES["1080p30"]
        )
        assert "width=1920,height=1080" in desc
        assert "framerate=30/1" in desc
        assert "profile=high" in desc
        assert "bitrate=14000" in desc

        desc = build_pipeline(
            "10.0.0.1", 19000, source="test", mode=MODES["720p30"]
        )
        assert "profile=constrained-baseline" in desc

    def test_screen_source(self):
        desc = build_pipeline(
            "192.168.49.1", 19000, source="screen", pipewire_fd=7, pipewire_node=42
        )
        assert "pipewiresrc fd=7 path=42" in desc
        assert "leaky=downstream" in desc
        assert "n-threads=4" in desc

    def test_screen_source_requires_node(self):
        with pytest.raises(ValueError):
            build_pipeline("192.168.49.1", 19000, source="screen")

    def test_screen_source_without_fd(self):
        # KWin-native virtual outputs have a node but no portal fd.
        desc = build_pipeline(
            "192.168.49.1", 19000, source="screen", pipewire_node=42
        )
        assert "pipewiresrc path=42" in desc
        assert "fd=" not in desc

    def test_1200p30_vesa_m4_line(self):
        assert MODES["1200p30"].m4_video_formats == (
            "wfd_video_formats: 00 00 02 10 00000000 10000000 00000000 "
            "00 0000 0000 00 none none"
        )

    def test_unknown_source(self):
        with pytest.raises(ValueError):
            build_pipeline("192.168.49.1", 19000, source="bogus")

    def test_shlex_splittable(self):
        # The launcher splits with shlex; quoted args must survive.
        desc = build_pipeline("10.0.0.1", 19000, source="test")
        parts = shlex.split(desc)
        assert "timeoverlay" in parts
        assert any(p.startswith("font-desc=") for p in parts)


class TestAudio:
    def test_audio_branch(self):
        desc = build_pipeline(
            "10.0.0.1", 19000, source="test",
            audio_monitor="alsa_output.test.monitor",
        )
        assert "pulsesrc device=alsa_output.test.monitor" in desc
        assert "fdkaacenc" in desc
        assert desc.count("mux.") == 2  # video and audio both feed the mux
        assert "mpegtsmux name=mux" in desc

    def test_no_audio_by_default(self):
        desc = build_pipeline("10.0.0.1", 19000, source="test")
        assert "pulsesrc" not in desc
