[English](README.md) | **简体中文**

# agy-quota-wakeup

AGY（Antigravity CLI）周额度重置自动检测与最小 Chat-only 唤醒工具。单文件脚本，兼容 Python 3.8+，零第三方依赖，支持 macOS 与 Linux / WSL2 / 远程 SSH 服务器。

## 背景

AGY 的周额度窗口（`gemini-weekly` 与 `3p-weekly` 两套独立池）在自然周到期或官方额外发放重置后，不会自动开始新一轮 7 天倒计时，而是等待该池发出首条请求后才从那一刻起算 7 天。若重置后隔十几个小时才首次使用，下一轮刷新时间也会顺延十几个小时。

## 工作方式

1. **平时零消耗探测**：默认每半小时在 `:03`、`:33`（及开机登录 / 休眠唤醒补跑时）执行 `agy --output-format json -p=/usage`（0 Token、不创建会话），检查两个周额度桶：
   - **已激活**：`remaining_fraction < 1`，或 `reset_time` 距当前已不足 `7天 - 5分钟`；直接记日志退出，零消耗。
   - **未激活**：`remaining_fraction == 1` 且 `reset_time` 仍随当前时刻贴着 `now + 7天` 漂移。
2. **最小 Chat-only 唤醒（带标准非 yolo 回退）**：仅当某池未激活时，优先在空临时目录内动态写入自包含的零工具配置（`.agents/agents/quota-wakeup.md`：`tools: []`、`inheritCustomizations: false`、`excludeDefaultComponents: true`），向目标池低思考深度模型发送一条单字 `OK` 请求（Gemini 池约 370 tokens，第三方池约 820 tokens、仅占周池约 `0.004%`）；若未来某版 `agy` 在发送前拒绝自定义配置或可选参数，自动回退到最精简的标准非 yolo 命令（`agy --model <id> --output-format json -p=...`，日志记录 `mode: "standard_fallback"`），随后复查 `/usage` 确认 7 天倒计时已锁定。
3. **跨代与跨平台免维护**：
   - 模型 ID 从 `agy models` 动态挑选最新一代 `Flash Low` / `Claude Sonnet Low`，后续模型换代无需改版本号；
   - macOS 使用用户级 `~/Library/LaunchAgents`（`launchd` `StartCalendarInterval`），Linux 使用用户级 `~/.config/systemd/user/`（`systemd --user` timer + `Persistent=true`），休眠唤醒后错过刻度自动合并补跑一次，不触碰系统级目录或 root 权限。

## 安全与防刷护栏

- **零工具 / 非 yolo 执行**：唤醒请求在空临时目录运行，禁用所有工具、规则、skills 与 MCP，不传 `--dangerously-skip-permissions` 或 `--sandbox`；剥离常见外部 API Key 及网关覆盖变量，不触碰用户凭据文件。
- **防重复扣费与冷却护栏**：超时、网络及服务端异常绝不自动重发；未验证锁定倒计时触发 2 小时冷却，并配合 `flock` 互斥锁防并发执行，遇异常干净退出、下一轮自动自愈。

## 快速安装

依赖：Python 3.8+（纯标准库）与已登录的 `agy` CLI（`>= 1.5.8`）。

```bash
chmod +x agy-quota-wakeup
mkdir -p ~/.local/bin && cp agy-quota-wakeup ~/.local/bin/
~/.local/bin/agy-quota-wakeup install
~/.local/bin/agy-quota-wakeup status
```

> - `install` 会把当前脚本路径写入系统定时任务，请先放到 `~/.local/bin` 等固定目录再安装，避免直接从 `~/Downloads`、`~/Desktop`、`~/Documents` 等受系统隐私保护或易删目录安装。若提示 `command not found`，将 `~/.local/bin` 加入 `PATH` 或使用完整路径调用。
> - 修改参数、脚本路径或代理设置后，直接重跑 `install` 即可幂等覆盖更新（无需先 `uninstall`；重装时请带齐需保留的可选参数）。
> - 同一账号通常在一台主力设备启用即可；若需在多台设备（如 macOS + Linux）并行守护，可在一端加 `--minute-offset 18` 错开触发分钟（`:18` / `:48`），避免同刻度重复探测。

运行测试：

```bash
python3 test_agy_quota_wakeup.py -v
```

## 常用命令

