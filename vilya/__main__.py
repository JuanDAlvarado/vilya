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
import subprocess
import sys

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


async def cmd_connect(args: argparse.Namespace) -> int:
    dev = _make_backend(args)
    await dev.open()
    dhcp: DHCPServer | None = None
    session: WFDSession | None = None
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
            on_state_change=lambda s: log.info("RTSP state: %s", s.value),
        )
        # Bind 0.0.0.0 so we accept the sink's connection on whichever
        # address it dials (it connects to the IP it assigned us).
        await session.serve(listen_host="0.0.0.0", accept_timeout=args.timeout)

        log.info("Handshake driven to %s. Ctrl-C to tear down.", session.state.value)
        while session.state != SessionState.TEARDOWN:
            await asyncio.sleep(1)
        return 0
    except (TimeoutError, asyncio.TimeoutError) as exc:
        log.error("%s", str(exc) or "Timed out (sink never contacted us)")
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if session:
            try:
                await session.teardown()
            except Exception:
                pass
        if dhcp:
            dhcp.stop()
        await dev.disconnect()
        await dev.close()


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
    p_conn.set_defaults(func=cmd_connect)

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

    if os.geteuid() != 0:
        log.error(
            "vilya needs root (wpa_supplicant D-Bus policy + DHCP port 67). "
            "Run: sudo %s -m vilya %s",
            sys.executable,
            args.command,
        )
        return 2

    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
