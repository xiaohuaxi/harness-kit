**English** | [简体中文](README.zh-CN.md)

# codex-quota-wakeup

Automatic weekly quota reset detection and minimal isolated wakeup daemon for **Codex CLI** (ChatGPT subscriptions). Single-file script, Python 3.8+ standard library only, supporting macOS, Linux, WSL2, and remote SSH servers.

## Why It Exists

Codex subscription weekly quota windows (`~10080` minutes / 7 days, compatible with `plus` / `pro` / `team` 5h+7d plans as well as weekly-only plans such as `prolite`) require a first request after cycle expiration or redeeming a `Full reset` voucher before the new 7-day countdown locks in. Waiting many hours before your first request pushes the next weekly refresh back by that same amount.

## How It Works

1. **Zero-Token Polling**: Runs every 30 minutes by default at `:03` and `:33` (plus catch-up on login / wake from sleep) via `codex app-server` stdio JSON-RPC to read effective config, subscription login state, and rate-limit windows (0 tokens, no session files created):
   - **Active**: `usedPercent > 0`, `resetsAt` is within window length minus 15 minutes, or any quota window is exhausted/limited—logs and exits with zero token usage.
   - **Unactivated**: `usedPercent == 0` (100% remaining) and 3 consecutive samples under the same account identity confirm `resetsAt` is continuously drifting forward with wall-clock time (prevents false triggers right after a first request while integer `usedPercent` still rounds to `0%`).
2. **Isolated Minimal Wakeup Request**: Once confirmed unactivated, sends a minimal ephemeral read-only request in an empty temporary directory (`codex exec --ephemeral --ignore-user-config --ignore-rules --skip-git-repo-check --sandbox read-only`, replying `OK`), then re-queries `account/rateLimits/read` to verify the 7-day countdown is locked. Total consumption per wakeup is typically well under `0.01%` of the weekly quota.
3. **Zero-Maintenance Across Model Upgrades**: Never hardcodes model versions; uses Codex's built-in default model with `low` reasoning effort by default (overridable via `--model` / `--effort`).
4. **Cross-Platform User-Level Scheduling**: Uses user-level `~/Library/LaunchAgents` (`launchd` `StartCalendarInterval`) on macOS and `~/.config/systemd/user/` (`systemd --user` timer with `Persistent=true`) on Linux—missed ticks after sleep coalesce into a single catch-up run without touching system directories or root privileges.

## Safety & Anti-Loop Guardrails

- **Never Touches Login Credentials**: Never reads or modifies `~/.codex/auth.json`; safely skips execution if a non-user `forced_*` policy conflicts with the active login to protect existing credentials (compliant Team cloud policies are allowed normally).
- **Never Enables Fast Mode or Privilege Bypass**: Explicitly locks the official ChatGPT backend URL and standard service tier (`service_tier="default"`, `features.fast_mode=false`), disables user MCP servers, hooks, and plugins, strips external API key / custom endpoint environment variables, and never bypasses sandbox permissions.
- **Hard Anti-Loop Caps**: Persists a 2-hour unverified cooldown before sending, enforces a rolling 7-day limit of at most 3 sends per account, and guards execution with a `flock` mutex. On network or CLI version errors, it exits cleanly and self-heals on the next scheduled tick.

For architecture decisions and non-goals, see [`DESIGN.md`](DESIGN.md) (in Chinese).

## Quick Install

Requirements: Python 3.8+ (standard library only) and `codex` CLI (`>= 0.158.0`) logged into a ChatGPT subscription via `codex login`.

```bash
chmod +x codex-quota-wakeup
mkdir -p ~/.local/bin && cp codex-quota-wakeup ~/.local/bin/
~/.local/bin/codex-quota-wakeup install
~/.local/bin/codex-quota-wakeup status
```

> - `install` writes the script's current path into the OS scheduler. Copy it to a stable directory such as `~/.local/bin` first—avoid installing directly from `~/Downloads`, `~/Desktop`, or `~/Documents`. If `codex-quota-wakeup` is not found in your shell, add `~/.local/bin` to `PATH` or invoke it by full path.
> - To change options, script path, or proxy settings, re-run `install` directly to update idempotently (no need to `uninstall` first; pass all optional flags you want to keep).
> - Enabling it on one primary machine per subscription is usually enough. If you run it on multiple machines (e.g. macOS + Linux), add `--minute-offset 18` on one machine (`:18` / `:48`) to avoid duplicate probes at the exact same minute.
> - If you frequently switch accounts or workspaces under default `~/.codex`, isolate background wakeup credentials with a dedicated `CODEX_HOME`: `CODEX_HOME=~/.codex-wakeup codex login && CODEX_HOME=~/.codex-wakeup codex-quota-wakeup install`.

