# Vilya

Vilya is my attempt to implement a Windows Key + K type implementation to my Dell XPS 13 running Arch Linux and KDE Plasma. The goal is for this to be as frictionless as possible with the least amount of dependencies as possible.

**Status:** Active development, prototype level.


## How It Works

Miracast is also named 'Wi-Fi Display', which is a Wi-Fi Alliance standard. You can read about it [here](https://raw.githubusercontent.com/wiki/albfan/miraclecast/files/Wi-Fi_Display_Technical_Specification_v2.1_0.pdf).

The laptop and the remote display create a network with the laptop as Group Owner of the Wi-Fi Direct (P2P) network. There are 7 distinct stages of the WFD RTSP session (M1-M7) in which the source and sink negotiate the connection parameters once at session start. After that, the RTP/UDP media stream (using H.264 + LPCM audio codecs) carries video and audio to the sink device at high frequency.

Vilya will control each layer directly. Current Linux Miracast implementations make use of NetworkManager or a larger GNOME stack. Vilya will be self-sufficient and lean.

Vilya is written in Python for the prototype phase. If the prototype proves out end-to-end, the plan is to rewrite in Go for distribution as a proper system daemon.


## Why

Miracast has had a rough time on Linux. gnome-network-displays works, but requires extra work to implement audio. Discovery is not smooth, and requires a good amount of troubleshooting to get working. The GStreamer pipeline has a bunch of issues and is not easy to work with. The most damning of issues though, is that it is not seamless. On Windows 11, I can tap Win + K, select my Galaxy Tab S8+, and it will connect perfectly. I can then use it as a touch enabled device and play audio over it. No issues. No troubleshooting. No finagling and bargaining. It just works (heh). This is the goal for Vilya. If I can get it working on my machine in that manner, then I will expand and publish it out and try to get it working for all other Linux devices.


## A Note

Here's the kicker. I am a Sys Admin by day. I do not have a strong background in software engineering, and an 'as needed' understanding of advanced network protocols (such as RTSP), protocol engineering (such as state machines, handshakes, etc.), and programmatic implementation of multimedia streaming (codecs, containers, RTP). However, the rise of AI, my ability to pick things up quickly, and a healthy (and purposeful) naivete of exactly what the hell I'm getting myself into drive my desire to work on this project.

I recently read this [article](https://www.dbreunig.com/2026/05/04/10-lessons-for-agentic-coding.html) on dbreunig.com, in which the author states as the number one lesson for agentic coding:

> "Implement to learn. You can go far with Spec-Driven Development, but the act of writing code surfaces decisions you hadn't considered and makes your spec better. When code is cheap, implement to learn."

This idea paired with tools like Claude Code and Codex will allow me to push myself to my limits on my own time. The university professors who watched me fail out of my first 3 attempts to pass their entry-level Object Oriented Programming class may be frustrated that it has taken me nearly 10 years to develop an interest in this level of learning. To them, and to my past self, I apologize. This project is for me, and if it ever is any good, for the people. But even if every single attempt fails, the effort expended and the lessons learned will far outweigh the value of any deliverable that may (but most likely definitely will not) come out of this.


## License

GPL-3.0. The commons stays common.

