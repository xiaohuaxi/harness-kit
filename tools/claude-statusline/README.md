**English** | [简体中文](README.zh-CN.md)

# claude-statusline

A soft 256-color, 7-segment `statusLine` command for **Claude Code**. Single-file script using Bash (3.2+) and Python 3.7+ standard library only, with zero third-party dependencies, supporting macOS, Linux, and WSL2.

## Output Example

```text
~/work/demo · Opus 4.6 (1M context) · high · 42k/200k 21% · 7d 12% ↻ 5.4d · 5h 8% ↻ 19:30 · main · $0.42
```

Segments (omitted automatically when absent):

1. **cwd**: Current directory (`workspace.current_dir` with fallback to `cwd` and `$PWD`), with optional longest-prefix alias rules (`CLAUDE_STATUSLINE_PATH_ALIASES`) and `$HOME` → `~` shortening.
2. **model**: Active model name (`model.display_name`, falling back to `model.id`), stripping only trailing effort tags such as `(High)` while preserving useful suffixes like `(1M context)`.
3. **effort**: Active reasoning effort (`effort.level`), hidden automatically when absent.
4. **context usage**: Used/total tokens (`k`/`M`) and percentage; falls back from `current_usage` to `total_input_tokens`, and renders standalone percentage (e.g. `24%`) when token totals are absent.
5. **quota (`7d` / `5h`)**: Independent weekly (`7d`) usage with remaining days countdown (`↻ X.Xd`) and 5-hour (`5h`) usage with local reset time (`↻ HH:MM` or `↻ MM-DD HH:MM`).
6. **git branch**: Current branch or short detached commit hash when inside a git work tree (requires optional `git`).
7. **cost**: Session total USD cost (`$X.XX`) when provided in the status payload.

## Key Features

- **Soft 256-Color Hierarchy**: Uses muted 256-color tones (gray labels, bold blue cwd, muted cyan model, sage green effort, deep amber percentages) for clear scanning without harsh ANSI brightness.
- **Resilient Claude Code Schema Handling**: Safely validates all nested JSON fields and types, preserves `(1M context)` suffixes in model names, handles missing `current_usage` / `rate_limits` / `effort` cleanly, and escapes all values before shell evaluation to prevent injection.
- **Opt-In Quota History Logging**: Disabled by default; when enabled via `CLAUDE_STATUSLINE_QUOTA_LOG`, appends deduplicated JSONL snapshots on quota changes without ever interrupting rendering on I/O errors.
- **Zero External Dependencies**: Runs out of the box on stock macOS Bash 3.2, Linux/WSL2 Bash 5.x, and Python 3.7+ standard library (`git` is optional).

## Account & Backend Support

- **Subscription Quotas (`5h` / `7d`)**: The `five_hour` and `seven_day` windows under `rate_limits` are populated by Claude Code for **claude.ai Pro / Max / Ultra** subscription accounts.
- **API Key & Third-Party Backends**: When `five_hour` and `seven_day` windows are absent (such as raw Anthropic API keys, third-party proxies like DeepSeek, Amazon Bedrock / Google Vertex AI, or Claude apps gateways providing only `spend_limit`), the quota segment hides automatically while all other segments (`cwd`, `model`, `effort` if present, `context usage`, `git branch`, `cost`) render normally.
- **Workspace Trust Gate**: Claude Code only executes custom `statusLine` commands in workspaces you have trusted.

## Quick Install

Requirements: Bash 3.2+, Python 3.7+ (standard library only), and optional `git`. Works identically on macOS, Linux, and WSL2.

1. Copy the script into your Claude config directory (`${CLAUDE_CONFIG_DIR:-$HOME/.claude}`) and make it executable:

```bash
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
mkdir -p "$CLAUDE_DIR"
cp tools/claude-statusline/claude-statusline "$CLAUDE_DIR/claude-statusline"
chmod +x "$CLAUDE_DIR/claude-statusline"
```

2. Enable it in `~/.claude/settings.json` (or `${CLAUDE_CONFIG_DIR}/settings.json`):

```json
{
  "statusLine": {
    "type": "command",
    "command": "bash \"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/claude-statusline\""
  }
}
```

> You can also add an optional `"refreshInterval": 30` (seconds) inside `statusLine` if your Claude Code version supports periodic background refreshes between turns (helpful for keeping reset countdowns and git branch status up to date). To pass custom environment variables inline, prefix the command with `env VAR=... bash ...`.

Run unit tests:

```bash
python3 tools/claude-statusline/test_claude_statusline.py -v
```

## Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `CLAUDE_STATUSLINE_STATE_DIR` | `${CLAUDE_CONFIG_DIR:-$HOME/.claude}` | Base directory for default `quota-log.jsonl` writes. Supports leading `~`/`~/` expansion. |
| `CLAUDE_STATUSLINE_PATH_ALIASES` | *(empty)* | Colon-separated `prefix=alias` rules (e.g. `~/work/my-repo=~/MR:~/projects/app=~/APP`). Left-hand prefixes expand leading `~`/`~/` against `$HOME`, match longest prefix first, and strictly respect directory boundaries (`/foo/bar` never matches `/foo/bar2`). |
| `CLAUDE_STATUSLINE_QUOTA_LOG` | *(disabled)* | Opt-in quota change history log. Unset, empty, `0`, or `false` (case-insensitive) disables logging; `1` or `true` writes to `${CLAUDE_STATUSLINE_STATE_DIR}/quota-log.jsonl`; any other non-empty value is treated as a custom file path (leading `~/` expanded, parent directories created automatically). Consecutive identical snapshots are deduplicated. |

## Local Log File

- `quota-log.jsonl` (under `${CLAUDE_STATUSLINE_STATE_DIR}`, default `${CLAUDE_CONFIG_DIR:-$HOME/.claude}`): Created **only** when `CLAUDE_STATUSLINE_QUOTA_LOG` is explicitly enabled. Appends one JSONL line (`ts`, `7d`, `7d_reset`, `5h`, `5h_reset`) whenever quota values or reset timestamps change. Any file I/O error is caught silently so the status line always renders cleanly.
