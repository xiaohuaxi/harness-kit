[English](README.md) | **简体中文**

# harness-kit

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

面向 **Claude Code**、**Codex** 与 **Antigravity（`agy`）** 的独立 CLI 工具、Agent 可执行操作指南与可复用组件套件。

> 本仓库只收录独立自包含、零第三方依赖（仅 Python 标准库）的实用模块，可直接按需接入你现有的 Coding Agent 环境，无需照搬整套个人配置文件。

---

## 收录内容

| 分类 | 模块 | 功能简介 | 入口文档 |
| --- | --- | --- | --- |
| **Tools** | `agy-quota-wakeup` | Antigravity 周额度（`gemini-weekly` 与 `3p-weekly`）零消耗定时探测与最小 Chat-only 自动唤醒守护工具（支持 macOS `launchd` 与 Linux/WSL2 `systemd --user`）。 | [`tools/agy-quota-wakeup/README.zh-CN.md`](tools/agy-quota-wakeup/README.zh-CN.md) |
| **Tools** | `codex-quota-wakeup` | Codex CLI（ChatGPT 订阅）周额度零消耗定时探测与隔离只读最小唤醒守护工具（支持 macOS `launchd` 与 Linux/WSL2 `systemd --user`）。 | [`tools/codex-quota-wakeup/README.zh-CN.md`](tools/codex-quota-wakeup/README.zh-CN.md) |
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

### 2. 使用 Agent 操作指南（`guides/google-ai-perks`）

直接把 [`guides/google-ai-perks/google-ai-perks-agent-guide.md`](guides/google-ai-perks/google-ai-perks-agent-guide.md) 交给你的 AI Agent（Antigravity / Claude Code / Codex），让它按指南帮你自动核查、领取并验收 Google AI 订阅福利。详情见 [`guides/google-ai-perks/README.zh-CN.md`](guides/google-ai-perks/README.zh-CN.md)。

---

## 运行测试

所有工具均自带基于 Python `unittest` 的独立测试：

```bash
python3 tools/agy-quota-wakeup/test_agy_quota_wakeup.py -v
python3 tools/codex-quota-wakeup/test_codex_quota_wakeup.py -v
```

---

## 开源协议

本项目基于 [Apache License 2.0](LICENSE) 开源。
