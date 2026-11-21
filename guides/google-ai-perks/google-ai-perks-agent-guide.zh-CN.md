# Google AI 订阅附带福利领取、配置与核验指南（Agent 执行指南）

> 使用说明（致人类读者）：无需人工逐条操作。直接将本文件发给你的 Google Antigravity（`agy` 桌面端或 CLI），附上指令：「按这份指南帮我检查、领取并验证我的 Google AI 订阅福利」。

## 1. 任务目标与入口规则（致执行 Agent）

你正在读取一份面向 AI Agent 编写的端到端实操指南。
- 若用户仅要求阅读、解释、审阅或评估安全性，只如实说明本指南将执行的操作、涉及的本机路径与权限边界，不运行探测命令，也不修改任何配置。
- 若用户要求按本指南检查、领取、配置订阅福利（或直接发来本文件交办），请由你全程主导推进：对照下文全部 7 项现役附带福利与常见过时说法勘误，依次完成现状摸底、领取激活、防超支安全设置、官方工具安装以及真机核验。

宿主兼容说明：
- 本指南以 Google Antigravity（桌面端 `Antigravity App` 或终端 `agy` CLI）为默认执行环境。
- 若你并非 `agy`，而是运行在 `Claude Code`、`Codex` 等其他 Agent 环境中，除「阶段 1：连接浏览器」改用你当前环境的浏览器工具（如 `chrome-devtools` 或 `playwright` MCP；若当前会话无浏览器工具或处于非交互模式，则直接把目标网址与核对项发给用户，由用户点开确认）外，其余所有检查逻辑、命令行操作、安全红线与验证方法完全通用。

## 2. 全程交互纪律与安全红线

- 先预告再动手：每进入一个新阶段，先用用户当前交流语言（默认自然平实中文）以两三句话告知接下来准备检查或操作什么、是否涉及费用；不要不打招呼连续执行一长串黑盒操作。
- 幂等探查优先：每项福利动手前先只读查看现状。若用户已有可用云结算账号、当月赠金已入账、或本机已装好并授权过 `colab` 工具，按实际状态补齐缺失勾选项或直接进入验证环节，**不要**重复点领；修改任何本机配置文件前先检查文件是否已存在，合并字段而非整文件覆盖。
- 红线（必须停下交由用户本人操作的人工接管点）：
  1. 输入 Google 账号密码、完成两步验证（2FA / Passkey）或通过人机验证；
  2. 在 Google Cloud 结算页面填写信用卡/借记卡卡号、安全码、账单地址，或在 Google AI Studio 确认首次预充值实际付款；
  3. 在用户自己的交互终端完成 `colab` 首次 OAuth 授权码粘贴，或在 `agy` CLI 输入框手动发送 `/browser` 指令并点击 Chrome 弹窗 `Allow`；
  4. 任何会产生真实美元扣费的资源购买操作。
  遇到以上节点，保持当前页面打开，说清需要用户操作哪一步，等用户回复确认后再继续。
- 红线（凭据保密）：新建或读取的 `GEMINI_API_KEY`、结算账号编号、OAuth 授权码等敏感凭据，仅写入用户确认的本机私有文件（权限设为 `600`），**严禁**在对话中完整明文回显，**严禁**拼入进程命令行参数（如 `ps` 可见的明文 `-H`），**严禁**写入受 Git 追踪的文件。
- 红线（算力计费隔离与保护用户既有任务）：订阅赠送的 GPU 额度仅限 Google Colab 计算单元（CU），**严禁**去 Google Cloud Compute Engine（GCP）创建 GPU 云主机（GCP 按 `$3.67+/小时` 另收美元）。拉起 Colab 测试实例前**必须**先记录用户原有的活跃会话，仅为本次测试指定专属会话名，测试后只清理本次新建的测试会话，**严禁**关停用户原本正在运行的训练或推理任务。

## 3. 阶段 0：本机只读摸底与开场对齐

