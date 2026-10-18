# WebRTC Remote Monitor

Live camera view, two-way voice, and data-channel teleoperation for an Orange Pi 5
(RK3588) — straight into a browser, with no plugins and no vendor SDK.

The publisher **dials out**; it opens zero inbound TCP ports. Media is standard WebRTC
(DTLS/SRTP + ICE), so it works on a LAN and across the public internet through a
coturn relay fallback.

![status](https://img.shields.io/badge/status-verified-success)
![python](https://img.shields.io/badge/python-3.10-blue)
![gstreamer](https://img.shields.io/badge/GStreamer-1.20-orange)
![license](https://img.shields.io/badge/license-MIT-blue)

<!-- TODO: record a ~30 s demo (camera switch + teleop overlay + voice), save as docs/demo.gif, uncomment -->
<!-- ![demo](docs/demo.gif) -->

---

## What it does

- **720p live video** — MIPI camera (OV13850) or USB camera, hardware H.264 encode on
  the RK3588, Opus audio, all negotiated as one WebRTC session.
- **Hot camera switching** — swap the capture chain at runtime from the page. No new
  offer, no reconnect, no audio interruption; the worst video stall is one GOP
  (measured **367 ms** at `gop=10`).
- **Two-way voice** — board microphone → browser, and browser microphone → board's
  3.5 mm output. Independent up/down counters, because "video works" says nothing
  about audio.
- **Teleoperation over a DataChannel** — whitelisted commands, per-command ACK/NACK,
  and the command is rendered into the video stream itself (`CMD #7 FORWARD 10 ...`
  in the top-left corner). The text can only come from the board, so it proves both
  the control path and the media path in one glance.
- **LAN and public deployment from the same code** — a single `signal_server.py` runs
  on the board for LAN-only mode or on a VPS with coturn for WAN access.

## Architecture

```
        ┌───────────────────── Public VPS (optional) ─────────────────────┐
        │  nginx :443 ──┬── /ws/publish ───────────────→ 127.0.0.1:8443   │
        │               ├── /ws/view /ice.json /diag ──→ 127.0.0.1:8443   │
        │               └── /webrtc /config.json /camera → 127.0.0.1:8444 │
        │  signal_server.py :8443          web_server.py :8444            │
        │  coturn :3478 (STUN/TURN fallback)                              │
        └───────────────▲──────────────────────────▲─────────────────────┘
                        │ wss (publish)            │ wss (view)
        ┌───────────────┴──────────────────────────┴─────────────────────┐
        │ board                                                          │
        │   device_agent.py  ← outbound only, zero inbound TCP ports     │
        │   web_server.py :8443 (LAN page + same-origin proxy)           │
        │   GStreamer: v4l2src → mpph264enc → rtph264pay → webrtcbin     │
        │              alsasrc → opusenc → rtpopuspay ────↗              │
        │              webrtcbin → opusdec → alsasink (return audio)     │
        └───────────────▲────────────────────────────────────────────────┘
                        │ DTLS/SRTP — direct, or TURN relay when needed
                   browser (LAN or WAN)
```

**Signaling is separate from media.** The signaling server only matches a publisher
and a viewer (and hands out TURN credentials); RTP never goes through it unless ICE
chooses the relay. In the LAN entry, the board's `web_server` relays the browser's
signaling to the VPS, so the browser only has to trust the board's self-signed
certificate.

Only **one viewer** is accepted at a time — this is designed as a teleoperation
console, not a broadcast service, so there is one session, one pipeline, and no
multi-viewer fan-out.

## Engineering highlights

### 1. Camera switching without renegotiation

The pipeline is split into an immutable **skeleton** and a replaceable **capture
segment**. The split point is after `h264parse`:

```
skeleton (built once)                       segment (replaced on every switch)
webrtcbin ← vq ← rtph264pay ←ghost─ v4l2src ! [mppjpegdec] ! mpph264enc ! h264parse
  ↑ audio aq ← rtpopuspay
```

Because `rtph264pay` stays in the skeleton, its RTP timestamp base and SSRC survive
the switch — the browser never sees a new stream. Both cameras are deliberately forced
to the same encoded caps, so no renegotiation is needed at all.

### 2. Image quality when the board has no 3A

Orange Pi 5 ships **no `rkaiq_3A_server`**, and Rockchip's AE/AWB live in that
userspace service. The result is one root cause with three symptoms: black (gain at the
driver default), green (no AWB), and blown out (gain pushed too hard).

- `camera.py` measures luminance on the ISP self-path and closes a loop on the sensor's
  `analogue_gain` / `exposure` subdev controls.
- `whitebalance.py` does a gray-world correction with a small
  `appsink → numpy → appsrc` bridge, calibrating on the first 15 frames and then
  freezing the gains. It rejects dark or clipped calibration frames instead of
  producing a confident wrong answer.
- The USB camera has its own AE/AWB, so the fallback is explicitly skipped for it.

### 3. A verification harness, not a vibe check

Each stage has a falsifiable acceptance criterion. The point is to catch the failures
that *look* like success:

| Criterion | The lie it prevents |
|---|---|
| Nominated candidate pair's byte counter keeps growing | `iceConnectionState=connected` with zero media |
| `framesDecoded` grows, not just `bytesReceived` | "RTP arrived" reported as "a frame decoded" |
| Sent vs received bytes ≈ 1.0 | One-way media mistaken for two-way |
| Steady-state sampling window | The startup ramp producing a fake 5 fps |
| `selectedCandidatePair` re-read every sample | A frozen pair from before an ICE restart |
| `sent == acked + nacked` | A dropped command mistaken for "the page never sent it" |
| Board-side `teleop_overlay` tracks ACKs | A truthful ACK over a dead video path |
| Board-side `teleop_rx` matches page-side `sent` | The page grading its own homework |

## Measured results

| Metric | Value | Notes |
|---|---|---|
| Video | 1280×720 H.264, 2500 kbps | MIPI `UYVY` path is ISP-throughput bound at **~20.4 fps** |
| Camera switch stall | **367 ms** | `gop=10`; upper bound is one IDR interval |
| MIPI low light | gain 16 → **Y≈9.7**; gain 248 + exposure 1648 → **Y≈78** | No AE; closed loop hits the sensor ceiling |
| USB camera | **Y≈226** | Built-in AE/AWB, fallback skipped |
| Missing AWB | **G/R=1.71, G/B=1.55** | Gray-world correction, ~0.4 core at 720p, frame rate unchanged |
| Microphone probe | USB mic **12.9 %** FS vs floating 3.5 mm **0.25 %** | Catches a TRS (no-mic) headset before it silently streams noise |
| Verification suite | **13 / 17 / 22 criteria** | video / + audio / + teleop |

## Quick start

### Prerequisites

Debian/Ubuntu on arm64, with the board's MPP GStreamer plugins installed
(`mpph264enc`, `mppjpegdec` — vendor packages, not in Debian):

```bash
sudo apt install python3-gi python3-aiohttp python3-numpy \
    gir1.2-glib-2.0 gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 gir1.2-gst-plugins-bad-1.0 \
    v4l-utils alsa-utils gstreamer1.0-tools
```

`numpy` is only needed for `--camera-wb`; see [requirements.txt](requirements.txt).

### LAN only (no VPS)

```bash
bash tools/gen_cert.sh          # self-signed cert; needed for the browser microphone

# terminal 1 — signaling (+ ICE config)
python3 src/signal_server.py --room demo --token hunter2 --ice-mode none --port 8080

# terminal 2 — publisher
python3 src/device_agent.py --signal ws://127.0.0.1:8080/ws/publish \
    --room demo --token hunter2
    # add --no-audio if there is no microphone

# terminal 3 — page + same-origin proxy (camera switching lives here)
python3 src/web_server.py --port 8443 --room demo --signal http://127.0.0.1:8080 \
    --tls-cert certs/board.crt --tls-key certs/board.key
```

Open `https://<board-ip>:8443/`. The page asks `/config.json` for the room number, so
`?room=` is optional. Accept the self-signed certificate once; that also unlocks
`getUserMedia` for two-way voice.

### Public deployment

```bash
# on the VPS
sudo bash tools/deploy_vps.sh

# on the machine that holds this repo (installs the page service + nginx routes)
bash tools/deploy_web_vps.sh

# on the board: fill in the env file, then run both services
cp .env.example .env && $EDITOR .env
tools/run-agent.sh
tools/run-web.sh
```

`deploy_vps.sh` generates the room, publisher token, TURN secret and viewer
credentials; `deploy_web_vps.sh` prints the external URL and protects every viewing
route with HTTP Basic Auth. See [docs/architecture.md](docs/architecture.md) for the
full topology, credential flow, and why `/ws/publish` deliberately stays outside
Basic Auth.

### Common flags

| Flag | Why it exists |
|---|---|
| `--gop 10` | Camera switch waits at most one IDR; `60` breaks the freeze-ratio criterion |
| `--camera-wb auto` | Gray-world AWB when the board has no 3A. Calibrate against a neutral surface |
| `--camera-ctl analogue_gain=176,exposure=1648` | Write sensor controls directly, no closed loop |
| `--ice-policy relay` | Force TURN; used to prove the relay path actually works |
| `--no-audio` | Video-only mode for boards without a usable microphone |

## Verification

```bash
# Main acceptance: 13 (video), 17 (+audio), or 22 (+teleop) criteria
python3 tools/analyze_diag.py --url https://<vps> --room <room> \
    --cafile certs/vps-self.crt --audio --teleop --teleop-expect-nack

# Camera switch: 13 criteria, board-side and browser-side
bash tools/check_switch.sh --base https://<board>:8443

# Device fds returned after session teardown (run on the board)
bash tools/check_fd.sh --cycles 3

# Protocol unit tests, no GStreamer required
python3 tools/check_teleop.py

# Signaling/server regressions: busy paths, viewer routing/isolation, lifecycle
# marshaling — no hardware required
python3 tools/test_signal_server.py

# Headless peer: real offer/answer, audio and DataChannel, no browser
python3 tools/selftest.py --signal https://<vps> --room <room> --duration 20
```

Diagnostics are collected rather than asserted: the page posts 1 Hz `getStats()`
summaries (including a 32×18 luminance thumbnail) and the board posts its own send
counters to `/diag`, all on the signaling server. `tools/analyze_diag.py` then checks
the criteria offline — which is the only way to verify headless runs, where nobody can
look at the picture.

## Bugs found and root-caused

These took longer than the features. Every one of them is silent: it shows up as some
*other* symptom.

| Symptom | Root cause | How it was pinned down |
|---|---|---|
| `iceConnectionState=connected`, zero media | No mDNS resolver (`libnss-mdns`) on the board image; Chrome obfuscates host candidates as `.local` | Checked the nominated candidate pair's byte counter instead of the state string |
| Board receives no audio; `pad-added` never fires; no error | `webrtcbin` routes inbound RTP by the remote SDP's `a=ssrc:<num> cname:<name>`; GStreamer omits that line when it acts as answerer | Compared SDPs from Chrome/Firefox and from a GStreamer peer; added the line in the headless peer |
| Video plays once, later viewers get black | `rtph264pay` `config-interval` defaults to `0`, so SPS/PPS are never re-sent | Late-joining browser + packet capture; fixed with `config-interval=-1` |
| Camera switch: new source emits nothing, no log | `GhostPad.new()` creates an **inactive** pad, and `add_pad()` does not activate it | Minimal switch test; fixed with `pad.set_active(True)` |
| One device fd leaks per session | `appsink` holds a v4l2 buffer → buffer pool holds the device fd; the pipeline is kept alive by a closure cycle spanning C and Python | `gc.get_referrers` named the cycle; fixed by removing the capture bin from the pipeline on teardown |
| `Failed to allocate required memory` on a manual login | BSP exports `GST_V4L2_USE_LIBV4L2=1` from `/etc/profile.d/`, **login shells only**; the shim breaks rkisp's multiplanar `VIDIOC_REQBUFS` | Same pipeline passed 20/20 runs without the variable; setting it to `0` does not disable it |
| Black / green / blown-out picture, treated as three bugs | One root cause: no `rkaiq_3A_server` | Traced all three to the sensor state and added the gain loop + AWB bridge |
| Adding `textoverlay` made the picture black | `wait-text` defaults to `true`, so no frame passes until the first text is set | Set `wait-text=false` explicitly |

The full write-ups, including dead ends that were tried and rejected, are in
[docs/architecture.md §7](docs/architecture.md).

## Repository layout

```
src/
  device_agent.py    publisher: signaling, session lifecycle, telemetry, local control socket
  session.py         one WebRTC session: pipeline + webrtcbin + DataChannel + signaling
  pipeline.py        GStreamer graph: permanent skeleton + replaceable capture segment
  sources.py         camera table: device node, native format, quality fallback
  camera.py          brightness closed loop (no AE on the board)
  whitebalance.py    gray-world AWB bridge (no AWB on the board)
  audio.py           ALSA device discovery and level probe
  signal_server.py   signaling + /ice.json + diagnostics (LAN or VPS, same file)
  web_server.py      page server + same-origin proxy (/ws/view, /ice.json, /diag, /camera)
  teleop.py          command protocol: whitelist, validation, ACK/NACK, overlay text
  sdpview.py         SDP → human-readable media sections (shared by tools)
web/
  monitor.html       the whole viewer UI: one file, no build step
tools/               deployment, acceptance criteria, protocol tests, hardware probes
docs/architecture.md  the deep dive: topology, threads, and every silent failure
```

## Documentation

| Doc | Contents |
|---|---|
| [README.zh-CN.md](README.zh-CN.md) | Chinese walkthrough and deployment notes |
| [docs/architecture.md](docs/architecture.md) | Module map, media pipeline, control channels, thread model, failure log |
| [.env.example](.env.example) | Configuration template and what each value is for |

## Known limitations

- **One viewer at a time.** A second viewer is rejected explicitly; multi-viewer needs
  a fan-out design (or an SFU), which is outside the teleoperation scenario.
- **Single offer, no renegotiation.** Enabling the browser microphone requires a page
  reconnect — the track has to be present before the first offer.
- **The page does not auto-reconnect** if the board's `web_server` restarts; refresh it.
- **The pipeline skeleton is not reclaimed promptly.** Device fds are returned
  (verified), but a C↔Python closure cycle keeps the skeleton alive between sessions
  (a few hundred KB per session). See `docs/architecture.md §11`.
- **MIPI quality is sensor-bound.** The loop can only reach Y≈78 in a dim room; more
  light or a different sensor is the only real fix.

## License

MIT — see [LICENSE](LICENSE).
