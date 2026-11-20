**English** | [简体中文](README.zh-CN.md)

# harness-kit

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Standalone CLI tools, agent-executable guides, and reusable building blocks for **Claude Code**, **Codex**, and **Antigravity (`agy`)**.

> Every module here is self-contained with zero third-party Python dependencies (standard library only), designed to drop directly into your existing coding agent setup without adopting a monolithic dotfiles repository.

---

## What's Included

| Category | Module | What It Does | Entry Point |
| --- | --- | --- | --- |
| **Tools** | `agy-quota-wakeup` | Zero-token polling & minimal chat-only weekly quota wakeup daemon (`launchd` / `systemd --user`) for Antigravity (`gemini-weekly` & `3p-weekly`). | [`tools/agy-quota-wakeup/README.md`](tools/agy-quota-wakeup/README.md) |
| **Tools** | `codex-quota-wakeup` | Zero-token polling & isolated minimal read-only weekly quota wakeup daemon (`launchd` / `systemd --user`) for Codex CLI ChatGPT subscriptions. | [`tools/codex-quota-wakeup/README.md`](tools/codex-quota-wakeup/README.md) |
| **Guides** | `google-ai-perks` | Agent-executable guide to inspect, claim, configure, and verify Google AI Pro / Ultra subscription perks (Cloud credits, Colab GPU CU, Flow, Drive, YouTube, etc.). | [`guides/google-ai-perks/README.md`](guides/google-ai-perks/README.md) |

---

## Quick Start

Clone the repository anywhere locally:

```bash
git clone https://github.com/xiaohuaxi/harness-kit.git
cd harness-kit
```

### 1. Quota Wakeup Tools (`tools/agy-quota-wakeup`, `tools/codex-quota-wakeup`)

Weekly quota windows for Antigravity and Codex only start their 7-day countdown after the first request following a reset. These single-file Python scripts poll status with zero tokens and send a minimal, isolated wakeup request only when an unactivated window is detected.

```bash
mkdir -p ~/.local/bin

# Antigravity (requires Python 3.8+ and logged-in agy >= 1.5.8)
cp tools/agy-quota-wakeup/agy-quota-wakeup ~/.local/bin/
chmod +x ~/.local/bin/agy-quota-wakeup
~/.local/bin/agy-quota-wakeup install
~/.local/bin/agy-quota-wakeup status

# Codex CLI (requires Python 3.8+ and logged-in codex >= 0.158.0)
cp tools/codex-quota-wakeup/codex-quota-wakeup ~/.local/bin/
chmod +x ~/.local/bin/codex-quota-wakeup
~/.local/bin/codex-quota-wakeup install
~/.local/bin/codex-quota-wakeup status
```

Full options, proxy inheritance, multi-machine `--minute-offset`, and Linux/WSL2 `systemd --user` notes are in each tool's README:
- [`tools/agy-quota-wakeup/README.md`](tools/agy-quota-wakeup/README.md)
- [`tools/codex-quota-wakeup/README.md`](tools/codex-quota-wakeup/README.md)

### 2. Agent Guides (`guides/google-ai-perks`)

Point your AI agent (Antigravity / Claude Code / Codex) to [`guides/google-ai-perks/google-ai-perks-agent-guide.md`](guides/google-ai-perks/google-ai-perks-agent-guide.md) and ask it to inspect, claim, configure, and verify your Google AI subscription perks. See [`guides/google-ai-perks/README.md`](guides/google-ai-perks/README.md) for details.

---

## Running Tests

Each tool includes standalone unit tests using Python's built-in `unittest`:

```bash
python3 tools/agy-quota-wakeup/test_agy_quota_wakeup.py -v
python3 tools/codex-quota-wakeup/test_codex_quota_wakeup.py -v
```

---

## License

Licensed under the [Apache License 2.0](LICENSE).