1. 在终端逐项运行零副作用只读探查（单条工具缺失时容错继续，**严禁**读取或打印环境变量值与密钥文件内容）：

```bash
uname -s
command -v python3 uv pipx node npx || true
command -v colab || test -x "$HOME/.local/bin/colab" && echo "colab_found" || true
CHROME_MAC="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
test -x "$CHROME_MAC" && "$CHROME_MAC" --version || true
command -v google-chrome && google-chrome --version || true
```

2. 向用户简短汇报本机工具现状（同时提醒用户在 `agy` 内可随时输入 `/usage` 查看模型配额池是否已按 Pro / Ultra 档位生效，确认当前登录的就是开通订阅的主账号），并仅就用户尚未说明的信息确认分流条件（用户开场已告知的直接采用，不重复追问）：
   - 当前订阅是 `Google AI Pro ($20/月)`、`Ultra 5x ($100/月)` 还是 `Ultra 20x ($200/月)`（是否为主账号，非家庭组共享成员、免费试用期或学生会员）？是通过 Google 网页直接订阅还是苹果 App Store 内购订阅？
   - 账号下是否已经有以前建好的 Google Cloud 结算账号，或手头是否有支持跨境美元扣款的 `Visa / Mastercard / Amex` 实体卡？
     （分流规则：若账号已有现成云结算账号，先去开发者权益页检查能否直接绑定；若尚无云结算账号且无外币实体卡，或因苹果内购无法同步开发者权益，提前说明原因并**跳过阶段 2**，直接从阶段 3 处理无需绑云结算卡的其余 6 项福利。）
   - 若领取云与 Gemini API 赠金，计划用在哪里（接入自己的 AI 应用、全天候个人助理 Personal Bot、远程无头服务器上的 `agy`，还是只留着抵扣谷歌云服务而不调 Gemini API）？

## 4. 阶段 1：打通浏览器访问通道

检查你当前的可用工具声明列表（Tool Declarations），按实际能力连接浏览器（等待用户握手期间可先执行阶段 3 的纯本地 CLI 安装）：

- 形态 A：当前工具列表中已有内置 `browser` 工具（如 `Antigravity App` 桌面端个人账号 `v2.30.0+`，或已完成 `/browser` 握手的会话）
  直接调用 `browser` 打开目标网页（原生内置能力，无需额外安装任何浏览器 Skill）。若页面未登录订阅主账号，原位暂停请用户在浏览器标签页登录，回复确认后继续。

- 形态 B：运行在交互式 `Antigravity CLI`（`agy` 终端版），且当前会话尚未挂载 `browser` 工具
  `agy` 原生内置 `/browser` 斜杠命令（底层通过 `npx` 自动启动官方 Chrome DevTools MCP，无需另装浏览器 Skill），需本机已装 Node.js 且 Google Chrome 版本不低于 `144`：
  1. 若阶段 0 发现本机无 Node.js、无 Chrome 或当前处于 `--print` 等非交互模式，直接切换为「网页向导模式」：由你逐步给出下方精确网址与核对字段，用户点看后口头反馈，你负责本机 CLI 安装与验证。
  2. 若环境满足，请用户按两步完成一次性握手：
     - 在已登录该 Google 订阅主账号的 Chrome 地址栏访问：
       `chrome://inspect/#remote-debugging`
       勾选开启 `Allow remote debugging for this browser instance`；
     - 在当前 `agy` 输入框发送 `/browser 请继续按福利指南检查并配置我的 Google AI 订阅权益`，并在 Chrome 弹出的确认框中于 60 秒内点击 `Allow`。

## 5. 阶段 2：领取每月 `$10 / $40 / $100` 云与 Gemini API 赠金并验证

（对应福利 1：云与 Gemini API 月度赠金；若确认无可用云结算账号且无外币实体卡，或苹果内购无法同步该权益，跳至阶段 3。）

### 5.1 开发者权益页绑定与自动续领设置

