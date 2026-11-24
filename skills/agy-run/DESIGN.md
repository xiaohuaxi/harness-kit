# agy-run 设计与实现说明

> 本文面向维护 `agy-run` 的开发者与 Agent，说明 ACP 状态机、托管 agent 预检、沙箱边界与并发互斥。日常调用只读 [`SKILL.md`](SKILL.md)，勿加载本文。

## 1. 为什么采用 `agy acp` + 单进程前台阻塞

1. 不用 `--input-format stream-json`：实测 `agy 1.5.8` 下即使配 `queuedMessages: "send-immediately"`，`stream-json` 仍在上一轮输出 `result` 后才顺序读取下一条 `stdin` 消息，无法中途纠偏；而 `agy acp` 在工具执行中收到新 `session/prompt` 会立即结算旧请求（`stopReason: "end_turn"`）并在当前工具步结束后即时注入新指令。
2. 不用常驻 Daemon：单次 `agy_run.py` 对应一个受管前台进程组，在 `session/new` 或 `session/resume` 及模型/思考深度配置完成后开启本地 `SteerServer` 并输出 `AGY_SESSION_ID=<uuid>`（及写 `--session-id-file`），直至本轮（含后台命令唤醒轮）完全收敛后退出。

## 2. ACP JSON-RPC 生命周期、`--list-models` 与 >10s 后台命令收敛

`agy_core.run_acp_turn()` 通过队列 `event_q` 汇聚 `stdout`、`stderr`、超时与本地 socket `steer` 事件：

1. 可执行文件发现、代理桥接与 `--list-models`：`agy_bin()` 按 `AGY_BIN` -> `shutil.which("agy", path=env["PATH"])` -> `<HOME>/.local/bin/agy` -> `/usr/local/bin/agy` -> `/opt/homebrew/bin/agy` -> `"agy"` 解析；`agy_env()` 剥离 API Key/Gateway 变量，并在未设 `HTTP_PROXY`/`HTTPS_PROXY` 时将 `ALL_PROXY`/`all_proxy` 桥接过去（防止 Go `net/http` 在 WSL2 忽略 `ALL_PROXY`）。`--list-models` 在读 stdin、建状态目录与 agent 预检之前直接执行 `agy models`（45 秒超时，超时按进程组 `os.killpg` 清理），成功时仅透传 `stdout` 以滤掉 Go `glog` 在 `stderr` 的噪音。
2. 握手与会话配置：`initialize`（`id=1`）-> `session/new` 或 `session/resume`（`id=2`）-> 按 `configOptions` 先解析并切换模型（`match_acp_model`），待响应刷新 `configOptions` 后再校验并设置思考深度（`match_acp_effort`），均经 `session/set_config_option` 串行下发。目标模型不支持 `thought_level` 且未显式传 `-e` 时自动跳过思考深度配置。
3. 非交互门禁：任何 `session/request_permission` 均即时回拒并记入 `denied_calls`（跑后报错）；其余 `ServerRequest` 立即回 JSON-RPC `-32601` 防阻塞；收到结果后若检测到已发起 `IsDaemon: true` 的 `run_command`（含后台唤醒阶段补检），即判败并终止本轮。
4. >10s 后台命令早退陷阱与 session 绑定正则：`agy` 内置 `run_command` 的 `WaitMsBeforeAsync` 上限为 `10000`ms，超 10 秒的命令会转入内部后台（`<sid>/task-<N>`）并使当前 `session/prompt` 提前返回 `end_turn`，且后续唤醒轮不再往 ACP `stdout` 推送 `session/update`。此时脚本保持 `agy acp` 进程存活，通过 `inspect_transcript_drain()` 追踪 `<HOME>/.gemini/antigravity-cli/brain/<sid>/.system_generated/logs/transcript_full.jsonl` 新增行。为避免 `read_file` 读取含正则字面量的源码/日志造成幽灵后台任务误判，匹配正则动态绑定当前 `session_id`：
   - spawn（`GENERIC`）：`(?m)^Tool is running as a background task with task id:\s*(<escaped_sid>/task-\d+)\s*$`（行首行尾锚定）；
   - done（`SYSTEM_MESSAGE`）：`Task id "(<escaped_sid>/task-\d+)" finished with result:`（兼容 `[Message] ... content=Task id "..." finished with result:` 包装）。
   待全部后台任务收到完成通知且唤醒后的最终 `PLANNER_RESPONSE`（`status == "DONE"` 且无后续 `tool_calls`）落盘后，提取最终答复与补充工具记录退出。

