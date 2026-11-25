**English** | [简体中文](README.zh-CN.md)

# agy-statusline

A soft 256-color, 7-segment `statusLine` command for **Antigravity CLI (`agy`)**. Single-file script using Bash (3.2+) and Python 3.7+ standard library only, with zero third-party dependencies, supporting macOS, Linux, and WSL2.

## Output Example

```text
~/work/demo · Sonnet 4.5 · high · 42k/200k 21% ⛁ 2 · G: 7d 12% ↻ 5.4d 5h 8% ↻ 19:30 · C: 7d 25% ↻ 3.2d · main · $0.42
```

Segments (omitted automatically when absent):

1. **cwd**: Current directory with optional longest-prefix alias rules (`AGY_STATUSLINE_PATH_ALIASES`) and `$HOME` → `~` shortening.
2. **model**: Active model name with vendor prefix (`Claude ` / `Gemini ` / `GPT `) and trailing effort tag stripped.
3. **effort**: Active reasoning effort (`low`, `medium`, `high`, `xhigh`, `max`).
4. **context usage**: Used/total tokens (`k`/`M`), percentage, and `⛁ N` compact count when checkpoints exist. When `AGY_CLI_AUTO_COMPACT_OVERRIDE` is valid and smaller than the model's `context_window_size`, uses that threshold as the effective denominator and recomputes the percentage (when equal, keeps the payload percentage).
5. **quota**: Independent Gemini (`G:`) weekly (`7d`) + 5-hour (`5h`) usage with reset countdown/clock, plus 3P/Claude (`C:`) weekly (`7d`) usage.
6. **git branch**: Current branch or short detached commit hash when inside a git work tree (requires optional `git`).
7. **cost**: Session total USD cost when provided in the status payload.

## Why It Exists

- **Transcript `CHECKPOINT` Fallback**: `agy`'s status payload may provide a `transcript_path` without the `-cli` suffix (pointing to `~/.gemini/antigravity/brain/...` instead of `~/.gemini/antigravity-cli/brain/...`). When the reported path does not exist, the script falls back to `${AGY_STATUSLINE_STATE_DIR}/brain/<conversation_id>/.system_generated/logs/transcript.jsonl` to keep the `⛁` compact counter accurate.
- **Quota Cache Persistence**: Some status ticks omit the `quota` payload. Whenever a non-empty `quota` object arrives, it is saved atomically to `quota-cache.json` (same-directory temp file + `os.replace`) and reused on subsequent quota-less ticks so the quota segment never flickers away.
- **Soft 256-Color Hierarchy**: Uses muted 256-color tones (gray labels, bold blue cwd, muted cyan model, sage green effort, deep amber percentages) for clear visual scanning without harsh ANSI brightness.
- **Zero External Dependencies**: Runs out of the box on stock macOS Bash 3.2, Linux/WSL2 Bash 5.x, and Python 3.7+ standard library (`git` is optional).

## Quick Install

Requirements: Bash 3.2+, Python 3.7+ (standard library only), and optional `git`. Works identically on macOS, Linux, and WSL2.

1. Copy the script to `~/.gemini/antigravity-cli/agy-statusline` and make it executable:

```bash
mkdir -p ~/.gemini/antigravity-cli
cp tools/agy-statusline/agy-statusline ~/.gemini/antigravity-cli/agy-statusline
chmod +x ~/.gemini/antigravity-cli/agy-statusline
```

2. Enable it in `~/.gemini/antigravity-cli/settings.json`:

```json
{
  "statusLine": {
    "enabled": true,
    "type": "command",
    "command": "~/.gemini/antigravity-cli/agy-statusline"
  }
}
```

> If your `agy` version does not expand `~` in `statusLine.command`, replace `~` with your expanded home directory path or wrap with `env` if you want to pass custom environment variables inline.

Run unit tests:

```bash
python3 tools/agy-statusline/test_agy_statusline.py -v
```

## Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `AGY_STATUSLINE_STATE_DIR` | `$HOME/.gemini/antigravity-cli` | Base directory for transcript fallback reads, `quota-cache.json`, and default `quota-log.jsonl`. Supports leading `~/` expansion. |
| `AGY_STATUSLINE_PATH_ALIASES` | *(empty)* | Colon-separated `prefix=alias` rules (e.g. `~/work/my-repo=~/MR:~/projects/app=~/APP`). Left-hand prefixes expand leading `~`/`~/` against `$HOME`, match longest prefix first, and strictly respect directory boundaries (`/foo/bar` never matches `/foo/bar2`). |
| `AGY_STATUSLINE_QUOTA_LOG` | *(disabled)* | Opt-in quota change history log. Unset, empty, `0`, or `false` (case-insensitive) disables logging; `1` or `true` writes to `${AGY_STATUSLINE_STATE_DIR}/quota-log.jsonl`; any other non-empty value is treated as a custom file path (leading `~/` expanded, parent directories created automatically). Consecutive identical snapshots are deduplicated. |
| `AGY_CLI_AUTO_COMPACT_OVERRIDE` | *(unset)* | Native `agy` auto-compaction threshold override (`350000`, `800K`, `0.5M`, or `80%`). Because `agy`'s statusLine payload currently reports only the full `context_window_size`, when this variable is valid and smaller than `context_window_size`, the status line uses it as the denominator and recalculates the context usage percentage against it; when equal, it keeps the payload percentage (invalid or out-of-range values fall back to `context_window_size`). |

## State & Cache Files

Stored under `${AGY_STATUSLINE_STATE_DIR}` (default `~/.gemini/antigravity-cli/`):

- `quota-cache.json`: Last seen non-empty `quota` payload, updated atomically via same-directory temporary file + `os.replace` so concurrent reads never see a truncated file.
- `quota-log.jsonl`: Created **only** when `AGY_STATUSLINE_QUOTA_LOG` is explicitly enabled; appends one JSONL line whenever parsed quota values or 5h reset timestamps change.
- `brain/<conversation_id>/.system_generated/logs/transcript.jsonl`: Read-only fallback source for counting `CHECKPOINT` entries when `transcript_path` in the payload does not exist.