1. 打开以下网址，确认当前登录的是开通订阅的主账号（家庭共享成员无此项赠金）：
   `https://me.developers.google.com/benefits`
2. 查找 `Google Cloud credits` 权益卡片（Pro 为 `$10/月`，Ultra 5x 为 `$40/月`，Ultra 20x 为 `$100/月`；每笔自入账起 365 天有效，按首次激活周年日按月发放，未使用余额跨月叠加滚存）。
3. 按页面实际状态分三路处理：
   - 已绑定结算账号且已勾选 `Always use this billing account`：记录状态，直接进入 `5.2`。
   - 已绑定结算账号但**未勾选** `Always use this billing account`（始终使用此结算账号）：补勾保存（漏勾会导致次月或升档后的新赠金留在权益页不自动入账），然后进入 `5.2`。
   - 尚未领取或未绑定结算账号：
     * 若账号下已有可用的 Google Cloud 结算账号，选中关联并勾选 `Always use this billing account`。
     * 若尚无云结算账号，点击新建进入支付资料页后**立即暂停**，请用户本人填写外币实体卡信息（不支持单标银联卡、虚拟卡与 Apple Pay；账单国家须与发卡地一致，可能有 `$1` 临时预授权）。用户建好后再回来完成绑定与勾选。

### 5.2 云结算后台核对赠金余额

打开以下网址，进入刚绑定的结算账号并查看左侧 `Credits`（赠金/抵用金）页：
`https://console.cloud.google.com/billing`

核实当月赠金（`$10 / $40 / $100`，若结算币种非美元则显示折算后的本币金额）已生效，记录可用总额与最早到期日。若用户在阶段 0 明确只把赠金用于谷歌云服务（不调用 Gemini API），本阶段到此即告完成，无需进入 `5.3` 预充值，可直接跳至阶段 3。

### 5.3 Google AI Studio 计费模式核查、防扣费限额与生成 API Key

1. 打开 `https://aistudio.google.com/app/apikey` 及对应的 Billing 页面，查看关联该云结算账号后的计费模式：
   - 若页面采用 `Prepay`（预充值）模式且尚未激活：首次启用通常要求刷卡实际支付一笔最小预充值（一般为 `$5.00`）。遇到支付确认页时**停下交由用户本人完成**，并说明激活后调用 Gemini API 会**优先抵扣**月度赠金，只要不超赠金余额，预充现金不会被扣减。
   - 若老结算账号显示为 `Postpay`（后付费按账单结算）模式：无需预充 `$5`，直接享受云结算账号内的月度赠金抵扣，但超额部分会按月向绑定的支付方式出账，更需配好下方限额。
2. 防超支安全设置（仅约束 AI Studio 下的 Gemini API 调用，不管控其他 GCP 云服务）：
   - 在 AI Studio Billing 页若存在 `Auto-reload`（自动充值）开关，确认其处于**关闭（Off）**状态；
   - 按当前可用赠金与用户预算，经用户确认后设置合理的月度 `Spend Cap`（月度支出上限；因统计拦截有 10 分钟以上延迟，Prepay 关自动充值加 `Spend Cap` 可最大限度防穿透扣卡；若用户另开 Cloud Run 等 GCP 服务，提醒在 Cloud Billing 另设 Budget 预算告警）。
3. 若尚未创建可用 Key，在关联同一云结算账号的 GCP 项目下生成 `GEMINI_API_KEY`。保存前先检查目标私有文件是否已存在（防覆盖既有 Key），经用户确认后保存（如 `~/.config/gemini/api_key`，执行 `chmod 600`）。

### 5.4 最小成本真机连通性验证（花费低于 `$0.0001`）

通过标准输入传递请求头（避免 Key 出现在子进程命令行参数中），使用当前在役轻量模型 `gemini-3.8-flash` 验证 Key 可用性与 HTTP 状态码（若用户改存其他路径，替换 `KEY_FILE`）：

