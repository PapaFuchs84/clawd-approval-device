# Wire protocol

Transport: one or more authenticated WebSocket connections from devices
(clients, e.g. the ESP32 and/or a Wear OS watch) to the daemon (server), on
your LAN. `Authorization: Bearer <token>` is sent by each device during the
WS handshake; the daemon rejects any connection with a missing/wrong token.
Multiple devices may be connected at the same time: `state` messages are
broadcast to all of them, and whichever device's button event arrives first
for the currently pending `request_id` resolves it (see below).

The server enables WebSocket-level ping/pong keepalive with a 20s interval
and a 30s timeout (`ping_interval`/`ping_timeout` in `serve(...)`) — more
generous than the library default (20s/20s) so a brief WiFi hiccup on a
device doesn't immediately tear down the connection with close code 1011
("keepalive ping timeout"). A genuinely dead connection is still detected
within `ping_interval + ping_timeout` (worst case ~50s) and cleaned up.

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

**Client rule for `request_id`:** a client must only adopt `request_id` from
a `state` message as its "currently pending approval" when `state ==
"WAITING_APPROVAL"`. Every other state (`IDLE`/`WORKING`/`SUCCESS`/`ERROR`)
always carries `request_id: ""` (see `show_state()` in daemon.py) and must
**not** clear or overwrite a client's existing pending request ID — doing so
would silently disarm the approve/deny buttons for a request the human
hasn't answered yet, mid-decision, the moment an unrelated status broadcast
arrives. Both the ESP32 firmware (`currentRequestId` in `main.cpp`) and the
Wear OS client (`requestId` in `ApprovalClient.kt`) implement this rule.

The device must respond with an ack:

```json
{"type": "ack", "msg_id": "a3f1c9..."}
```

Acks are tracked per `(connection, msg_id)` pair on the daemon side, not
just by `msg_id` — the same broadcast `msg_id` is in flight on every
connected device simultaneously, so an ack from device A must never be able
to satisfy the wait for device B's copy of that same message.

The daemon retries sending up to 2 times (2s timeout each) per device
before treating that specific device as unreachable and dropping its
connection. A `state` broadcast counts as delivered ("sent") as soon as
**at least one** connected device acknowledges it; only if every connected
device fails to ack (or none are connected at all) is the request treated
as `unreachable`. These numbers are intentionally short: a healthy device
on the LAN should ack in well under a second, and a longer wait only adds
latency to an already-dead connection.

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
side.

### HMAC canonicalization rules

Every client's canonical string must be byte-for-byte identical to what
`json.dumps(body, sort_keys=True, separators=(",", ":"))` produces (Python's
`ensure_ascii` defaults to `True` and is never overridden here), or the
signature will be silently rejected as invalid:

- **Key order:** exactly alphabetical — `button`, `event_id`, `request_id`,
  `ts`, `type`. No other order is accepted.
- **Whitespace:** none. No space after `:` or `,`, no trailing newline.
- **String escaping:** `"` and `\` are backslash-escaped; the named
  shorthands `\n \r \t \b \f` are used for those specific control
  characters; every other character below U+0020 or above U+007E (i.e. any
  non-ASCII character) is emitted as a `\uXXXX` escape — this is what
  `ensure_ascii=True` does. Characters in the printable ASCII range
  (U+0020–U+007E) other than `"` and `\` are emitted literally.
  UTF-16 clients (Kotlin/Java `String`s) naturally represent characters
  outside the Basic Multilingual Plane as surrogate pairs; each surrogate
  half must be `\u`-escaped individually — this matches how CPython's JSON
  encoder represents the same astral code points with `ensure_ascii=True`.
  In practice, all fields that get signed (`button`, `event_id`,
  `request_id`, `type`) are always plain ASCII by construction (fixed
  literals or hex tokens), so this mainly matters as a defense-in-depth
  guarantee, not as something that fires under normal operation today.
