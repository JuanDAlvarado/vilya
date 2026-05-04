"""WFD RTSP session state machine.

Tracks the M1–M7 exchange state and owns the asyncio TCP connection.
Media pipeline integration will be added in a later phase; this module
only handles the control channel.
"""

from __future__ import annotations

import asyncio
import enum
import logging
from typing import Callable, Optional

from .message import RTSPBuffer, RTSPMessage, format_request, format_response

log = logging.getLogger(__name__)

RTSP_PORT = 7236
KEEPALIVE_INTERVAL = 30  # seconds between GET_PARAMETER keepalives


class SessionState(enum.Enum):
    IDLE = "IDLE"
    M1_SENT = "M1_SENT"        # OPTIONS sent to sink
    M2_DONE = "M2_DONE"        # sink's OPTIONS request answered
    M3_SENT = "M3_SENT"        # GET_PARAMETER (WFD capabilities) sent
    NEGOTIATING = "NEGOTIATING" # processing M3 response
    M4_SENT = "M4_SENT"        # SET_PARAMETER (WFD selected params) sent
    M5_SENT = "M5_SENT"        # SET_PARAMETER trigger SETUP sent
    SETUP_WAIT = "SETUP_WAIT"  # waiting for sink's SETUP
    PLAY_WAIT = "PLAY_WAIT"    # waiting for sink's PLAY
    STREAMING = "STREAMING"    # RTP stream active
    TEARDOWN = "TEARDOWN"      # session ending


