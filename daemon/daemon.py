#!/usr/bin/env python3
"""Host daemon for the Clawd Approval Device.

Runs on the machine where Claude Code runs (or anywhere reachable on the
same LAN as the device). Accepts MULTIPLE authenticated WebSocket
connections at once (e.g. the ESP32 device and a Wear OS watch), broadcasts
state to all of them, and resolves an approval request with the first valid
button event it receives from any connected device. Manages approval
requests with fail-closed behavior, and exposes a local Unix socket for
Claude Code hooks (and a manual interactive test mode).

Required environment variables (never hardcode these):
    CLAWD_APPROVAL_TOKEN    - bearer token each device sends on connect
    CLAWD_APPROVAL_HMAC_KEY - key used to verify button event signatures

See docs/PROTOCOL.md for the wire protocol.
"""
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from collections import deque

import websockets
from websockets.server import serve

AUTH_TOKEN = os.environ.get("CLAWD_APPROVAL_TOKEN")
HMAC_KEY = os.environ.get("CLAWD_APPROVAL_HMAC_KEY")
if not AUTH_TOKEN or not HMAC_KEY:
    sys.exit("Error: CLAWD_APPROVAL_TOKEN and CLAWD_APPROVAL_HMAC_KEY must be set.")

HMAC_KEY_BYTES = HMAC_KEY.encode()
# Previously 2s/2 attempts (4s budget) - too tight in practice: the firmware
# does a full redraw on every state update (fillScreen + text + sprite
# compositing + pushSprite), and under a burst of rapid state broadcasts
# (several tool calls in quick succession) the ack regularly arrived a
# little late, causing the daemon to wrongly drop an otherwise healthy
# connection as "unreachable". See also PING_TIMEOUT_S below for the
# WebSocket-level keepalive side of this.
ACK_TIMEOUT_S = 4
MAX_RETRIES = 3
# More generous than the websockets default (20s) so a brief WiFi hiccup on
# the device doesn't immediately tear down the connection (close code 1011
# "keepalive ping timeout"). A truly dead connection is still detected
# within PING_INTERVAL_S + PING_TIMEOUT_S.
PING_INTERVAL_S = 20
PING_TIMEOUT_S = 30
# Short window: the physical/touch buttons are an ALTERNATIVE to the normal
# digital confirmation, not a replacement. If nobody responds on any device,
# the hook falls back to Claude Code's normal confirmation dialog (see
# pre_tool_use_approval.py, result "timeout" -> permissionDecision "ask").
APPROVAL_TIMEOUT_S = int(os.environ.get("CLAWD_APPROVAL_TIMEOUT", "25"))


