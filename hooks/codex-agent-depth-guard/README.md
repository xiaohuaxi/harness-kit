**English** | [简体中文](README.zh-CN.md)

# codex-agent-depth-guard

Fail-closed `PreToolUse` depth guard hook for **Codex CLI** multi-agent (`multi_agent_v2`). Single-file script, Python 3.8+ standard library only, zero external dependencies.

## Why It Exists

- **Prevents runaway recursive delegation**: With `multi_agent_v2` enabled, subagents can call `spawn_agent` to spawn further subagents. Without a hard depth cap, a misbehaving or overly ambitious subagent tree can recurse indefinitely, exhausting concurrency slots and token budgets.
- **Closes Codex's fail-open hook gap**: By default, Codex fails open if a hook crashes or exits with an unexpected error. This script catches all validation errors and uncaught exceptions and converts them into an explicit protocol-level denial (exit code `2` via `os._exit(2)` plus a clear reason on `stderr`).

## Depth Hierarchy

`CODEX_MAX_AGENT_DEPTH` sets the 0-based **maximum agent depth** allowed in the delegation tree (the root session is `depth 0`, children are `depth 1`, grandchildren are `depth 2`, and so on). A `spawn_agent` call is blocked whenever `caller_depth + 1 > CODEX_MAX_AGENT_DEPTH`. Setting `CODEX_MAX_AGENT_DEPTH=2` (the default) caps the tree at **3 levels total** (`depth 0` → `depth 1` → `depth 2`):

```text
depth 0  (root session)      ──▶ spawn_agent ALLOWED (creates depth 1 <= MAX_AGENT_DEPTH=2)
  └─ depth 1  (child)        ──▶ spawn_agent ALLOWED (creates depth 2 <= MAX_AGENT_DEPTH=2)
       └─ depth 2  (grandchild) ──▶ spawn_agent BLOCKED (may not create depth 3 > 2)
```

In general, `CODEX_MAX_AGENT_DEPTH = N` allows agent depths `0 .. N` (`N + 1` levels total):
- `0` → 1 level (`depth 0` root only; all subagent delegation blocked)
- `1` → 2 levels (`depth 0` → `1`: `root` → `child`; children cannot spawn)
- `2` (default) → 3 levels (`depth 0` → `1` → `2`: `root` → `child` → `grandchild`; grandchildren cannot spawn)
- `3` → 4 levels (`depth 0` → `1` → `2` → `3`: `root` → `child` → `grandchild` → `great-grandchild`; depth 3 cannot spawn)

## How It Works

1. **Configuration & Protocol Checks**: Parses `CODEX_MAX_AGENT_DEPTH` (strictly non-negative ASCII base-10 integer, or empty/unset for default `2`), parses JSON from `stdin`, and verifies `hook_event_name == "PreToolUse"` and `tool_name.endswith("spawn_agent")` (covering both plain `spawn_agent` and flattened v2 names such as `multi_agent_v2.spawn_agent`).
2. **Authoritative Depth Lookup**: Reads the first JSONL line of `transcript_path` and checks its `session_meta` payload:
   - **Root session** (`source` is a non-empty string such as `"cli"`, `"exec"`, or `"vscode"`): treated as `depth = 0`, and cross-checked so neither `agent_id` nor `agent_type` is present in the hook payload.
   - **Subagent session** (`source.subagent.thread_spawn.depth`): validates `depth` is a positive integer (rejecting booleans, zero, negatives, floats, and strings) and requires `agent_id` in the hook payload.
   - **Missing or unrecognized `source`**: rejected immediately (fail-closed) rather than assumed to be a root session.
3. **Fail-Closed Enforcement**: If `depth + 1 > max_agent_depth`—or if any check or I/O operation fails—the script writes `CODEX MULTI-AGENT DEPTH GUARD: BLOCKED spawn_agent: ...` to `stderr` and terminates immediately with `os._exit(2)`.

## Quick Install

Requirements: Python 3.8+ (standard library only) and a Codex CLI release supporting `PreToolUse` hooks.

1. Copy the script into your Codex hooks directory (`${CODEX_HOME:-$HOME/.codex}/hooks`) and make it executable:

```bash
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
mkdir -p "$CODEX_DIR/hooks"
cp hooks/codex-agent-depth-guard/codex-agent-depth-guard.py "$CODEX_DIR/hooks/codex-agent-depth-guard.py"
chmod +x "$CODEX_DIR/hooks/codex-agent-depth-guard.py"
```

2. Add the hook configuration to `${CODEX_HOME:-$HOME/.codex}/config.toml` (default: `~/.codex/config.toml`):

```toml
[features]
hooks = true
# CSV fanout spawns workers outside the single-agent spawn_agent hook path.
enable_fanout = false

[[hooks.PreToolUse]]
# Matches both plain "spawn_agent" and flattened v2 names such as "multi_agent_v2.spawn_agent".
matcher = "spawn_agent$"

[[hooks.PreToolUse.hooks]]
type = "command"
command = '"${CODEX_HOME:-$HOME/.codex}/hooks/codex-agent-depth-guard.py"'
timeout = 10
```

> - Keep `enable_fanout = false` under `[features]` so CSV fanout cannot bypass the `spawn_agent` hook path.
> - To customize the limit per config without exporting a global shell variable, prefix the command:
>   `command = 'CODEX_MAX_AGENT_DEPTH=3 "${CODEX_HOME:-$HOME/.codex}/hooks/codex-agent-depth-guard.py"'`

## Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `CODEX_MAX_AGENT_DEPTH` | `2` | 0-based maximum agent depth allowed in the delegation tree (`0 .. N`, `N + 1` levels total). Unset or empty string falls back to `2` (3 levels total: `depth 0..2`). `0` disables all subagent delegation (root only). Must be a non-negative ASCII base-10 integer (`0`, `1`, `2`, ...); any invalid value (such as `-1`, `abc`, `1.5`, or whitespace) fails closed with exit code `2`. |

## Fail-Closed Boundaries

Any of the following conditions immediately denies `spawn_agent` (exit code `2` + diagnostic on `stderr`):

- Invalid `CODEX_MAX_AGENT_DEPTH` value (negative, non-integer, float, whitespace, non-ASCII digits)
- Malformed JSON, empty `stdin`, or non-object JSON payload
- `hook_event_name` is not `"PreToolUse"`, or `tool_name` does not end with `"spawn_agent"`
- Missing, empty, non-string, or unreadable `transcript_path`
- Empty transcript file, non-JSON first line, or first line `type != "session_meta"`
- Missing, null, empty, or unrecognized `session_meta` `source` (neither a non-empty string root source nor a `subagent` object)
- Root `session_meta` combined with non-null `agent_id` or `agent_type` in the hook input
- Subagent `session_meta` missing `thread_spawn`, with non-positive / boolean / non-integer `depth`, or missing `agent_id` in the hook input
- Any unexpected runtime exception (`BaseException`), caught at top level and converted to `os._exit(2)`

## Running Tests

```bash
python3 hooks/codex-agent-depth-guard/test_codex_agent_depth_guard.py -v
```