```bash
KEY_FILE="$HOME/.config/gemini/api_key"
BASE="https://generativelanguage.googleapis.com/v1beta"
URL="$BASE/models/gemini-3.8-flash:generateContent"
BODY='{"contents":[{"parts":[{"text":"Reply OK"}]}]}'
OPTS=(-sS -w "\nHTTP_STATUS:%{http_code}\n" -H "Content-Type: application/json")
printf 'x-goog-api-key: %s\n' "$(tr -d '\r\n ' < "$KEY_FILE")" |
  curl "${OPTS[@]}" -H @- -d "$BODY" "$URL"
```

确认输出 `HTTP_STATUS:200` 且响应正文含 `OK`（这证明 API Key 有效且模型可调；账单抵扣明细通常有数十分钟延迟，分开如实记录「Key 连通通过 + 项目已绑对结算账号」即可）。

### 5.5 四大常用去处配置说明（严守 `agy` 认证互斥边界）

- 去处 1（接入自有 AI 应用与脚本）：从私有配置读取 `GEMINI_API_KEY` 注入运行环境，为自研产品、内部小工具或日常脚本直接提供官方 Gemini API 额度。
- 去处 2（接入全天候个人 Agent）：配给 24 小时待命的个人助理（Personal Bot），用于日常问答、信息汇总或定时自动化任务。
- 去处 3（云主机单独运行 `agy`）：**务必注意**，`agy` 的 Google 个人账号登录态**不支持**自带 API Key（BYOK）叠加配额，**切勿**在日常已登录 Google 个人订阅的本机 `~/.gemini/antigravity-cli/settings.json` 中写入 `"modelProvider": "gemini"`，否则会断开个人订阅配额会话！仅在**不便登录个人 Google 账号的远程服务器 / 云主机**上，才在远端机器单独配置：
  1. 在远端 `~/.gemini/antigravity-cli/settings.json` 顶层 JSON 对象中**合并写入** `"modelProvider": "gemini"`（保留原有其他字段，勿整文件覆盖）；
  2. 在远端通过权限 `600` 的私有文件加载环境变量 `GEMINI_API_KEY`（避免明文写进共享命令历史）。
- 去处 4（抵扣谷歌云轻量服务）：同一笔月度赠金也会自动优先抵扣该结算账号下的模型平台（`Vertex AI`）、轻量部署（`Cloud Run`）以及云存储与 CDN 加速（`Cloud Storage + Cloud CDN`）费用。

## 6. 阶段 3：安装官方 Colab CLI 与官方 Skill，核验 GPU 算力单元

（对应福利 2：GPU 算力单元 `200 / 1,000 / 2,000 CU`；免绑云结算卡，仅限订阅主账号可用，家庭共享成员、免费试用期与学生会员无此项权益。）

权益规格与实测基准：
- `Pro ($20)`：每月 `200 CU`（官方单买价 `$9.99 / 100 CU`，单买约值 `$20`），不支持关掉网页继续在后台运行；
- `Ultra 5x ($100)` / `Ultra 20x ($200)`：每月 `1,000 CU` / `2,000 CU`（单买约值 `$100` / `$200`），账号可见 `H100 / G4 / A100 / L4 / T4` 与 `TPU (v6e1 / v5e1)`，均支持**关掉网页后最长在后台继续运行 24 小时**；
- 真机拉起 `40G A100` 实测 `BF16` 矩阵吞吐达 `275.8 TFLOPS`，实时消耗速率为 `6.73 CU/小时`（比社区流传的 `5.38 CU/小时` 偏高，实际可用时长少近两成），三档每月新增额度分别折合约 `30 / 150 / 300` 小时 A100，主要用于模型训练、微调，或加载开源权重跑推理与实验。

### 6.1 安装官方 `google-colab-cli`（支持 macOS 与 Linux）

