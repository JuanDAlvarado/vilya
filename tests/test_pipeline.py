"""Tests for the GStreamer pipeline description builder."""

import shlex

import pytest

from vilya.media.pipeline import build_pipeline


class TestBuildPipeline:
    def test_test_source(self):
        desc = build_pipeline("192.168.49.1", 19000, source="test")
        assert desc.startswith("videotestsrc")
        assert "host=192.168.49.1" in desc
        assert "port=19000" in desc
        assert "profile=constrained-baseline" in desc
        assert "rtpmp2tpay" in desc
        assert "mpegtsmux alignment=7" in desc

    def test_screen_source(self):
        desc = build_pipeline(
            "192.168.49.1", 19000, source="screen", pipewire_fd=7, pipewire_node=42
        )
        assert "pipewiresrc fd=7 path=42" in desc
        assert "videoscale" in desc

    def test_screen_source_requires_pipewire(self):
        with pytest.raises(ValueError):
            build_pipeline("192.168.49.1", 19000, source="screen")

    def test_unknown_source(self):
        with pytest.raises(ValueError):
            build_pipeline("192.168.49.1", 19000, source="bogus")

    def test_shlex_splittable(self):
        # The launcher splits with shlex; quoted args must survive.
        desc = build_pipeline("10.0.0.1", 19000, source="test")
        parts = shlex.split(desc)
        assert "timeoverlay" in parts
        assert any(p.startswith("font-desc=") for p in parts)
