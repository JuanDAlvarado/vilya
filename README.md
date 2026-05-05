# Vilya
 
> *"Vilya, the Ring of Air, mightiest of the three..."*
 
An attempt to fix the Miracast source stack on Linux, once and for all.
 
Vilya is a Wi-Fi Display (Miracast) source implementation for Linux, written in Python. It allows a Linux machine running KDE Plasma (or any Wayland/X11 desktop) to wirelessly extend its display to a Miracast-capable sink — the way Win+K works on Windows — without proprietary software, without a virtual display hack, and without a browser tab pretending to be a monitor.
 
**Status: Active development. Phase 1 (RTSP protocol layer) complete. Not yet functional end-to-end.**
 
---
 
## The Problem
 
Miracast on Linux is broken. It has been broken for years. `gnome-network-displays` gets close but has brittle capability negotiation and a fragile GStreamer pipeline. `miraclecast` is structurally sound but unmaintained. No implementation reliably completes the full M1–M7 WFD handshake and sustains a video stream against real-world sink devices.
 
Vilya is a ground-up rewrite built against a real Wireshark capture of a working Windows → Samsung Galaxy Tab S8+ Miracast session. Every protocol decision is derived from observed behavior, not assumptions.
 
---
 
## How It Works
 
Miracast is Wi-Fi Display (WFD) — a Wi-Fi Alliance standard. The stack, bottom to top:
 
```
Wi-Fi Direct (P2P)        wpa_supplicant, laptop as Group Owner
        │
WFD RTSP Session          TCP port 7236, M1–M7 handshake
        │
RTP/UDP Media Stream      H.264 + LPCM audio → MPEG-TS → RTP → sink:19000
        │
Sink Display              Your Miracast device renders the stream
```
 
Vilya controls each layer directly. No NetworkManager dependency. No GNOME stack required.
 
---
 
## Target Hardware
 
**Developed and tested against:** Samsung Galaxy Tab S8+ (Android 16, `SECMX` Miracast stack)
 
**Source machine:** Arch Linux, KDE Plasma, Qualcomm Atheros QCA6174 (`ath10k` driver)
 
Any Miracast-compliant sink should work. Any Linux machine with a Wi-Fi card that supports P2P Group Owner mode should work as a source.
 
---
 
## Architecture
 
```
vilya/
├── rtsp/
│   ├── message.py       # RTSP parser and formatter (TCP stream buffering, header parsing)
│   └── session.py       # WFDSession asyncio state machine (M1–M7, keepalive, teardown)
├── p2p/
│   └── wifidirect.py    # wpa_supplicant P2P Group Owner, dnsmasq DHCP
├── pipeline/
│   └── gstreamer.py     # PipeWire capture → H.264 → MPEG-TS → RTP
└── main.py              # Entry point
```
 
---
 
## Development Roadmap
 
- [x] **Phase 1** — RTSP protocol layer: parser, formatter, WFD session state machine, 27 unit tests
- [ ] **Phase 2** — Wi-Fi Direct P2P: Group Owner mode, WFD IE advertisement, DHCP
- [ ] **Phase 3** — Media pipeline: PipeWire capture, GStreamer H.264/MPEG-TS/RTP, capability negotiation
- [ ] **Phase 4** — UIBC: touch input back-channel forwarded to Linux host via uinput
- [ ] **Future** — Rewrite in Go for distribution as a proper system daemon
---
 
## Language
 
The prototype is written in Python. This is intentional. The hardest part of this project is not the language — it is understanding the protocol well enough to implement it correctly. Python removes friction during that learning phase. When something breaks, it is a protocol bug, not a memory bug or a type system fight.
 
Once Vilya works end-to-end and every layer is fully understood, the plan is a rewrite in Go. Go produces a single static binary, packages cleanly on AUR, and its concurrency model (goroutines) is a natural fit for a daemon that simultaneously manages a TCP control channel, a UDP media stream, and a keepalive timer. `tailscaled` — which implements a custom VPN protocol from scratch as a system daemon — is the closest spiritual analog and it is Go.
 
The Python prototype is not throwaway work. The protocol logic, state machine design, and test suite all transfer directly. Code is the least valuable artifact. The understanding is the valuable thing.
 
---
 
## Known Protocol Decisions
 
| Topic | Decision |
|---|---|
| HDCP | Advertised by sink, not implemented — not required |
| `microsoft_*` / `intel_*` extensions | Samsung sink replies `none` to all — not implemented |
| Audio codec | LPCM 48kHz stereo only |
| Video codec | H.264 Constrained Baseline Profile, up to 1080p60 |
| RTP port | 19000 UDP (sink), blocksize 1328 |
| RTSP control port | 7236 TCP |
| Keepalive interval | 30s `GET_PARAMETER` |
| P2P subnet | 192.168.137.x |
 
---
 
## Dependencies
 
```
python >= 3.11
gstreamer >= 1.22
gst-plugins-good
gst-plugins-bad (x264enc)
pipewire
wpa_supplicant (with P2P support)
dnsmasq
```
 
Python dependencies are stdlib only for the protocol layer. GStreamer is accessed via `gst-python` bindings.
 
---
 
## Reference Material
 
- [gnome-network-displays](https://gitlab.gnome.org/GNOME/gnome-network-displays) — WFD state machine reference (`src/wfd/`)
- [miraclecast (albfan fork)](https://github.com/albfan/miraclecast) — protocol structure and bitmask reference (`src/shared/wfd.c`)
- Wi-Fi Display Technical Specification v1.1 — Wi-Fi Alliance (widely mirrored)
- Real Wireshark pcap of Windows → Samsung Tab S8+ session (in `docs/`)
---
 
## License
 
GPL-3.0. The commons stays common.
 
---
 
## Name
 
Vilya is the Ring of Air in Tolkien's legendarium — the mightiest of the three Elven rings, associated with sky, wind, and things that travel unseen through the air. The Miracast signal travels through the air. It seemed right.
 
The obvious names were taken by defense contractors.
