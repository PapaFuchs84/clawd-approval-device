# Claude Code hooks: verifying and wiring them up

Claude Code's hook input/output JSON contract is part of its SDK and **can
change between versions**. Don't trust this document (or any other) blindly
— verify it against your own installed version before enabling the blocking
approval hook.

## 1. Verify the contract for your version

If your Claude Code install ships TypeScript type definitions (look for a
`claude-agent-sdk` package, e.g. under `node_modules/@anthropic-ai/`), the
authoritative source is `sdk.d.ts` — search it for `PreToolUseHookInput`,
`SyncHookJSONOutput`, and `HookPermissionDecision`.

If you can't find type definitions, verify empirically instead: register a
throwaway logging hook and inspect what it actually receives.

```bash
cat > /tmp/hook_debug.sh <<'EOF'
#!/usr/bin/env bash
cat >> /tmp/hook_debug.log
echo "---" >> /tmp/hook_debug.log
exit 0
EOF
chmod +x /tmp/hook_debug.sh
```

Add to your Claude Code `settings.json`:
```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash", "hooks": [{"type": "command", "command": "/tmp/hook_debug.sh"}]}
    ]
  }
}
```

Run any Bash tool call, then inspect `/tmp/hook_debug.log` for the exact
JSON shape your version sends.

At the time this project was built, the verified contract was:

```json
// stdin (PreToolUse)
{
  "session_id": "...", "transcript_path": "...", "cwd": "...",
  "permission_mode": "auto", "hook_event_name": "PreToolUse",
  "tool_name": "Bash", "tool_input": {"command": "..."},
  "tool_use_id": "toolu_..."
}
```
```json
// stdout, either form works
{"decision": "approve"}
// or, more specific:
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
```
`permissionDecision` accepts `allow | deny | ask | defer`.

## 2. Enabling the blocking approval hook

**Careful: this affects the session you enable it in immediately**, including
an interactive session you're currently using — the very next matching tool
call will wait for a button press (or fall back to `ask` after the
configured timeout).

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "python3 /path/to/daemon/pre_tool_use_approval.py"}]
      }
    ]
  }
}
```

Widen `matcher` (e.g. `"Bash|Write|Edit|MultiEdit"`) to cover more tools once
you're comfortable with the behavior.

## 3. Enabling the non-blocking live-status hooks

These never block anything and are safe to enable in an active session:

```json
{
  "hooks": {
    "SessionStart":     [{"hooks": [{"type": "command", "command": "python3 /path/to/daemon/session_status_hook.py"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 /path/to/daemon/session_status_hook.py"}]}],
    "PostToolUse":      [{"hooks": [{"type": "command", "command": "python3 /path/to/daemon/session_status_hook.py"}]}],
    "Stop":             [{"hooks": [{"type": "command", "command": "python3 /path/to/daemon/session_status_hook.py"}]}],
    "StopFailure":       [{"hooks": [{"type": "command", "command": "python3 /path/to/daemon/session_status_hook.py"}]}],
    "SessionEnd":        [{"hooks": [{"type": "command", "command": "python3 /path/to/daemon/session_status_hook.py"}]}]
  }
}
```

Both hook blocks can be combined in the same `settings.json`.
