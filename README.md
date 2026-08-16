# Clawd Approval Device

A physical approve/deny button box for [Claude Code](https://claude.com/claude-code), built by repurposing an ESP32-based [NerdMiner](https://github.com/BitMaker-hub/NerdMiner_v2) (LilyGO/TTGO T-Display). It shows live Claude Code session activity (idle, working, waiting for approval, success, error, disconnected) as an animated [Clawd](https://github.com/marciogranzotto/clawd-tank) crab mascot, and lets you approve or deny pending tool calls with two physical buttons — **as an alternative to the normal digital confirmation dialog, not a replacement for it.**

<p align="center">
  <em>IDLE · WORKING · WAITING_APPROVAL · SUCCESS · ERROR · DISCONNECTED</em>
</p>

## Why this exists

This started as a practical question: an old NerdMiner (an ESP32 dev board with a small display and two buttons, originally used to mine Bitcoin shares as a novelty display) was sitting unused. Rather than buying new hardware, this project repurposes it as a physical status/approval device for Claude Code — reusing the display, both buttons, and Wi-Fi connectivity that were already there.

## How it works

```
┌──────────────┐   hook events    ┌────────────────┐   authenticated WS   ┌──────────────┐
│  Claude Code  │ ───────────────▶ │  Host daemon    │ ◀──────────────────▶ │  ESP32 device │
│  (your machine)│  (stdin JSON)   │  (Python,       │   JSON + HMAC-signed  │  (T-Display)  │
└──────────────┘                  │  LAN-only)      │   button events        │  2 buttons    │
                                   └────────────────┘                        └──────────────┘
```

- **Firmware** (`firmware/`): PlatformIO/Arduino project for the classic ESP32 LilyGO/TTGO T-Display. Connects to the daemon over an authenticated WebSocket, renders an animated status display, and sends HMAC-signed button events.
- **Daemon** (`daemon/`): a small Python service that holds exactly one authenticated device connection, exposes a local Unix socket, and talks to Claude Code via hooks.
- **Hooks**: two independent hook scripts —
  - `pre_tool_use_approval.py` — a **blocking** `PreToolUse` hook. Waits briefly for a button press; on timeout or if the device is unreachable it returns `ask`, deferring to Claude Code's normal confirmation dialog. It never auto-approves.
  - `session_status_hook.py` — a **non-blocking** status hook (`SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop`, `StopFailure`, `SessionEnd`) that just updates the display to reflect live session activity.

See [docs/PROTOCOL.md](docs/PROTOCOL.md) for the wire protocol and [docs/HOOKS.md](docs/HOOKS.md) for how the Claude Code hook contract was verified.

## Security model

- Buttons are an **alternative** to the normal confirmation flow, never a silent bypass of it.
- **Fail-closed, always.** Timeout, disconnect, or any error never results in an approved action. The worst case is that you fall back to answering the normal Claude Code prompt yourself.
- The device authenticates to the daemon with a bearer token; the daemon accepts exactly one device connection at a time.
- Button events are HMAC-signed and bound to a specific `request_id`; stale or duplicate events are rejected.
- The daemon is meant to run **LAN-only**. Do not expose its WebSocket port to the internet.

## Hardware

Verified against a classic (non-S3) LilyGO/TTGO T-Display:

| | |
|---|---|
| Chip | ESP32-D0WDQ6 (classic dual-core, **not** S3/C3/C6) |
| Display | ST7789, 135×240 IPS |
| Buttons | GPIO0 (Approve) / GPIO35 (Deny) |
| Flash | ≥4 MB (16 MB tested) |

Your board may differ — see [docs/HARDWARE.md](docs/HARDWARE.md) for how to identify it safely (read-only checks) before flashing anything, plus a backup/restore procedure so you can always get back to stock firmware.

**This project intentionally does not try to fork/reuse [Clawd Tank](https://github.com/marciogranzotto/clawd-tank), [Clawd Mochi](https://github.com/yousifamanuel/clawd-mochi), or [Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter) directly** — they target different, newer ESP32 chip families (C6/C3/S3) that a NerdMiner-based board typically isn't. This repo reuses Clawd Tank's mascot artwork and general architecture idea (hooks → daemon → device), rebuilt for the classic ESP32 + TFT_eSPI toolchain.

## Getting started

1. **Identify your board and back it up.** See [docs/HARDWARE.md](docs/HARDWARE.md). Do not skip this — flashing the wrong pin/display config can require a full re-flash to recover, and you should always be able to restore your device's original firmware.
2. **Set up the daemon:**
   ```bash
   cd daemon
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   cp .env.example .env   # fill in real random tokens
   set -a; source .env; set +a
   .venv/bin/python daemon.py
   ```
   For a persistent setup, see the example systemd unit in `daemon/systemd/`.
3. **Build and flash the firmware:**
   ```bash
   cd firmware
   cp include/secrets.h.example include/secrets.h   # fill in real values, matching daemon/.env
   pip install platformio
   pio run -t upload
   ```
4. **(Optional) regenerate the sprites** if you want to tweak them:
   ```bash
   cd firmware/tools
   python3 -m venv .venv && .venv/bin/pip install Pillow
   apt install librsvg2-bin   # or your platform's rsvg-convert package
   .venv/bin/python svg_to_rgb565.py
   ```
5. **Register the hooks** in your Claude Code `settings.json` — see [docs/HOOKS.md](docs/HOOKS.md) for the exact structure and how to verify the contract against your installed version before enabling the blocking approval hook.

## Status

Built and tested end-to-end on real hardware: WiFi + display + both buttons + authenticated WebSocket + HMAC-signed events + live `PreToolUse` gating of a real Claude Code session + animated status display + non-blocking session activity hooks. See [docs/PROTOCOL.md](docs/PROTOCOL.md) and [docs/HOOKS.md](docs/HOOKS.md) for details and known gotchas (in particular a `websockets` Python package version pin that matters, see there).

## License

MIT, see [LICENSE](LICENSE). Sprite artwork and pin/display reference values are derived from third-party MIT-licensed projects — see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for full attribution.

## Disclaimer

Independent hobby project. Not affiliated with, sponsored by, or endorsed by Anthropic. "Claude" and "Clawd" are used descriptively; see the upstream Clawd Tank project for its own trademark/branding notes.