Run unit tests:

```bash
python3 test_codex_quota_wakeup.py -v
```

## Common Commands

```bash
# Show scheduler status, schedule ticks, saved proxy settings, and recent log summary
codex-quota-wakeup status

# Run one check immediately (wakes if unactivated; add --dry-run to inspect without sending)
codex-quota-wakeup run
codex-quota-wakeup run --dry-run

# Install or update periodic task (default :03, :33; use --minute-offset 18 for multi-machine stagger)
codex-quota-wakeup install
codex-quota-wakeup install --minute-offset 18

# Uninstall periodic task (removes scheduler unit and proxy.json; keeps logs and state.json; on Linux run `loginctl disable-linger` if you also want to disable linger)
codex-quota-wakeup uninstall

# View recorded logs (all / activations only / errors only / raw JSONL)
codex-quota-wakeup logs -n 30
codex-quota-wakeup logs --activations
codex-quota-wakeup logs --errors
codex-quota-wakeup logs --json -n 20
```

## Options & Environment Variables

| Entry | Flag / Variable | Description |
| --- | --- | --- |
| `install` | `--interval SECONDS` | Check interval in seconds (default `1800`, i.e. every 30 minutes) |
| `install` | `--minute-offset 0-59` | Minute offset for hour-aligned ticks (default `3` → `:03,:33`; e.g. `18` → `:18,:48`) |
| `install` / `run` | `--codex-bin PATH` | Explicit path to the `codex` binary (for custom install locations) |
| `install` / `run` | `--model MODEL` | Override the wakeup model ID (defaults to Codex's built-in default model) |
| `install` | `--no-proxy` | Do not inherit current shell proxy settings and clear saved upstream proxy config (preserves custom CA) |
| `run` | `--dry-run` / `--force` | Check only without sending / force one minimal wakeup (still subject to cooldown and 7-day cap) |
| `run` | `--effort LEVEL` | Override reasoning effort for the wakeup turn (default `low`) |
| `run` | `--retry-delays LIST` | Retry delay list in seconds for transient network errors (default `5,15`) |
| Env var | `CODEX_HOME` | Dedicated Codex config and login directory |
| Env var | `CODEX_QUOTA_WAKEUP_STATE_DIR` | Override the state and log directory (keep consistent across subcommands after install) |

Run `codex-quota-wakeup --help` or `codex-quota-wakeup <subcommand> --help` for full details.

## Proxy Inheritance & Linux / WSL2 Notes

- **Automatic Proxy & Custom CA Inheritance**: `install` captures `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, and `NO_PROXY` (case-insensitive) plus `CODEX_CA_CERTIFICATE` / `SSL_CERT_FILE` (resolved to absolute paths) and stores them in `proxy.json` with mode `0600` for background runs (explicit proxy env vars in a foreground run take precedence). Re-run `codex-quota-wakeup install` (or pass `--no-proxy` to clear upstream proxy) whenever your proxy changes; `install` also triggers an initial background check immediately.
- **Linux / WSL2 & SSH Persistence**: Where `systemctl --user` is available, `install` configures the user timer and automatically runs `loginctl enable-linger` so timers stay active after all SSH sessions disconnect (prints the corresponding `sudo` command if restricted; inspect unit-level issues with `journalctl --user -u codex-quota-wakeup`). If `systemd --user` is unavailable, `install` exits safely and prints a `crontab -e` example including `PATH` (manually remove cron entries when uninstalling). Note that `linger` does not prevent Windows from auto-shutting down idle WSL instances; missed ticks catch up once the instance starts again (`Persistent=true`).

## State & Log Files

Stored by default under `~/.local/state/codex-quota-wakeup/` (override with `CODEX_QUOTA_WAKEUP_STATE_DIR`, exported in your shell profile for persistent use):

- `wakeup.jsonl` (`0600`, rotates to `wakeup.jsonl.1` at 2 MiB): structured record of every check and wakeup
- `state.json`: last wakeup, cooldown, and rolling 7-day counter state (written atomically)
- `proxy.json`: background proxy and CA certificate paths captured at install time (`0600`, omitted if none configured)
- `wakeup.lock`: `flock` mutex preventing concurrent runs
- `launchd-stderr.log`: fallback stderr log for macOS `launchd`