Windows 用户可在 WSL2 内执行，若 macOS/Linux 本机无 `uv`/`pipx` 安装环境或为纯 Windows，直接引导用户在浏览器打开 `https://colab.research.google.com` 右上角资源面板核对剩余计算单元余额。有 `uv` 或 `pipx` 时按需安装：

```bash
# 优先使用 uv（无 uv 则换用 pipx install google-colab-cli）
uv tool install google-colab-cli
```

确定可执行文件路径 `COLAB_BIN="$(command -v colab || echo "$HOME/.local/bin/colab")"`，运行 `"$COLAB_BIN" version` 确认可用（若 `~/.local/bin` 未进 `PATH`，顺手提示用户加入 Shell 配置）。

### 6.2 自动导出并挂载 Google 官方内置的 `colab-operator` Skill

`google-colab-cli` 自带官方编写的 Agent Skill。`Antigravity App` 与 `agy` CLI 共同读取的用户级技能目录为 `~/.gemini/config/skills/`（不要只写进 App 不读的 `antigravity-cli/skills/`）。用临时文件安全导出后再落位，防止命令失败截空原文件：

```bash
COLAB_BIN="$(command -v colab || echo "$HOME/.local/bin/colab")"
# agy App 与 CLI 共用目录（Codex 改 ~/.agents/skills，Claude 改 ~/.claude/skills）
SKILL_DIR="$HOME/.gemini/config/skills/colab-operator"
mkdir -p "$SKILL_DIR"
TMP_SKILL="$(mktemp)"
"$COLAB_BIN" skill > "$TMP_SKILL"
test -s "$TMP_SKILL" && mv "$TMP_SKILL" "$SKILL_DIR/SKILL.md"
```

（若目标 `SKILL.md` 此前已存在且内容不同，替换前先征得用户同意。）

### 6.3 首次交互授权与零消耗核验算力单元余额（必做）

1. 检查本地 OAuth 凭据文件是否存在（仅测存在性，**严禁**读取内容）：

```bash
test -f "$HOME/.config/colab-cli/token.json" && echo "HAS_TOKEN" || echo "NO_TOKEN"
```

2. **关键防卡死机制**：`google-colab-cli` 当前版本默认走 `--auth oauth2` 远程粘贴授权码流程（打印授权链接并在终端 `stdin` 等待粘贴授权码，并非本地端口自动回调）。若输出 `NO_TOKEN`（或后续运行报 `401` 令牌失效），**Agent 切勿在后台直接运行阻塞等待**，而是请用户自己在系统终端窗口里按 `6.1` 探到的路径手动执行一次（如 `~/.local/bin/colab usage`），由用户在浏览器打开终端打印的 Google 授权链接、点允许后把显示的授权码直接粘贴回该终端（授权码全程不经过聊天窗口，完成后会自动生成 `~/.config/colab-cli/token.json`）。
3. 凭据就绪后，由 Agent 运行只读核验：

```bash
COLAB_BIN="$(command -v colab || echo "$HOME/.local/bin/colab")"
"$COLAB_BIN" usage
"$COLAB_BIN" sessions
```

记录返回的 `Current balance`（单笔 CU 有效期 90 天，含往月结余时余额可高于当月标准发放量）、`Usage rate` 与当前已有的活跃会话列表。

### 6.4 可选：专属命名会话 10 秒 GPU 冒烟验证（约耗 `0.02 CU`）

先询问用户是否需要拉起一张云端 GPU 跑一次 10 秒连通测试。仅在用户同意后执行：
1. 先记录测试前已有的活跃会话列表（保护用户可能正在跑的训练任务）；
2. 为本次测试指定唯一会话名 `agy-perk-smoke`（**切勿**加 `--keep` 参数）：

```bash
COLAB_BIN="$(command -v colab || echo "$HOME/.local/bin/colab")"
SMOKE_PY="$(mktemp /tmp/colab_gpu_smoke.XXXXXX.py)"
echo 'import subprocess as s' > "$SMOKE_PY"
echo 'print(s.check_output(["nvidia-smi", "-L"], text=True))' >> "$SMOKE_PY"
"$COLAB_BIN" run -s agy-perk-smoke --gpu A100 "$SMOKE_PY"
rm -f "$SMOKE_PY"
```

