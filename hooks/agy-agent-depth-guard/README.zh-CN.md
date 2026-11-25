[English](README.md) | **简体中文**

# agy-agent-depth-guard

面向 **Antigravity CLI（`agy`）** 子代理委派（`invoke_subagent`）的严格 fail-closed `PreToolUse` 深度守卫钩子。单文件脚本，兼容 Python 3.8+，纯标准库实现，零第三方依赖。

> **支持范围**：已验证支持 **Antigravity CLI（`agy`）** 的 `brain/<conversationId>/.system_generated/` 会话目录布局。不静默兼容 Antigravity IDE 或 Antigravity 2.0 等其他布局；显式传入无法识别的非 CLI 路径时直接按 fail-closed 拒绝。

## 为什么需要它

- **防止无限递归委派**：通过 `invoke_subagent` 创建的子代理仍可继续创建下级子代理。若缺少硬性深度封顶，失控的递归委派会迅速耗尽并发能力与 Token 额度。
- **补齐 agy hook 异常默认 fail-open 的漏洞**：`agy` 在 hook 脚本崩溃或异常退出时默认放行调用。本脚本将所有校验失败与未捕获异常统一转为协议级拒绝（退出码 `0` 并在 `stdout` 输出 `{"decision": "deny", "reason": "..."}`）。

## 深度层级图示

`AGY_MAX_AGENT_DEPTH` 采用 **0 基**口径，设定整个委派树允许存在的最大 agent 深度（主会话为 `depth 0`，子代理为 `depth 1`，孙代理为 `depth 2`，依此类推）。只要即将创建的子代理深度 `caller_depth + 1 > AGY_MAX_AGENT_DEPTH` 即拦截。默认 `AGY_MAX_AGENT_DEPTH=2` 对应整棵树 **3 层封顶**（`depth 0` → `depth 1` → `depth 2`）：

```text
depth 0  (root 主会话)              ──▶ 允许 invoke_subagent（创建 depth 1 <= MAX_AGENT_DEPTH=2）
  └─ depth 1  (child 子代理)        ──▶ 允许 invoke_subagent（创建 depth 2 <= MAX_AGENT_DEPTH=2）
       └─ depth 2  (grandchild 孙代理) ──▶ 拒绝 invoke_subagent（禁止创建 depth 3 > 2）
```

一般地，`AGY_MAX_AGENT_DEPTH = N` 允许存在深度 `0 .. N`（共 `N + 1` 层）：
- `0` → 1 层（仅 `depth 0` 主会话，禁止任何子代理委派）
- `1` → 2 层（`depth 0` → `1`：`root` → `child`，子代理不可再派）
- `2`（默认）→ 3 层（`depth 0` → `1` → `2`：`root` → `child` → `grandchild`，孙代理不可再派）
- `3` → 4 层（`depth 0` → `1` → `2` → `3`：`root` → `child` → `grandchild` → `great-grandchild`，depth 3 不可再派）

## 工作原理

1. **配置与协议校验**：解析并校验 `AGY_MAX_AGENT_DEPTH`（未设置或空串回退为 `2`，否则必须为纯 ASCII 十进制非负整数）；解析 `stdin` JSON，校验 `hookEventName`（存在且非空时必须为 `"PreToolUse"`，缺失或为 `null` 时向后兼容旧版 `agy`）、`toolCall.name == "invoke_subagent"` 与合法的 `conversationId`。
2. **CLI Brain 根目录定位**：从 `artifactDirectoryPath` 与 `transcriptPath` 提取符合 CLI 布局的候选目录，优先选中包含 `<cid>/.system_generated/` 的候选（同时允许以 `<conversationId>` 结尾且父目录已存在的 early root 路径）。若显式提供了路径字段却无法识别为 CLI 布局，直接按 fail-closed 拒绝，不静默回退；仅当两个路径字段均未提供或为空时，兜底使用 `~/.gemini/antigravity-cli/brain`。
3. **父链溯源（主通道 + 兜底通道）**：
   - **主通道**：使用 `os.scandir` 遍历 `.system_generated/messages/*.json`（跳过 `read.json`，避免低版本 Python `Path.glob` 吞掉权限错误），查找 `recipient` 匹配当前会话且 `sourceMetadata.tool.toolCall.name == "invoke_subagent"` 的创建消息。
   - **兜底通道**：读取 `.system_generated/logs/transcript.jsonl` 首行，仅当 `step_index == 0`（排除布尔值）时生效：`source == "USER_EXPLICIT"` 确认为主会话；`source == "SYSTEM"` 且 `type == "SYSTEM_MESSAGE"` 时从 `[Message]` 头提取完整 `sender=<token>`，仅在整串严格为 UUID 时采信（避免把 `<UUID>/task-7` 等后台任务消息误判为父会话）。
   - **完整性校验**：沿父边最多向上回溯 `max_agent_depth` 步（当 `max_agent_depth == 0` 时无需回溯，直接拦截根会话委派）。已确认祖先的 `.system_generated` 目录缺失、不可读元数据（且无 `USER_EXPLICIT` 根会话证明）或父链成环（含自环）一律按 fail-closed 拒绝。
