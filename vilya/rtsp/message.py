"""RTSP message parsing and formatting.

Handles TCP stream buffering: bytes arrive in chunks and may contain
partial messages or multiple messages. Call feed() with each chunk;
take_message() returns the next complete message or None.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RTSPMessage:
    """A parsed RTSP request or response."""

    # Requests: method is set, status_code is None.
    # Responses: status_code is set, method is None.
    method: Optional[str] = None
    uri: Optional[str] = None
    version: str = "RTSP/1.0"

    status_code: Optional[int] = None
    reason: Optional[str] = None

    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def is_request(self) -> bool:
        return self.method is not None

    @property
    def is_response(self) -> bool:
        return self.status_code is not None

    def get_header(self, name: str) -> Optional[str]:
        """Case-insensitive header lookup."""
        name_lower = name.lower()
        for k, v in self.headers.items():
            if k.lower() == name_lower:
                return v
        return None

    def cseq(self) -> Optional[int]:
        val = self.get_header("CSeq")
        if val is None:
            return None
        try:
            return int(val.strip())
        except ValueError:
            return None


class RTSPParseError(Exception):
    pass


# First line of a request:  METHOD uri RTSP/1.0
_REQUEST_LINE_RE = re.compile(
    r"^([A-Z_]+)\s+(\S+)\s+(RTSP/\d+\.\d+)$"
)
# First line of a response: RTSP/1.0 200 OK
_STATUS_LINE_RE = re.compile(
    r"^(RTSP/\d+\.\d+)\s+(\d{3})\s*(.*)$"
)


def _parse_header_block(header_text: str) -> dict[str, str]:
    """Parse the header block (everything after the first line) into a dict.

    Multi-line (folded) headers are joined. Duplicate header names are
    combined with a comma per RFC 2326 §4.2.
    """
    headers: dict[str, str] = {}
    current_name: Optional[str] = None
    current_value: Optional[str] = None

    for line in header_text.splitlines():
        if line and line[0] in (" ", "\t"):
            # Folded continuation
            if current_name is not None:
                current_value = (current_value or "") + " " + line.strip()
            continue

        if current_name is not None:
            headers[current_name] = (
                headers[current_name] + ", " + current_value
                if current_name in headers
                else current_value  # type: ignore[assignment]
            )
            current_name = None
            current_value = None

        if ":" not in line:
            if line:
                raise RTSPParseError(f"Malformed header line: {line!r}")
            continue

        name, _, value = line.partition(":")
        current_name = name.strip()
        current_value = value.strip()

    if current_name is not None:
        headers[current_name] = (
            headers[current_name] + ", " + current_value
            if current_name in headers
            else current_value  # type: ignore[assignment]
        )

    return headers


def parse_message(data: bytes) -> RTSPMessage:
    """Parse a complete RTSP message from bytes.

    ``data`` must contain exactly one message (headers + body).
    For streaming use, see :class:`RTSPBuffer`.
    """
    header_end = data.find(b"\r\n\r\n")
    if header_end == -1:
        raise RTSPParseError("No header terminator found")

    header_bytes = data[:header_end]
    body = data[header_end + 4 :]

    try:
        header_text = header_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RTSPParseError(f"Header decode error: {exc}") from exc

    lines = header_text.split("\r\n")
    first_line = lines[0]
    rest = "\r\n".join(lines[1:])

    msg = RTSPMessage()

    req_match = _REQUEST_LINE_RE.match(first_line)
    if req_match:
        msg.method = req_match.group(1)
        msg.uri = req_match.group(2)
        msg.version = req_match.group(3)
    else:
        resp_match = _STATUS_LINE_RE.match(first_line)
        if resp_match:
            msg.version = resp_match.group(1)
            msg.status_code = int(resp_match.group(2))
            msg.reason = resp_match.group(3)
        else:
            raise RTSPParseError(f"Unrecognised first line: {first_line!r}")

    msg.headers = _parse_header_block(rest)
    msg.body = body
    return msg


def format_request(
    method: str,
    uri: str,
    headers: dict[str, str],
    body: bytes = b"",
    version: str = "RTSP/1.0",
) -> bytes:
    """Serialise an outgoing RTSP request to bytes."""
    return _format_message(f"{method} {uri} {version}", headers, body)


def format_response(
    status_code: int,
    reason: str,
    headers: dict[str, str],
    body: bytes = b"",
    version: str = "RTSP/1.0",
) -> bytes:
    """Serialise an outgoing RTSP response to bytes."""
    return _format_message(f"{version} {status_code} {reason}", headers, body)


def _format_message(
    first_line: str, headers: dict[str, str], body: bytes
) -> bytes:
    if body:
        headers = dict(headers)
        headers.setdefault("Content-Length", str(len(body)))

    lines = [first_line]
    for name, value in headers.items():
        lines.append(f"{name}: {value}")
    lines.append("")
    lines.append("")

    header_bytes = "\r\n".join(lines).encode("utf-8")
    return header_bytes + body


class RTSPBuffer:
    """Accumulates TCP stream bytes and yields complete RTSP messages.

    Usage::

        buf = RTSPBuffer()
        buf.feed(data)          # call each time data arrives
        while (msg := buf.take_message()) is not None:
            handle(msg)
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)

    def take_message(self) -> Optional[RTSPMessage]:
        """Return the next complete message, or None if more data is needed."""
        # Tolerate stray CRLFs between messages (Samsung sinks append an
        # extra one after requests); RFC 2326 says to ignore them.
        while self._buf[:2] == b"\r\n":
            del self._buf[:2]

        header_end = self._buf.find(b"\r\n\r\n")
        if header_end == -1:
            return None

        header_bytes = self._buf[: header_end]
        try:
            header_text = header_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            # Consume the garbage so callers can resync on the next message.
            del self._buf[: header_end + 4]
            raise RTSPParseError(f"Header decode error: {exc}") from exc

        # Determine expected body length.
        content_length = 0
        for line in header_text.split("\r\n")[1:]:
            if line.lower().startswith("content-length"):
                _, _, val = line.partition(":")
                try:
                    content_length = int(val.strip())
                except ValueError:
                    pass
                break

        message_end = header_end + 4 + content_length
        if len(self._buf) < message_end:
            return None  # Body not yet fully received.

        raw = bytes(self._buf[:message_end])
        del self._buf[:message_end]
        return parse_message(raw)
