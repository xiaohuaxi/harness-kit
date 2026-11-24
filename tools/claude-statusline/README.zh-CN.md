[English](README.md) | **简体中文**

# claude-statusline

**Claude Code** 的柔和 256 色 7 段状态栏命令脚本。单文件实现，仅依赖 Bash（3.2+）与 Python 3.7+ 标准库，零第三方依赖，支持 macOS、Linux 与 WSL2。

## 状态栏输出示例

```text
~/work/demo · Opus 4.6 (1M context) · high · 42k/200k 21% · 7d 12% ↻ 5.4d · 5h 8% ↻ 19:30 · main · $0.42
```

各段含义（缺少数据时自动隐藏对应分段）：

1. **cwd**：当前工作目录（优先取 `workspace.current_dir`，依次回退至 `cwd` 与 `$PWD`），支持按最长前缀缩写自定义别名（`CLAUDE_STATUSLINE_PATH_ALIASES`），未命中时将 `$HOME` 缩写为 `~`。
2. **model**：当前模型名（优先取 `model.display_name`，回退至 `model.id`），仅剥离尾部思考强度括号（如 `(High)`），保留 `(1M context)` 等有效信息。
3. **effort**：当前思考强度（`effort.level`），缺失时自动隐藏。
4. **context 用量**：上下文已用/总量（`k`/`M`）与百分比；`current_usage` 缺失或为 0 时回退读取 `total_input_tokens`；缺少 token 明细但存在有效 `used_percentage` 时单独展示百分比（如 `24%`）。
5. **quota 配额（`7d` / `5h`）**：独立展示 7 天用量与剩余重置天数（`↻ X.Xd`）、5 小时用量与本地重置时刻（当日 `↻ HH:MM`，跨日 `↻ MM-DD HH:MM`）。
6. **git 分支**：位于 git 工作树时展示当前分支名或 detached HEAD 短哈希（`git` 为可选依赖）。
7. **cost**：当输入 JSON 提供费用字段时展示会话美元花费（`$X.XX`）。

## 核心特性

- **柔和 256 色层次配色**：使用低饱和 256 色（灰阶标签、粗蓝路径、雾青模型、雾绿 effort、深琥珀百分比），兼顾清晰辨识与耐看度。
- **稳健的 Claude Code Schema 兼容与安全防护**：对顶层与所有嵌套字段做严格类型守卫，避免误删 `(1M context)`，兼容缺段与异常输入，并通过单引号转义 + `%s` 格式化杜绝 shell 注入。
- **可选配额变更历史日志**：默认关闭不写盘；按需开启 `CLAUDE_STATUSLINE_QUOTA_LOG` 后仅在配额或重置时间变化时追加 JSONL 记录，写盘异常静默兜底不影响渲染。
- **零第三方依赖与跨平台兼容**：开箱即用于 macOS 原生 Bash 3.2、Linux/WSL2 Bash 5.x 与 Python 3.7+ 标准库（`git` 可选）。

## 账号与后端说明

- **订阅配额（`5h` / `7d`）**：`rate_limits` 下的 `five_hour`、`seven_day` 窗口由 **claude.ai Pro / Max / Ultra** 订阅账号提供。
- **API Key 与第三方后端**：当输入缺少 `five_hour` 和 `seven_day` 窗口时（如原生 API Key、第三方代理如 DeepSeek、Amazon Bedrock / Google Vertex AI，或仅下发 `spend_limit` 的 Claude apps 网关），脚本会自动隐藏配额段，其余分段（`cwd`、`model`、`effort`、`context`、`git branch`、`cost`）照常正常渲染。
- **Workspace Trust 门禁**：受 Claude Code 安全机制约束，自定义 `statusLine` 命令仅在已信任的工作区中执行。

## 快速安装

依赖：Bash 3.2+、Python 3.7+（仅标准库），以及可选的 `git`。macOS、Linux 与 WSL2 通用。

1. 复制脚本到 Claude 配置目录（`${CLAUDE_CONFIG_DIR:-$HOME/.claude}`）并赋予可执行权限：

```bash
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
mkdir -p "$CLAUDE_DIR"
cp tools/claude-statusline/claude-statusline "$CLAUDE_DIR/claude-statusline"
chmod +x "$CLAUDE_DIR/claude-statusline"
```

2. 在 `~/.claude/settings.json`（或 `${CLAUDE_CONFIG_DIR}/settings.json`）中启用：

```json
{
  "statusLine": {
    "type": "command",
    "command": "bash \"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/claude-statusline\""
  }
}
```

> 若希望在对话间隙定时刷新倒计时或 git 分支状态，可在 `statusLine` 对象中按需补充可选的 `"refreshInterval": 30`（单位：秒，需对应版本支持）。如需内联传递环境变量，可用 `env VAR=... bash ...` 形式配置。

运行单元测试：

```bash
python3 tools/claude-statusline/test_claude_statusline.py -v
```

## 环境变量配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CLAUDE_STATUSLINE_STATE_DIR` | `${CLAUDE_CONFIG_DIR:-$HOME/.claude}` | 状态根目录，用于确定默认 `quota-log.jsonl` 的落盘位置，支持前导 `~`/`~/` 展开。 |
| `CLAUDE_STATUSLINE_PATH_ALIASES` | *（空）* | 用冒号 `:` 分隔的多条 `prefix=alias` 路径缩写规则（如 `~/work/my-repo=~/MR:~/projects/app=~/APP`）。左侧前导 `~`/`~/` 按 `$HOME` 展开，按前缀长度从长到短匹配，严格遵守目录边界（`/foo/bar` 不会误匹配 `/foo/bar2`）。 |
| `CLAUDE_STATUSLINE_QUOTA_LOG` | *（关闭）* | 配额变更历史日志开关。未设置、为空、`0` 或 `false`（大小写不敏感）均关闭；设为 `1` 或 `true` 写入 `${CLAUDE_STATUSLINE_STATE_DIR}/quota-log.jsonl`；设为其他非空值则视为自定义目标文件路径（支持前导 `~/` 展开，自动创建父目录）。相邻相同快照自动去重。 |

## 本地日志文件

- `quota-log.jsonl`（默认位于 `${CLAUDE_STATUSLINE_STATE_DIR}`，即 `${CLAUDE_CONFIG_DIR:-$HOME/.claude}`）：**仅在显式开启** `CLAUDE_STATUSLINE_QUOTA_LOG` 时创建，每次 `7d`/`5h` 用量或重置时间戳变化时追加一行 JSONL（字段为 `ts`、`7d`、`7d_reset`、`5h`、`5h_reset`），相邻重复快照自动跳过；任何写盘异常都会静默兜底，绝不中断状态栏输出。
