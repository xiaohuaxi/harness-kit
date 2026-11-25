**English** | [简体中文](README.zh-CN.md)

# agy-agent-depth-guard

Fail-closed `PreToolUse` depth guard hook for **Antigravity CLI (`agy`)** subagent delegation (`invoke_subagent`). Single-file script, Python 3.8+ standard library only, zero external dependencies.

> **Supported scope**: Verified against **Antigravity CLI (`agy`)** using the `brain/<conversationId>/.system_generated/` session layout. Other layouts (such as Antigravity IDE or Antigravity 2.0) are not silently mapped to CLI defaults; unrecognized explicit paths fail closed.

## Why It Exists

- **Prevents runaway recursive delegation**: Subagents launched via `invoke_subagent` can spawn further subagents. Without a hard depth cap, a misbehaving or overly ambitious subagent tree can recurse indefinitely, exhausting concurrency and token budgets.
- **Closes agy's fail-open hook gap**: By default, `agy` fails open if a hook crashes or exits abnormally. This script catches validation errors and unexpected runtime exceptions and converts them into an explicit protocol-level denial (`exit 0` with `{"decision": "deny", "reason": "..."}` on `stdout`).

## Depth Hierarchy

`AGY_MAX_AGENT_DEPTH` sets the 0-based **maximum agent depth** allowed in the delegation tree (the root session is `depth 0`, children are `depth 1`, grandchildren are `depth 2`, and so on). An `invoke_subagent` call is blocked whenever `caller_depth + 1 > AGY_MAX_AGENT_DEPTH`. Setting `AGY_MAX_AGENT_DEPTH=2` (the default) caps the tree at **3 levels total** (`depth 0` → `depth 1` → `depth 2`):

```text
depth 0  (root session)         ──▶ invoke_subagent ALLOWED (creates depth 1 <= MAX_AGENT_DEPTH=2)
  └─ depth 1  (child)           ──▶ invoke_subagent ALLOWED (creates depth 2 <= MAX_AGENT_DEPTH=2)
       └─ depth 2  (grandchild) ──▶ invoke_subagent BLOCKED (may not create depth 3 > 2)
```

In general, `AGY_MAX_AGENT_DEPTH = N` allows agent depths `0 .. N` (`N + 1` levels total):
- `0` → 1 level (`depth 0` root only; all subagent delegation blocked)
- `1` → 2 levels (`depth 0` → `1`: `root` → `child`; children cannot spawn)
- `2` (default) → 3 levels (`depth 0` → `1` → `2`: `root` → `child` → `grandchild`; grandchildren cannot spawn)
- `3` → 4 levels (`depth 0` → `1` → `2` → `3`: `root` → `child` → `grandchild` → `great-grandchild`; depth 3 cannot spawn)

## How It Works

1. **Configuration & Protocol Checks**: Parses `AGY_MAX_AGENT_DEPTH` (strictly non-negative ASCII base-10 integer, or empty/unset for default `2`), parses JSON from `stdin`, and verifies `hookEventName == "PreToolUse"` whenever present and non-null (omitted/`null` is accepted for backward compatibility with older `agy` versions), `toolCall.name == "invoke_subagent"`, and a valid `conversationId`.
2. **CLI Brain Root Resolution**: Extracts CLI-layout candidates from `artifactDirectoryPath` and `transcriptPath`, preferring the candidate containing `<cid>/.system_generated/` (and allowing an early-root path ending in `<conversationId>` whose parent directory already exists). If explicit paths are provided but do not match the CLI layout, the hook fails closed instead of silently falling back. Only when both path fields are omitted/empty does it fall back to `~/.gemini/antigravity-cli/brain`.
3. **Ancestry Tracing (Primary + Fallback)**:
   - **Primary**: Scans `.system_generated/messages/*.json` (skipping `read.json`, using `os.scandir` so directory permission errors are never swallowed) for a message targeting the current conversation with `sourceMetadata.tool.toolCall.name == "invoke_subagent"`.
   - **Fallback**: Reads the first line of `.system_generated/logs/transcript.jsonl` when `step_index == 0`. `source == "USER_EXPLICIT"` confirms a root session; `source == "SYSTEM"` and `type == "SYSTEM_MESSAGE"` extracts the full `sender=<token>` field from the `[Message]` header and accepts it only when the entire token is a UUID (avoiding false matches on background task senders such as `<UUID>/task-7`).
   - **Integrity Checks**: Walks parent edges up to `max_agent_depth` (0 steps when `max_agent_depth == 0`, immediately denying root delegation). Missing ancestor metadata directories, unreadable message/transcript files without root proof, and ancestry cycles (including self-loops) all fail closed.