- **Numbers:** `ts` is a Unix timestamp in whole seconds, encoded as a bare
  JSON integer — no decimal point, no exponent, no quotes (e.g. `1755331205`,
  never `1755331205.0`). Python's `int` and Kotlin's `Long`/`Int` both
  serialize this way natively when concatenated into the hand-built
  canonical string (see `ApprovalClient.sendButton()`); just make sure
  whatever produces `ts` on a new client is an integer type, not a float.
- **Encoding:** the resulting string is UTF-8 encoded before HMAC-SHA256 is
  computed over the raw bytes.

The ESP32 firmware builds this string via `ArduinoJson`'s compact
`serializeJson()` with keys inserted in alphabetical order (all fields are
ASCII, so escaping is moot there in practice). The Wear OS client
(`ApprovalClient.kt`, `jsonStr()`) builds it by hand with explicit
character-by-character escaping matching the rules above, rather than
relying on any particular JSON library's key ordering or default escaping
behavior for something this security-sensitive.

The daemon:

1. Verifies the HMAC signature; rejects on mismatch.
2. Rejects `event_id`s it has already seen (duplicate/replay protection).
3. Rejects events whose `request_id` doesn't match the currently pending
   request (stale button presses from an old, already-resolved request).
4. Only then resolves the pending approval as `approve` or `deny`.

Any connected device may send a `button_event`. With multiple devices
connected, the first valid event for the currently pending `request_id`
wins — the request is resolved immediately, and any button event received
afterwards (from the same or a different device) is rejected as stale by
rule 3, since `current_request_id` has already moved on.

## Local daemon ↔ hook interface (Unix socket)

Single-shot, one line of JSON in, one line of JSON out, then the connection
closes. The request must be a JSON object; anything else (or malformed JSON)
is treated as an error and the daemon simply closes the connection without
writing a response line — see "Fail-closed on malformed input" below. Two
request shapes:

**Approval request** (blocks until a button is pressed or the timeout
elapses):
```json
{"summary": "Bash: rm -rf build/"}
```
→
```json
{"result": "approve"}
```
`result` is one of `approve`, `deny`, `timeout`, `unreachable`, `busy`. Only
`approve` should ever be treated as permission granted.

Only **one** approval decision is ever in flight at a time. `current_request_id`
and its associated future are single global slots inside the daemon, so if a
second `{"summary": ...}` request arrives on the control socket while a first
one is still pending (e.g. two parallel tool calls from Claude Code), the
daemon does **not** queue it — it fails closed immediately with
`{"result": "busy"}`. This is deliberate: queueing risks a button press
being attributed, by timing coincidence, to the wrong request. Like
`timeout`/`unreachable`, the hook (`pre_tool_use_approval.py`) maps `busy` to
`permissionDecision: "ask"`, never `"allow"`.

**Display-only update** (true fire-and-forget — the daemon responds as
soon as it has handed the message to a background broadcast task, without
waiting for any device to ack it):
```json
{"show_state": "WORKING", "summary": "Tool: Bash"}
```
→
```json
{"sent": true}
```
`sent: true` means at least one device was connected to receive the
broadcast, not that it was necessarily acknowledged. This is intentional:
`session_status_hook.py` calls in with a very short local read timeout
(0.5s) since it must never noticeably delay Claude Code, so the daemon must
never block this response on a device's ack — doing so used to occasionally
surface as `ConnectionResetError` on the control socket once a
connected-but-unresponsive device burned through its retries after the hook
had already stopped listening.

`show_state` broadcasts always carry `request_id: ""` (see the `state`
message shape above) and are **suppressed entirely** (`{"sent": false}`,
nothing sent to any device) while an approval decision is pending
(`current_request_id` is set) — a client mid-decision must never have its
`WAITING_APPROVAL` screen silently replaced by an unrelated
`WORKING`/`SUCCESS`/`ERROR` update from a different, concurrent session.
The pending request's own `WAITING_APPROVAL` message is unaffected by this.

Background broadcast tasks from `show_state` are tracked and coalesced: a
new `show_state` call cancels the previous call's still-in-flight broadcast
task (only the latest status matters) rather than letting a burst of rapid
status updates (e.g. fast `PostToolUse` hooks) pile up unboundedly.

