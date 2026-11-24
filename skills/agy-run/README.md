**English** | [简体中文](README.zh-CN.md)

# agy-run

Run, resume, or live-steer bounded Gemini agent tasks through local **Antigravity CLI (`agy`)** over `agy acp`. Pure Python 3.8+ standard library with zero third-party dependencies.

## What It Does

- **Foreground-blocking convergence**: Drives `agy acp` in its own process group, publishes `AGY_SESSION_ID=<uuid>` as soon as the session starts, automatically waits for >10s background `run_command` wakeup rounds to converge, and emits structured `AGY_RUN_META={...}` on exit.
- **Resume and live steering**: Supports `--resume <uuid-or-file>` for follow-up turns and `--steer <uuid-or-file>` over a local Unix domain socket to redirect an active turn without killing the process.
- **Three-tier sandbox enforcement**:
  - `read-only` (default): read-only tools + `--sandbox` commands + pre/post zero-change workspace snapshot and `BypassSandbox` checks.
  - `--write` (`workspace-write`): workspace edits + `--sandbox` commands + post-run outside-workspace write and `BypassSandbox` checks.
  - `--yolo` (`danger-full-access`): full tools (including `generate_image`) without terminal sandboxing, while still blocking recursive `agy_run.py`, `claude`, and `codex` calls.
- **Headless safety guardrails**: Immediately denies interactive permission requests, aborts on `IsDaemon: true` commands, disables built-in subagents/MCP tools, and strips `GEMINI_API_KEY` / `AGY_LLM_GATEWAY_*` so runs stay on subscription auth.

## Prerequisites & Platform Support

- **Operating System**: POSIX hosts only (**macOS**, **Linux**, and **WSL2**). Native Windows is not supported (relies on `fcntl.flock`, Unix domain sockets, and POSIX process groups).
- **Runtime**: Python 3.8+ (standard library only) and a logged-in `agy` CLI.
  - Binary discovery order: `AGY_BIN` → `PATH` → `~/.local/bin/agy` → `/usr/local/bin/agy` → `/opt/homebrew/bin/agy`.
  - Proxy bridging: if `HTTP_PROXY` / `HTTPS_PROXY` are unset while `ALL_PROXY` / `all_proxy` is set, `agy-run` bridges `ALL_PROXY` to `HTTP_PROXY` and `HTTPS_PROXY` (avoiding Go `net/http` ignoring `ALL_PROXY` on WSL2).
- **Sandbox verification scope**:
  - The POSIX runner itself (ACP loop, `--list-models`, `--install-agents`, `--resume`, `--steer`, background drain, workspace snapshot, outside-write check, `BypassSandbox` inspection, and anti-recursion guards) works across macOS, Linux, and WSL2.
  - Terminal `--sandbox` OS-level isolation (`read-only` and `--write`) has been **empirically verified on macOS Seatbelt**. On Linux and WSL2, OS-level terminal isolation depends on the host `agy` build's `--sandbox` implementation; `agy-run`'s own snapshot, outside-write, `BypassSandbox`, and anti-recursion checks still run on all POSIX hosts.

## Installation

Copy or symlink the `agy-run` skill directory into your host agent's skill directory:

| Host | Skill directory |
| --- | --- |
| Claude Code | `~/.claude/skills/agy-run` |
| Codex | `~/.agents/skills/agy-run` |
| Antigravity (`agy`) | `~/.gemini/config/skills/agy-run` |

### Required One-Time Agent Initialization

`agy-run` uses 6 managed custom agent definitions (`agy-{readonly,write,yolo}[-rules].md`) under `~/.gemini/config/agents`. Install them before your first run:

```bash
python3 <skill-dir>/scripts/agy_run.py --install-agents
```

- Idempotent: re-running when all 6 files already match makes zero writes.
- Safe against conflicts and symlinks: if any file differs locally (or `~/.gemini/config/agents` is a symlink with missing/outdated files), `--install-agents` refuses partial or symlink-piercing writes and prints the exact explicit refresh command (`--write-agent-files <target-dir>`).
- Pre-flight check: before starting or resuming a turn, `agy_run.py` verifies that the selected agent definition exists and matches the current version, failing fast before launching `agy acp` if setup or an upgrade is needed.

## Usage Examples

List available models (clean `stdout`, stderr noise filtered) and copy the first column for `-m`:

```bash
python3 <skill-dir>/scripts/agy_run.py --list-models
```

Run a read-only inspection turn:

```bash
python3 <skill-dir>/scripts/agy_run.py \
  -m <model-slug-from-first-column> -e medium \
  -C /path/to/repo \
  --session-id-file /tmp/agy-demo.sid \
  -o /tmp/agy-demo.out \
  "Summarize the authentication flow and list potential edge cases."
```

Edit files inside a workspace (`--write`) or run standalone media/image tasks (`--no-rules`, add `--yolo` for image generation):

```bash
python3 <skill-dir>/scripts/agy_run.py -m <model> --write -C /path/to/repo "Fix the parser bug and run unit tests."
python3 <skill-dir>/scripts/agy_run.py -m <model> --no-rules -C /path/to/media "Transcribe and summarize clip.mp4."
python3 <skill-dir>/scripts/agy_run.py -m <model> --no-rules --yolo -C /path/to/out "Generate a minimalist icon and save to icon.png."
```

Resume a completed session or live-steer an active one:

```bash
python3 <skill-dir>/scripts/agy_run.py --resume /tmp/agy-demo.sid "Now check the token refresh path."
python3 <skill-dir>/scripts/agy_run.py --steer /tmp/agy-demo.sid "Focus only on server-side middleware."
```

## Conditional Features

- **Claude project memory (read-only)**: When rules are enabled (no `--no-rules`) and either `<cwd>/.agents/memory/MEMORY.md` or `~/.claude/projects/<encoded-cwd>/memory/MEMORY.md` exists, `agy-run` exposes that memory directory read-only to the agent (copying external memory into the per-run scratch directory under writable modes and verifying SHA-256 digests post-run). When no matching directory exists, this step is silently skipped and Claude Code is not required.

## Testing

Run the offline unit and integration test suite (uses a built-in fake ACP server, no `agy` quota spent):

```bash
python3 skills/agy-run/tests/test_agy_run.py -v
```

Optional live integration tests against real `agy acp` (consumes quota; requires `--install-agents` and a logged-in `agy`):

```bash
AGY_LIVE=1 python3 skills/agy-run/tests/live_agy_check.py -v
```
