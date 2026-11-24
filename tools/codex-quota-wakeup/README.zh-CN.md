[English](README.md) | **简体中文**

# codex-quota-wakeup

Codex CLI（ChatGPT 订阅）周额度重置自动检测与最小 Agent 唤醒工具。单文件脚本，兼容 Python 3.8+，零第三方依赖，支持 macOS 与 Linux / WSL2 / 远程 SSH 服务器。

## 背景

Codex 订阅的周额度窗口（`~10080` 分钟 / 7 天，兼容 `plus` / `pro` / `team` 的 5h+7d 双窗口与 `prolite` 等单周窗口计划）在周期到期或兑换官方赠送的 `Full reset` 额度重置券后，需要发出新一轮首条请求才会锁定新的 7 天刷新倒计时。若重置后隔十几个小时才首次使用，下一轮周额度刷新时间也会顺延十几个小时。

## 工作方式

1. **平时零消耗探测**：默认每半小时在 `:03`、`:33` 通过 `codex app-server` stdio JSON-RPC 读取生效配置、订阅登录态与额度窗口（0 Token、不创建会话文件）；系统重启后不立即抢跑，而是自然顺延到下一个计划刻度，并由后台默认 `--min-uptime 300` 在开机前 5 分钟内自动宽限等待：
   - **已激活**：`usedPercent > 0`、或 `resetsAt` 距当前已不足窗口时长减 15 分钟、或任一额度窗口已耗尽/受限时，直接记日志退出，零消耗。
   - **未激活**：`usedPercent == 0`（剩余 100%）且同一账号身份下连续 3 次采样确认 `resetsAt` 持续随当前时间向后漂移，才判定为需要唤醒（避免刚发完首条请求但整数百分比仍显示 `0%` 时误触发）。
2. **隔离的最小唤醒请求**：确认未激活后，在空临时目录内发起一次最小化只读临时请求（`codex exec --ephemeral --ignore-user-config --ignore-rules --skip-git-repo-check --sandbox read-only`，回复 `OK`），随后复查 `account/rateLimits/read` 确认 7 天倒计时已锁定。单次唤醒总消耗通常不足周额度的 `0.01%`。
3. **跨代免维护**：不硬编码具体模型版本，默认直接使用 Codex 内置默认模型与 `low` 推理深度（可通过 `--model` / `--effort` 手动覆盖）。
4. **跨平台用户级调度**：macOS 使用用户级 `~/Library/LaunchAgents`（`launchd` `StartCalendarInterval`、`RunAtLoad=false`），Linux 使用用户级 `~/.config/systemd/user/`（`systemd --user` timer `OnCalendar`，不设 `OnStartupSec` / `Persistent=true`），不触碰系统级目录或 root 权限。`install` 完成后会立即后台执行一次首跑；系统重启后不抢跑，顺延到下一个正常刻度，并配合默认 `--min-uptime 300` 兜底等待网络与本地服务就绪。

## 安全与防刷护栏

- **不触碰登录凭据**：不读取或修改 `~/.codex/auth.json`，检测到与当前登录冲突的非用户层 `forced_*` 策略限制时安全跳过以保护既有凭据（合规的 Team 云端策略正常放行）。
- **绝不开启 Fast 或越权模式**：强制锁定官方 ChatGPT 后端地址与标准服务档（`service_tier="default"`、`features.fast_mode=false`），不加载用户 MCP、hooks 与 plugins，剥离外部 API Key / 自定义端点环境变量，不开启权限绕过。
- **防死循环硬上限**：发送前落盘 2 小时未验证冷却 + 单账号滚动 7 天最多 3 次发送上限 + `flock` 互斥锁，遇网络或版本异常时干净退出，等恢复后下一轮自动自愈。

更多设计定位与取舍边界见同目录 [`DESIGN.md`](DESIGN.md)。

## 快速安装

依赖：Python 3.8+（纯标准库）与已通过 `codex login` 登录 ChatGPT 订阅账号的 `codex` CLI（`>= 0.158.0`）。

```bash
chmod +x codex-quota-wakeup
mkdir -p ~/.local/bin && cp codex-quota-wakeup ~/.local/bin/
~/.local/bin/codex-quota-wakeup install
~/.local/bin/codex-quota-wakeup status
```

> - `install` 会把当前脚本路径写入系统定时任务，请先放到 `~/.local/bin` 等固定目录再安装，避免直接从 `~/Downloads`、`~/Desktop`、`~/Documents` 等受系统隐私保护或易删目录安装。若提示 `command not found`，将 `~/.local/bin` 加入 `PATH` 或使用完整路径调用。
> - 修改参数、脚本路径、代理设置或升级脚本后，请带上原安装参数直接重跑一次 `install`，即可幂等刷新已有的 LaunchAgent / systemd 定时配置（无需先 `uninstall`；手工配置 `crontab` 的用户请在对应命令中补上 `--min-uptime 300`）。
> - 同一订阅账号通常在一台主力设备启用即可；若需在多台设备（如 macOS + Linux）并行守护，可在一端加 `--minute-offset 18` 错开触发分钟（`:18` / `:48`），避免同刻度重复探测。
> - 若本机经常在默认 `~/.codex` 下切换不同账号或工作区，可用独立 `CODEX_HOME` 隔离后台唤醒登录态：`CODEX_HOME=~/.codex-wakeup codex login && CODEX_HOME=~/.codex-wakeup codex-quota-wakeup install`。

