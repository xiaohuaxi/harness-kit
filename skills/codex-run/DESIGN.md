# codex-run 设计与实现说明

> 本文面向维护 `codex-run` 的开发者与 Agent，说明状态机、并发互斥与安全边界。日常调用只读 [`SKILL.md`](SKILL.md)，勿加载本文。

## 1. 为什么采用单进程前台阻塞 + `codex app-server --stdio`

1. 不用 `codex exec`：`codex exec` 不暴露内部 `spawn_agent` 生命周期事件（根线程未 `wait_agent` 早退时外部无法感知悬空子任务），且不支持运行中注入 `turn/steer`。
2. 不用常驻 Daemon：agy、Claude Code 与 Codex 均支持长命令后台等待并在进程退出时唤醒调用方。一次 `codex_run.py` 对应一个前台阻塞进程，内建 `codex app-server --stdio` 会话组，待当前 turn（含未显式 `wait_agent` 的子/孙 agent）完全收敛后退出，无轮询也无孤儿守护进程。

## 2. JSON-RPC 生命周期与非交互防挂死

`run_codex_app_server()` 通过单线程队列 `event_q` 汇聚 `stdout`、`stderr`、超时与本地 socket `steer` 事件：

1. `initialize`（`id=1`）：通过 `optOutNotificationMethods` 屏蔽 `agentMessage/delta` 与 reasoning delta 等高频流式通知，仅收结构化生命周期与完整 `item/completed` 事件。
2. `initialized` -> `thread/start` 或 `thread/resume`（`id=2`）：拿到响应后校验 `root_tid`、记录有效 `model`/`reasoningEffort`、启动 `SteerServer` 并落盘元数据，再发首轮 `turn/start`。
3. 双重非交互防挂死：
   - CLI 固定传 `-a never` 与 `-c tools.experimental_request_user_input.enabled=false`；
   - 若服务端仍发来任何 `ServerRequest`（同时含 `id` 与 `method`），立即回 JSON-RPC `-32601` 错误，防 `app-server` 阻塞等待。

## 3. 子 Agent 排空、自动续跑与竞态收敛

内部 `spawn_agent` 派出的子/孙线程共享同一 `app-server` 事件流（`params.threadId != root_tid`）。`active_subagent_threads` 在收到子线程 `turn/started` 时加入，在 `turn/completed`、`thread/closed` 或 `thread/status/changed`（`idle` / `notLoaded` / `systemError`）时移除。

根线程收到 `turn/completed`（`status == "completed"`）时走四路判据：

1. 仍有活跃子/孙且 `auto_continues < MAX_AUTO_CONTINUES (3)`：置 `waiting_for_sub_drain = True`，静默等 `active_subagent_threads` 全部归零后，合并期间排队的 `queued_steer_texts`，向根线程发 1 次带 `AUTO_CONTINUE_PROMPT` 的 `turn/start`（`auto_continues += 1`）。
2. 无活跃子/孙，但 `queued_steer_texts` 非空且未达上限：处理 `--steer` 与 `turn/completed` 撞车的边界竞态，立即触发一次自动续跑送达调用方补充指令，防丢消息。
3. 仍有活跃子/孙但已达 `MAX_AUTO_CONTINUES (3)`：终止进程组并以退出码 `1` 报错，兼顾防死循环与防半截结果误判成功。
4. 无活跃子/孙且无排队 steer：置 `completed_cleanly = True` 正常退出（`0`）；若 `auto_continues > 0`，在 `stdout`/`stderr`/`-o` 末尾附早退提示供调用方向用户披露。

## 4. 实时纠偏（`--steer`）与并发互斥锁

- 本地 Unix Socket 与路径回退：`SteerServer` 默认监听 `<state_dir>/active/<thread_id>.sock` 并在同名 `.json` 记录实际 `socket_path`。因 macOS `SUN_LEN` 限 104B（Linux 108B），路径超 95 字节时自动回退至 `/tmp/codex-run-<uid>/<thread_id>.sock`（强校验属主 UID、非软链与 `0700` 权限）。
- 三态投递：首轮 `turn/started` 前到达的请求暂存 `pending_live_steers`，启动即冲刷；活跃 turn 期间发 `turn/steer` RPC（若恰逢转入 `waiting_for_sub_drain` 导致 RPC 报错则自动降级入 `queued_steer_texts`）；排空等待期间直接入 `queued_steer_texts` 随自动续跑一并注入。
- 独占文件锁（防并发 `--resume`）：`<state_dir>/active/<thread_id>.lock` 用 `flock(LOCK_EX | LOCK_NB)` 互斥。`--resume` 在读线程元数据和拉起子进程前先拿锁；`finally` 严格保证先停进程组（`stop_process_group`），再关 socket、删 `.sock`/`.json` 并解锁，且永不 `unlink` `.lock` 文件以规避 POSIX `flock + unlink` 换 inode 竞态。

## 5. 进程组回收、隔离与脱敏

- 会话组与 Watchdog：`Popen(..., start_new_session=True)` 使 `pgid == proc.pid`；`stop_process_group()` 对整个进程组发 `SIGTERM`（2s）-> `SIGKILL`（2s），主进程提前退出仍能清干净孙进程；独立 `threading.Timer` 超时主动杀组返回 `124`。
- 异常退出防误报：仅根线程完成且无悬空子任务才置 `completed_cleanly = True`；中途崩溃（EOF）即使已有中间消息也必返退出码 `1`。
- 环境隔离、防递归与 Provider 守卫：使用一次性空 `ZDOTDIR`、禁 login shell、通过 `shell_environment_policy.exclude` 过滤敏感变量，输出经 `sanitize()` 脱敏；注入 `CODEX_RUN_ACTIVE=1` 拦截嵌套调用；`--profile deepseek` 额外注入 `DEEPSEEK_GUARD` 禁用内置 `spawn_agent`（规避外置 provider 子消息丢明文及全历史 fork 递归重放缺陷）。

## 6. 模型查询（`--list-models`）

- 独立信息查询路径：`main()` 在读取任务文本与运行 `validate_not_nested()` 之前直接处理 `--list-models`，不拉起 `codex app-server`。
- 无 `--profile` 时：复用 `load_model_catalog()` 读取 `$CODEX_HOME/models_cache.json` 中 `visibility == "list"` 的条目，仅输出 `<slug>` 与支持的 `effort`，过滤 `instructions_template` 等长字段；缺失或无可见条目时返回非零并提示刷新缓存或省略 `-m` / 传全名。
- 带 `--profile` 时：复用 `validate_profile()` 校验 `$CODEX_HOME/<name>.config.toml`，优先使用 `tomllib`（Python 3.11+），在 Python 3.8–3.10 回退到首个 `[table]` 之前的顶层字符串键正则解析，读取 `model`、`model_reasoning_effort` 与 `model_catalog_json`（支持基于 `env["HOME"]` 的 `~` 展开与相对 profile 目录解析）。若配置了 `model_catalog_json` 但文件缺失、损坏或无有效条目，直接报错返回非零，不回退到普通 GPT 缓存。
