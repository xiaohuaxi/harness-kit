[English](README.md) | **简体中文**

# harness-kit

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

面向 **Claude Code**、**Codex** 与 **Antigravity（`agy`）** 的独立 CLI 工具、跨端 Skill、安全钩子、Agent 可执行操作指南与可复用组件套件。

> 本仓库只收录独立自包含、零第三方依赖（仅 Python 标准库 / 系统原生 Bash）的实用模块，可直接按需接入你现有的 Coding Agent 环境，无需照搬整套个人配置文件。

---

## 收录内容

| 分类 | 模块 | 功能简介 | 入口文档 |
| --- | --- | --- | --- |
| **Tools** | `agy-quota-wakeup` | Antigravity 周额度（`gemini-weekly` 与 `3p-weekly`）零消耗定时探测与最小 Chat-only 自动唤醒守护工具（支持 macOS `launchd` 与 Linux/WSL2 `systemd --user`）。 | [`tools/agy-quota-wakeup/README.zh-CN.md`](tools/agy-quota-wakeup/README.zh-CN.md) |
| **Tools** | `agy-statusline` | Antigravity CLI（`agy`）的柔和 256 色 7 段 `statusLine` 状态栏脚本，支持 transcript `CHECKPOINT` 兜底统计与配额缓存原子持久化。 | [`tools/agy-statusline/README.zh-CN.md`](tools/agy-statusline/README.zh-CN.md) |
| **Tools** | `claude-statusline` | Claude Code 的柔和 256 色 7 段 `statusLine` 状态栏脚本，兼容订阅配额与 API Key / 第三方后端优雅降级，并支持可选配额变更日志。 | [`tools/claude-statusline/README.zh-CN.md`](tools/claude-statusline/README.zh-CN.md) |
| **Tools** | `codex-quota-wakeup` | Codex CLI（ChatGPT 订阅）周额度零消耗定时探测与隔离只读最小唤醒守护工具（支持 macOS `launchd` 与 Linux/WSL2 `systemd --user`）。 | [`tools/codex-quota-wakeup/README.zh-CN.md`](tools/codex-quota-wakeup/README.zh-CN.md) |
| **Hooks** | `codex-depth-guard` | Codex CLI 多智能体（`multi_agent_v2`）的严格 fail-closed `PreToolUse` 深度守卫钩子，基于 transcript 权威深度拦截超限 `spawn_agent` 递归委派。 | [`hooks/codex-depth-guard/README.zh-CN.md`](hooks/codex-depth-guard/README.zh-CN.md) |
| **Skills** | `agy-run` | 通过 `agy acp` 执行有界 Antigravity（Gemini）任务的跨端 Skill 与纯标准库 runner，支持 `--resume`、运行中 `--steer`、三档沙箱核验与 `--list-models`。 | [`skills/agy-run/README.zh-CN.md`](skills/agy-run/README.zh-CN.md) |
| **Skills** | `codex-run` | 通过 `codex app-server` 执行有界 Codex 任务的跨端 Skill 与纯标准库 runner，支持 `--resume`、运行中 `--steer`、防早退子 Agent 排空、外置 profile 与 `--list-models`。 | [`skills/codex-run/README.zh-CN.md`](skills/codex-run/README.zh-CN.md) |
| **Guides** | `google-ai-perks` | 给 AI Agent 直接读取执行的 Google AI Pro / Ultra 订阅福利全量核查、领取、配置与验收指南（Cloud 赠金、Colab GPU 算力、Flow、云盘、YouTube 等）。 | [`guides/google-ai-perks/README.zh-CN.md`](guides/google-ai-perks/README.zh-CN.md) |

---

## 快速开始

先将仓库克隆到本地任意目录：

```bash
git clone https://github.com/xiaohuaxi/harness-kit.git
cd harness-kit
```

### 1. 安装周额度自动唤醒工具（`tools/agy-quota-wakeup`、`tools/codex-quota-wakeup`）