4. **Fail-Closed Enforcement**: Returns `{"decision": "allow"}` when `depth + 1 <= max_agent_depth`, or `{"decision": "deny", "reason": "AGY AGENT DEPTH GUARD: BLOCKED invoke_subagent: ..."}` with exit code `0` on depth limit violations or any error.

## Quick Install

Requirements: Python 3.8+ (standard library only) and Antigravity CLI (`agy`) with `PreToolUse` hook support.

1. Copy the script into your agy hooks directory and make it executable:

```bash
mkdir -p ~/.gemini/config/hooks
cp hooks/agy-agent-depth-guard/agy-agent-depth-guard.py ~/.gemini/config/hooks/agy-agent-depth-guard.py
chmod +x ~/.gemini/config/hooks/agy-agent-depth-guard.py
```

2. Register the hook in `~/.gemini/config/hooks.json` (leave `timeout` unset to keep agy's default 30-second timeout):

```json
{
  "agy-agent-depth-guard": {
    "PreToolUse": [
      {
        "matcher": "invoke_subagent",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.gemini/config/hooks/agy-agent-depth-guard.py"
          }
        ]
      }
    ]
  }
}
```

> To customize the limit per config without exporting a global shell variable, prefix the command:
> `"command": "AGY_MAX_AGENT_DEPTH=3 python3 ~/.gemini/config/hooks/agy-agent-depth-guard.py"`

## Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `AGY_MAX_AGENT_DEPTH` | `2` | 0-based maximum agent depth allowed in the delegation tree (`0 .. N`, `N + 1` levels total). Unset or empty string falls back to `2` (3 levels total: `depth 0..2`). `0` disables all subagent delegation (root only). Must be a non-negative ASCII base-10 integer (`0`, `1`, `2`, ...); any invalid value (such as `-1`, `abc`, `1.5`, or whitespace) fails closed with `{"decision": "deny", ...}`. |

## Fail-Closed Boundaries

Any of the following conditions immediately denies `invoke_subagent` (`exit 0` + `{"decision": "deny", "reason": ...}`):

- Invalid `AGY_MAX_AGENT_DEPTH` value (negative, non-integer, float, whitespace, non-ASCII digits)
- Empty `stdin`, malformed JSON, or non-object JSON payload
- `hookEventName` is present (non-null) and not `"PreToolUse"`, or `toolCall.name` is not `"invoke_subagent"`
- Missing, empty, non-string, or path-traversal `conversationId`
- Explicit `artifactDirectoryPath` / `transcriptPath` that cannot be mapped to a CLI `brain/<conversationId>/` layout (while allowing an early-root path ending in `<conversationId>` whose parent directory already exists)
- Broken ancestry where a confirmed parent conversation's `.system_generated` directory is missing
- Unreadable `messages/` directory or corrupt metadata when the session cannot be proven to be a `USER_EXPLICIT` root
- Ancestry cycle (self-loop or multi-hop cycle)
- Any unexpected runtime exception, caught at top level and converted into a JSON denial

> **Normal root sessions**: Because agy root sessions have no creation message and may invoke a subagent before metadata is flushed, a session with no `.system_generated` directory yet, no `invoke_subagent` creation message, or a `step_index == 0` `USER_EXPLICIT` transcript entry is treated as `depth = 0` (allowed whenever `AGY_MAX_AGENT_DEPTH >= 1`; blocked when `AGY_MAX_AGENT_DEPTH == 0`). A single concurrently half-written message file in `messages/` will not block if another valid `invoke_subagent` message or a `USER_EXPLICIT` root record is present.

## Running Tests

```bash
python3 hooks/agy-agent-depth-guard/test_agy_agent_depth_guard.py -v
```
