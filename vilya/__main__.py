"""Vilya CLI.

    sudo .venv/bin/python -m vilya scan
    sudo .venv/bin/python -m vilya connect --peer "Tab S8"

Root is required: wpa_supplicant's D-Bus policy only admits root, and
the DHCP server binds port 67.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys

import json

from .input.uibc import (
    GENERIC_TOUCH_DOWN,
    GENERIC_TOUCH_MOVE,
    GENERIC_TOUCH_UP,
    TouchEvent,
    UIBCServer,
)
from .input.uinput import AbsolutePointer
from .media import audio
from .media.kwin_screencast import KWinVirtualOutput
from .media.pipeline import MediaPipeline, default_monitor, missing_elements
from .media.portal import ScreenCastSession
from .modes import MODES
from .p2p.dhcp import SERVER_IP, DHCPServer
from .p2p.nm import NMP2PDevice
from .p2p.supplicant import Group, P2PDevice
from .rtsp.session import SessionState, WFDSession

log = logging.getLogger("vilya")


def _make_backend(args: argparse.Namespace) -> P2PDevice | NMP2PDevice:
    if args.backend == "nm":
        return NMP2PDevice(args.interface)
    return P2PDevice(args.interface)


def _run(cmd: list[str], check: bool = True) -> None:
    log.debug("exec: %s", " ".join(cmd))
    subprocess.run(cmd, check=check, capture_output=True)


def _configure_group_interface(group: Group) -> None:
    """Give the GO interface its static IP (supplicant backend only;
    with the NM backend, NM applies the profile's manual IP itself)."""
    if shutil.which("nmcli"):
        _run(["nmcli", "device", "set", group.ifname, "managed", "no"], check=False)
    _run(["ip", "addr", "replace", f"{SERVER_IP}/24", "dev", group.ifname])
    _run(["ip", "link", "set", group.ifname, "up"])
    log.info("%s configured as %s/24", group.ifname, SERVER_IP)


async def cmd_scan(args: argparse.Namespace) -> int:
    dev = _make_backend(args)
    await dev.open()
    try:
        await dev.set_device_name(args.name)
        await dev.set_wfd_ie()
        await dev.find()
        log.info("Scanning for %ds... (put the Tab in Second Screen mode)", args.time)
        await asyncio.sleep(args.time)
        await dev.stop_find()
        if not dev.peers:
            log.warning("No P2P peers found")
            return 1
        print(f"{'NAME':30s} {'ADDRESS':17s}")
        for peer in dev.peers.values():
            print(f"{peer.name:30s} {peer.address:17s}")
        return 0
    finally:
        await dev.close()


async def _start_uibc(
    session: WFDSession, mode, target_substr: str
) -> tuple[UIBCServer, AbsolutePointer]:
    """Bring up the touch back-channel: uinput device, UIBC listener,
    then tell the sink to start sending (M14)."""
    target, union_w, union_h = _screen_layout(target_substr)
    tx, ty, tw, th = target
    pointer = AbsolutePointer(union_w, union_h)
    log.info(
        "Touch mapping: sink %dx%d -> output at %d,%d (%dx%d) in %dx%d union",
        mode.width, mode.height, tx, ty, tw, th, union_w, union_h,
    )

    def on_touch(ev: TouchEvent) -> None:
        # Sink coords are in mode-space; scale into the target output.
        x = tx + ev.x * tw // max(mode.width, 1)
        y = ty + ev.y * th // max(mode.height, 1)
        if ev.kind == GENERIC_TOUCH_DOWN:
            pointer.press(x, y)
        elif ev.kind == GENERIC_TOUCH_MOVE:
            pointer.move(x, y)
        elif ev.kind == GENERIC_TOUCH_UP:
            pointer.release()

    uibc = UIBCServer(on_touch, port=session.uibc_port)
    await uibc.start()
    await session.enable_uibc()
    return uibc, pointer


async def cmd_connect(args: argparse.Namespace) -> int:
    audio_monitor = None
    audio_route = None
    if not args.no_audio:
        audio_route = audio.setup()
        if audio_route:
            audio_monitor = audio_route.monitor
        else:
            audio_monitor = default_monitor()  # plays on both, but works
        if audio_monitor is None:
            log.warning("No default audio sink found; continuing without audio")
    missing = missing_elements(audio=audio_monitor is not None)
    if missing:
        log.error(
            "Missing GStreamer elements: %s. Install: "
            "sudo pacman -S --needed gst-plugins-good gst-plugins-bad "
            "gst-plugins-ugly x264",
            ", ".join(missing),
        )
        return 2

    dev = _make_backend(args)
    await dev.open()
    dhcp: DHCPServer | None = None
    session: WFDSession | None = None
    pipeline: MediaPipeline | None = None
    portal: ScreenCastSession | None = None
    kwin_output: KWinVirtualOutput | None = None
    uibc: UIBCServer | None = None
    pointer: AbsolutePointer | None = None
    pw_fd = pw_node = None
    try:
        await dev.set_device_name(args.name)
        await dev.set_wfd_ie()
        await dev.find()

        log.info("Looking for peer matching %r...", args.peer)
        peer = await dev.wait_for_peer(args.peer, timeout=args.timeout)
        await dev.stop_find()

        group = None
        for attempt in range(3):
            try:
                group = await dev.connect(peer)
                break
            except (RuntimeError, TimeoutError) as exc:
                log.warning(
                    "Group formation failed (%s). The Tab may have left its "
                    "'waiting for connection' screen -- make sure Second "
                    "Screen is armed. Retrying (%d/3)...",
                    exc,
                    attempt + 1,
                )
                await asyncio.sleep(2)
        if group is None:
            log.error(
                "Could not form a P2P group. Toggle Second Screen off/on "
                "on the Tab and run vilya again."
            )
            return 1

        sink_ip: str | None = getattr(dev, "sink_ip", None)
        local_ip: str = getattr(dev, "local_ip", None) or SERVER_IP

        if sink_ip is None:
            # We are GO: hand the sink an address ourselves.
            if group.role != "GO":
                log.error("P2P client role but no sink address -- cannot proceed")
                return 1
            if args.backend == "supplicant":
                _configure_group_interface(group)

            loop = asyncio.get_event_loop()
            lease_fut: asyncio.Future[str] = loop.create_future()

            def on_lease(mac: str, ip: str) -> None:
                if not lease_fut.done():
                    lease_fut.set_result(ip)

            dhcp = DHCPServer(group.ifname, on_lease=on_lease)
            await dhcp.start()

            log.info("Waiting for the Tab to request an IP address...")
            sink_ip = await asyncio.wait_for(lease_fut, timeout=60)

        # Negotiate screen capture BEFORE the RTSP handshake: the portal
        # may show a picker dialog, and the Tab only waits ~20 s for RTP
        # after PLAY. With a restore token this is instant.
        mode = MODES[
            args.mode or ("1200p30" if args.display == "extend" else "1080p30")
        ]
        if args.source == "screen":
            if args.display == "extend":
                # Prefer KWin's native protocol: it honors our exact
                # dimensions (e.g. 1920x1200). Needs the whitelisted
                # interpreter (vilya setup-extend); fall back to the
                # portal's fixed-1080p virtual output otherwise.
                kwin_output = KWinVirtualOutput()
                try:
                    pw_node = await kwin_output.open(
                        mode.width, mode.height, name="vilya"
                    )
                    pw_fd = None
                except RuntimeError as exc:
                    log.warning(
                        "KWin virtual output unavailable (%s); falling "
                        "back to portal (fixed 1080p). For native sizes "
                        "run 'vilya setup-extend' once and launch via "
                        ".venv/bin/vilya-python.",
                        exc,
                    )
                    kwin_output = None
                    portal = ScreenCastSession()
                    await portal.open(force_picker=args.reselect, virtual=True)
                    pw_fd, pw_node = portal.pipewire_fd, portal.node_id
                log.info(
                    "Extended-desktop mode: a virtual monitor will appear "
                    "in Plasma once streaming starts (drag windows onto it)"
                )
            else:
                portal = ScreenCastSession()
                await portal.open(force_picker=args.reselect)
                pw_fd, pw_node = portal.pipewire_fd, portal.node_id

        # In WFD the source LISTENS on its RTSP port (7236) and the sink
        # dials in. We advertise 7236 in our WFD IE, so the Tab connects
        # to us at local_ip:7236.
        log.info(
            "We are %s, sink %s -- listening for the sink's RTSP connection",
            local_ip,
            sink_ip,
        )
        session = WFDSession(
            sink_ip,
            local_ip,
            video_format_line=mode.m4_video_formats,
            advertise_audio=audio_monitor is not None,
            on_state_change=lambda s: log.info("RTSP state: %s", s.value),
        )
        # Bind 0.0.0.0 so we accept the sink's connection on whichever
        # address it dials (it connects to the IP it assigned us).
        await session.serve(listen_host="0.0.0.0", accept_timeout=args.timeout)

        log.info("Handshake driven to %s. Ctrl-C to tear down.", session.state.value)
        try:
            while session.state != SessionState.TEARDOWN:
                if session.state == SessionState.STREAMING and pipeline is None:
                    pipeline = MediaPipeline(
                        sink_ip,
                        session.sink_rtp_port,
                        source=args.source,
                        pipewire_fd=pw_fd,
                        pipewire_node=pw_node,
                        mode=mode,
                        audio_monitor=audio_monitor,
                    )
                    pipeline.start()
                    if session.uibc_negotiated:
                        try:
                            target = (
                                "virtual" if args.display == "extend" else "eDP"
                            )
                            uibc, pointer = await _start_uibc(
                                session, mode, target
                            )
                        except Exception as exc:
                            log.warning("UIBC setup failed: %s", exc)
                if pipeline and pipeline.poll() is not None:
                    log.error("Media pipeline died; tearing down")
                    break
                await asyncio.sleep(0.2)
            return 0
        finally:
            if portal:
                await portal.close()
    except (TimeoutError, asyncio.TimeoutError) as exc:
        log.error("%s", str(exc) or "Timed out (sink never contacted us)")
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if audio_route:
            audio.teardown(audio_route)
        if uibc:
            uibc.stop()
        if pointer:
            try:
                pointer.close()
            except Exception:
                pass
        if pipeline:
            pipeline.stop()
        if kwin_output:
            try:
                await kwin_output.close()
            except Exception:
                pass
        if session:
            try:
                await session.teardown()
            except Exception:
                pass
        if dhcp:
            dhcp.stop()
        await dev.disconnect()
        await dev.close()


def _screen_layout(target_substr: str) -> tuple[tuple[int, int, int, int], int, int]:
    """Return ((x, y, w, h) of the target output, union_w, union_h).

    Uses kscreen-doctor JSON. The uinput pointer spans the union of all
    outputs; touches map into the target output's rectangle within it.
    """
    out = subprocess.run(
        ["kscreen-doctor", "-j"], capture_output=True, text=True, check=True
    )
    data = json.loads(out.stdout)
    union_w = union_h = 0
    target = None
    for output in data.get("outputs", []):
        if not output.get("enabled"):
            continue
        pos = output["pos"]
        size = output["size"]
        union_w = max(union_w, pos["x"] + size["width"])
        union_h = max(union_h, pos["y"] + size["height"])
        if target_substr.lower() in output.get("name", "").lower():
            target = (pos["x"], pos["y"], size["width"], size["height"])
    if target is None:
        raise RuntimeError(f"No enabled output matching {target_substr!r}")
    return target, union_w, union_h


async def cmd_setup_extend(args: argparse.Namespace) -> int:
    """Install the KWin whitelist pieces for native-size virtual outputs.

    KWin only exposes its screencast protocol to executables matched to
    a desktop file declaring X-KDE-Wayland-Interfaces. A *copy* (not
    symlink) of the interpreter gives vilya its own /proc/<pid>/exe
    identity to whitelist.
    """
    venv_bin = os.path.dirname(sys.executable)
    dedicated = os.path.join(venv_bin, "vilya-python")
    real = os.path.realpath(sys.executable)
    if os.path.realpath(dedicated) != real or not os.path.exists(dedicated):
        shutil.copy2(real, dedicated)
    log.info("Interpreter copy: %s", dedicated)

    apps = os.path.join(
        os.environ.get(
            "XDG_DATA_HOME", os.path.expanduser("~/.local/share")
        ),
        "applications",
    )
    os.makedirs(apps, exist_ok=True)
    desktop = os.path.join(apps, "vilya.desktop")
    with open(desktop, "w") as f:
        f.write(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Vilya\n"
            f"Exec={dedicated}\n"
            "NoDisplay=true\n"
            "X-KDE-Wayland-Interfaces=zkde_screencast_unstable_v1\n"
        )
    log.info("Desktop entry: %s", desktop)
    subprocess.run(["kbuildsycoca6"], capture_output=True)
    log.info(
        "Done. Use extended mode via: %s -m vilya connect --display extend",
        dedicated,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="vilya")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--interface", default="wlan0", help="Wi-Fi interface")
    parser.add_argument("--name", default="vilya", help="our P2P device name")
    parser.add_argument(
        "--backend",
        choices=["nm", "supplicant"],
        default="nm",
        help="P2P control backend: NetworkManager (default; required when NM "
        "manages the Wi-Fi interface, since NM removes P2P groups formed "
        "behind its back) or wpa_supplicant directly",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="list visible P2P peers")
    p_scan.add_argument("--time", type=int, default=15, help="scan seconds")
    p_scan.set_defaults(func=cmd_scan)

    p_conn = sub.add_parser("connect", help="connect to a sink and run the handshake")
    p_conn.add_argument("--peer", default="Tab", help="substring of sink device name")
    p_conn.add_argument("--timeout", type=int, default=60, help="discovery timeout")
    p_conn.add_argument(
        "--source",
        choices=["screen", "test"],
        default="screen",
        help="video source: 'screen' (Wayland portal capture, default) or "
        "'test' (SMPTE bars, validates the media path)",
    )
    p_conn.add_argument(
        "--mode",
        choices=sorted(MODES),
        default=None,
        help="video mode (resolution/framerate). Defaults: mirror=1080p30 "
        "(panel-native), extend=1200p30 (Tab-native 16:10 shape)",
    )
    p_conn.add_argument(
        "--reselect",
        action="store_true",
        help="show the screen picker again instead of reusing the saved choice",
    )
    p_conn.add_argument(
        "--no-audio",
        action="store_true",
        help="video only: no desktop audio capture and no audio in M4",
    )
    p_conn.add_argument(
        "--display",
        choices=["mirror", "extend"],
        default="mirror",
        help="mirror an existing screen (default) or create a virtual "
        "monitor so the sink acts as an extended desktop",
    )
    p_conn.set_defaults(func=cmd_connect)

    p_setup = sub.add_parser(
        "setup-extend",
        help="one-time install of the KWin whitelist for native-size "
        "virtual outputs (extended-desktop mode)",
    )
    p_setup.set_defaults(func=cmd_setup_extend)

    # Accept global flags after the subcommand too; SUPPRESS keeps the
    # subparser from clobbering values given before it.
    for p in (p_scan, p_conn):
        p.add_argument(
            "-v", "--verbose", action="store_true", default=argparse.SUPPRESS
        )
        p.add_argument(
            "--backend",
            choices=["nm", "supplicant"],
            default=argparse.SUPPRESS,
        )

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # The supplicant backend talks to wpa_supplicant's root-only D-Bus
    # API and binds DHCP port 67. The NM backend needs neither -- and
    # screen capture REQUIRES the user session (the portal is not
    # reachable as root), so don't run nm-backend connects with sudo.
    if args.backend == "supplicant" and os.geteuid() != 0:
        log.error(
            "The supplicant backend needs root. Run: sudo %s -m vilya %s",
            sys.executable,
            args.command,
        )
        return 2
    if (
        args.backend == "nm"
        and os.geteuid() == 0
        and getattr(args, "source", None) == "screen"
    ):
        log.error(
            "Screen capture needs your user session (xdg-desktop-portal); "
            "run WITHOUT sudo: %s -m vilya connect",
            sys.executable,
        )
        return 2

    # SIGTERM should tear the session down exactly like Ctrl-C, so the
    # finally-blocks (pipeline stop, RTSP TEARDOWN, NM deactivate) run.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))

    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
