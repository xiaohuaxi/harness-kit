[English](README.md) | **简体中文**

# claude-repo-memory-setup

单目录、零第三方依赖的跨 Agent Skill 与 Bash 工具：将项目默认的 Claude Code 自动记忆（`<claude-dir>/projects/<encoded>/memory`）安全迁入 `<repo>/.agents/memory/` 并原位建立软链回指。仅处理 `memory` 目录，同级会话转录、file-history 与 checkpoints 保持不动。

## 环境要求

- **操作系统**：POSIX 系统（macOS、Linux 或 WSL2）。
- **Shell**：Bash 3.2+（已在 macOS `/bin/bash` 3.2.57 与 Ubuntu `bash` 5.1.16 实测通过），零外部运行时依赖，仅使用标准 POSIX 工具（`git`/`svn` 按需使用）。
- **测试**：Python 3.8+ 标准库（零第三方包）。

## 与 Claude Code 原生 `autoMemoryDirectory` / `CLAUDE_CODE_PROJECT_DIR_NAME` 的对比

Claude Code 原生支持在 `.claude/settings.local.json`（或受信任工作区的 `.claude/settings.json`）配置 `autoMemoryDirectory`，也支持通过 `CLAUDE_CODE_PROJECT_DIR_NAME` 覆盖默认项目目录键。

- **何时原生配置更简单**：单机全新仓库，仅需将记忆指向一个固定的绝对路径或 `~/` 路径且没有历史记忆待迁移时，直接配置 `autoMemoryDirectory` 更省事，无需维护 `~/.claude/projects/` 软链。
- **何时选择 `claude-repo-memory-setup`**：
  1. **多机不同克隆路径共享同一份入库记忆**：Claude Code 要求 `autoMemoryDirectory` 必须填写**绝对路径或 `~/` 路径**（不接受 `.agents/memory` 这样的仓库相对路径），因此无法在入库的 `.claude/settings.json` 中写一条对所有机器不同克隆目录都生效的路径。在每台机器克隆后运行一次本工具，即可把本机默认的 `<claude-dir>/projects/<encoded>/memory` 软链到版本化的 `<repo>/.agents/memory`。
  2. **老仓历史记忆安全迁移**：修改 `autoMemoryDirectory` 只改变后续读写目标，不会迁移已有的 `~/.claude/projects/<encoded>/memory/` 文件。本工具提供 `cp -a` + `diff -rq` 校验、不覆盖单轮备份、编码碰撞拦截、写透探针与隔离提交。
  3. **零本地配置漂移与 Git worktree 自动统一**：无需在每个克隆或 worktree 维护 `.claude/settings.local.json`。Claude Code v2.1.63+ 会自动将标准 Git 仓库的子目录和 linked worktree 归并回主仓根的默认记忆目录，在主仓根运行一次即可覆盖所有子目录与临时 worktree。
- **适用前提与核验**：面向 Claude Code 默认记忆布局（未设置 `CLAUDE_CODE_PROJECT_DIR_NAME` 且未用 `autoMemoryDirectory` 重定向）。运行期间请暂停目标项目中正在写入记忆的 Claude Code 会话；完成后可在 Claude Code 中执行 `/memory` 核验实际加载路径。

## 安装方式

将 `claude-repo-memory-setup` 目录复制或软链到对应宿主的 skills 目录：

| 宿主 Agent | Skill 目录 |
| --- | --- |
| Claude Code | `~/.claude/skills/claude-repo-memory-setup` |
| Codex | `~/.agents/skills/claude-repo-memory-setup` |
| Antigravity（`agy`） | `~/.gemini/config/skills/claude-repo-memory-setup` |

示例：

```bash
mkdir -p ~/.claude/skills
cp -R skills/claude-repo-memory-setup ~/.claude/skills/claude-repo-memory-setup
```

- **Codex 用户提示**：请保留目录内的 `agents/openai.yaml`，以维持 `policy.allow_implicit_invocation: false`（防止隐式自动触发）。
- **可选：软链到 `PATH` 供任意终端直接调用**：

```bash
mkdir -p ~/.local/bin
ln -s "$HOME/.claude/skills/claude-repo-memory-setup/claude-repo-memory-setup.sh" ~/.local/bin/claude-repo-memory-setup
```

## 使用方式

### CLI 直接运行

```bash
# 对当前目录或指定仓库路径执行迁移与建链
claude-repo-memory-setup
claude-repo-memory-setup /path/to/repo

# 路径以 '-' 开头时先传 '--'
claude-repo-memory-setup -- -my-repo

# 只读预检（检查 Git 边界、编码、长度与目录安全，零写盘）
claude-repo-memory-setup --check-only /path/to/repo

# 原始路径编码调试（不解析软链；非法或超过 200 字符时 stdout 为空并退出 1）
claude-repo-memory-setup --encode /path/to/repo

# 覆盖 Claude 配置目录（优先级：CLAUDE_HOME > CLAUDE_CONFIG_DIR > $HOME/.claude）
CLAUDE_CONFIG_DIR=/custom/claude claude-repo-memory-setup /path/to/repo
```

### Skill 显式调用