class ApprovalDaemon:
    def __init__(self):
        self.devices = set()  # all currently connected+authenticated device websockets
        self.current_request_id = None
        self.seen_event_ids = deque(maxlen=200)
        self.pending_futures = {}
        self.pending_acks = {}  # (ws, msg_id) -> asyncio.Event
        # Only one approval decision may be in flight at a time: current_request_id
        # is a single global slot, so two concurrent request_approval() calls (e.g.
        # from two Unix-socket hook clients at once) would otherwise stomp on each
        # other's request_id/future bookkeeping. Fail-closed: a request that can't
        # acquire this immediately is rejected as "busy" rather than queued - see
        # request_approval() and docs/PROTOCOL.md.
        self.request_lock = asyncio.Lock()
        # Tracks the single in-flight show_state() broadcast task, so a burst of
        # status updates (WORKING/SUCCESS/...) coalesces into "only the latest
        # matters" instead of piling up unbounded background tasks.
        self._state_broadcast_task: asyncio.Task | None = None
        self.background_tasks: set[asyncio.Task] = set()

    def _track_background_task(self, task: asyncio.Task):
        self.background_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)

    def _on_background_task_done(self, task: asyncio.Task):
        self.background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            print(f"[ERROR] Background task failed: {exc!r}")

    def _hmac_ok(self, payload: dict) -> bool:
        sig = payload.get("sig")
        if not isinstance(sig, str) or not sig:
            return False
        body = {k: v for k, v in payload.items() if k != "sig"}
        # separators=(",", ":") -> compact form with no spaces, exactly what
        # ArduinoJson::serializeJson() (and the Wear OS client) produce.
        # Without this flag Python inserts spaces after ':' and ',' -> HMAC
        # mismatch, every signature would be rejected as invalid.
        raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        expected = hmac.new(HMAC_KEY_BYTES, raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)

    async def handle_connection(self, ws):
        auth_header = ws.request_headers.get("Authorization", "")
        if auth_header != f"Bearer {AUTH_TOKEN}":
            print(f"[REJECTED] Invalid/missing auth header from {ws.remote_address}")
            await ws.close(code=4401, reason="unauthorized")
            return

        print(f"[CONNECTED] Device {ws.remote_address} authenticated "
              f"({len(self.devices) + 1} device(s) now connected)")
        self.devices.add(ws)
        try:
            async for raw in ws:
                try:
                    await self._handle_message(ws, raw)
                except Exception as e:
                    # One malformed/unexpected message must not tear down the
                    # whole connection - log and keep reading.
                    print(f"[ERROR] Failed to handle message from {ws.remote_address}: {e!r}")
        except websockets.ConnectionClosed as e:
            print(f"[DISCONNECT-DETAIL] {e!r}")
        finally:
            print(f"[DISCONNECTED] {ws.remote_address} "
                  f"({len(self.devices) - 1} device(s) remaining)")
            self.devices.discard(ws)
            # Fail-closed: only give up on a pending request once the LAST
            # connected device is gone. As long as at least one device is
            # still connected, it can still resolve the pending approval.
            if not self.devices:
                for fut in self.pending_futures.values():
                    if not fut.done():
                        fut.set_result("unreachable")

    async def _handle_message(self, ws, raw: str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[WARN] Ignoring invalid JSON: {raw!r}")
            return

        if not isinstance(payload, dict):
            print(f"[WARN] Ignoring non-object JSON: {raw!r}")
            return

        mtype = payload.get("type")
        if mtype == "ack":
            msg_id = payload.get("msg_id")
            # msg_id must be a hashable, well-formed key (it's always a hex
            # string on the sending side - see secrets.token_hex() calls
            # below). An attacker/buggy client could otherwise send an
            # unhashable msg_id (e.g. a JSON object/array), which would raise
            # TypeError as soon as it's used in the (ws, msg_id) dict lookup.
            if not isinstance(msg_id, str):
                print(f"[WARN] Ignoring ack with invalid msg_id: {payload!r}")
                return
            # Keyed by (ws, msg_id), NOT just msg_id: two devices can be
            # in-flight for the same broadcast msg_id at once (see
            # _send_to_one), and an ack from device A must never satisfy the
            # wait for device B's copy of that message.
            ev = self.pending_acks.get((ws, msg_id))
            if ev:
                ev.set()
            return

        if mtype == "button_event":
            await self._handle_button_event(payload)
            return

        print(f"[UNKNOWN] Ignoring message type: {mtype}")

    async def _handle_button_event(self, payload: dict):
        if not self._hmac_ok(payload):
            print(f"[SECURITY] Discarding button event with invalid signature: {payload}")
            return

        event_id = payload.get("event_id")
        if event_id in self.seen_event_ids:
            print(f"[DUPLICATE] Button event {event_id} already processed, ignoring")
            return
        self.seen_event_ids.append(event_id)

        request_id = payload.get("request_id")
        if request_id != self.current_request_id:
            print(f"[STALE] Discarding button event for unknown/old request_id {request_id} "
                  f"(current: {self.current_request_id})")
            return

        # First valid button event for the current request_id wins - any
        # device (ESP32 or watch) can resolve it, whichever the human
        # reacts on first. Once current_request_id is cleared below (via
        # request_approval's finally block) any further presses are stale.
        button = payload.get("button")
        print(f"[BUTTON] {button} for request_id={request_id}")
        fut = self.pending_futures.get(request_id)
        if fut and not fut.done():
            fut.set_result("approve" if button == "approve" else "deny")

    async def _send_to_one(self, ws, msg: dict) -> bool:
        msg_id = msg["msg_id"]
        key = (ws, msg_id)
        ack_event = asyncio.Event()
        self.pending_acks[key] = ack_event
        try:
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    await ws.send(json.dumps(msg))
                except websockets.ConnectionClosed:
                    return False
                try:
                    await asyncio.wait_for(ack_event.wait(), ACK_TIMEOUT_S)
                    return True
                except asyncio.TimeoutError:
                    print(f"[RETRY {attempt}/{MAX_RETRIES}] No ACK for {msg_id} from {ws.remote_address}")
            return False
        finally:
            self.pending_acks.pop(key, None)

    async def _send_with_ack(self, msg: dict) -> bool:
        """Broadcasts to every connected device. Returns True if at least
        one device acknowledged it. Devices that fail to ack after retries
        are dropped (forced to reconnect) - mirrors the old single-device
        "treat as unreachable" behavior, just per-device instead of global."""
        targets = list(self.devices)
        if not targets:
            print("[ERROR] No device connected, cannot send message")
            return False

        results = await asyncio.gather(*(self._send_to_one(ws, msg) for ws in targets))
        for ws, ok in zip(targets, results):
            if not ok:
                print(f"[ERROR] {ws.remote_address} not responding, treating as unreachable")
                self.devices.discard(ws)
                try:
                    await ws.close()
                except Exception:
                    pass
        return any(results)

    async def show_state(self, state: str, summary: str = "") -> bool:
        """Displays an arbitrary state WITHOUT waiting for the ack - true
        fire-and-forget, as documented in docs/PROTOCOL.md. Used for visual
        testing (WORKING/SUCCESS/ERROR have no button trigger of their own)
        and for real WORKING/SUCCESS/ERROR signals from Claude Code hooks
        (Stop/PostToolUse etc.), which call in via session_status_hook.py
        with a very short local read timeout (0.5s).

        This method used to await _send_with_ack synchronously here (up to
        ACK_TIMEOUT_S * MAX_RETRIES per device) even though the hook script
        had usually already stopped listening - writing the response back
        to the control socket then failed with ConnectionResetError as soon
        as a connected-but-unresponsive device burned through its retries.
        The actual broadcast+retry now runs as a background task; the
        return value only reflects whether a device was connected to hand
        the message to, not whether it was acknowledged.

        While an approval decision is pending (self.current_request_id is
        set), non-approval status broadcasts are suppressed entirely rather
        than sent: this always carries request_id="" (see below), and a
        client that is mid-decision must never have its WAITING_APPROVAL
        screen silently replaced by an unrelated WORKING/SUCCESS/ERROR
        update - that would visually disconnect the human from what they're
        actually approving. The pending request's own WAITING_APPROVAL
        message (sent by request_approval, not this method) is unaffected.
        """
        if self.current_request_id is not None:
            print(f"[SUPPRESSED] Approval {self.current_request_id} pending, "
                  f"not broadcasting status {state}: {summary[:60]!r}")
            return False

        print(f"[STATE] {state}: {summary[:60]!r}")
        if not self.devices:
            print("[ERROR] No device connected, cannot send message")
            return False
        msg = {
            "type": "state",
            "msg_id": secrets.token_hex(8),
            "state": state,
            "request_id": "",
            "summary": summary,
            "ts": int(time.time()),
        }
        # Coalesce: a status update superseded by a newer one before it even
        # finished sending is pointless to keep retrying - cancel it so
        # show_state() bursts (e.g. rapid PostToolUse hooks) can't pile up
        # unbounded background tasks.
        if self._state_broadcast_task is not None and not self._state_broadcast_task.done():
            self._state_broadcast_task.cancel()
        task = asyncio.create_task(self._send_with_ack(msg))
        self._state_broadcast_task = task
        self._track_background_task(task)
        return True

    async def request_approval(self, summary: str) -> str:
        """Returns: 'approve' | 'deny' | 'timeout' | 'unreachable' | 'busy'.

        Only 'approve' allows the action. All other values mean 'no explicit
        approve' to the hook - the difference between 'deny' (explicitly
        rejected on a device) and 'timeout'/'unreachable'/'busy' (no response
        possible) only decides whether the hook hard-denies or falls back to
        the normal confirmation dialog (pre_tool_use_approval.py maps every
        non-approve/non-deny result to 'ask').

        current_request_id/pending_futures are single global slots, and
        multiple Unix-socket hook clients could call this concurrently (e.g.
        two parallel tool calls from Claude Code). Without serialization, a
        second concurrent call would overwrite current_request_id and its
        `finally` could delete the FIRST call's still-pending entry - button
        events could then never be attributed to the right request, or a
        stale finally could wipe out a request that's still legitimately
        pending. Rather than queue concurrent requests (which risks a button
        press being attributed to the wrong request in time), this fails
        closed immediately: only one approval decision is in flight at a
        time; any other concurrent caller gets 'busy' right away.
        """
        if self.request_lock.locked():
            print("[BUSY] An approval is already in progress, rejecting concurrent request")
            return "busy"

        async with self.request_lock:
            request_id = f"req_{secrets.token_hex(6)}"
            self.current_request_id = request_id
            fut = asyncio.get_event_loop().create_future()
            self.pending_futures[request_id] = fut

            msg = {
                "type": "state",
                "msg_id": secrets.token_hex(8),
                "state": "WAITING_APPROVAL",
                "request_id": request_id,
                "summary": summary,
                "ts": int(time.time()),
            }
            try:
                sent = await self._send_with_ack(msg)
                if not sent:
                    return "unreachable"

                try:
                    return await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT_S)
                except asyncio.TimeoutError:
                    print(f"[TIMEOUT] No response for {request_id} after {APPROVAL_TIMEOUT_S}s")
                    return "timeout"
            finally:
                self.current_request_id = None
                self.pending_futures.pop(request_id, None)


