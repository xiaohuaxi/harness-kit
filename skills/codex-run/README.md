**English** | [简体中文](README.zh-CN.md)

# codex-run

A cross-agent skill and Python CLI runner that drives local `codex app-server --stdio` for bounded Codex tasks, supporting thread `--resume`, live mid-turn `--steer`, anti-early-exit subagent draining, external Codex profiles, and `--list-models` discovery.

## Key Features

- **Single-Process Foreground Lifecycle**: Blocks until the root turn and any unawaited internal `spawn_agent` descendants finish, letting host agents (Claude Code, Antigravity `agy`, Codex) wake up natively on process exit without polling or orphan daemons.
- **Anti-Early-Exit Drain & Auto-Continue**: Tracks child/grandchild subagent threads on the shared `app-server` stream; if the root turn ends with active subagents, waits for drain and triggers up to 3 bounded auto-continue turns, appending a visible notice to `stdout`, `stderr`, and `-o`.
- **Live Mid-Turn Steer & Concurrency Lock**: Exposes a per-thread Unix domain socket for `--steer` (`turn/steer` RPC) and guards each thread with an exclusive `flock` to prevent concurrent `--resume` corruption.
- **Model Discovery (`--list-models`)**: Inspects `$CODEX_HOME/models_cache.json` or an external profile's `$CODEX_HOME/<name>.config.toml` and `model_catalog_json` with zero third-party dependencies (Python 3.8+ compatible).
- **Isolation & Redaction**: Runs in a fresh process group with an empty `ZDOTDIR`, non-login shell, excluded secret environment variables, `CODEX_RUN_ACTIVE=1` recursion guard, and output secret redaction.

## Requirements

- **OS**: POSIX (macOS, Linux, or WSL2). Native Windows is not supported (`AF_UNIX`, `fcntl.flock`, and POSIX process-group signals are required).
- **Python**: Python 3.8+ (standard library only, zero external dependencies).
- **Codex CLI**: Local `codex` binary on `PATH` (or via `CODEX_BIN`), authenticated for Codex or configured with an external profile under `$CODEX_HOME`.

## Installation

Copy or symlink this `codex-run` directory into your agent's skills directory:

- **Claude Code**: `~/.claude/skills/codex-run`
- **Codex**: `~/.agents/skills/codex-run`
- **Antigravity (`agy`)**: `~/.gemini/config/skills/codex-run`

Example:

```bash
mkdir -p ~/.claude/skills
cp -R skills/codex-run ~/.claude/skills/codex-run
```

## Quick Examples

```bash
# List available models from local models_cache.json
python3 skills/codex-run/scripts/codex_run.py --list-models

# List models configured for an external profile ($CODEX_HOME/deepseek.config.toml)
python3 skills/codex-run/scripts/codex_run.py --list-models --profile deepseek

# Run a bounded task (default read-only; add --write for workspace edits or --yolo for full access)
python3 skills/codex-run/scripts/codex_run.py \
  -m gpt-5.4 -e medium \
  -C /path/to/repo --write \
  --session-id-file /tmp/codex-demo.sid \
  -o /tmp/codex-demo.out \
  "Review the auth module and fix the failing unit test"

# Run with an external profile
python3 skills/codex-run/scripts/codex_run.py \
  --profile deepseek -C /path/to/repo "Summarize the module architecture"

# Resume a finished thread
python3 skills/codex-run/scripts/codex_run.py \
  --resume /tmp/codex-demo.sid "Proceed with option B and run the test suite"

# Live-steer a currently running turn
python3 skills/codex-run/scripts/codex_run.py \
  --steer /tmp/codex-demo.sid "Skip the migration file; focus only on unit tests"
```

## Conditional Behavior Note

When `--profile deepseek` is passed, `codex_run.py` appends a prompt-level `DEEPSEEK_GUARD` instructing the delegated root not to invoke built-in `spawn_agent`. This works around current Codex V2 external-provider issues where child agent plaintext messages are not delivered reliably and full-history forks can replay the root delegation prompt. Standard Codex runs and other external profiles do not inject this guard.

## Running Tests

```bash
python3 skills/codex-run/tests/test_codex_run.py -v
```

For architecture, JSON-RPC state machine, and lock invariants, see [DESIGN.md](DESIGN.md).