## 3. 托管 Agent 安装、运行前单文件预检与三档沙箱

6 份全局自定义 agent（`agy-{readonly,write,yolo}[-rules].md`）默认托管在 `<HOME>/.gemini/config/agents`：

- `--install-agents` 与 `--write-agent-files <DIR>`：`--install-agents` 统一按 UTF-8 与 LF/CRLF 归一化先全量核对 6 份文件再落盘，避免半安装。若默认目录是软链，全部一致则零写入返回 0，有缺失或漂移则拒写穿软链并打印物理目标路径与 `--write-agent-files <physical_target>` 命令；若为普通目录，遇到同名内容冲突或同名文件本身为软链时整批拒写并给出 `--write-agent-files <agents_dir>` 显式覆盖命令，仅无冲突时补全缺失文件。
- 运行前单文件预检（`ensure_agent_installed`）：在 `resolve_turn_config()` 确定本轮 `agent_name` 后、创建运行目录与工作区快照前执行（覆盖新会话与 `--resume`，`--steer` 与 `--list-models` 跳过）。只核对本轮选中的那 1 份 `<agents_dir>/<agent_name>.md`，区分缺失（提示 `--install-agents` 或软链物理目标刷新命令）与本地修改/过旧（提示 `--write-agent-files <dir>`）。
- 为什么 `read-only` 与 `workspace-write` 均传 `--dangerously-skip-permissions --sandbox`：实测 `agy 1.5.8` 下，仅传 `--sandbox` 而不传 `--dangerously-skip-permissions` 会对 `run_command` 触发 `session/request_permission`，一旦客户端回 `"allow"`，该命令会脱离 macOS Seatbelt 直接在宿主机执行（`~/.ssh` 可读）；同时传两项参数时，所有未设 `BypassSandbox` 的命令均跑在 Seatbelt 内（允许读写项目工作区、临时目录与常见构建缓存，屏蔽 `~/.ssh`、`.env` 及未挂载家目录路径，且默认无终端外网）。
- 纵深校验：启动前若检测到目标仓 `.agents/agents/` 等目录存在同名 `agy-*` agent 即拒跑防遮蔽。因 Seatbelt 将项目工作区挂载为可读写，`read-only` 额外移除写/改文件工具并在跑前跑后做 `workspace_snapshot()` 零改动比对；`read-only` 与 `workspace-write` 均双重核查 ACP/transcript 参数与会话 SQLite 库（`stored_commands()`，支持 5 字节 protobuf varint）拦截 `BypassSandbox`；`workspace-write` 校验 `writes_outside()`；启用规范时对 Claude 项目 memory 做拷贝隔离与跑后 SHA-256 校验。
- 跨版本工具名映射：agent 文件沿用 1.3.5 工具名（`view_file`、`write_to_file`、`replace_file_content`），`agy 1.5.x` 将其映射为 `read_file`、`write_file`、`edit_file`；若未来新版改名，在 `AGY15_NAMES` 与 `ACTION_TOOLS` 补齐映射即可。

## 4. 实时纠偏（`--steer`）、互斥锁与防递归

- 本地 Socket 与全周期锁：`SteerServer` 监听 `<state_dir>/active/<sid>.sock`（超 95 字节自动回退 `/tmp/agy-run-<uid>/<sid>.sock`，校验属主 UID、非软链与 `0700` 权限）。`--resume` 在读取会话配置前先拿 `flock(LOCK_EX | LOCK_NB)`；新会话则在完成 `session/new` 及模型/思考深度配置后启动 `SteerServer`、拿锁并交由外层 `run_turn()` 持有，最后发布会话 ID。ACP 轮次结束时仅关闭 socket 与 `active/<sid>.json`，文件锁一直保持至跑后快照/memory 校验与 `conversations/<sid>.json` 落盘全部完成后才释放，且 `.lock` 文件永不 `unlink` 以防 inode 替换竞态。活跃 `session/prompt` 期间收到 `--steer` 立即发新 `session/prompt` 覆盖旧轮；后台排空等待期间暂存入 `queued_steers`，待排空完成后接续执行。
- 环境白名单与防递归：`ENV_KEEP` 剥离 `GEMINI_API_KEY` 与 `AGY_LLM_GATEWAY_*` 确保走订阅登录；注入 `AGY_RUN_ACTIVE=1` 事前拒绝嵌套调用 `agy_run.py`；对 `claude` 与 `codex` 则通过 `PATH` 拒绝桩阻拦普通调用，并在跑后做命令位语法扫描判败。
