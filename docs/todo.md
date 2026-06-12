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

## Phase 3: Media Pipeline — **DONE, VERIFIED ON HARDWARE 2026-06-11/12** (tuning items remain)

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
- **Extended display: DONE (2026-06-12).** Two routes, best-first:
  (1) KWin's `zkde_screencast_unstable_v1` via our hand-rolled Wayland client
  (`vilya/media/kwin_screencast.py`) — any size; requires the whitelist that
  `vilya setup-extend` installs (interpreter copy + desktop file with
  X-KDE-Wayland-Interfaces, matched by /proc/pid/exe) and launching via
  `.venv/bin/vilya-python`. (2) Portal VIRTUAL source fallback (fixed 1080p).
  The Tab ACCEPTS WFD VESA 1920x1200p30 (M4 200 OK) despite advertising no
  VESA modes — extend defaults to 1200p30, full-bleed 16:10. Latency measured
  100-230 ms across runs.
- **Latency variance**: most sessions settle ~150-200 ms; occasionally one
  calibrates >1 s at startup and stays there (Tab's adaptive jitter buffer vs
  our 1/s IDR bursts, most likely). Next experiment: x264 `intra-refresh=true`
  (rolling refresh, no keyframe bursts — what Windows uses for Miracast).
- Mirror resolution is capped by the panel (1920x1080): streaming higher would
  only upscale.
- [x] **Audio: DONE (2026-06-12)** — AAC 48 kHz stereo instead of LPCM (the Tab
      offers AAC in M3; AAC-in-TS is standard, WFD LPCM needs a private-stream
      encapsulation GStreamer lacks). Desktop audio is routed through a session
      null sink (`vilya_cast`): laptop goes silent, the Tab is the audio
      device, previous default restored on teardown. Volume is two-stage by
      design: laptop keys scale the encoded stream, the Tab's buttons scale
      its own output — they multiply (extra granularity, verified welcome).
- [ ] Complete `_process_m3_response`: real capability negotiation for M4
- [ ] Tune encoder (bitrate/latency); consider vah264enc (Intel VA-API) later
- [ ] Latency/quality pass once pixels are flowing

---

## Phase 4: UIBC Touch Input — **DONE, VERIFIED ON HARDWARE 2026-06-12**

Goal: touch events on the Tab are forwarded back to the Linux host as pointer input.

- [x] Parse `wfd_uibc_capability` from M3 response
- [x] Negotiate UIBC in M4 `SET_PARAMETER` (Generic SingleTouch)
- [x] TCP back-channel listener for UIBC HID events (`vilya/input/uibc.py`)
- [x] Translate UIBC touch events to `uinput` pointer events on the host
      (`vilya/input/uinput.py` — absolute pointer mapped to the cast output)
- [ ] MultiTouch + Keyboard (the Tab offers both; we currently use
      SingleTouch as a pointer — tap = click)

---

## Phase 5: UX — cast picker UI — **DONE, VERIFIED ON HARDWARE 2026-06-12**

Goal: the Win+K experience — a summonable picker instead of a terminal.

- [x] `vilya/ui/app.py` — PySide6 tray app: scan list (WFD-capable peers
      selectable), Mirror/Extend toggle, mode dropdown, connect/disconnect.
      Thin shell over the CLI via QProcess, so the protocol stays in one
      place; single-instance via QLocalSocket (relaunch = raise window)
- [x] `vilya scan --porcelain` — machine-readable `name\taddress\twfd` for
      the UI (and anything else) to consume
- [x] `vilya ui` / `vilya setup-ui` subcommands; `pip install .[ui]` extra
      for PySide6
- [x] Meta+K global shortcut, registered live with kglobalaccel
- [ ] Tray/daemon mode polish: start hidden on login, KRunner entry

### kglobalacceld can crash the compositor (learned in blood, 2026-06-12)

Two full KWin session crashes taught us how NOT to register a global
shortcut on Plasma:

- `plasma-kglobalaccel.service` is a decoy on this Plasma version — the
  real kglobalacceld lives **inside kwin_wayland** (the standalone binary
  exits 0 immediately because the bus name is taken). "Restart the
  shortcut daemon" is a no-op at best.
- KGlobalAccel marshals a QKeySequence over D-Bus as **exactly four
  int32s** and the demarshaller reads all four without checking the array
  length (`kglobalshortcutinfo_dbus.cpp`). Any client that sends a
  shorter `(ai)` — say, a hand-rolled `busctl` probe with one int — makes
  kwin_wayland abort via a fatal libdbus check. An unprivileged process
  can kill the whole Wayland session with one well-typed message;
  upstream-reportable.
- The correct, crash-free path (what `setup-ui` now does): the daemon's
  own client API over dbus-fast — `doRegister(actionId)` then
  `setShortcutKeys(actionId, [[key,0,0,0]], 6)` (SetPresent |
  NoAutoloading). A componentUnique ending in `.desktop` makes the daemon
  create a KServiceActionComponent that launches the desktop entry itself
  and survives our client disconnecting; the daemon persists
  kglobalshortcutsrc on its own (batched ~500 ms).

---

## Known constraints / decisions

| Topic | Decision |
|---|---|
| HDCP | Advertised by Tab, skipped by us — Tab does not require it |
| microsoft_* / intel_* extensions | Tab replies `none` to all — not implemented |
| Audio codec | AAC 48 kHz stereo (Tab offers it in M3; WFD LPCM needs private-stream TS encapsulation GStreamer lacks) |
| Video codec | H.264 CBP L3.1–CHP L4.2 depending on mode; Tab accepts VESA 1920x1200p30 despite not advertising it |
| RTP port | 19000 UDP (sink), blocksize 1328 |
| RTSP port | source LISTENS on 7236; sink dials in. Sink's advertised port (Tab 49158) is not a control listener |
| Keepalive interval | 30 s (GET_PARAMETER) |
| P2P subnet | 192.168.137.x (from Wireshark capture of working Win→Tab session) |