```bash
# 查看定时任务状态、调度时间、已存代理与最近日志摘要
agy-quota-wakeup status

# 手动跑一次检查（未激活则立即唤醒；加 --dry-run 只看判定与选型不发请求）
agy-quota-wakeup run
agy-quota-wakeup run --dry-run

# 安装 / 覆盖更新定时任务（默认每半小时 :03、:33；单池可加 --pools gemini；多机并用可错开为 :18、:48）
agy-quota-wakeup install
agy-quota-wakeup install --minute-offset 18

# 卸载定时任务（移除系统定时器与 proxy.json，保留历史日志与 state.json；Linux 已开启的 linger 如需关闭可运行 loginctl disable-linger）
agy-quota-wakeup uninstall

# 查看历史日志（全部 / 仅唤醒事件 / 仅异常 / 原始 JSONL）
agy-quota-wakeup logs -n 30
agy-quota-wakeup logs --activations
agy-quota-wakeup logs --errors
agy-quota-wakeup logs --json -n 20
```

## 参数与环境变量速查

| 入口 | 参数 / 变量 | 说明 |
| --- | --- | --- |
| `install` | `--interval SECONDS` | 检查间隔秒数（默认 `1800`，即每半小时） |
| `install` | `--minute-offset 0-59` | 小时对齐刻度的分钟偏移（默认 `3` → `:03,:33`；如 `18` → `:18,:48`） |
| `install` / `run` | `--pools {all,gemini,3p}` | 指定守护的周额度池（默认 `all`） |
| `install` / `run` | `--agy-bin PATH` | 显式指定 `agy` 可执行文件路径（自定义安装位置时使用） |
| `install` | `--no-proxy` | 不继承当前 shell 代理并清除已保存的代理配置 |
| `run` | `--dry-run` / `--force` | 只检不发 / 强制对目标池发一次最小唤醒 |
| `run` | `--gemini-model` / `--3p-model` | 手动覆盖对应额度池的唤醒模型 ID |
| `run` | `--retry-delays LIST` | `/usage` 瞬态网络错误重试间隔秒数列表（默认 `5,15`） |
| 环境变量 | `AGY_QUOTA_WAKEUP_STATE_DIR` | 覆盖状态与日志目录（安装后手动跑其他子命令需保持一致） |

更多选项可运行 `agy-quota-wakeup --help` 或 `agy-quota-wakeup <subcommand> --help`。

## 网络代理与 Linux / WSL2 说明

- **代理自动继承**：`install` 时会自动捕获当前终端的 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY`（大小写均兼容；仅设 `ALL_PROXY` / `all_proxy` 时自动补齐 `HTTP(S)_PROXY` 映射），以 `0600` 权限保存在 `proxy.json` 供后台定时任务继承（运行时若当前进程已显式设置代理则优先使用现场值）。更换或关闭代理后重跑一次 `agy-quota-wakeup install`（或加 `--no-proxy` 清除已存代理）即可刷新；`install` 完成后会立即后台执行一次首跑检查。
- **Linux / WSL2 与 SSH 常驻**：本机 `systemctl --user` 可用时直接运行 `install` 即可，且会自动调用 `loginctl enable-linger` 让用户定时器在退出全部 SSH 会话后继续常驻（若受系统权限限制会打印对应 `sudo` 命令；unit 级异常可用 `journalctl --user -u agy-quota-wakeup` 排查）。若环境未启用 `systemd --user`，命令会安全退出并打印默认每半小时（`:03,:33`）的 `crontab -e` 示例（手工添加的 `cron` 条目停用时需自行移除）。注意 `linger` 不会阻止 Windows 将空闲 WSL 实例自动关机，关机期间不触发，下次启动后 `Persistent=true` 会自动补跑。

## 状态与日志位置

默认位于 `~/.local/state/agy-quota-wakeup/`（可通过 `AGY_QUOTA_WAKEUP_STATE_DIR` 覆盖，长期使用时建议写入 shell `export`）：

- `wakeup.jsonl`（满 2 MiB 自动轮转为 `wakeup.jsonl.1`）：每次检查与唤醒的结构化记录
- `state.json`：各池最近一次唤醒状态（原子写）
- `proxy.json`：安装时捕获的后台代理配置（`0600` 权限，无代理时不生成）
- `wakeup.lock`：`flock` 互斥锁，防并发执行
- `launchd-stderr.log`：macOS `launchd` 未捕获标准错误兜底日志