Antigravity 与 Codex 的周额度窗口在到期或官方重置后，需要发出新一轮首条请求才开始 7 天倒计时。这两个单文件工具平时零 Token 探测，仅在检测到未激活的漂移窗口时发起一次隔离最小唤醒请求：

```bash
mkdir -p ~/.local/bin

# Antigravity（要求 Python 3.8+ 与已登录的 agy >= 1.5.8）
cp tools/agy-quota-wakeup/agy-quota-wakeup ~/.local/bin/
chmod +x ~/.local/bin/agy-quota-wakeup
~/.local/bin/agy-quota-wakeup install
~/.local/bin/agy-quota-wakeup status

# Codex CLI（要求 Python 3.8+ 与已登录的 codex >= 0.158.0）
cp tools/codex-quota-wakeup/codex-quota-wakeup ~/.local/bin/
chmod +x ~/.local/bin/codex-quota-wakeup
~/.local/bin/codex-quota-wakeup install
~/.local/bin/codex-quota-wakeup status
```

更多参数、代理自动继承、多机错峰 `--minute-offset` 以及 Linux/WSL2 `systemd --user` 说明见各自 README：
- [`tools/agy-quota-wakeup/README.zh-CN.md`](tools/agy-quota-wakeup/README.zh-CN.md)
- [`tools/codex-quota-wakeup/README.zh-CN.md`](tools/codex-quota-wakeup/README.zh-CN.md)

### 2. 安装 Antigravity 状态栏（`tools/agy-statusline`）

复制脚本（要求 Bash 3.2+ 与 Python 3.7+，`git` 可选），并在 `~/.gemini/antigravity-cli/settings.json` 中启用 `statusLine`：

```bash
mkdir -p ~/.gemini/antigravity-cli
cp tools/agy-statusline/agy-statusline ~/.gemini/antigravity-cli/agy-statusline
chmod +x ~/.gemini/antigravity-cli/agy-statusline
```

```json
{
  "statusLine": {
    "enabled": true,
    "type": "command",
    "command": "~/.gemini/antigravity-cli/agy-statusline"
  }
}
```

路径缩写别名（`AGY_STATUSLINE_PATH_ALIASES`）、可选配额变更日志（`AGY_STATUSLINE_QUOTA_LOG`）与本地状态文件说明见 [`tools/agy-statusline/README.zh-CN.md`](tools/agy-statusline/README.zh-CN.md)。

### 3. 安装 Claude Code 状态栏（`tools/claude-statusline`）

复制脚本（要求 Bash 3.2+ 与 Python 3.7+，`git` 可选）到 `${CLAUDE_CONFIG_DIR:-$HOME/.claude}`，并在 `~/.claude/settings.json`（或 `${CLAUDE_CONFIG_DIR}/settings.json`）中启用 `statusLine`：

```bash
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
mkdir -p "$CLAUDE_DIR"
cp tools/claude-statusline/claude-statusline "$CLAUDE_DIR/claude-statusline"
chmod +x "$CLAUDE_DIR/claude-statusline"
```

```json
{
  "statusLine": {
    "type": "command",
    "command": "bash \"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/claude-statusline\""
  }
}
```

路径缩写别名（`CLAUDE_STATUSLINE_PATH_ALIASES`）、可选配额变更日志（`CLAUDE_STATUSLINE_QUOTA_LOG`）与状态目录说明（`CLAUDE_STATUSLINE_STATE_DIR`）见 [`tools/claude-statusline/README.zh-CN.md`](tools/claude-statusline/README.zh-CN.md)。

### 4. 安装 Codex 多智能体深度守卫钩子（`hooks/codex-depth-guard`）

复制钩子脚本（要求 Python 3.8+ 与支持 `PreToolUse` hooks 的 Codex CLI），并在 `${CODEX_HOME:-$HOME/.codex}/config.toml`（默认 `~/.codex/config.toml`）中挂载：

