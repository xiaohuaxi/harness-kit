# 设计定位与演进边界（codex-quota-wakeup）

本文件记录 `codex-quota-wakeup` 的产品定位、核心技术取舍与明确的非目标，供后续维护与代码审查对齐尺度，避免把个人辅助小工具越改越重。

## 1. 产品定位
- 这是一个面向个人及普通 ChatGPT 订阅（Plus / Pro / ProLite / Team）用户的**辅助性定时小工具**：帮用户在周额度重置后及时启动新的 7 天倒计时，避免几天没用造成刷新周期白白顺延。
- **核心底线（绝不妥协）**：
  1. 平时每半小时检测零 Token 消耗、不创建会话历史；
  2. 绝不死循环刷额度（发送前落盘 2h 冷却 + 7 天最多 3 次硬上限 + 进程锁）；
  3. 绝不直接或间接开启 Fast 模式（固定 `service_tier="default"` + `features.fast_mode=false`）；
  4. 绝不触碰、泄露或误删用户既有 `~/.codex` 登录凭据（不读 `auth.json`，不向 `codex exec` 透传可能触发 `logout_with_message` 的 `forced_*` 快照）。
- **允许的失败方式**：作为辅助工具，遇到短暂网络故障、CLI 版本过旧或异常配置时，只需**安全退出、记清日志、不崩溃不循环**，待网络恢复或用户升级 Codex 后下一轮定时自动自愈。

## 2. 关键技术取舍

### 为何用 `codex app-server` 查额度 + `codex exec` 唤醒？
- Codex CLI 没有类似 `agy --output-format json -p=/usage` 的只读额度查询命令；若完全不用 `app-server`，要么每半小时盲发 `codex exec` 浪费额度与撞限流，要么用本地时钟猜周期（遇到休眠、换机或 `Full reset` 券即失准）。
- `codex app-server` 的 `account/rateLimits/read` 是官方唯一零消耗读额度的正式接口，同时自动兼容 Keychain 与 OAuth token 续期；加上 `-c features.hooks=false -c features.plugins=false` 可避免后台拉起钩子与插件市场 `git` 同步。
- 真正唤醒时改用 `codex exec --ephemeral --ignore-user-config --ignore-rules --sandbox read-only`，避免加载用户全局配置里的一堆 MCP server，同时不留会话记录。

### 为何直接用 Codex 默认模型，不做自动猜轻量模型？
- 唤醒一周最多只发一次简短 `OK`（`low` 推理深度），用 Codex 默认模型的总消耗也远低于周额度的 `0.01%`，完全在可接受范围内。
- 此前曾实现过 `model/list` 分页 + 后缀启发式猜轻量模型 + 版本号排序 + 独立计量池黑名单状态机，结果引入了数倍复杂度和边界 bug（日期快照版本倒挂、偶发延迟永久拉黑轻量模型）。删繁就简后直接走默认模型（保留 `--model` / `--effort` 给需要手工指定的人），既跨代免维护又彻底消除整类状态机故障。

### 为何保留 `codex` 父目录进 `PATH`？
- Codex 核心虽已切换为 Rust 原生二进制，但官方仍主推 `npm install -g @openai/codex`（通过 `#!/usr/bin/env node` 包装脚本拉起原生程序）。仅需 3 行代码把解析出的 `codex` 目录前置进 `PATH`，即可在无 `nvm` 的 `launchd` / `systemd` / `cron` 环境中稳定找到同目录 `node`。

## 3. 明确的非目标（审查与迭代红线）
- **不做旧版 Codex 长期兼容层**：Codex 迭代极快且自带更新检查/自动升级。设置最低版本门槛（当前 `>= 0.158.0`）只为在 `install` 时给过旧版本明确升级提示，不为历史版本维护分支兼容逻辑。
- **不做对抗宿主机 root / MDM 企业托管配置的「安全沙箱」**：例如 `codex app-server` 在启动期即可能与托管策略配置的后端通信，外层个人脚本不可能也不应替官方二进制对抗管理员级配置。配置层检查仅限于避免本工具自身使用 `--ignore-user-config` 误伤既有凭据，且不得误拦合规的 ChatGPT Team 云端策略。
- **不为极端合成竞态继续叠甲**：在满足上述 4 条核心底线的前提下，拒绝脱离个人工具运行边界的过度防御和重型抽象。
