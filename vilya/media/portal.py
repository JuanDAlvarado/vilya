"""Screen capture negotiation via the xdg-desktop-portal ScreenCast API.

On Wayland there is no direct screen grabbing; the portal brokers access:
CreateSession -> SelectSources -> Start (shows the compositor's picker
dialog once) -> OpenPipeWireRemote. The result is a PipeWire node id plus
a connection fd that GStreamer's pipewiresrc can consume.

A restore token is persisted so the picker dialog only appears on the
first run -- subsequent connects are silent, Win+K style.

Must run on the *user* session bus (not as root).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

log = logging.getLogger(__name__)

PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"

SOURCE_TYPE_MONITOR = 1
CURSOR_MODE_EMBEDDED = 2
PERSIST_MODE_PERMANENT = 2

TOKEN_PATH = Path(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
) / "vilya" / "screencast-token"


def _load_restore_token() -> Optional[str]:
    try:
        return TOKEN_PATH.read_text().strip() or None
    except OSError:
        return None


def _save_restore_token(token: str) -> None:
    try:
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(token)
    except OSError as exc:
        log.warning("Could not persist screencast restore token: %s", exc)


class ScreenCastSession:
    """One negotiated portal screencast: a PipeWire fd + node id."""

    def __init__(self) -> None:
        self._bus: Optional[MessageBus] = None
        self._screencast = None
        self._session_handle: Optional[str] = None
        self._counter = 0

        self.pipewire_fd: Optional[int] = None
        self.node_id: Optional[int] = None

    async def open(self) -> None:
        """Run the full portal negotiation. May show the picker dialog."""
        self._bus = await MessageBus(
            bus_type=BusType.SESSION, negotiate_unix_fd=True
        ).connect()
        intro = await self._bus.introspect(PORTAL_SERVICE, PORTAL_PATH)
        obj = self._bus.get_proxy_object(PORTAL_SERVICE, PORTAL_PATH, intro)
        self._screencast = obj.get_interface(SCREENCAST_IFACE)

        results = await self._request(
            self._screencast.call_create_session,
            {"session_handle_token": Variant("s", "vilya_session")},
        )
        self._session_handle = results["session_handle"].value

        select_opts = {
            "types": Variant("u", SOURCE_TYPE_MONITOR),
            "multiple": Variant("b", False),
            "cursor_mode": Variant("u", CURSOR_MODE_EMBEDDED),
            "persist_mode": Variant("u", PERSIST_MODE_PERMANENT),
        }
        token = _load_restore_token()
        if token:
            select_opts["restore_token"] = Variant("s", token)
        await self._request(
            self._screencast.call_select_sources,
            self._session_handle,
            select_opts,
        )

        results = await self._request(
            self._screencast.call_start, self._session_handle, "", {}
        )
        streams = results["streams"].value
        if not streams:
            raise RuntimeError("Portal returned no screencast streams")
        self.node_id = streams[0][0]
        new_token = results.get("restore_token")
        if new_token:
            _save_restore_token(new_token.value)

        self.pipewire_fd = await self._screencast.call_open_pipe_wire_remote(
            self._session_handle, {}
        )
        log.info(
            "Screencast ready: PipeWire node %d (fd %d)",
            self.node_id,
            self.pipewire_fd,
        )

    async def close(self) -> None:
        if self.pipewire_fd is not None:
            try:
                os.close(self.pipewire_fd)
            except OSError:
                pass
            self.pipewire_fd = None
        if self._bus and self._session_handle:
            # Closing the session releases the capture grant.
            try:
                await self._bus.call(
                    Message(
                        destination=PORTAL_SERVICE,
                        path=self._session_handle,
                        interface="org.freedesktop.portal.Session",
                        member="Close",
                    )
                )
            except Exception as exc:
                log.debug("Portal session close: %s", exc)
        if self._bus:
            self._bus.disconnect()

    # ------------------------------------------------------------------
    # Request/Response plumbing
    # ------------------------------------------------------------------

    async def _request(self, method, *args) -> dict:
        """Call a portal method and await its Request object's Response.

        Portal methods return immediately with a Request object path; the
        actual results arrive as a Response signal on that path. The path
        is predictable from our unique name + handle_token, so we listen
        before calling to avoid the race.
        """
        self._counter += 1
        token = f"vilya{self._counter}"
        sender = self._bus.unique_name[1:].replace(".", "_")
        request_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict] = loop.create_future()

        def handler(msg: Message):
            if (
                msg.message_type == MessageType.SIGNAL
                and msg.interface == REQUEST_IFACE
                and msg.member == "Response"
                and msg.path == request_path
            ):
                code, results = msg.body
                if not fut.done():
                    if code == 0:
                        fut.set_result(results)
                    else:
                        fut.set_exception(
                            RuntimeError(
                                f"Portal request failed (code={code}; "
                                "user cancelled the dialog?)"
                            )
                        )

        self._bus.add_message_handler(handler)
        try:
            # Inject our handle_token into the options dict (last arg).
            *head, options = args
            options = dict(options)
            options["handle_token"] = Variant("s", token)
            await method(*head, options)
            return await asyncio.wait_for(fut, timeout=120)
        finally:
            self._bus.remove_message_handler(handler)
