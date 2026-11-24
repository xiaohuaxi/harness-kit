---
name: codex-run
description: Run, resume, or live-steer a bounded Codex agent task through local codex CLI. Load when calling GPT models or external Codex profiles (such as DeepSeek) from another agent family like Claude Code or agy; inside Codex, load only for external profiles—use native spawn_agent for regular GPT subtasks and do not load.
---

# codex-run

Drive local `codex app-server --stdio` through `<skill-dir>/scripts/codex_run.py`. The script blocks in the foreground until the current turn (including any internal subagents not explicitly awaited with `wait_agent`) converges, so the host wakes the caller on process exit.

## Quick commands

Provide the task as a trailing positional argument, via `--prompt-file`, or on `stdin`:

```bash
# 1. List available models (local models_cache.json or an external profile)
python3 <skill-dir>/scripts/codex_run.py --list-models
python3 <skill-dir>/scripts/codex_run.py --list-models --profile deepseek

# 2. Start a new thread (default read-only; --write for workspace edits; --yolo for writes outside -C)
python3 <skill-dir>/scripts/codex_run.py \
  -m <model-slug-or-suffix> -e <low|medium|high|xhigh> \
  -C /abs/path/to/repo [--write | --yolo] \
  --session-id-file /tmp/codex-<tag>.sid \
  -o /tmp/codex-<tag>.out \
  --prompt-file /tmp/codex-<tag>.prompt.md

# 3. External profile (reads $CODEX_HOME/<name>.config.toml)
python3 <skill-dir>/scripts/codex_run.py --profile deepseek -C /abs/path/to/repo "Task prompt"

# 4. Resume a finished thread (reuses saved profile/model/effort/sandbox/cwd unless overridden)
python3 <skill-dir>/scripts/codex_run.py --resume /tmp/codex-<tag>.sid "Follow-up instruction or decision"

# 5. Live-steer an active turn (injects turn/steer without killing the process)
python3 <skill-dir>/scripts/codex_run.py --steer /tmp/codex-<tag>.sid "Additional context or course correction"
```

## Host async wakeup

Always use the host's managed background execution (never detach with shell `&`):

- agy: call `run_command` with `WaitMsBeforeAsync: 500` and `IsDaemon: false`; once backgrounded, end the turn (or do parallel work) and wait for completion wakeup without polling.
- Claude Code: call `Bash` with `run_in_background: true` and wait for `<task-notification>` without `sleep` polling.
- Codex: call `exec_command` with `yield_time_ms: 30000`; if it returns a running `session_id`, continue with `write_stdin` (`chars: ""`, `yield_time_ms: 300000`) until exit.

## Key flags and behavior

- Models and effort: run `--list-models` (or `--list-models --profile <name>`) to inspect available slugs and effort levels. `-m` accepts three forms: omitted (uses the default model), a full model slug, or a short suffix name. Suffix auto-resolution to the newest version applies only without `--profile` when local `$CODEX_HOME/models_cache.json` exists; with `--profile`, `-m` is passed through verbatim and omitting `-m` or `-e` inherits the profile config. Without `--profile`, `-e` defaults to `medium`.
- Sandbox and `-C` boundary: defaults to `read-only`. Prefer `--write` (`workspace-write`, writable inside `-C` and temp dirs) for repository edits. When writes outside `-C` or full access are needed, keep `-C` on the target repo and pass `--yolo` (`danger-full-access`). Never move `-C` up to a parent directory to satisfy `--write`, or the agent loses the target repo's `AGENTS.md`, `.codex/config.toml`, and project-level skills.
- Non-interactive mode and anti-early-exit: interactive user-input prompts are disabled; if the delegated agent needs a decision, it prints the question and ends its turn so the caller can reply with `--resume`. If it spawns internal `spawn_agent` children and exits without `wait_agent`, the script waits for all subagents to drain and triggers up to 3 auto-continue turns; when `auto_continues > 0`, a notice is appended to `stdout`, `stderr`, and `-o`, and the caller should report it to the user.
- Subagent fan-out and timeout: recursive `codex-run` calls are blocked (`CODEX_RUN_ACTIVE=1`). State explicitly in the prompt if internal `spawn_agent` fan-out is desired (`--profile deepseek` adds prompt-level instructions forbidding built-in `spawn_agent`). Default timeout is `3600` seconds (`--timeout`, exit code `124`).
- Output contract: the final reply is written to `stdout` (and `-o` if set); `stderr` emits `CODEX_THREAD_ID=<uuid>` on startup and `CODEX_RUN_META={...}` at exit (actual model, effort, token summary, and `auto_continues`).

> Architecture and state-machine details (for maintainers only; do not read during routine runs): see [DESIGN.md](DESIGN.md).
