---
name: agy-run
description: Run, resume, or live-steer a bounded Gemini agent task through local agy (Antigravity CLI). Load when calling Gemini (including audio/video/PDF or image generation) from another agent family such as Codex or Claude Code; inside agy, load only when native invoke_subagent (which only inherits the current session model or runs flash, with inherited effort and sandbox) cannot satisfy the target model, reasoning effort, or sandbox mode—otherwise use invoke_subagent and do not load.
---

# agy-run

Drive local `agy acp` via `<skill-dir>/scripts/agy_run.py`. The script blocks in the foreground until the turn—including any >10s background command wakeup rounds—converges, so the host wakes the caller on process exit.

## Quick commands

Prompt input accepts a trailing positional argument, `--prompt-file`, or `stdin`:

```bash
# 0. One-time agent setup and model discovery
python3 <skill-dir>/scripts/agy_run.py --install-agents
python3 <skill-dir>/scripts/agy_run.py --list-models

# 1. Start a new session (default read-only sandbox; --write for workspace edits; --yolo for outside writes, unmounted $HOME toolchains, or image generation)
python3 <skill-dir>/scripts/agy_run.py \
  -m <model-slug-or-short-name> -e <low|medium|high|xhigh> \
  -C /abs/path/to/repo [--write | --yolo] [--add-dir /abs/extra] [--no-rules] \
  --session-id-file /tmp/agy-<tag>.sid \
  -o /tmp/agy-<tag>.out \
  --prompt-file /tmp/agy-<tag>.prompt.md

# 2. Standalone audio/video/PDF inspection or image generation (--no-rules saves ~25-35K input tokens; keep read-only for inspection, pass --yolo for image generation)
python3 <skill-dir>/scripts/agy_run.py -m <model> --no-rules -C /abs/target/dir "Inspect the media or PDF file"
python3 <skill-dir>/scripts/agy_run.py -m <model> --no-rules --yolo -C /abs/output/dir "Generate image and copy it to target path"

# 3. Resume a completed session (inherits saved config; -m/-e/sandbox can override, --add-dir accumulates, cwd and rules flag stay fixed)
python3 <skill-dir>/scripts/agy_run.py --resume /tmp/agy-<tag>.sid "Follow-up instructions"

# 4. Live-steer an active session (injects mid-turn guidance without killing the process)
python3 <skill-dir>/scripts/agy_run.py --steer /tmp/agy-<tag>.sid "Additional context or direction change"
```

## Host async wakeup

Always use the host's managed background execution (never detach with shell `&`):
- agy: `run_command` with `WaitMsBeforeAsync: 500`, `IsDaemon: false`. Once backgrounded, end the turn (or do parallel work) and wait for exit wakeup—do not poll.
- Claude Code: `Bash` with `run_in_background: true`, wait for `<task-notification>`, do not `sleep`-poll.
- Codex: `exec_command` with `yield_time_ms: 30000`; if a `session_id` returns while still running, continue with `write_stdin` (`chars: ""`, `yield_time_ms: 300000`) until exit.

## Key flags and behavior

- Model, effort, and timeout: `-m` supports three forms—omitted (keeps `agy`'s current default model), a full slug from `--list-models` (with or without an effort suffix), or a unique versioned suffix short name (resolves to the highest matching version). `-e` sets `low`, `medium`, `high`, or `xhigh` (defaults to `medium` when supported; omitted automatically if the model lacks `thought_level` and `-e` was not passed). Default timeout is `3600`s (`--timeout`, exit code `124`).
- Sandbox modes and directory boundaries:
  - Default `read-only`: enables file reads, built-in web search, and `--sandbox` commands, plus post-run zero-change workspace snapshot and `BypassSandbox` checks. Refuses workspaces over 300,000 entries; keep `--session-id-file` and `-o` outside the read-only workspace (e.g. `/tmp`).
  - Workspace edits (`--write`): adds workspace file editing and `--sandbox` commands; post-run outside-workspace write checks cover built-in file-write tool targets (not arbitrary shell writes). `--sandbox` isolation of `~/.ssh`, `.env`, unmounted home paths, and terminal network is verified on macOS Seatbelt (Linux/WSL2 isolation depends on the platform `agy` build); use `--yolo` when tasks need unmounted home toolchains or terminal network.
  - Full access (`--yolo`): disables terminal sandboxing and enables `generate_image`. Generated images start in agy's internal directory and are copied to the requested path; verify the actual image format on receipt (occasionally JPEG with a `.png` suffix).
  - Scope limits: Seatbelt isolates `run_command` only, while built-in read tools can still read outside the workspace because interactive approvals are skipped—guard against prompt injection on untrusted repos or web pages. `--add-dir` adds extra directories and accumulates across `--resume` (e.g. `--add-dir <main-repo/.git>` for git worktrees). Never move `-C` to a parent directory just to widen write access; keep `-C` on the target repo so its rules and config load properly.
- Rules and Claude project memory: rules load by default, and any matching Claude project memory (`.agents/memory/` or `~/.claude/projects/<encoded>/memory/`) is mounted read-only (edits under `--write` or `--yolo` fail post-run SHA-256 checks). Pass `--no-rules` for standalone media, search, or image tasks.
- Headless guardrails: interactive permission prompts are denied immediately; `IsDaemon: true` commands fail and abort the turn; finite commands exceeding 10s are drained automatically until wakeup converges. Built-in subagents and MCP tools are disabled; nested `agy-run` is blocked by env marker, and `claude` / `codex` are blocked via `PATH` stubs plus post-run command-position scanning.
- Output and troubleshooting: final text goes to `stdout` (and `-o`); `stderr` emits `AGY_SESSION_ID=<uuid>` first and `AGY_RUN_META={...}` last (actual model, effort, tokens, seconds, drained background tasks, `sandbox_check`). Treat `sandbox_check: "unverified"` as potentially unsandboxed. Retry with rephrased prompts on sporadic Gemini safety filter blocks, avoid unrelated sensitive paths such as `~/.ssh` in prompts to prevent verbal refusals (which still exit `0`), inspect workspace state before retrying failed runs (disk edits are not rolled back), and run `agy -p /usage` to check quota at zero token cost.

> Architecture and ACP state machine details (for maintainers only, do not read during routine runs): see [`DESIGN.md`](DESIGN.md).