```bash
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
mkdir -p "$CODEX_DIR/hooks"
cp hooks/codex-depth-guard/multi-agent-depth-guard.py "$CODEX_DIR/hooks/multi-agent-depth-guard.py"
chmod +x "$CODEX_DIR/hooks/multi-agent-depth-guard.py"
```

```toml
[features]
hooks = true
enable_fanout = false

[[hooks.PreToolUse]]
matcher = "spawn_agent$"

[[hooks.PreToolUse.hooks]]
type = "command"
command = '"${CODEX_HOME:-$HOME/.codex}/hooks/multi-agent-depth-guard.py"'
timeout = 10
```

深度层级规则（`CODEX_MAX_CALLER_DEPTH` 默认 `1`，即 3 层封顶）与 fail-closed 拦截边界见 [`hooks/codex-depth-guard/README.zh-CN.md`](hooks/codex-depth-guard/README.zh-CN.md)。

### 5. 安装跨端执行 Skill（`skills/agy-run`、`skills/codex-run`）

两个 Skill 均要求 POSIX 平台（macOS、Linux 或 WSL2）、Python 3.8+ 及对应的本地 CLI（`agy` / `codex`）。将各自目录复制或软链到目标宿主的 skill 目录：

| 宿主 | Skill 目录 |
| --- | --- |
| Claude Code | `~/.claude/skills/<skill>` |
| Codex | `~/.agents/skills/<skill>` |
| Antigravity（`agy`） | `~/.gemini/config/skills/<skill>` |

```bash
# 示例：安装到 Claude Code 的 skill 目录（Codex 或 agy 请替换为对应目录）
mkdir -p ~/.claude/skills
cp -R skills/agy-run ~/.claude/skills/agy-run
cp -R skills/codex-run ~/.claude/skills/codex-run

# agy-run（首次运行前必做：在 ~/.gemini/config/agents 初始化 6 份托管 agent 定义）
python3 ~/.claude/skills/agy-run/scripts/agy_run.py --install-agents
python3 ~/.claude/skills/agy-run/scripts/agy_run.py --list-models

# codex-run（查看本地 models_cache.json 可用模型，外置配置可加 --profile <name>）
python3 ~/.claude/skills/codex-run/scripts/codex_run.py --list-models
```

完整参数（`--write`、`--yolo`、`--resume`、`--steer`、`--profile`）、沙箱实证范围与架构说明见各自 README：
- [`skills/agy-run/README.zh-CN.md`](skills/agy-run/README.zh-CN.md)
- [`skills/codex-run/README.zh-CN.md`](skills/codex-run/README.zh-CN.md)

### 6. 使用 Agent 操作指南（`guides/google-ai-perks`）

直接把 [`guides/google-ai-perks/google-ai-perks-agent-guide.zh-CN.md`](guides/google-ai-perks/google-ai-perks-agent-guide.zh-CN.md) 交给你的 AI Agent（Antigravity / Claude Code / Codex），让它按指南帮你自动核查、领取并验收 Google AI 订阅福利。详情见 [`guides/google-ai-perks/README.zh-CN.md`](guides/google-ai-perks/README.zh-CN.md)。

---

## 运行测试

所有工具、钩子与 Skill 均自带基于 Python `unittest` 的独立测试：

```bash
python3 tools/agy-quota-wakeup/test_agy_quota_wakeup.py -v
python3 tools/agy-statusline/test_agy_statusline.py -v
python3 tools/claude-statusline/test_claude_statusline.py -v
python3 tools/codex-quota-wakeup/test_codex_quota_wakeup.py -v
python3 hooks/codex-depth-guard/test_codex_depth_guard.py -v
python3 skills/agy-run/tests/test_agy_run.py -v
python3 skills/codex-run/tests/test_codex_run.py -v
```

---

## 开源协议

本项目基于 [Apache License 2.0](LICENSE) 开源。
