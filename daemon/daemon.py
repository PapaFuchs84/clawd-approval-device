#!/usr/bin/env python3
"""Host daemon for the Clawd Approval Device.

Runs on the machine where Claude Code runs (or anywhere reachable on the
same LAN as the device). Accepts exactly ONE authenticated WebSocket
connection from the ESP32, manages approval requests with fail-closed
behavior, and exposes a local Unix socket for Claude Code hooks (and a
manual interactive test mode).

Required environment variables (never hardcode these):
    CLAWD_APPROVAL_TOKEN    - bearer token the device sends on connect
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
ACK_TIMEOUT_S = 3
MAX_RETRIES = 3
# Short window: the physical buttons are an ALTERNATIVE to the normal digital
# confirmation, not a replacement. If nobody responds on the device, the hook
# falls back to Claude Code's normal confirmation dialog (see
# pre_tool_use_approval.py, result "timeout" -> permissionDecision "ask").
APPROVAL_TIMEOUT_S = int(os.environ.get("CLAWD_APPROVAL_TIMEOUT", "25"))


class ApprovalDaemon:
    def __init__(self):
        self.device_ws = None
        self.current_request_id = None
        self.seen_event_ids = deque(maxlen=200)
        self.pending_futures = {}
        self.pending_acks = {}  # msg_id -> asyncio.Event

    def _hmac_ok(self, payload: dict) -> bool:
        sig = payload.get("sig")
        if not sig:
            return False
        body = {k: v for k, v in payload.items() if k != "sig"}
        # separators=(",", ":") -> compact form with no spaces, exactly what
        # ArduinoJson::serializeJson() produces. Without this flag Python
        # inserts spaces after ':' and ',' -> HMAC mismatch, every signature
        # would be rejected as invalid.
        raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        expected = hmac.new(HMAC_KEY_BYTES, raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)

    async def handle_connection(self, ws):
        auth_header = ws.request_headers.get("Authorization", "")
        if auth_header != f"Bearer {AUTH_TOKEN}":
            print(f"[REJECTED] Invalid/missing auth header from {ws.remote_address}")
            await ws.close(code=4401, reason="unauthorized")
            return

        if self.device_ws is not None:
            print(f"[REJECTED] A device is already connected, rejecting connection from {ws.remote_address}")
            await ws.close(code=4409, reason="already-connected")
            return

        print(f"[CONNECTED] Device {ws.remote_address} authenticated")
        self.device_ws = ws
        try:
            async for raw in ws:
                await self._handle_message(raw)
        except websockets.ConnectionClosed as e:
            print(f"[DISCONNECT-DETAIL] {e!r}")
        finally:
            print(f"[DISCONNECTED] {ws.remote_address}")
            if self.device_ws is ws:
                self.device_ws = None
            # Fail-closed: any pending request is resolved on disconnect.
            # "unreachable" (NOT "deny"!) -> the hook falls back to the normal
            # confirmation dialog instead of hard-denying.
            for fut in self.pending_futures.values():
                if not fut.done():
                    fut.set_result("unreachable")

    async def _handle_message(self, raw: str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[WARN] Ignoring invalid JSON: {raw!r}")
            return

        mtype = payload.get("type")
        if mtype == "ack":
            ev = self.pending_acks.get(payload.get("msg_id"))
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

        button = payload.get("button")
        print(f"[BUTTON] {button} for request_id={request_id}")
        fut = self.pending_futures.get(request_id)
        if fut and not fut.done():
            fut.set_result("approve" if button == "approve" else "deny")

    async def _send_with_ack(self, msg: dict) -> bool:
        if self.device_ws is None:
            print("[ERROR] No device connected, cannot send message")
            return False
        msg_id = msg["msg_id"]
        ack_event = asyncio.Event()
        self.pending_acks[msg_id] = ack_event
        try:
            for attempt in range(1, MAX_RETRIES + 1):
                await self.device_ws.send(json.dumps(msg))
                try:
                    await asyncio.wait_for(ack_event.wait(), ACK_TIMEOUT_S)
                    return True
                except asyncio.TimeoutError:
                    print(f"[RETRY {attempt}/{MAX_RETRIES}] No ACK for {msg_id}")
            print("[ERROR] Device not responding, treating as unreachable")
            self.device_ws = None
            return False
        finally:
            self.pending_acks.pop(msg_id, None)

    async def show_state(self, state: str, summary: str = "") -> bool:
        """Displays an arbitrary state without waiting for a button response.
        Used for visual testing (WORKING/SUCCESS/ERROR have no button
        trigger of their own) and for real WORKING/SUCCESS/ERROR signals
        from Claude Code hooks (Stop/PostToolUse etc.)."""
        print(f"[STATE] {state}: {summary[:60]!r}")
        msg = {
            "type": "state",
            "msg_id": secrets.token_hex(8),
            "state": state,
            "request_id": "",
            "summary": summary,
            "ts": int(time.time()),
        }
        return await self._send_with_ack(msg)

    async def request_approval(self, summary: str) -> str:
        """Returns: 'approve' | 'deny' | 'timeout' | 'unreachable'.

        Only 'approve' allows the action. All other values mean 'no explicit
        approve' to the hook - the difference between 'deny' (explicitly
        rejected on the device) and 'timeout'/'unreachable' (no response)
        only decides whether the hook hard-denies or falls back to the
        normal confirmation dialog.
        """
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
        sent = await self._send_with_ack(msg)
        if not sent:
            self.current_request_id = None
            self.pending_futures.pop(request_id, None)
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
        print(f"Sending WAITING_APPROVAL to device: {summary!r} ...")
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
        if "show_state" in req:
            ok = await daemon.show_state(req["show_state"], req.get("summary", ""))
            writer.write((json.dumps({"sent": ok}) + "\n").encode())
        else:
            summary = req.get("summary", "(no summary given)")
            result = await daemon.request_approval(summary)
            writer.write((json.dumps({"result": result}) + "\n").encode())
        await writer.drain()
    except Exception as e:
        print(f"[CONTROL-ERROR] {e!r}")
    finally:
        writer.close()


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

    # subprotocols=["arduino"]: the Links2004 WebSockets library sends
    # "Sec-WebSocket-Protocol: arduino" and expects this subprotocol
    # confirmed in the server response, otherwise it disconnects immediately.
    async with serve(daemon.handle_connection, host, port, subprotocols=["arduino"]):
        print(f"Daemon running on ws://{host}:{port} (LAN-only, do not expose a TLS endpoint for this)")
        async with control_server:
            if sys.stdin.isatty():
                await test_cli(daemon)
            else:
                await control_server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
