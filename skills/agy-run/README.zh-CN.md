[English](README.md) | **简体中文**

# agy-run

通过本地 **Antigravity CLI（`agy`）** 的 `agy acp` 协议执行、续跑与中途纠偏有界 Gemini agent 任务。纯 Python 3.8+ 标准库实现，零第三方依赖。

## 核心能力

- **前台阻塞收敛**：以独立进程组驱动 `agy acp`，会话建立即发布 `AGY_SESSION_ID=<uuid>`，自动等待超 10 秒转入内部后台的 `run_command` 唤醒轮完全收敛，退出时在 `stderr` 末行输出结构化 `AGY_RUN_META={...}`。
- **会话续跑与实时纠偏**：支持 `--resume <uuid-or-file>` 接续已完成会话，支持通过本地 Unix domain socket 执行 `--steer <uuid-or-file>` 向运行中的会话即时注入新指令，无需杀进程重跑。
- **三档沙箱与纵深校验**：
  - `read-only`（默认）：开放读文件、内置联网搜索与 `--sandbox` 命令，配合跑前/跑后工作区零改动快照与 `BypassSandbox` 核验。
  - `--write`（`workspace-write`）：开放工作区内编辑与 `--sandbox` 命令，配合跑后工作区外写入检查与 `BypassSandbox` 核验。
  - `--yolo`（`danger-full-access`）：解除终端沙箱并开放 `generate_image` 生图，仍阻拦递归调用 `agy_run.py`、`claude` 与 `codex`。
- **无头安全护栏**：即时拒绝交互权限弹窗，检测到 `IsDaemon: true` 守护命令即中止判败，禁用内置通用 subagent 与 MCP 工具，并剥离 `GEMINI_API_KEY` / `AGY_LLM_GATEWAY_*` 保持订阅登录。

## 前提要求与平台支持

- **操作系统**：仅支持 POSIX 平台（**macOS**、**Linux**、**WSL2**）。不支持原生 Windows（依赖 `fcntl.flock`、Unix domain socket 与 POSIX 进程组）。
- **运行环境**：Python 3.8+（仅标准库）以及已登录的 `agy` CLI。
  - 可执行文件查找顺序：`AGY_BIN` → `PATH` → `~/.local/bin/agy` → `/usr/local/bin/agy` → `/opt/homebrew/bin/agy`。
  - 代理桥接：若未设置 `HTTP_PROXY` / `HTTPS_PROXY` 而设置了 `ALL_PROXY` / `all_proxy`，脚本会自动桥接到 `HTTP_PROXY` 与 `HTTPS_PROXY`（解决 WSL2 下 Go `net/http` 忽略 `ALL_PROXY` 的问题）。
- **沙箱实证范围说明**：
  - POSIX runner 本体（ACP 状态机、`--list-models`、`--install-agents`、`--resume`、`--steer`、后台命令排空、工作区快照、越界写检测、`BypassSandbox` 核验及防递归护栏）在 macOS、Linux 与 WSL2 均可运行。
  - `--sandbox` 终端系统级隔离（`read-only` 与 `--write`）目前已在 **macOS Seatbelt** 上完成实测验证；在 Linux 与 WSL2 上，系统级终端隔离能力取决于宿主 `agy` 版本自身的 `--sandbox` 实现，但 `agy-run` 自带的快照、越界写、`BypassSandbox` 与防递归校验在所有 POSIX 平台均生效。

## 安装方式

将 `agy-run` skill 目录复制或软链到目标宿主的 skill 目录：

| 宿主 | Skill 目录 |
| --- | --- |
| Claude Code | `~/.claude/skills/agy-run` |
| Codex | `~/.agents/skills/agy-run` |
| Antigravity（`agy`） | `~/.gemini/config/skills/agy-run` |

### 必做：首次初始化托管 Agent 定义

`agy-run` 依赖位于 `~/.gemini/config/agents` 的 6 份托管自定义 agent 定义（`agy-{readonly,write,yolo}[-rules].md`）。首次运行前请执行一次初始化：

```bash
python3 <skill-dir>/scripts/agy_run.py --install-agents
```

- **幂等零写入**：6 份文件均已匹配时直接通过，不改写磁盘文件。
- **冲突与软链保护**：遇同名内容冲突（或 `~/.gemini/config/agents` 为软链且存在缺失/漂移文件）时绝不做半安装或写穿软链，直接报错并打印显式刷新命令（`--write-agent-files <目标目录>`）。
- **运行前单文件预检**：每次新会话或 `--resume` 在拉起 `agy acp` 之前会先校验本轮选中的那 1 份托管定义是否存在且与当前版本一致，缺失或版本过旧时立即报错并给出对应命令。

## 使用示例

查看可用模型列表（仅输出干净 `stdout`，自动滤除 `stderr` 噪音），复制第一列传给 `-m`：

```bash
python3 <skill-dir>/scripts/agy_run.py --list-models
```

启动只读排查会话：

```bash
python3 <skill-dir>/scripts/agy_run.py \
  -m <从第一列复制的模型名> -e medium \
  -C /path/to/repo \
  --session-id-file /tmp/agy-demo.sid \
  -o /tmp/agy-demo.out \
  "梳理认证流程并指出潜在边界问题。"
```

工作区改盘（`--write`）或独立音视频/PDF 解析与生图（加 `--no-rules` 省 token；生图加 `--yolo`）：

```bash
python3 <skill-dir>/scripts/agy_run.py -m <model> --write -C /path/to/repo "修复解析器 bug 并跑通单测。"
python3 <skill-dir>/scripts/agy_run.py -m <model> --no-rules -C /path/to/media "解析并总结 clip.mp4 内容。"
python3 <skill-dir>/scripts/agy_run.py -m <model> --no-rules --yolo -C /path/to/out "生成极简图标并保存到 icon.png。"
```

续跑已完成会话或向活跃会话实时纠偏：

```bash
python3 <skill-dir>/scripts/agy_run.py --resume /tmp/agy-demo.sid "继续排查 token 刷新链路。"
python3 <skill-dir>/scripts/agy_run.py --steer /tmp/agy-demo.sid "只聚焦服务端中间件逻辑。"
```

## 条件特性说明

- **Claude 项目记忆只读挂载**：仅在未传 `--no-rules` 且目标目录存在 `<cwd>/.agents/memory/MEMORY.md` 或 `~/.claude/projects/<编码路径>/memory/MEMORY.md` 时生效，脚本将其以只读方式暴露给被调 agent（可写模式下对仓外 memory 做临时拷贝并在跑后做 SHA-256 防改写校验）。若对应目录不存在则自动跳过，不依赖 Claude Code。

## 测试校验

运行离线单元与集成测试（内置 fake ACP 服务，不消耗 `agy` 配额）：

```bash
python3 skills/agy-run/tests/test_agy_run.py -v
```

可选的真机 `agy acp` 集成测试（消耗少量配额，需先完成 `--install-agents` 且本机 `agy` 已登录）：

```bash
AGY_LIVE=1 python3 skills/agy-run/tests/live_agy_check.py -v
```
