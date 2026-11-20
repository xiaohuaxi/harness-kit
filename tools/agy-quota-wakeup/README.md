**English** | [简体中文](README.zh-CN.md)

# agy-quota-wakeup

Automatic weekly quota reset detection and minimal chat-only wakeup daemon for **Antigravity CLI (`agy`)**. Single-file script, Python 3.8+ standard library only, supporting macOS, Linux, WSL2, and remote SSH servers.

## Why It Exists

Antigravity's weekly quota windows (`gemini-weekly` and `3p-weekly`, two independent pools) do not start a new 7-day countdown automatically when a cycle expires or an extra reset is granted—the 7-day timer only locks in after the first request to that pool. If you do not send a request for many hours after reset, your next refresh shifts back by that same delay.

## How It Works

1. **Zero-Token Polling**: Runs every 30 minutes by default at `:03` and `:33` (plus catch-up on login / wake from sleep) via `agy --output-format json -p=/usage` (0 tokens, no session created) to inspect both weekly quota buckets:
   - **Active**: `remaining_fraction < 1`, or `reset_time` is less than `7 days - 5 minutes` away—logs the check and exits with zero token usage.
   - **Unactivated**: `remaining_fraction == 1` and `reset_time` is still drifting forward alongside `now + 7 days`.
2. **Minimal Chat-Only Wakeup (with Standard Non-YOLO Fallback)**: Only when a pool is unactivated, writes a self-contained zero-tool custom agent (`.agents/agents/quota-wakeup.md` with `tools: []`, `inheritCustomizations: false`, `excludeDefaultComponents: true`) inside an empty temporary directory and sends a single-word `OK` prompt to that pool's low-thinking model (~370 tokens for Gemini, ~820 tokens for 3P—around `0.004%` of a weekly pool). If a future `agy` release rejects custom agent flags before sending, it falls back cleanly to a minimal standard non-YOLO invocation (`agy --model <id> --output-format json -p=...`, logged as `mode: "standard_fallback"`), then re-checks `/usage` to confirm the 7-day countdown is locked.
3. **Zero-Maintenance Across Model Generations & Platforms**:
   - Dynamically selects the latest `Flash Low` / `Claude Sonnet Low` model ID from `agy models`, so model upgrades require no manual edits.
   - Uses user-level `~/Library/LaunchAgents` (`launchd` `StartCalendarInterval`) on macOS and `~/.config/systemd/user/` (`systemd --user` timer with `Persistent=true`) on Linux—missed ticks after sleep coalesce into a single catch-up run without touching system directories or root privileges.

## Safety & Anti-Loop Guardrails

- **Zero-Tool / Non-YOLO Execution**: Runs inside an empty temporary directory with all tools, rules, skills, and MCP servers disabled; never passes `--dangerously-skip-permissions` or `--sandbox`; strips common external API key and gateway override environment variables and never touches user credential files.
- **Duplicate-Charge & Cooldown Protection**: Never auto-retries timeouts, network failures, or server errors; enters a 2-hour cooldown if the countdown lock cannot be verified; uses a `flock` mutex to prevent concurrent runs and exits cleanly on errors to self-heal on the next scheduled tick.

## Quick Install

Requirements: Python 3.8+ (standard library only) and a logged-in `agy` CLI (`>= 1.5.8`).

```bash
chmod +x agy-quota-wakeup
mkdir -p ~/.local/bin && cp agy-quota-wakeup ~/.local/bin/
~/.local/bin/agy-quota-wakeup install
~/.local/bin/agy-quota-wakeup status
```

> - `install` writes the script's current path into the OS scheduler. Copy it to a stable directory such as `~/.local/bin` first—avoid installing directly from `~/Downloads`, `~/Desktop`, or `~/Documents`. If `agy-quota-wakeup` is not found in your shell, add `~/.local/bin` to `PATH` or invoke it by full path.
> - To change options, script path, or proxy settings, re-run `install` directly to update idempotently (no need to `uninstall` first; pass all optional flags you want to keep).
> - Enabling it on one primary machine per account is usually enough. If you run it on multiple machines (e.g. macOS + Linux), add `--minute-offset 18` on one machine (`:18` / `:48`) to avoid duplicate probes at the exact same minute.