（若当刻机房 `A100` 库存紧缺报错，先运行 `"$COLAB_BIN" sessions` 确认 `agy-perk-smoke` 未残留后，可改试 `--gpu L4` 或 `--gpu T4`。）

销毁复核（红线，每次必跑）：测试结束后**必须**立即执行：

```bash
COLAB_BIN="$(command -v colab || echo "$HOME/.local/bin/colab")"
"$COLAB_BIN" sessions
"$COLAB_BIN" usage
```

- 验证标准：`agy-perk-smoke` 已不在活跃会话列表中（若仍在，立即运行 `"$COLAB_BIN" stop -s agy-perk-smoke` 仅关停该测试会话；若测试前账号本就无任务，确认 `Usage rate: 0.00/hr`；若测试前用户本就有在跑的会话，确认原会话未受影响且费率恢复到测试前水平）。

## 7. 阶段 4：其余 5 项现役福利核验与常见过时说法勘误

以下福利均无需绑定 Google Cloud 结算信用卡，通过浏览器只读访问帮用户逐项核对到账情况与使用边界：

1. 福利 3：Veo 视频生成（`Google Flow`，每月 `1,000 / 10,000 / 25,000 积分`）
   - 打开以下网址查看右上角月度积分余额（暂无官方 CLI，走网页端使用）：
     `https://labs.google/fx/tools/flow`
   - 换算与清晰度口径：按 8 秒有声视频计，`Veo 3.1 Quality` 精制档扣 `100` 积分/条，`Omni Flash 1.2` 扣 `24` 积分/条，`Veo 3.1 Fast` 在 Pro 扣 `20` 积分/条、在 Ultra 扣 `10` 积分/条，因此三档每月分别折合 `10～50` 条（Pro 最高输出 `1080p`）、`100～1,000` 条、`250～2,500` 条；`5x` 与 `20x` 支持生成 `4K` 视频（`1～10` 秒视频转 `4K` 另加扣 `50` 积分）。

2. 福利 4：AI 音乐生成（`Google Flow Music`，每月 `10,000 / 30,000 / 30,000 积分`）
   - 与视频 Flow 分属两套独立服务、各自独立计分。打开以下网址核对独立音乐积分余额：
     `https://flowmusic.app`
   - `10,000` 积分约合 `2,000` 首、`30,000` 积分约合 `6,000` 首，基于 `Lyria 3.5` 支持人声/器乐生成与分轨，含商用免版权授权（走网页端使用）。

3. 福利 5：`Gemini Notebook`（原 `NotebookLM`，单本 `300 / 500 / 600` 源）
   - 打开以下网址查看右上角头像旁的升级标识，或点开任一笔记本的 `Add sources` 查看单本常规来源上限：
     `https://notebooklm.google.com`
   - 分档核验标准：免费版仅 `50` 源；`Pro` 为 `300` 源；`Ultra 5x（20 TB）` 为 `500` 源（笔记类来源 `600`）；`Ultra 20x（30 TB）` 为 `600` 源，三档付费版均支持最多 `500` 个笔记本。