本 Skill 在三端均关闭自动触发，仅支持显式调用：

- Claude Code / Antigravity（`agy`）：`/claude-repo-memory-setup`
- Codex：`$claude-repo-memory-setup`

### 从旧名 `repo-memory-setup` 迁移

如果你此前使用过旧名 `repo-memory-setup`：

- CLI 命令更名：`repo-memory-setup` → `claude-repo-memory-setup`
- Skill 显式调用更名：`/repo-memory-setup` & `$repo-memory-setup` → `/claude-repo-memory-setup` & `$claude-repo-memory-setup`
- 已经建立并生效的 `<claude-dir>/projects/<encoded>/memory → <repo>/.agents/memory` 软链继续有效，**无需重新迁移**。

## 安全护栏与边界

- **Git 与 SVN 边界 fail-closed 防护（拒绝前零写盘）**：
  - **支持**：标准 Git 主仓根目录（仓根存在 `.git/`）、SVN 工作副本根目录与普通非版本库目录。
  - **拒绝（`exit 1`）**：Git 子目录、linked Git worktree（根目录或子目录）、bare 仓库、`.git/` 元数据目录、Git submodule（含 gitfile 与旧式实体 `.git/` 布局）、`--separate-git-dir` 仓库、存在 `.git` 祖先标记但无法被 `git` 验证的目录、SVN 工作副本子目录与 `.svn/` 元数据目录（或存在祖先 `.svn` 但无法被 `svn` 验证的目录），以及物理路径包含换行符的目录。对 Git/SVN 子目录和可经 Git 二次反查核实主根的 linked worktree，错误信息会给出应重跑的根目录路径。
- **编码、Locale 与 200 字符上限**：
  - 将非 ASCII 字母数字的字符统一替换为单个 `-`（保留大小写、不折叠连续 `-`）。
  - 编码超过 200 字符、含 4 字节 UTF-8 字符（如 emoji，在 JS 中按两个 UTF-16 码元编成两个 `-`）、或字节型 `C`/`POSIX` locale 下含非 ASCII 字符的路径，均在写盘前拦截退出。
  - 检测到 `CLAUDE_CODE_PROJECT_DIR_NAME` 非空时直接拒退，避免与覆盖后的项目键失配。
- **数据安全、失败回滚与单轮备份清理**：
  - 拦截 `.agents` / `.agents/memory` 软链越界、全局与仓内路径重叠（含大小写不敏感卷别名及不存在目录段之后的 `.` / `..`）、任一端记忆目录内的软链或特殊文件（含新克隆、重跑与 `--check-only`）、不可读目录以及双端非空冲突（统一使用 `diff -rq`，绝不将记忆正文输出到 `stderr`）。
  - 若在全局 `memory` 改名备份为 `memory.bak*` 之后、创建软链之前失败或收到中断信号，`EXIT` 处理器会在原位置仍空闲时自动将备份改回原路径，避免留下缺失入口。
  - 绝不删除历次运行或用户手工留存的 `memory.bak*`；仅清理本轮创建的备份：空目录仅用 `rmdir`，非空迁移在删前再次执行 `diff -rq` 复核（无版本库的普通目录复核一致即清理；Git/SVN 仓库还须确认版本库已跟踪普通文件）。若 `add` 受阻（如目录被忽略），保留备份、输出 `WARN` 并退出 `0`；若提交失败、规则导致 0 个普通文件被跟踪，或迁移后 `diff -rq` 不一致，则保留备份并退出 `1`。
  - Git 提交通过 `git commit -m ... -- .agents/memory/` 限定路径，不卷入工作区其他已暂存或未暂存改动；SVN 会先以 `--depth empty` 提交新建的 `.agents` 目录，再提交 `.agents/memory` 至中央仓库。对于本轮未迁移非空全局记忆的运行（如已建链重跑），若 `.agents/memory/` 已有未提交改动，则保留暂存区与工作区不变、输出 `WARN` 并退出 `0`，不顺带暂存或提交。

## 给 Codex / Antigravity（`agy`）用户的按需查阅配置片段

Codex 与 Antigravity（`agy`）均不会自动加载 `.agents/memory/`。如需让它们在日常任务中按需查阅入库记忆，可将以下简短说明粘贴进项目 `AGENTS.md` 或用户级全局规范：

```markdown
## 项目知识库（Claude Code Memory）

项目长期记忆版本化存放在 `<repo>/.agents/memory/MEMORY.md`（同目录存放各主题笔记）。先扫 `MEMORY.md` 索引，命中再按需读取对应主题文件；若仓库尚未将记忆迁入 `.agents/memory/`，请查看 `${CLAUDE_CONFIG_DIR:-~/.claude}/projects/` 或在 Claude Code 中运行 `/memory` 确认实际目录。
```

## 运行测试

```bash
# 单文件直跑隔离测试
python3 skills/claude-repo-memory-setup/tests/test_claude_repo_memory_setup.py -v

# 或通过 unittest 目录发现运行
python3 -m unittest discover -s skills/claude-repo-memory-setup/tests -v
```

本工具集的其他公开模块见 [harness-kit](https://github.com/xiaohuaxi/harness-kit)。
