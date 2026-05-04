"""Unit tests for RTSP message parsing and formatting."""

import pytest

from vilya.rtsp.message import (
    RTSPBuffer,
    RTSPMessage,
    RTSPParseError,
    format_request,
    format_response,
    parse_message,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

OPTIONS_REQUEST = (
    b"OPTIONS rtsp://192.168.137.2/wfd1.0 RTSP/1.0\r\n"
    b"CSeq: 1\r\n"
    b"Require: org.wfa.wfd1.0\r\n"
    b"\r\n"
)

OPTIONS_RESPONSE = (
    b"RTSP/1.0 200 OK\r\n"
    b"CSeq: 1\r\n"
    b"Public: org.wfa.wfd1.0, GET_PARAMETER, SET_PARAMETER\r\n"
    b"\r\n"
)

GET_PARAM_REQUEST_WITH_BODY = (
    b"GET_PARAMETER rtsp://localhost/wfd1.0 RTSP/1.0\r\n"
    b"CSeq: 2\r\n"
    b"Content-Type: text/parameters\r\n"
    b"Content-Length: 19\r\n"
    b"\r\n"
    b"wfd_video_formats\r\n"
)


# ---------------------------------------------------------------------------
# parse_message: happy paths
# ---------------------------------------------------------------------------


class TestParseMessageRequest:
    def test_basic_options(self):
        msg = parse_message(OPTIONS_REQUEST)
        assert msg.is_request
        assert msg.method == "OPTIONS"
        assert msg.uri == "rtsp://192.168.137.2/wfd1.0"
        assert msg.version == "RTSP/1.0"
        assert msg.cseq() == 1
        assert msg.get_header("Require") == "org.wfa.wfd1.0"
        assert msg.body == b""

    def test_get_parameter_with_body(self):
        msg = parse_message(GET_PARAM_REQUEST_WITH_BODY)
        assert msg.method == "GET_PARAMETER"
        assert msg.cseq() == 2
        assert msg.body == b"wfd_video_formats\r\n"

    def test_set_parameter(self):
        body = b"wfd_trigger_method: SETUP\r\n"
        raw = (
            b"SET_PARAMETER rtsp://localhost/wfd1.0 RTSP/1.0\r\n"
            b"CSeq: 5\r\n"
            b"Content-Type: text/parameters\r\n"
            + b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"\r\n"
            + body
        )
        msg = parse_message(raw)
        assert msg.method == "SET_PARAMETER"
        assert msg.body == body


class TestParseMessageResponse:
    def test_basic_200(self):
        msg = parse_message(OPTIONS_RESPONSE)
        assert msg.is_response
        assert msg.status_code == 200
        assert msg.reason == "OK"
        assert msg.cseq() == 1

    def test_200_reason_preserved(self):
        raw = b"RTSP/1.0 200 OK\r\nCSeq: 3\r\n\r\n"
        msg = parse_message(raw)
        assert msg.reason == "OK"

    def test_404(self):
        raw = b"RTSP/1.0 404 Not Found\r\nCSeq: 7\r\n\r\n"
        msg = parse_message(raw)
        assert msg.status_code == 404
        assert msg.reason == "Not Found"


class TestParseMessageHeaders:
    def test_case_insensitive_lookup(self):
        msg = parse_message(OPTIONS_REQUEST)
        assert msg.get_header("cseq") == "1"
        assert msg.get_header("CSEQ") == "1"
        assert msg.get_header("CSeq") == "1"

    def test_missing_header_returns_none(self):
        msg = parse_message(OPTIONS_REQUEST)
        assert msg.get_header("Session") is None

    def test_no_body_when_no_content_length(self):
        msg = parse_message(OPTIONS_REQUEST)
        assert msg.body == b""


# ---------------------------------------------------------------------------
# parse_message: error paths
# ---------------------------------------------------------------------------


class TestParseMessageErrors:
    def test_no_header_terminator(self):
        with pytest.raises(RTSPParseError, match="No header terminator"):
            parse_message(b"OPTIONS rtsp://x RTSP/1.0\r\nCSeq: 1\r\n")

    def test_malformed_first_line(self):
        with pytest.raises(RTSPParseError, match="Unrecognised first line"):
            parse_message(b"NOT VALID\r\n\r\n")

    def test_malformed_header_line(self):
        # A header line without a colon that is also not a folded line.
        with pytest.raises(RTSPParseError, match="Malformed header line"):
            parse_message(b"OPTIONS rtsp://x RTSP/1.0\r\nBadHeader\r\n\r\n")


# ---------------------------------------------------------------------------
# format_request / format_response
# ---------------------------------------------------------------------------


class TestFormatRequest:
    def test_round_trip(self):
        raw = format_request(
            "OPTIONS",
            "rtsp://192.168.137.2/wfd1.0",
            {"CSeq": "1", "Require": "org.wfa.wfd1.0"},
        )
        msg = parse_message(raw)
        assert msg.method == "OPTIONS"
        assert msg.cseq() == 1
        assert msg.get_header("Require") == "org.wfa.wfd1.0"

    def test_body_sets_content_length(self):
        body = b"wfd_video_formats\r\n"
        raw = format_request(
            "GET_PARAMETER",
            "rtsp://localhost/wfd1.0",
            {"CSeq": "2", "Content-Type": "text/parameters"},
            body,
        )
        msg = parse_message(raw)
        assert msg.body == body
        assert msg.get_header("Content-Length") == str(len(body))

    def test_no_content_length_when_no_body(self):
        raw = format_request("OPTIONS", "rtsp://x/wfd1.0", {"CSeq": "1"})
        msg = parse_message(raw)
        assert msg.get_header("Content-Length") is None

    def test_explicit_content_length_not_overridden(self):
        body = b"hello"
        raw = format_request(
            "GET_PARAMETER",
            "rtsp://x",
            {"CSeq": "1", "Content-Length": "99"},
            body,
        )
        assert b"Content-Length: 99" in raw


class TestFormatResponse:
    def test_200_ok(self):
        raw = format_response(200, "OK", {"CSeq": "1"})
        msg = parse_message(raw)
        assert msg.status_code == 200
        assert msg.reason == "OK"

    def test_body_attached(self):
        body = b"wfd_video_formats: none\r\n"
        raw = format_response(200, "OK", {"CSeq": "3"}, body)
        msg = parse_message(raw)
        assert msg.body == body


# ---------------------------------------------------------------------------
# RTSPBuffer: streaming / chunked input
# ---------------------------------------------------------------------------


class TestRTSPBuffer:
    def test_complete_single_message(self):
        buf = RTSPBuffer()
        buf.feed(OPTIONS_REQUEST)
        msg = buf.take_message()
        assert msg is not None
        assert msg.method == "OPTIONS"
        assert buf.take_message() is None

    def test_split_across_chunks(self):
        buf = RTSPBuffer()
        mid = len(OPTIONS_REQUEST) // 2
        buf.feed(OPTIONS_REQUEST[:mid])
        assert buf.take_message() is None
        buf.feed(OPTIONS_REQUEST[mid:])
        msg = buf.take_message()
        assert msg is not None
        assert msg.method == "OPTIONS"

    def test_split_within_body(self):
        buf = RTSPBuffer()
        # Feed everything up to the last byte of the body.
        buf.feed(GET_PARAM_REQUEST_WITH_BODY[:-1])
        assert buf.take_message() is None
        buf.feed(GET_PARAM_REQUEST_WITH_BODY[-1:])
        msg = buf.take_message()
        assert msg is not None
        assert msg.method == "GET_PARAMETER"

    def test_two_messages_in_one_recv(self):
        buf = RTSPBuffer()
        buf.feed(OPTIONS_REQUEST + OPTIONS_RESPONSE)
        msg1 = buf.take_message()
        msg2 = buf.take_message()
        assert msg1 is not None and msg1.is_request
        assert msg2 is not None and msg2.is_response
        assert buf.take_message() is None

    def test_many_one_byte_chunks(self):
        buf = RTSPBuffer()
        for byte in OPTIONS_REQUEST:
            buf.feed(bytes([byte]))
        msg = buf.take_message()
        assert msg is not None
        assert msg.method == "OPTIONS"

    def test_no_content_length_body_treated_as_empty(self):
        # When Content-Length is absent, buffer consumes no body bytes.
        raw = b"OPTIONS rtsp://x/wfd1.0 RTSP/1.0\r\nCSeq: 1\r\n\r\n"
        buf = RTSPBuffer()
        buf.feed(raw)
        msg = buf.take_message()
        assert msg is not None
        assert msg.body == b""

    def test_partial_header_returns_none(self):
        buf = RTSPBuffer()
        buf.feed(b"OPTIONS rtsp://x RTSP/1.0\r\nCSeq: 1")
        assert buf.take_message() is None

    def test_three_messages_sequential(self):
        frames = OPTIONS_REQUEST + OPTIONS_RESPONSE + OPTIONS_REQUEST
        buf = RTSPBuffer()
        buf.feed(frames)
        msgs = []
        while (m := buf.take_message()) is not None:
            msgs.append(m)
        assert len(msgs) == 3

    def test_buffer_empty_after_all_consumed(self):
        buf = RTSPBuffer()
        buf.feed(OPTIONS_REQUEST)
        buf.take_message()
        assert buf.take_message() is None
        # Feed a new message to confirm buffer is truly empty, not corrupted.
        buf.feed(OPTIONS_RESPONSE)
        msg = buf.take_message()
        assert msg is not None and msg.is_response
