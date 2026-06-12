# Vilya

Vilya is my attempt to implement a Windows Key + K type implementation to my Dell XPS 13 running Arch Linux and KDE Plasma. The goal is for this to be as frictionless as possible with the least amount of dependencies as possible.

**Status:** It works — the Win+K experience, on Linux. Press Meta+K, pick
the tablet, and it becomes a touch-enabled second monitor with audio.
Verified against a Samsung Tab S8+ from Arch Linux + KDE Plasma.

## What works today

- **A real cast picker**: Meta+K opens a small Qt tray app — pick the
  tablet, choose Mirror or Extend, connect; disconnect from the tray.
  The picker is a thin shell over the CLI, so the protocol machinery
  stays in one place
- **Extended desktop** at the tablet's native 16:10 shape (1920x1200), or
  classic mirroring at panel-native 1080p — ~150 ms typical latency
- **Touch**: tablet touches drive the Linux pointer (UIBC); tap = click
- **Audio** follows the cast: the laptop goes silent, the tablet plays
  (AAC 48 kHz); laptop volume keys scale the stream, tablet volume scales
  its output — the two multiply
- **One command, no sudo**: `python -m vilya connect --display extend`
  works without the GUI
- Sessions survive idle indefinitely; everything tears down cleanly
  (virtual monitor removed, audio routing restored) on Ctrl-C or
  Disconnect

## Running it

```
pip install -e ".[ui]"        # PySide6 is only needed for the picker
python -m vilya setup-extend  # once: allow native-size virtual monitors
python -m vilya setup-ui      # once: desktop entry + the Meta+K shortcut

# then either press Meta+K and pick the tablet, or:
python -m vilya connect --display extend
```

`setup-ui` registers the shortcut through kglobalaccel's D-Bus client API
(`doRegister`/`setShortcutKeys`) rather than config-file edits — the daemon
lives inside kwin_wayland and, as this repo learned the hard way, a
malformed message to it takes down the entire compositor (see
docs/todo.md).

Positioning vs. gnome-network-displays: g-n-d mirrors your screen to a
Miracast sink. Vilya makes a Miracast sink a full second monitor —
extended display, touch input, audio — as a lean CLI/daemon-to-be,
desktop-agnostic at the protocol layer (the capture layer is KDE-first
for now).


## How It Works

Miracast is also named 'Wi-Fi Display', which is a Wi-Fi Alliance standard. You can read about it [here](https://raw.githubusercontent.com/wiki/albfan/miraclecast/files/Wi-Fi_Display_Technical_Specification_v2.1_0.pdf).

The laptop and the remote display form a Wi-Fi Direct (P2P) group (with the
tablet as Group Owner, it turns out — it assigns us an address during the WPA
handshake, no DHCP needed). The source then *listens* on RTSP port 7236; the
sink dials in and the 7-stage WFD session (M1-M7) negotiates parameters.
After that, an RTP/UDP MPEG-TS stream (H.264 + AAC) carries video and audio,
and the sink opens a second TCP channel back to us (UIBC) carrying touch
events, which land in the kernel via /dev/uinput.

Vilya drives each layer directly where the OS allows: NetworkManager's D-Bus
API for P2P (NM deliberately destroys P2P groups formed behind its back — a
lesson this repo's docs/todo.md records in blood), wpa_supplicant directly on
NM-less systems, KWin's screencast Wayland protocol for native-size virtual
monitors, and GStreamer for the media pipeline.

Vilya is written in Python for the prototype phase. If the prototype proves out end-to-end, the plan is to rewrite in Go for distribution as a proper system daemon.


## Why

Miracast has had a rough time on Linux. gnome-network-displays works, but requires extra work to implement audio. Discovery is not smooth, and requires a good amount of troubleshooting to get working. The GStreamer pipeline has a bunch of issues and is not easy to work with. The most damning of issues though, is that it is not seamless. On Windows 11, I can tap Win + K, select my Galaxy Tab S8+, and it will connect perfectly. I can then use it as a touch enabled device and play audio over it. No issues. No troubleshooting. No finagling and bargaining. It just works (heh). This is the goal for Vilya. If I can get it working on my machine in that manner, then I will expand and publish it out and try to get it working for all other Linux devices.

**Update, June 2026:** on this machine, the goal is reached. Meta+K, pick
the Tab, and it connects — extended touch display, audio and all. The
first draft of this very update was typed with the tablet as the screen.
What remains is the second half of the sentence above: making it work for
everyone else.


## A Note

Here's the kicker. I am a Sys Admin by day. I do not have a strong background in software engineering, and an 'as needed' understanding of advanced network protocols (such as RTSP), protocol engineering (such as state machines, handshakes, etc.), and programmatic implementation of multimedia streaming (codecs, containers, RTP). However, the rise of AI, my ability to pick things up quickly, and a healthy (and purposeful) naivete of exactly what the hell I'm getting myself into drive my desire to work on this project.

I recently read this [article](https://www.dbreunig.com/2026/05/04/10-lessons-for-agentic-coding.html) on dbreunig.com, in which the author states as the number one lesson for agentic coding:

> "Implement to learn. You can go far with Spec-Driven Development, but the act of writing code surfaces decisions you hadn't considered and makes your spec better. When code is cheap, implement to learn."

This idea paired with tools like Claude Code and Codex will allow me to push myself to my limits on my own time. The university professors who watched me fail out of my first 3 attempts to pass their entry-level Object Oriented Programming class may be frustrated that it has taken me nearly 10 years to develop an interest in this level of learning. To them, and to my past self, I apologize. This project is for me, and if it ever is any good, for the people. But even if every single attempt fails, the effort expended and the lessons learned will far outweigh the value of any deliverable that may (but most likely definitely will not) come out of this.


## License

GPL-3.0. The commons stays common.