**Fail-closed on malformed input:** if the request line isn't valid JSON,
isn't a JSON object, or has a `show_state` value that isn't a string, the
daemon logs the error and closes the connection **without** writing a
response line. `pre_tool_use_approval.py` treats a connection that closes
without a response as `"ask"` — never `"allow"`.

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

## Known limitation: cleartext transport (planned follow-up, not yet implemented)

**This is a real, currently-accepted security weakness, not a hypothetical
one.** The `ws://` transport carries the bearer token (on every connect) and
enough information for a LAN-position attacker to forge an approval:
`request_id` is visible on the wire in every `state` broadcast, and the HMAC
key itself is provisioned out-of-band but never rotated — so anyone who can
read LAN traffic during a legitimate approval can also read `request_id`
and, if they ever obtain the HMAC key (e.g. by extracting it from a device),
can fabricate a matching `button_event`. A passive LAN eavesdropper alone
cannot forge an approval without the HMAC key, but they *can* trivially
capture the bearer token and impersonate a device toward the daemon, and
they can observe exactly when and what is being approved.

This has **not** been fixed with a partial/untested TLS switch, because a
half-migrated `wss://` setup that isn't verified against both the ESP32
(`Links2004/WebSockets`, which has real but limited/version-dependent TLS
support) and the Wear OS client (`OkHttp`, full TLS support) risks silently
breaking connectivity to one of them, or worse, silently downgrading to
unverified/unpinned TLS that gives a false sense of security. Getting this
wrong is worse than the current, clearly-documented cleartext state.

**Planned migration (not yet started), for whoever picks this up:**

1. **Transport:** move to `wss://` on both the daemon (`websockets.serve(...,
   ssl=...)`) and both clients. Before writing any code, verify on real
   hardware whether the pinned `Links2004/WebSockets` version (see the
   `websockets==12.0` gotcha above — Arduino-side library compatibility has
   already bitten this project once) supports TLS with a *self-signed*
   certificate (this is a LAN-only daemon; a publicly-trusted CA is not
   applicable) without excessive RAM/flash cost on the ESP32.
2. **Certificate validation:** for the Wear OS client, use OkHttp's
   `CertificatePinner` pinned to the daemon's self-signed cert (not a
   blanket "trust all certs" `TrustManager` — that would be strictly worse
   than the current cleartext state, since it looks secure but isn't). For
   the ESP32, evaluate whether the WebSocketsClient TLS API supports
   fingerprint pinning; if not, document that limitation explicitly rather
   than shipping unpinned TLS silently.
3. **Alternative/complementary: mTLS.** Since both endpoints are trusted,
   provisioned devices (not arbitrary public clients), client-certificate
   auth via mTLS would let the daemon authenticate devices at the TLS layer
   instead of (or in addition to) the current bearer-token-in-header scheme,
   removing the token-in-cleartext-handshake exposure entirely. Higher
   implementation cost on the ESP32 side; evaluate feasibility before
   committing to it as the primary mechanism.
4. **Network-layer mitigation in the meantime:** until the above lands, bind
   the daemon to a specific interface/VLAN via `CLAWD_APPROVAL_BIND` rather
   than `0.0.0.0`, and firewall port 8765 to only the specific device IPs
   that need it. This doesn't fix the cleartext weakness but shrinks who can
   observe the traffic in the first place.
5. **Test plan before merging any of this:** a real approval end-to-end
   (WAITING_APPROVAL → button press → approve) on real ESP32 hardware and a
   real Wear OS device/emulator, both over the new transport, plus an
   explicit negative test that an untrusted/wrong certificate is rejected by
   both clients (not just "TLS handshake succeeds with the right cert" —
   confirm it also correctly *fails closed* with the wrong one).

Until this migration happens: treat this daemon as appropriate only for a
LAN you already trust at the physical/link layer, exactly as the existing
`docs/PROTOCOL_REFERENCE.md` "Security model" section for the Wear app
already states.
