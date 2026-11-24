[English](README.md) | **简体中文**

# codex-depth-guard

面向 **Codex CLI** 多智能体（`multi_agent_v2`）的严格 fail-closed `PreToolUse` 深度守卫钩子。单文件脚本，兼容 Python 3.8+，纯标准库实现，零第三方依赖。

## 为什么需要它

- **防止无限递归委派**：启用 `multi_agent_v2` 后，子 agent 也能继续调用 `spawn_agent`。若没有硬性层级封顶，失控的递归委派会迅速耗尽并发线程槽位与 Token 额度。
- **补齐 Codex hook 异常默认 fail-open 的漏洞**：Codex 在 hook 脚本崩溃或非预期退出时默认放行调用。本脚本将所有校验失败与未捕获异常统一转为协议级拒绝（通过 `os._exit(2)` 返回退出码 `2`，并在 `stderr` 打印清晰拒绝原因）。

## 深度层级图示

`CODEX_MAX_CALLER_DEPTH` 限制的是**调用方（caller）**允许发起 `spawn_agent` 的最大深度。由于 `depth = N` 的调用方创建的是 `depth = N + 1` 的子 agent，因此默认 `CODEX_MAX_CALLER_DEPTH=1` 对应整棵树 **3 层封顶**（`depth 0` → `depth 1` → `depth 2`）：

```text
depth 0  (root 主会话)       ──▶ 允许 spawn_agent（创建 depth 1）
  └─ depth 1  (child 子代理) ──▶ 允许 spawn_agent（depth 1 <= MAX_CALLER_DEPTH=1；创建 depth 2）
       └─ depth 2  (grandchild 孙代理) ──▶ 拒绝 spawn_agent（depth 2 > 1；禁止创建 depth 3）
```

一般地，`CODEX_MAX_CALLER_DEPTH = N` 允许存在深度 `0 .. N + 1`（共 `N + 2` 层）：
- `0` → 2 层封顶（仅 `root` 可派 `child`，子代理不可再派）
- `1`（默认）→ 3 层封顶（`root` → `child` → `grandchild`，孙代理不可再派）
- `2` → 4 层封顶（`depth 0` → `1` → `2` → `3`，depth 3 不可再派）

## 工作原理

1. **配置与协议校验**：读取并校验 `CODEX_MAX_CALLER_DEPTH`（未设置或空串回退为 `1`，否则必须为纯十进制非负整数）；解析 `stdin` JSON，校验 `hook_event_name == "PreToolUse"` 且 `tool_name` 以 `"spawn_agent"` 结尾（同时兼容 `spawn_agent` 与 `multi_agent_v2.spawn_agent` 等带前缀名称）。
2. **从 transcript 读取权威深度**：打开 `transcript_path` 读取首行 JSONL，校验其为 `session_meta`：
   - **主会话（`source` 为非空字符串，如 `"cli"`、`"exec"`、`"vscode"`）**：判定 `depth = 0`，同时反向核对 hook 输入中 `agent_id` 与 `agent_type` 均为空，防止元数据与 hook 字段冲突。
   - **子会话（存在 `source.subagent`）**：读取 `source.subagent.thread_spawn.depth`，校验其为正整数（拒绝布尔值、0、负数、浮点与字符串），且 hook 输入中必须提供 `agent_id`。
   - **`source` 缺失或无法识别**：直接按 fail-closed 拒绝，不默认视作主会话放行。
3. **严格 Fail-Closed 拦截**：若 `depth > max_caller_depth`，或任意解析/文件读取/字段校验出现异常，脚本向 `stderr` 输出 `CODEX MULTI-AGENT DEPTH GUARD: BLOCKED spawn_agent: ...` 并立即执行 `os._exit(2)` 阻断调用。

## 快速安装

依赖：Python 3.8+（纯标准库）与支持 `PreToolUse` hooks 的 Codex CLI。

1. 复制脚本到 Codex hooks 目录（`${CODEX_HOME:-$HOME/.codex}/hooks`）并赋予可执行权限：

```bash
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
mkdir -p "$CODEX_DIR/hooks"
cp hooks/codex-depth-guard/multi-agent-depth-guard.py "$CODEX_DIR/hooks/multi-agent-depth-guard.py"
chmod +x "$CODEX_DIR/hooks/multi-agent-depth-guard.py"
```

2. 在 `${CODEX_HOME:-$HOME/.codex}/config.toml`（默认 `~/.codex/config.toml`）中启用钩子并挂载守卫：

```toml
[features]
hooks = true
# 关闭 CSV 扇出，防止它在单 agent spawn_agent 钩子路径之外创建子 worker。
enable_fanout = false

[[hooks.PreToolUse]]
# 同时匹配普通 "spawn_agent" 与 v2 扁平名称（如 "multi_agent_v2.spawn_agent"）。
matcher = "spawn_agent$"

[[hooks.PreToolUse.hooks]]
type = "command"
command = '"${CODEX_HOME:-$HOME/.codex}/hooks/multi-agent-depth-guard.py"'
timeout = 10
```

> - 建议同时设置 `enable_fanout = false`，避免 CSV 扇出绕开单 agent 的 `spawn_agent` 钩子检查。
> - 如需在配置里单独调整最大深度（无需设置全局 shell 环境变量），可在 `command` 前缀传入：
>   `command = 'CODEX_MAX_CALLER_DEPTH=2 "${CODEX_HOME:-$HOME/.codex}/hooks/multi-agent-depth-guard.py"'`

## 环境变量配置

| 变量名 | 默认值 | 说明 |
| --- | --- | --- |
| `CODEX_MAX_CALLER_DEPTH` | `1` | 允许调用 `spawn_agent` 的最大调用方深度。未设置或为空字符串时回退到 `1`（3 层封顶）。仅接受纯 ASCII 十进制非负整数（如 `0`、`1`、`2`）；任何非法值（如 `-1`、`abc`、`1.5` 或带空白字符）都会触发 fail-closed 拒绝（退出码 `2`）。 |

## Fail-Closed 边界说明

以下任一情况均会直接拦截 `spawn_agent`（退出码 `2` 并在 `stderr` 给出原因）：

- `CODEX_MAX_CALLER_DEPTH` 配置无效（负数、非整数、浮点数、含空白、非 ASCII 数字）
- `stdin` 为空、非法 JSON 或顶层不是 JSON 对象
- `hook_event_name` 不是 `"PreToolUse"`，或 `tool_name` 不以 `"spawn_agent"` 结尾
- `transcript_path` 缺失、为空、非字符串、指向不存在或不可读的文件
- transcript 为空、首行非合法 JSON，或首行 `type` 不是 `"session_meta"`
- `session_meta` 的 `source` 缺失、`null`、为空或为无法识别的类型（既非非空字符串来源，也非包含 `subagent` 的对象）
- transcript 显示为主会话（root），但 hook 输入中带有非空的 `agent_id` 或 `agent_type`
- transcript 显示为 subagent，但缺少 `thread_spawn`、`depth` 非正整数（含布尔值）或 hook 输入缺少 `agent_id`
- 任何未预期的运行时异常（`BaseException`），均在顶层兜底转为 `os._exit(2)`

## 运行测试

```bash
python3 hooks/codex-depth-guard/test_codex_depth_guard.py -v
```
