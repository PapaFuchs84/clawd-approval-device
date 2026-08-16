#!/usr/bin/env python3
"""Non-blocking status hook for Claude Code (live activity display).

Unlike pre_tool_use_approval.py, this hook makes NO decision and blocks
NOTHING - it just pushes a display state to the device and returns
immediately. Field names per event type were verified against the installed
Claude Code SDK type definitions at the time of writing; verify against your
own installation (see docs/HOOKS.md). Errors are deliberately swallowed: a
display glitch must never affect a Claude Code action.

Register for: SessionStart, UserPromptSubmit, PostToolUse, Stop,
StopFailure, SessionEnd.
"""
import json
import os
import socket
import sys

SOCK_PATH = os.environ.get("CLAWD_APPROVAL_SOCK", "/tmp/clawd-approval.sock")
CONNECT_TIMEOUT_S = 1.5  # short: must never noticeably delay Claude Code


def send_state(state: str, summary: str):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(CONNECT_TIMEOUT_S)
        s.connect(SOCK_PATH)
        s.sendall((json.dumps({"show_state": state, "summary": summary[:200]}) + "\n").encode())
        # No response needed, but read briefly so the server side can
        # process the request cleanly before the socket goes away.
        s.settimeout(0.5)
        try:
            s.recv(64)
        except socket.timeout:
            pass
        s.close()
    except Exception:
        # Device/daemon unreachable -> just don't display anything, never fail
        pass


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        return

    event = payload.get("hook_event_name", "")

    if event == "SessionStart":
        source = payload.get("source", "?")
        send_state("IDLE", f"Session started ({source})")
    elif event == "UserPromptSubmit":
        prompt = payload.get("prompt", "")
        send_state("WORKING", prompt)
    elif event == "PostToolUse":
        tool_name = payload.get("tool_name", "?")
        send_state("WORKING", f"Tool: {tool_name}")
    elif event == "Stop":
        msg = payload.get("last_assistant_message") or "Response finished"
        send_state("SUCCESS", msg)
    elif event == "StopFailure":
        err = payload.get("error_details") or str(payload.get("error", "Error"))
        send_state("ERROR", err)
    elif event == "SessionEnd":
        reason = payload.get("reason", "?")
        send_state("IDLE", f"Session ended ({reason})")


if __name__ == "__main__":
    main()