Run unit tests:

```bash
python3 test_agy_quota_wakeup.py -v
```

## Common Commands

```bash
# Show scheduler status, schedule ticks, saved proxy settings, and recent log summary
agy-quota-wakeup status

# Run one check immediately (wakes unactivated pools; add --dry-run to inspect without sending)
agy-quota-wakeup run
agy-quota-wakeup run --dry-run

# Install or update periodic task (default :03, :33; use --pools gemini for one pool, --minute-offset 18 for multi-machine stagger)
agy-quota-wakeup install
agy-quota-wakeup install --minute-offset 18

# Uninstall periodic task (removes scheduler unit and proxy.json; keeps logs and state.json; on Linux run `loginctl disable-linger` if you also want to disable linger)
agy-quota-wakeup uninstall

# View recorded logs (all / activations only / errors only / raw JSONL)
agy-quota-wakeup logs -n 30
agy-quota-wakeup logs --activations
agy-quota-wakeup logs --errors
agy-quota-wakeup logs --json -n 20
```

## Options & Environment Variables

| Entry | Flag / Variable | Description |
| --- | --- | --- |
| `install` | `--interval SECONDS` | Check interval in seconds (default `1800`, i.e. every 30 minutes) |
| `install` | `--minute-offset 0-59` | Minute offset for hour-aligned ticks (default `3` → `:03,:33`; e.g. `18` → `:18,:48`) |
| `install` / `run` | `--pools {all,gemini,3p}` | Weekly quota pools to monitor (default `all`) |
| `install` / `run` | `--agy-bin PATH` | Explicit path to the `agy` binary (for custom install locations) |
| `install` | `--no-proxy` | Do not inherit current shell proxy settings and clear any saved proxy config |
| `run` | `--dry-run` / `--force` | Check only without sending / force one minimal wakeup on target pool(s) |
| `run` | `--gemini-model` / `--3p-model` | Override the wakeup model ID for the corresponding pool |
| `run` | `--retry-delays LIST` | Retry delay list in seconds for transient `/usage` network errors (default `5,15`) |
| Env var | `AGY_QUOTA_WAKEUP_STATE_DIR` | Override the state and log directory (keep consistent across subcommands after install) |

Run `agy-quota-wakeup --help` or `agy-quota-wakeup <subcommand> --help` for full details.

## Proxy Inheritance & Linux / WSL2 Notes

- **Automatic Proxy Inheritance**: `install` captures `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, and `NO_PROXY` from the current shell (case-insensitive; automatically populates `HTTP(S)_PROXY` when only `ALL_PROXY` / `all_proxy` is set) and stores them in `proxy.json` with mode `0600` for background runs (explicit proxy env vars in a foreground run take precedence). Re-run `agy-quota-wakeup install` (or pass `--no-proxy` to clear) whenever your proxy changes; `install` also triggers an initial background check immediately.
- **Linux / WSL2 & SSH Persistence**: Where `systemctl --user` is available, `install` configures the user timer and automatically runs `loginctl enable-linger` so timers stay active after all SSH sessions disconnect (prints the corresponding `sudo` command if restricted; inspect unit-level issues with `journalctl --user -u agy-quota-wakeup`). If `systemd --user` is unavailable, `install` exits safely and prints a ready-to-use `crontab -e` example (`:03,:33`; manually remove cron entries when uninstalling). Note that `linger` does not prevent Windows from auto-shutting down idle WSL instances; missed ticks catch up once the instance starts again (`Persistent=true`).

## State & Log Files

Stored by default under `~/.local/state/agy-quota-wakeup/` (override with `AGY_QUOTA_WAKEUP_STATE_DIR`, exported in your shell profile for persistent use):

- `wakeup.jsonl` (rotates to `wakeup.jsonl.1` at 2 MiB): structured record of every check and wakeup
- `state.json`: last wakeup state per pool (written atomically)
- `proxy.json`: background proxy configuration captured at install time (`0600`, omitted if no proxy is set)
- `wakeup.lock`: `flock` mutex preventing concurrent runs
- `launchd-stderr.log`: fallback stderr log for macOS `launchd`