class WFDSession:
    """Manages a single WFD source-side RTSP session.

    Instantiate, then call ``connect()`` to establish the TCP control
    channel and drive the M1–M7 handshake.

    Parameters
    ----------
    sink_host:
        IP address of the Miracast sink (assigned by our DHCP server).
    local_host:
        Our own IP on the P2P interface (used in WFD descriptors).
    on_state_change:
        Optional callback invoked whenever the session state changes.
    """

    def __init__(
        self,
        sink_host: str,
        local_host: str,
        on_state_change: Optional[Callable[[SessionState], None]] = None,
    ) -> None:
        self.sink_host = sink_host
        self.local_host = local_host
        self._on_state_change = on_state_change

        self._state = SessionState.IDLE
        self._cseq = 0
        self._session_id: Optional[str] = None
        self._stream_url: Optional[str] = None

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._buf = RTSPBuffer()
        self._keepalive_task: Optional[asyncio.Task] = None
        self._pending: dict[int, asyncio.Future] = {}  # cseq -> Future

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> SessionState:
        return self._state

    async def connect(self) -> None:
        """Open the TCP control channel and run the handshake."""
        log.info("Connecting to sink %s:%d", self.sink_host, RTSP_PORT)
        self._reader, self._writer = await asyncio.open_connection(
            self.sink_host, RTSP_PORT
        )
        self._set_state(SessionState.IDLE)
        recv_task = asyncio.create_task(self._recv_loop(), name="rtsp-recv")
        try:
            await self._handshake()
        except Exception:
            recv_task.cancel()
            raise
        # recv_task keeps running for keepalives / sink-initiated messages.

    async def teardown(self) -> None:
        """Initiate a clean teardown."""
        if self._state == SessionState.TEARDOWN:
            return
        self._set_state(SessionState.TEARDOWN)
        if self._keepalive_task:
            self._keepalive_task.cancel()
        try:
            await self._send_set_parameter_trigger("TEARDOWN")
        except Exception:
            pass
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()

    # ------------------------------------------------------------------
    # Internal: state
    # ------------------------------------------------------------------

    def _set_state(self, new_state: SessionState) -> None:
        if new_state != self._state:
            log.debug("State %s -> %s", self._state.value, new_state.value)
            self._state = new_state
            if self._on_state_change:
                self._on_state_change(new_state)

    def _next_cseq(self) -> int:
        self._cseq += 1
        return self._cseq

    # ------------------------------------------------------------------
    # Internal: I/O
    # ------------------------------------------------------------------

    async def _send(self, data: bytes) -> None:
        assert self._writer is not None
        log.debug(">> %s", data[:200])
        self._writer.write(data)
        await self._writer.drain()

    async def _recv_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                chunk = await self._reader.read(4096)
                if not chunk:
                    log.info("Sink closed connection")
                    break
                log.debug("<< %s", chunk[:200])
                self._buf.feed(chunk)
                while (msg := self._buf.take_message()) is not None:
                    await self._dispatch(msg)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.error("recv loop error: %s", exc)

    async def _dispatch(self, msg: RTSPMessage) -> None:
        """Route an inbound message to a pending request future or a handler."""
        if msg.is_response:
            cseq = msg.cseq()
            if cseq is not None and cseq in self._pending:
                fut = self._pending.pop(cseq)
                if not fut.done():
                    fut.set_result(msg)
                return
            log.warning("Unsolicited response CSeq=%s", cseq)
            return

        # Sink-initiated requests.
        method = msg.method
        if method == "OPTIONS":
            await self._handle_m2(msg)
        elif method == "SETUP":
            await self._handle_setup(msg)
        elif method == "PLAY":
            await self._handle_play(msg)
        elif method == "TEARDOWN":
            await self._handle_teardown(msg)
        elif method == "GET_PARAMETER":
            await self._reply_ok(msg)
        else:
            log.warning("Unhandled sink request: %s", method)
            await self._reply(msg, 501, "Not Implemented")

    async def _request(
        self,
        method: str,
        uri: str,
        extra_headers: Optional[dict[str, str]] = None,
        body: bytes = b"",
    ) -> RTSPMessage:
        """Send a request and await its response."""
        cseq = self._next_cseq()
        headers: dict[str, str] = {"CSeq": str(cseq)}
        if extra_headers:
            headers.update(extra_headers)

        loop = asyncio.get_event_loop()
        fut: asyncio.Future[RTSPMessage] = loop.create_future()
        self._pending[cseq] = fut

        await self._send(format_request(method, uri, headers, body))
        return await asyncio.wait_for(fut, timeout=10)

    async def _reply(
        self,
        req: RTSPMessage,
        status: int,
        reason: str,
        extra_headers: Optional[dict[str, str]] = None,
        body: bytes = b"",
    ) -> None:
        cseq = req.cseq()
        headers: dict[str, str] = {}
        if cseq is not None:
            headers["CSeq"] = str(cseq)
        if extra_headers:
            headers.update(extra_headers)
        await self._send(format_response(status, reason, headers, body))

    async def _reply_ok(
        self,
        req: RTSPMessage,
        extra_headers: Optional[dict[str, str]] = None,
        body: bytes = b"",
    ) -> None:
        await self._reply(req, 200, "OK", extra_headers, body)

    # ------------------------------------------------------------------
    # Internal: M1–M7 handshake
    # ------------------------------------------------------------------

    async def _handshake(self) -> None:
        await self._send_m1()
        # M2 is answered inside _dispatch when the sink sends OPTIONS.
        await self._send_m3()
        await self._send_m4()
        await self._send_m5()
        # M6 (SETUP) and M7 (PLAY) are sink-initiated; handled in _dispatch.

    # M1 -- Source sends OPTIONS to sink.
    async def _send_m1(self) -> None:
        self._set_state(SessionState.M1_SENT)
        resp = await self._request(
            "OPTIONS",
            f"rtsp://{self.sink_host}/wfd1.0",
            {"Require": "org.wfa.wfd1.0"},
        )
        log.debug("M1 response: %s %s", resp.status_code, resp.reason)
        self._set_state(SessionState.M2_DONE)

    # M2 -- Sink sends OPTIONS to source; we reply with our supported methods.
    async def _handle_m2(self, req: RTSPMessage) -> None:
        await self._reply_ok(
            req,
            {
                "Public": (
                    "org.wfa.wfd1.0, GET_PARAMETER, SET_PARAMETER, "
                    "SETUP, PLAY, PAUSE, TEARDOWN"
                )
            },
        )

    # M3 -- Source requests WFD capability parameters from sink.
    async def _send_m3(self) -> None:
        self._set_state(SessionState.M3_SENT)
        body = (
            "wfd_video_formats\r\n"
            "wfd_audio_codecs\r\n"
            "wfd_client_rtp_ports\r\n"
            "wfd_display_edid\r\n"
        ).encode()
        resp = await self._request(
            "GET_PARAMETER",
            "rtsp://localhost/wfd1.0",
            {"Content-Type": "text/parameters"},
            body,
        )
        self._set_state(SessionState.NEGOTIATING)
        self._process_m3_response(resp)

    def _process_m3_response(self, resp: RTSPMessage) -> None:
        """Parse sink capabilities from M3 response body (text/parameters)."""
        for line in resp.body.decode(errors="replace").splitlines():
            log.debug("M3 param: %s", line)
        # Full capability negotiation will be implemented with the media pipeline.

    # M4 -- Source sets WFD presentation parameters.
    async def _send_m4(self) -> None:
        self._set_state(SessionState.M4_SENT)
        # H.264 CBP level 3.2, 1920x1080 30fps, LPCM 48kHz stereo, RTP port 19000.
        body = (
            "wfd_video_formats: 00 00 02 10 0001DEFF 00000000 00000000 00 0000 0000 00 none none\r\n"
            "wfd_audio_codecs: LPCM 00000002 00\r\n"
            f"wfd_presentation_URL: rtsp://{self.local_host}/wfd1.0/streamid=0 none\r\n"
            "wfd_client_rtp_ports: RTP/AVP/UDP;unicast 19000 0 mode=play\r\n"
        ).encode()
        resp = await self._request(
            "SET_PARAMETER",
            "rtsp://localhost/wfd1.0",
            {"Content-Type": "text/parameters"},
            body,
        )
        log.debug("M4 response: %s %s", resp.status_code, resp.reason)
        self._stream_url = f"rtsp://{self.local_host}/wfd1.0/streamid=0"

    # M5 -- Source triggers the sink to send SETUP.
    async def _send_m5(self) -> None:
        self._set_state(SessionState.M5_SENT)
        body = b"wfd_trigger_method: SETUP\r\n"
        resp = await self._request(
            "SET_PARAMETER",
            "rtsp://localhost/wfd1.0",
            {"Content-Type": "text/parameters"},
            body,
        )
        log.debug("M5 response: %s %s", resp.status_code, resp.reason)
        self._set_state(SessionState.SETUP_WAIT)

    async def _send_set_parameter_trigger(self, trigger: str) -> None:
        body = f"wfd_trigger_method: {trigger}\r\n".encode()
        await self._request(
            "SET_PARAMETER",
            "rtsp://localhost/wfd1.0",
            {"Content-Type": "text/parameters"},
            body,
        )

    # M6 -- Sink sends SETUP; source replies with session ID.
    async def _handle_setup(self, req: RTSPMessage) -> None:
        if self._state != SessionState.SETUP_WAIT:
            log.warning("SETUP received in unexpected state %s", self._state)
        self._session_id = "vilya0001"
        await self._reply_ok(
            req,
            {
                "Session": f"{self._session_id};timeout=60",
                "Transport": req.get_header("Transport") or "",
            },
        )
        self._set_state(SessionState.PLAY_WAIT)

    # M7 -- Sink sends PLAY; source begins streaming.
    async def _handle_play(self, req: RTSPMessage) -> None:
        if self._state != SessionState.PLAY_WAIT:
            log.warning("PLAY received in unexpected state %s", self._state)
        await self._reply_ok(req, {"Session": self._session_id or ""})
        self._set_state(SessionState.STREAMING)
        log.info("Session active -- RTP stream should start now")
        self._keepalive_task = asyncio.create_task(
            self._keepalive_loop(), name="rtsp-keepalive"
        )

    async def _handle_teardown(self, req: RTSPMessage) -> None:
        await self._reply_ok(req)
        self._set_state(SessionState.TEARDOWN)
        if self._keepalive_task:
            self._keepalive_task.cancel()
        log.info("Session torn down by sink")

    # ------------------------------------------------------------------
    # Internal: keepalive
    # ------------------------------------------------------------------

    async def _keepalive_loop(self) -> None:
        try:
            while self._state == SessionState.STREAMING:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                if self._state != SessionState.STREAMING:
                    break
                log.debug("Sending keepalive GET_PARAMETER")
                try:
                    await self._request(
                        "GET_PARAMETER",
                        f"rtsp://{self.sink_host}/wfd1.0",
                        {"Session": self._session_id or ""},
                    )
                except asyncio.TimeoutError:
                    log.warning("Keepalive timed out -- session may be dead")
        except asyncio.CancelledError:
            pass
