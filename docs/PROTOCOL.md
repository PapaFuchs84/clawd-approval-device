# Wire protocol

Transport: a single authenticated WebSocket connection from the device
(client) to the daemon (server), on your LAN. `Authorization: Bearer
<token>` is sent by the device during the WS handshake; the daemon rejects
any connection with a missing/wrong token, and rejects any second
concurrent connection while one device is already connected.

## Daemon → device: `state`

```json
{
  "type": "state",
  "msg_id": "a3f1c9...",
  "state": "WAITING_APPROVAL",
  "request_id": "req_9f2c...",
  "summary": "Bash: rm -rf build/",
  "ts": 1755331200
}
```

States: `IDLE`, `WORKING`, `WAITING_APPROVAL`, `SUCCESS`, `ERROR`,
`DISCONNECTED` (the last one is set locally by the device itself when the WS
connection drops, not sent by the daemon).

The device must respond with an ack:

```json
{"type": "ack", "msg_id": "a3f1c9..."}
```

The daemon retries up to 3 times (3s timeout each) before treating the
device as unreachable.

## Device → daemon: `button_event`

```json
{
  "type": "button_event",
  "event_id": "evt_7c1a...",
  "button": "approve",
  "request_id": "req_9f2c...",
  "ts": 1755331205,
  "sig": "<hmac-sha256-hex>"
}
```

`sig` is HMAC-SHA256 (hex-encoded) over the **compact, alphabetically
key-sorted** JSON of all other fields (i.e. without `sig` itself) — matching
`json.dumps(body, sort_keys=True, separators=(",", ":"))` on the Python
side. The daemon:

1. Verifies the HMAC signature; rejects on mismatch.
2. Rejects `event_id`s it has already seen (duplicate/replay protection).
3. Rejects events whose `request_id` doesn't match the currently pending
   request (stale button presses from an old, already-resolved request).
4. Only then resolves the pending approval as `approve` or `deny`.

## Local daemon ↔ hook interface (Unix socket)

Single-shot, one line of JSON in, one line of JSON out, then the connection
closes. Two request shapes:

**Approval request** (blocks until a button is pressed or the timeout
elapses):
```json
{"summary": "Bash: rm -rf build/"}
```
→
```json
{"result": "approve"}
```
`result` is one of `approve`, `deny`, `timeout`, `unreachable`. Only
`approve` should ever be treated as permission granted.

**Display-only update** (fire-and-forget, does not wait for a button):
```json
{"show_state": "WORKING", "summary": "Tool: Bash"}
```
→
```json
{"sent": true}
```

## Known implementation gotcha

Python's `websockets` package versions ≥13 (the newer asyncio-native HTTP
parser) fail the handshake with the `Links2004/WebSockets` Arduino client
library with a `1002 protocol error`, even with otherwise well-formed
headers. This project pins `websockets==12.0` in `daemon/requirements.txt`.
If you upgrade it, retest the full connect → auth → button-event path on
real hardware before trusting it.

Also: `serve(..., subprotocols=["arduino"])` must be set — the Arduino
client sends `Sec-WebSocket-Protocol: arduino` and disconnects immediately
if the server's response doesn't confirm it.
