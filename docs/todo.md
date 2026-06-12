# Vilya — Development TODO

## Phase 1: RTSP Protocol Layer — DONE

- [x] `vilya/rtsp/message.py` — TCP stream buffer, RTSP parser, message formatter
- [x] `vilya/rtsp/session.py` — WFDSession asyncio state machine (M1–M7, keepalive, teardown)
- [x] `tests/test_rtsp_message.py` — 27 unit tests, all passing
- [x] `pyproject.toml` — project scaffold

**Left incomplete by design (deferred to Phase 3):**
- `_process_m3_response` in `session.py` logs capability lines but does not negotiate codec
  parameters yet — needs the GStreamer pipeline to know available encoders.

---

## Phase 2: Wi-Fi Direct P2P — **DONE, VERIFIED ON HARDWARE 2026-06-11**

Full M1–M7 handshake completed against the Tab S8+ (`SECMX/Android16`): sink
connected to our listener, all messages exchanged, session reached STREAMING.
The Tab waits **20 s** for RTP on its port 19000, then sends TEARDOWN with
Samsung's "Can't show protected content. Stream is not secure." toast — that is
simply its no-stream timeout; Phase 3 (media pipeline) is the remaining work.

Tab's M3 capability response (input for Phase 3 negotiation):

    wfd_video_formats: 00 00 01 01 00000001 00000000 00000000 00 0000 0000 00 none none
    wfd_audio_codecs: LPCM 00000002 00, AAC 00000001 00
    wfd_client_rtp_ports: RTP/AVP/UDP;unicast 19000 0 mode=play
    wfd_display_edid: none

(Note: it advertises only CEA 640x480p60 in M3 yet accepted our 1080p M4 —
revisit during Phase 3 tuning.)

Goal: laptop acts as P2P Group Owner; Tab S8+ connects as client; DHCP assigns 192.168.137.x.

- [x] `vilya/p2p/supplicant.py` — wpa_supplicant D-Bus driver (dbus-fast):
      find, peer match by name, PBC connect with go_intent=15, GroupStarted
- [x] `vilya/p2p/wfd_ie.py` — WFD IE (source + session-available, RTSP 7236);
      set via the `WFDIEs` D-Bus property so Second Screen recognizes us
- [x] `vilya/p2p/dhcp.py` — minimal pure-Python DHCP server (dnsmasq not needed)
- [x] `vilya/__main__.py` — CLI: `scan` / `connect`; configures group iface
      (192.168.137.1/24), marks it NM-unmanaged, hands sink lease to `WFDSession`
- [ ] **Hardware test against the Tab S8+** — `sudo .venv/bin/python -m vilya connect`
- [ ] Handle role=client outcome (sink wins GO negotiation) — currently aborts
- [ ] Integration test: P2P handshake completes and the sink's RTSP port is reachable

### Root cause of the group teardown (found 2026-06-11)

Hardware runs proved GO negotiation, WPS, and group formation all **succeed**
(`P2P-GROUP-STARTED p2p-wlan0-N GO`), but the group died ~100 ms later.
**NetworkManager deliberately removes P2P groups it did not initiate**: its
GroupStarted handler wraps the new supplicant interface object, the P2P device
ignores the event ("we are not trying to connect"), and the wrapper's dispose()
calls RemoveInterface in wpa_supplicant (`nm-supplicant-interface.c`,
`set_state_down(self, TRUE, "NMSupplicantInterface is disposing")`). The removal
is silent (no P2P-GROUP-REMOVED event) and cancels the still-pending formation
timer, producing the empty-reason P2P-GROUP-FORMATION-FAILURE. No config option
avoids this; it is why gnome-network-displays drives P2P *through* NM.

Consequence: `vilya/p2p/nm.py` — NM backend (default) that forms the group via
AddAndActivateConnection2 (wifi-p2p type, wfd-ies, wps-method=pbc, manual IP
192.168.137.1/24 so dnsmasq is not needed, persist=volatile,
bind-activation=dbus-client). The supplicant backend remains for NM-less systems
(`--backend supplicant`).

Also learned: the Tab S8+ advertises RTSP control port **49158** (0xC006) in its WFD
IE, not the canonical 7236 — vilya now reads the port from the peer's IE.