4. **严格 Fail-Closed 拦截**：`depth + 1 <= max_agent_depth` 返回 `{"decision": "allow"}`；超限或任意校验/运行时错误返回 `{"decision": "deny", "reason": "AGY AGENT DEPTH GUARD: BLOCKED invoke_subagent: ..."}` 并以退出码 `0` 正常结束。

## 快速安装

依赖：Python 3.8+（纯标准库）与支持 `PreToolUse` hooks 的 Antigravity CLI（`agy`）。

1. 复制脚本到 agy hooks 目录并赋予可执行权限：

```bash
mkdir -p ~/.gemini/config/hooks
cp hooks/agy-agent-depth-guard/agy-agent-depth-guard.py ~/.gemini/config/hooks/agy-agent-depth-guard.py
chmod +x ~/.gemini/config/hooks/agy-agent-depth-guard.py
```

2. 在 `~/.gemini/config/hooks.json` 中注册钩子（省略 `timeout` 以沿用 agy 默认 30 秒超时）：

```json
{
  "agy-agent-depth-guard": {
    "PreToolUse": [
      {
        "matcher": "invoke_subagent",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.gemini/config/hooks/agy-agent-depth-guard.py"
          }
        ]
      }
    ]
  }
}
```

> 如需在配置里单独调整最大深度（无需设置全局 shell 环境变量），可在 `command` 前缀传入：
> `"command": "AGY_MAX_AGENT_DEPTH=3 python3 ~/.gemini/config/hooks/agy-agent-depth-guard.py"`

## 环境变量配置

| 变量名 | 默认值 | 说明 |
| --- | --- | --- |
| `AGY_MAX_AGENT_DEPTH` | `2` | 0 基最大 agent 深度（允许存在 `depth 0..N`，共 `N + 1` 层）。未设置或为空字符串时回退到 `2`（3 层封顶：`depth 0..2`）。设为 `0` 时仅允许主会话，禁止所有子代理委派。仅接受纯 ASCII 十进制非负整数（如 `0`、`1`、`2`）；任何非法值（如 `-1`、`abc`、`1.5` 或带空白字符）都会触发 fail-closed 拒绝。 |

## Fail-Closed 边界说明

以下任一情况均会直接拦截 `invoke_subagent`（退出码 `0` 并输出 `{"decision": "deny", "reason": ...}`）：

- `AGY_MAX_AGENT_DEPTH` 配置无效（负数、非整数、浮点数、含空白、非 ASCII 数字）
- `stdin` 为空、非法 JSON 或顶层不是 JSON 对象
- `hookEventName` 存在且非空（非 `null`）但不是 `"PreToolUse"`，或 `toolCall.name` 不是 `"invoke_subagent"`
- `conversationId` 缺失、为空、非字符串或包含路径穿越字符
- 显式提供了 `artifactDirectoryPath` 或 `transcriptPath`，但无法匹配 CLI 的 `brain/<conversationId>/` 目录布局（允许以 `<conversationId>` 结尾且父目录已存在的 early root 路径）
- 沿已确认父边回溯时，祖先会话的 `.system_generated` 目录缺失（断链）
- `messages/` 不可读或元数据损坏，且无法通过 `step_index == 0` 的 `USER_EXPLICIT` 证明为主会话
- 父链成环（自环或任意多跳环）
- 任何未预期的运行时异常，均在顶层兜底转为 JSON 拒绝响应

> **正常主会话说明**：agy 主会话没有正向创建消息，且极早期调用时可能尚未落盘元数据；因此当前会话尚未生成 `.system_generated`、无 `invoke_subagent` 创建消息或首行明确为 `step_index == 0` 的 `USER_EXPLICIT` 时，均判定为 `depth = 0`（当 `AGY_MAX_AGENT_DEPTH >= 1` 时放行，为 `0` 时拦截）。`messages/` 中存在单个并发半写文件，但另有合法 `invoke_subagent` 消息或 `USER_EXPLICIT` 根会话记录时，也不会误杀。

## 运行测试

```bash
python3 hooks/agy-agent-depth-guard/test_agy_agent_depth_guard.py -v
```
