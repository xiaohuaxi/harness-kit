[English](README.md) | **简体中文**

# agy-statusline

**Antigravity CLI（`agy`）** 的柔和 256 色 7 段状态栏命令脚本。单文件实现，仅依赖 Bash（3.2+）与 Python 3.7+ 标准库，零第三方依赖，支持 macOS、Linux 与 WSL2。

## 状态栏输出示例

```text
~/work/demo · Sonnet 4.5 · high · 42k/200k 21% ⛁ 2 · G: 7d 12% ↻ 5.4d 5h 8% ↻ 19:30 · C: 7d 25% ↻ 3.2d · main · $0.42
```

各段含义（缺少数据时自动隐藏对应分段）：

1. **cwd**：当前工作目录，支持按最长前缀缩写自定义别名（`AGY_STATUSLINE_PATH_ALIASES`），未命中时将 `$HOME` 缩写为 `~`。
2. **model**：当前模型名，自动剥离 `Claude ` / `Gemini ` / `GPT ` 前缀与尾部思考强度标记。
3. **effort**：当前思考强度（`low`、`medium`、`high`、`xhigh`、`max`）。
4. **context 用量**：上下文已用/总量（`k`/`M`）、百分比以及压缩次数 `⛁ N`。
5. **quota 配额**：独立展示 Gemini 池（`G:`）7 天与 5 小时用量及重置倒计时/时刻，以及第三方/Claude 池（`C:`）7 天用量与重置倒计时。
6. **git 分支**：位于 git 工作树时展示当前分支名或 detached HEAD 短哈希（`git` 为可选依赖）。
7. **cost**：当输入 JSON 提供费用字段时展示会话美元花费。

## 核心特性

- **Transcript `CHECKPOINT` 兜底统计**：`agy` 输出的状态数据里，`transcript_path` 可能漏掉 `-cli` 后缀（指向 `~/.gemini/antigravity/brain/...` 而非 `~/.gemini/antigravity-cli/brain/...`）。当该路径不存在时，脚本自动按 `conversation_id` 回退读取 `${AGY_STATUSLINE_STATE_DIR}/brain/<cid>/.system_generated/logs/transcript.jsonl`，保证 `⛁` 压缩计数准确。
- **Quota 本地缓存保活**：部分刷新事件不携带 `quota` 字段；脚本在收到非空 `quota` 时通过同目录临时文件 + `os.replace` 原子写入 `quota-cache.json`，并在后续空 `quota` 事件中回退读取缓存，避免配额段闪烁消失。
- **柔和 256 色层次配色**：使用低饱和 256 色（灰阶标签、粗蓝路径、雾青模型、雾绿 effort、深琥珀百分比），兼顾可读性与耐看度。
- **零第三方依赖与跨平台兼容**：兼容 macOS 原生 Bash 3.2、Linux/WSL2 Bash 5.x 与 Python 3.7+ 标准库（`git` 可选）。

## 快速安装

依赖：Bash 3.2+、Python 3.7+（仅标准库），以及可选的 `git`。macOS、Linux 与 WSL2 通用。

1. 复制脚本到 `~/.gemini/antigravity-cli/agy-statusline` 并赋予可执行权限：

```bash
mkdir -p ~/.gemini/antigravity-cli
cp tools/agy-statusline/agy-statusline ~/.gemini/antigravity-cli/agy-statusline
chmod +x ~/.gemini/antigravity-cli/agy-statusline
```

2. 在 `~/.gemini/antigravity-cli/settings.json` 中启用：

```json
{
  "statusLine": {
    "enabled": true,
    "type": "command",
    "command": "~/.gemini/antigravity-cli/agy-statusline"
  }
}
```

> 若所使用的 `agy` 版本不会在 `statusLine.command` 中展开 `~`，请将其替换为展开后的绝对路径；如需内联自定义环境变量，可用 `env VAR=... <路径>` 形式配置。

运行单元测试：

```bash
python3 tools/agy-statusline/test_agy_statusline.py -v
```

## 环境变量配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AGY_STATUSLINE_STATE_DIR` | `$HOME/.gemini/antigravity-cli` | 状态根目录，统一用于 transcript 兜底读取、`quota-cache.json` 与默认 `quota-log.jsonl` 落点，支持前导 `~/` 展开。 |
| `AGY_STATUSLINE_PATH_ALIASES` | *（空）* | 用冒号 `:` 分隔的多条 `prefix=alias` 路径缩写规则（如 `~/work/my-repo=~/MR:~/projects/app=~/APP`）。左侧前导 `~`/`~/` 按 `$HOME` 展开，按前缀长度从长到短匹配，严格遵守目录边界（`/foo/bar` 不会误匹配 `/foo/bar2`）。 |
| `AGY_STATUSLINE_QUOTA_LOG` | *（关闭）* | 配额变更历史日志开关。未设置、为空、`0` 或 `false`（大小写不敏感）均关闭；设为 `1` 或 `true` 写入 `${AGY_STATUSLINE_STATE_DIR}/quota-log.jsonl`；设为其他非空值则视为自定义目标文件路径（支持前导 `~/` 展开，自动创建父目录）。相邻相同快照自动去重。 |

## 本地状态与缓存文件

默认存放在 `${AGY_STATUSLINE_STATE_DIR}`（即 `~/.gemini/antigravity-cli/`）：

- `quota-cache.json`：最近一次非空 `quota` 快照，写入采用同目录临时文件 + `os.replace` 原子替换，避免并发读取撞上半写文件。
- `quota-log.jsonl`：**仅在显式开启** `AGY_STATUSLINE_QUOTA_LOG` 时创建，按 JSONL 追加记录每次发生变化的配额快照。
- `brain/<conversation_id>/.system_generated/logs/transcript.jsonl`：只读兜底文件，当输入 `transcript_path` 不存在时用于统计 `CHECKPOINT` 次数。