运行测试：

```bash
python3 test_codex_quota_wakeup.py -v
```

## 常用命令

```bash
# 查看定时任务状态、调度时间、已存代理与最近日志摘要
codex-quota-wakeup status

# 手动跑一次检查（未激活则立即唤醒；加 --dry-run 只看判定结果不发请求）
codex-quota-wakeup run
codex-quota-wakeup run --dry-run

# 安装 / 覆盖更新定时任务（默认每半小时 :03、:33；多机并用可错开为 :18、:48）
codex-quota-wakeup install
codex-quota-wakeup install --minute-offset 18

# 卸载定时任务（移除系统定时器与 proxy.json，保留历史日志与 state.json；Linux 已开启的 linger 如需关闭可运行 loginctl disable-linger）
codex-quota-wakeup uninstall

# 查看历史日志（全部 / 仅唤醒事件 / 仅异常 / 原始 JSONL）
codex-quota-wakeup logs -n 30
codex-quota-wakeup logs --activations
codex-quota-wakeup logs --errors
codex-quota-wakeup logs --json -n 20
```

## 参数与环境变量速查

| 入口 | 参数 / 变量 | 说明 |
| --- | --- | --- |
| `install` | `--interval SECONDS` | 检查间隔秒数（默认 `1800`，即每半小时） |
| `install` | `--minute-offset 0-59` | 小时对齐刻度的分钟偏移（默认 `3` → `:03,:33`；如 `18` → `:18,:48`） |
| `install` / `run` | `--codex-bin PATH` | 显式指定 `codex` 可执行文件路径（自定义安装位置时使用） |
| `install` / `run` | `--model MODEL` | 覆盖唤醒所用模型 ID（默认使用 Codex 内置默认模型） |
| `install` | `--no-proxy` | 不继承当前 shell 代理并清除已保存的上游代理配置（保留自定义 CA） |
| `run` | `--dry-run` / `--force` | 只检不发 / 强制发一次最小唤醒（仍受冷却与 7 天上限约束） |
| `run` | `--effort LEVEL` | 覆盖唤醒轮次的推理深度（默认 `low`） |
| `run` | `--retry-delays LIST` | 瞬态网络错误重试间隔秒数列表（默认 `5,15`） |
| `run` | `--min-uptime SECONDS` | 检测前等待系统开机时长达到该秒数（`0-3600`，默认 `0` 不等待；后台定时任务默认传 `300`） |
| 环境变量 | `CODEX_HOME` | 指定独立的 Codex 配置与登录目录 |
| 环境变量 | `CODEX_QUOTA_WAKEUP_STATE_DIR` | 覆盖状态与日志目录（安装后手动跑其他子命令需保持一致） |

更多选项可运行 `codex-quota-wakeup --help` 或 `codex-quota-wakeup <subcommand> --help`。

## 网络代理与 Linux / WSL2 说明

- **代理与自定义 CA 自动继承**：`install` 时会自动捕获当前终端的 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY`（大小写均兼容）以及 `CODEX_CA_CERTIFICATE` / `SSL_CERT_FILE`（转为绝对路径），以 `0600` 权限保存在 `proxy.json` 供后台定时任务继承（运行时若当前进程已显式设置代理则优先使用现场值）。更换或关闭代理后重跑一次 `codex-quota-wakeup install`（或加 `--no-proxy` 清除上游代理）即可刷新；`install` 完成后会立即后台执行一次首跑检查。
- **Linux / WSL2 与 SSH 常驻**：本机 `systemctl --user` 可用时直接运行 `install` 即可，且会自动调用 `loginctl enable-linger` 让用户定时器在退出全部 SSH 会话后继续常驻（若受系统权限限制会打印对应 `sudo` 命令；unit 级异常可用 `journalctl --user -u codex-quota-wakeup` 排查）。若环境未启用 `systemd --user`，命令会安全退出并打印带 `PATH` 与 `--min-uptime 300` 的 `crontab -e` 示例（手工添加的 `cron` 条目停用时需自行移除）。注意 `linger` 不会阻止 Windows 将空闲 WSL 实例自动关机，关机期间不触发，下次启动后将在下一个正常刻度恢复检测（并受默认 300 秒开机宽限保护），不会在开机瞬间抢跑。

## 状态与日志位置

默认位于 `~/.local/state/codex-quota-wakeup/`（可通过 `CODEX_QUOTA_WAKEUP_STATE_DIR` 覆盖，长期使用时建议写入 shell `export`）：

- `wakeup.jsonl`（`0600` 权限，满 2 MiB 自动轮转为 `wakeup.jsonl.1`）：每次检查与唤醒的结构化记录
- `state.json`：最近一次唤醒、冷却与 7 天次数状态（原子写）
- `proxy.json`：安装时捕获的后台代理与证书路径配置（`0600` 权限，无配置时不生成）
- `wakeup.lock`：`flock` 互斥锁，防并发执行
- `launchd-stderr.log`：macOS `launchd` 未捕获标准错误兜底日志