Second NM-backend run: with NM's hardcoded go_intent=7 the **Tab wins GO negotiation**
(vs. our supplicant backend's intent 15 where we won). As P2P client this is simpler:
the Tab assigns our IP via EAPOL IP allocation (us=192.168.49.200, GO=192.168.49.1,
Android convention: GO is always x.y.z.1) — no DHCP in either direction. The
connection profile now uses ipv4.method=auto. Read the assigned address from the
**kernel** (`ip -j addr`), not NM's Ip4Config D-Bus prop — the latter lags the
ACTIVATED state change and returned empty, making vilya misread role=GO.

### RTSP transport direction was backwards (fixed 2026-06-11)

ConnectionRefused (RST) on every dial to the Tab's ports revealed it: in WFD the
**source LISTENS on RTSP 7236 and the sink connects to it** — not the reverse.
(Confirmed: MS-MICE + Wi-Fi Display spec; our own Win capture shows the source at
7236.) `WFDSession.serve()` now binds 0.0.0.0:7236, accepts the sink's connection,
then sends M1 as before. The old active-connect `connect()` is kept for tests.
The sink's advertised port (Tab=49158) is NOT a listener — ignore it for the
control channel. Full M1–M7 verified against a simulated sink (reaches STREAMING).

Findings that unblocked this phase (2026-06-11):
- The old venv pointed at `/home/juan/projects/vilya` (renamed dir) — every tool in it was broken.
- wpa_supplicant D-Bus policy is root-only; `wpa_cli` as a user can never work. Run vilya with sudo.
- NM auto-creates `p2p-dev-wlan0` but had never performed any P2P operation (journal is clean).

---

## Phase 3: Media Pipeline — IN PROGRESS (built 2026-06-11, needs hardware test)

Goal: screen pixels flow from KDE Plasma → Tab display via RTP.

- [x] `vilya/media/portal.py` — xdg-desktop-portal ScreenCast negotiation
      (session bus; restore-token persisted in ~/.local/state/vilya so the
      picker dialog appears only once)
- [x] `vilya/media/pipeline.py` — gst-launch subprocess: 720p30 H.264 CBP L3.1
      (matches M4) → mpegtsmux alignment=7 → rtpmp2tpay → udpsink. `--source
      test` (SMPTE bars + clock) to validate the path before real capture.
- [x] Pipeline start on STREAMING, stop on TEARDOWN/pipeline-death; RTP port
      parsed from M6 SETUP `client_port`
- [x] M4 now selects one mode (CBP L3.1 720p30) matching the Tab's M3 caps
- [x] **No more sudo for the NM backend** — polkit allows user P2P activation,
      and the portal *requires* the user session. supplicant backend still root.
- [x] **Hardware test, test source**: SMPTE bars + clock rendered on the Tab
      (2026-06-11). The fuzzy bottom-right box is videotestsrc's built-in noise
      square — worst-case encoder load, passed fine.
- [x] **Hardware test, screen source**: desktop mirrored to the Tab (2026-06-11).
      First cut had ~7 s latency at 720p.
- [x] Latency/resolution pass: leaky queues (drop stale frames instead of
      accumulating delay), threaded videoconvert, vbv-buf-capacity=300,
      `--mode 720p30|1080p30|1080p60` (default 1080p30 = panel-native, no
      scaling; M4 line derives from the mode — CHP L4.2 for 1080p)
- [x] **Hardware test: latency + 1080p30** — sub-0.5 s perceived latency,
      native-res mirror working (2026-06-11)
- [x] `--reselect` flag: re-show the portal screen picker (otherwise the
      saved restore token reconnects silently)
- [ ] Try `--mode 1080p60`

### Known behavior / open items

- **Samsung stale-pairing wedge** (bit us 2026-06-12): every vilya session makes
  the Tab (as GO) mint a new persistent group ("DIRECT-xx"). After ~15
  accumulated entries the Tab still completes P2P pairing but its Miracast
  client never dials our RTSP port — looks exactly like a vilya regression but
  isn't. Fix: clear the Tab's Wi-Fi Direct pairings or reboot it. Long-term:
  investigate persistent-group reuse / invitation flow (NM's wifi-p2p setting
  exposes no persistent flag; would need the supplicant backend).

- Samsung shows "Can't show protected content. Stream is not secure." on
  connect: informational — we do no HDCP, so DRM apps won't render over this
  link; normal desktop pixels are unaffected. Implementing HDCP 2.x is out of
  scope (licensed keys). May also flash during the ~1 s PLAY→first-RTP gap.
- **Extended (vs mirrored) display**: requires creating a virtual output. KWin
  (Plasma 6.6) exposes no D-Bus for this; the path is KWin's
  `zkde_screencast_unstable_v1` Wayland protocol (create-virtual-output-stream,
  as used by krfb-virtualmonitor) → yields a PipeWire node directly. Pairs
  naturally with streaming a Tab-native-ish resolution (2560x1600 via WFD VESA
  modes) since a virtual display isn't bound to the panel's 1080p. Big feature,
  own cycle.
- Mirror resolution is capped by the panel (1920x1080): streaming higher would
  only upscale. Higher-than-panel resolution only makes sense with a virtual
  display (above).
- [ ] Audio: LPCM 48 kHz capture (pipewiresrc monitor) muxed into the TS
- [ ] Complete `_process_m3_response`: real capability negotiation for M4
- [ ] Tune encoder (bitrate/latency); consider vah264enc (Intel VA-API) later
- [ ] Latency/quality pass once pixels are flowing

---

## Phase 4: UIBC Touch Input — TODO (future)

Goal: touch events on the Tab are forwarded back to the Linux host as pointer input.

- [ ] Parse `wfd_uibc_capability` from M3 response
- [ ] Negotiate UIBC in M4 `SET_PARAMETER`
- [ ] TCP back-channel listener for UIBC HID events
- [ ] Translate UIBC touch events to `uinput` pointer events on the host

---

## Known constraints / decisions

| Topic | Decision |
|---|---|
| HDCP | Advertised by Tab, skipped by us — Tab does not require it |
| microsoft_* / intel_* extensions | Tab replies `none` to all — not implemented |
| Audio codec | LPCM only (48kHz stereo); AAC advertised by Tab but not needed |
| Video codec | H.264 CBP level 3.2, up to 1080p60 |
| RTP port | 19000 UDP (sink), blocksize 1328 |
| RTSP port | source LISTENS on 7236; sink dials in. Sink's advertised port (Tab 49158) is not a control listener |
| Keepalive interval | 30 s (GET_PARAMETER) |
| P2P subnet | 192.168.137.x (from Wireshark capture of working Win→Tab session) |