async def test_cli(daemon: ApprovalDaemon):
    """Manual interactive test mode: simulates an approval request without
    real Claude Code hooks."""
    await asyncio.sleep(1)
    while True:
        summary = await asyncio.get_event_loop().run_in_executor(
            None, input, "\nSend test request (Enter for sample text, 'q' to quit): "
        )
        if summary.strip().lower() == "q":
            break
        if not summary.strip():
            summary = "Bash: rm -rf build/"
        print(f"Sending WAITING_APPROVAL to device(s): {summary!r} ...")
        result = await daemon.request_approval(summary)
        print(f"=> Result: {result}")


SOCK_PATH = os.environ.get("CLAWD_APPROVAL_SOCK", "/tmp/clawd-approval.sock")


async def handle_control_client(daemon: ApprovalDaemon, reader, writer):
    """Local IPC interface (Unix socket): ONE line of JSON in
    ({"summary": "..."} or {"show_state": "...", "summary": "..."}), ONE
    line of JSON result back. This is the interface used by the Claude Code
    hooks (pre_tool_use_approval.py, session_status_hook.py)."""
    try:
        line = await reader.readline()
        req = json.loads(line)
        if not isinstance(req, dict):
            raise ValueError(f"control request must be a JSON object, got {type(req).__name__}")

        if "show_state" in req:
            state = req["show_state"]
            if not isinstance(state, str):
                raise ValueError("show_state must be a string")
            ok = await daemon.show_state(state, str(req.get("summary", "")))
            writer.write((json.dumps({"sent": ok}) + "\n").encode())
        else:
            summary = str(req.get("summary", "(no summary given)"))
            result = await daemon.request_approval(summary)
            writer.write((json.dumps({"result": result}) + "\n").encode())
        await writer.drain()
    except Exception as e:
        # Fail-closed by construction: on any error here we simply don't
        # write a response. pre_tool_use_approval.py treats a connection
        # that closes without a response line as "ask" (never "allow").
        print(f"[CONTROL-ERROR] {e!r}")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def main():
    daemon = ApprovalDaemon()
    host = os.environ.get("CLAWD_APPROVAL_BIND", "0.0.0.0")
    port = int(os.environ.get("CLAWD_APPROVAL_PORT", "8765"))

    if os.path.exists(SOCK_PATH):
        os.remove(SOCK_PATH)
    control_server = await asyncio.start_unix_server(
        lambda r, w: handle_control_client(daemon, r, w), path=SOCK_PATH
    )
    print(f"Local test/hook interface: {SOCK_PATH}")

    # subprotocols=["arduino"]: the Links2004 WebSockets library (ESP32 side)
    # sends "Sec-WebSocket-Protocol: arduino" and expects this subprotocol
    # confirmed in the server response, otherwise it disconnects immediately.
    # The Wear OS client (OkHttp) doesn't require this but is unaffected by
    # the server offering it.
    async with serve(
        daemon.handle_connection,
        host,
        port,
        subprotocols=["arduino"],
        ping_interval=PING_INTERVAL_S,
        ping_timeout=PING_TIMEOUT_S,
    ):
        print(f"Daemon running on ws://{host}:{port} (LAN-only, do not expose a TLS endpoint for this)")
        async with control_server:
            if sys.stdin.isatty():
                await test_cli(daemon)
            else:
                await control_server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
