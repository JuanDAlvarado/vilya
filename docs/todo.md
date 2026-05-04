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

## Phase 2: Wi-Fi Direct P2P — TODO

Goal: laptop acts as P2P Group Owner; Tab S8+ connects as client; DHCP assigns 192.168.137.x.

- [ ] wpa_supplicant GO mode via D-Bus (or wpa_cli fallback)
- [ ] Advertise WFD IE in P2P probe responses (device type, session availability, RTP port 19000)
- [ ] dnsmasq DHCP server on the P2P interface
- [ ] Determine sink IP after DHCP lease, hand it to `WFDSession`
- [ ] Integration test: P2P handshake completes and TCP 7236 is reachable

---

## Phase 3: Media Pipeline — TODO

Goal: screen pixels flow from KDE Plasma → Tab display via RTP.

- [ ] PipeWire screen capture (pipewire-portal / xdg-desktop-portal)
- [ ] GStreamer pipeline: `pipewiresrc → videoconvert → x264enc (CBP, CBR) → mpegtsmux → rtpmp2tpay → udpsink`
  - RTP destination: sink IP, port 19000, blocksize 1328
  - Audio: `pipewiresrc (monitor) → audioconvert → audioresample → rawaudioenc (LPCM 48kHz stereo) → mux`
- [ ] Wire GStreamer start/stop to `STREAMING` / `TEARDOWN` state transitions in `WFDSession`
- [ ] Complete `_process_m3_response`: parse sink's `wfd_video_formats` and `wfd_audio_codecs`,
  select best mutually supported profile/level, populate M4 `SET_PARAMETER` body dynamically
- [ ] Tune H.264 encoder parameters (keyframe interval, bitrate, latency preset)

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
| RTSP port | 7236 TCP |
| Keepalive interval | 30 s (GET_PARAMETER) |
| P2P subnet | 192.168.137.x (from Wireshark capture of working Win→Tab session) |
