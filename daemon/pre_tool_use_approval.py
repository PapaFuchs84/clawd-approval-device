#!/usr/bin/env python3
"""PreToolUse hook for Claude Code.

The exact stdin/stdout contract for hooks is part of Claude Code's SDK
type definitions and can change between versions. Verify it for your own
installation before relying on this (see docs/HOOKS.md for how). This
script was written and tested against a PreToolUse contract of:

  stdin:  JSON including tool_name, tool_input, tool_use_id, session_id
  stdout: JSON with hookSpecificOutput.permissionDecision = allow|deny|ask

Design: the physical buttons are an ALTERNATIVE to the normal digital
confirmation, not a replacement. Only an explicit button event decides
actively (approve -> allow, deny -> deny). If nobody responds on the device
(timeout) or it is unreachable, the hook returns 'ask': Claude Code then
falls back to its normal confirmation dialog. There is NO code path that
returns 'allow' on error or timeout.
"""
import json
import os
import socket
import sys

SOCK_PATH = os.environ.get("CLAWD_APPROVAL_SOCK", "/tmp/clawd-approval.sock")
# A bit more generous than the daemon-internal APPROVAL_TIMEOUT_S (default
# 25s in daemon.py), so the hook doesn't give up BEFORE the daemon does.
SOCKET_TIMEOUT_S = int(os.environ.get("CLAWD_APPROVAL_TIMEOUT", "25")) + 10


def build_summary(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", ""))[:200]
        return f"Bash: {cmd}"
    if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = tool_input.get("file_path", "?")
        return f"{tool_name}: {path}"
    # Generic fallback for all other tools
    compact = json.dumps(tool_input, ensure_ascii=False)[:200]
    return f"{tool_name}: {compact}"


def respond(decision: str, reason: str = None):
    """decision: 'allow' | 'deny' | 'ask'. The single output path - prints
    valid JSON and exits 0."""
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision}}
    if reason:
        out["hookSpecificOutput"]["permissionDecisionReason"] = reason
    print(json.dumps(out))
    sys.exit(0)


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
    except Exception as e:
        # Can't interpret the request at all -> defer to the normal dialog
        respond("ask", f"Hook could not read stdin JSON: {e!r}")
        return

    tool_name = payload.get("tool_name", "?")
    tool_input = payload.get("tool_input") or {}
    summary = build_summary(tool_name, tool_input)

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(SOCKET_TIMEOUT_S)
        s.connect(SOCK_PATH)
        s.sendall((json.dumps({"summary": summary}) + "\n").encode())

        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()

        if not buf:
            respond("ask", "Approval daemon closed the connection without a response")
            return

        result = json.loads(buf.decode()).get("result")
    except (ConnectionRefusedError, FileNotFoundError):
        respond("ask", "Approval device unreachable (daemon not running) -> normal dialog")
        return
    except socket.timeout:
        respond("ask", f"No response from approval daemon after {SOCKET_TIMEOUT_S}s -> normal dialog")
        return
    except Exception as e:
        respond("ask", f"Unexpected error during approval request: {e!r} -> normal dialog")
        return

    # Only an EXPLICIT button event decides actively. Everything else
    # (timeout/unreachable/busy - nobody responded, or the daemon was
    # already handling a different concurrent approval) -> 'ask', NEVER
    # 'allow'.
    if result == "approve":
        respond("allow")
    elif result == "deny":
        respond("deny", "Rejected on the physical device")
    elif result == "busy":
        respond("ask", "Approval daemon already busy with another request -> normal confirmation dialog")
    else:
        respond("ask", f"No response on the device ({result}) -> normal confirmation dialog")


if __name__ == "__main__":
    main()
