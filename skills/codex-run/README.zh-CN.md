[English](README.md) | **简体中文**

# codex-run

跨 Agent 的 Codex CLI 执行器与 Skill，通过本地 `codex app-server --stdio` 驱动有界 Codex 任务，支持线程续跑（`--resume`）、运行中实时纠偏（`--steer`）、防早退子 Agent 排空、外置 Codex profile 以及 `--list-models` 模型查询。

## 核心特性

- **单进程前台收敛**：脚本在前台阻塞至根线程及所有未显式 `wait_agent` 的内部子/孙 Agent 完成，供 Claude Code、Antigravity（`agy`）、Codex 等宿主直接按进程退出事件唤醒，无需轮询也无常驻孤儿进程。
- **防早退排空与自动续跑**：在共享 `app-server` 事件流上追踪内部 `spawn_agent` 生命周期；若根线程在子任务未完时提前结束，脚本等待全部子任务排空后自动续跑汇总（最多 3 次），并在 `stdout`、`stderr` 与 `-o` 末尾附带显式提示。
- **实时纠偏与并发互斥**：为活跃线程建立本地 Unix Socket 支持 `--steer`（`turn/steer` RPC），并通过独占 `flock` 防止并发 `--resume` 破坏同一线程。
- **模型查询（`--list-models`）**：纯标准库读取 `$CODEX_HOME/models_cache.json` 或外置 profile 的 `$CODEX_HOME/<name>.config.toml` 与 `model_catalog_json`，兼容 Python 3.8+。
- **隔离与脱敏**：独立进程组、空 `ZDOTDIR`、禁用 login shell、敏感环境变量过滤、`CODEX_RUN_ACTIVE=1` 防递归守卫以及输出密钥脱敏。

## 环境要求

- **操作系统**：POSIX 系统（macOS、Linux 或 WSL2）。不支持原生 Windows（依赖 `AF_UNIX`、`fcntl.flock` 与 POSIX 进程组信号）。
- **Python**：Python 3.8+（仅依赖标准库，零第三方包）。
- **Codex CLI**：本地 `PATH` 中可用的 `codex` 可执行文件（或通过 `CODEX_BIN` 指定），并已完成登录或配置好 `$CODEX_HOME` 下的外置 profile。

## 安装方式

将 `codex-run` 目录复制或软链到对应宿主的 skills 目录：

- **Claude Code**：`~/.claude/skills/codex-run`
- **Codex**：`~/.agents/skills/codex-run`
- **Antigravity（`agy`）**：`~/.gemini/config/skills/codex-run`

示例：

```bash
mkdir -p ~/.claude/skills
cp -R skills/codex-run ~/.claude/skills/codex-run
```

## 快速示例

```bash
# 查看本地 models_cache.json 中的可用模型与 effort
python3 skills/codex-run/scripts/codex_run.py --list-models

# 查看外置 profile（$CODEX_HOME/deepseek.config.toml）配置的可用模型
python3 skills/codex-run/scripts/codex_run.py --list-models --profile deepseek

# 启动有界任务（默认只读；改盘加 --write；需写工作目录外或解除沙箱加 --yolo）
python3 skills/codex-run/scripts/codex_run.py \
  -m gpt-5.4 -e medium \
  -C /path/to/repo --write \
  --session-id-file /tmp/codex-demo.sid \
  -o /tmp/codex-demo.out \
  "审查鉴权模块并修复失败的单元测试"

# 使用外置 profile 执行任务
python3 skills/codex-run/scripts/codex_run.py \
  --profile deepseek -C /path/to/repo "梳理模块架构与关键依赖"

# 续跑已结束的线程
python3 skills/codex-run/scripts/codex_run.py \
  --resume /tmp/codex-demo.sid "按方案 B 推进并运行测试套件"

# 向运行中的 turn 实时纠偏
python3 skills/codex-run/scripts/codex_run.py \
  --steer /tmp/codex-demo.sid "跳过迁移脚本，只聚焦单元测试"
```

## 条件特性说明

仅当显式传入 `--profile deepseek` 时，`codex_run.py` 会在提示词中注入 `DEEPSEEK_GUARD`，在提示词层面禁止调用内置 `spawn_agent` 工具，以规避当前 Codex V2 外置 provider 子消息丢明文及全历史 fork 递归重放问题。普通 Codex 任务与其他外置 profile 不会注入该守卫。

## 运行测试

```bash
python3 skills/codex-run/tests/test_codex_run.py -v
```

架构设计、JSON-RPC 状态机与互斥锁不变量详见 [DESIGN.md](DESIGN.md)。