4. 福利 6：`Google 云盘`（Google One `5 TB / 20 TB / 30 TB` 免流量费存储）
   - 依次核对存储总容量与最多 6 人（主号 + 5 位成员）家庭共享设置：
     `https://one.google.com/storage`
     `https://one.google.com/settings`
     （提醒：云盘、日常 AI 与 Ultra 的 YouTube 完整家庭版可家庭共享，云与 API 赠金、Colab 算力归主号独享；Google Drive 无按 GB 出站流量费。）
   - 用法与外链避坑：① Colab 代码内运行 `from google.colab import drive; drive.mount('/content/drive')`（或由人类用户在本地交互终端运行 `colab drivemount -s <name>`；注意 `colab drivemount`、`colab repl`、`colab console` 需交互 TTY，**Agent 切勿在后台直调以防挂起**）走谷歌机房内网读写百 GB 模型权重；② 存 `4K/8K` 未压缩录屏原片归档，或配 `rclone` 定时加密冷备；③ **切勿**把网盘公开分享链接当网站视频播放器或公开媒体源，几人同看即触发 `HTTP 403` 锁死 24 小时（对外发视频用阶段 2 的云赠金配云存储与 CDN 加速 `Cloud Storage + Cloud CDN` 即可）。

5. 福利 7：`YouTube 会员`（Pro 含 `Premium Lite 个人版` / `5x & 20x` 含 `完整家庭版`）及其他限定项
   - 先打开以下两个页面核对当前会员生效状态（仅在部分支持国家/地区提供，以页面实际显示为准）：
     `https://www.youtube.com/paid_memberships`
     `https://one.google.com/benefits`
   - 分档与冲突规则：符合地区条件的付费 `Pro` 主号在无既有独立 YouTube 会员时，`YouTube Premium Lite` 个人版会**自动生效**（大多数非音乐视频免广告，不含 YouTube Music 且不可家庭共享）；`Ultra 5x / 20x` 在支持地区已由官方自动升级为 **`YouTube Premium Family` 完整家庭版**（全站免广告、后台播放、离线下载与 YouTube Music，主号作为家庭组管理员可与最多 5 位家庭成员共 6 人共享）。若检测到账号已有单独付费的 YouTube 会员导致未叠加生效，**仅向用户如实报告冲突现状，不要擅自引导取消原有订阅**。
   - 其他限定项：Ultra 另附 `Project Genie`（Genie 3 实时 3D 世界，单次 60 秒）及美印等地区限定的 `Chrome 自动浏览` / 代打电话实验功能，`Google Home Premium`（需 Nest 设备）与 `Fitbit / Health Premium`（需 Pixel Watch / Fitbit）按硬件持有情况标注。

### 2026 年旧福利下线勘误

同时告知用户，社区过时攻略仍常提的两项旧福利的真实状态：
- 「赠送 30 个 Firebase Studio 工作区」：已于 2026 年 6 月 22 日停止新建工作区与注册，正式关停时间为 2027 年 3 月 22 日；
- 面向个人、Google AI Pro 与 Ultra 账号的「个人版 Gemini Code Assist IDE 插件 / 旧 Gemini CLI」：已于 2026 年 6 月 18 日停服。

官方已统一迁入 `AI Studio` 与 `Antigravity`（VS Code / JetBrains 可直接装 `Antigravity` 官方扩展），无需再找。

## 8. 阶段 5：输出本账号全量福利验收总表

全部检查与配置完成后，按以下 7 项现役福利 + 扩展项向用户输出汇总表：
1. `云与 Gemini API 赠金`（结算账号绑定与自动续领勾选状态、可用赠金余额与到期日、计费模式 Prepay/Postpay 与 `Auto-reload`/`Spend Cap` 状态、API Key 连通状态与保存路径）；
2. `GPU 算力单元（Colab）`（CLI 与 `~/.gemini/config/skills/colab-operator` 安装状态、剩余 `CU` 余额、测试实例已清理确认）；
3. `Veo 视频（Google Flow）`（当月视频积分余额）；
4. `AI 音乐（Google Flow Music）`（当月独立音乐积分余额）；
5. `Gemini Notebook（原 NotebookLM）`（套餐标识与单本来源上限：`300` / `500` / `600`）；
6. `Google 云盘`（总存储容量 `5 TB / 20 TB / 30 TB` 及家庭共享状态）；
7. `YouTube 会员与扩展限定项`（当前生效状态、家庭管理员共享条件、地区/硬件匹配情况，或既有订阅冲突说明）。
