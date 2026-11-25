**English** | [简体中文](README.zh-CN.md)

# claude-repo-memory-setup

A self-contained, zero-dependency skill and Bash utility that moves a project's default Claude Code auto-memory (`<claude-dir>/projects/<encoded>/memory`) into `<repo>/.agents/memory/` and symlinks it back. Only `memory` is moved; sibling session transcripts, file history, and checkpoints stay untouched.

## Requirements

- **OS**: POSIX (macOS, Linux, or WSL2).
- **Shell**: Bash 3.2+ (tested on macOS `/bin/bash` 3.2.57 and Ubuntu `bash` 5.1.16), zero external runtime dependencies beyond standard POSIX utilities (`git`/`svn` optional).
- **Tests**: Python 3.8+ standard library only (no third-party packages).

## Comparing with Native `autoMemoryDirectory` and `CLAUDE_CODE_PROJECT_DIR_NAME`

Claude Code natively supports configuring `autoMemoryDirectory` in `.claude/settings.local.json` (or trusted project `.claude/settings.json`) and overriding the project directory key with `CLAUDE_CODE_PROJECT_DIR_NAME`.

- **When native `autoMemoryDirectory` is simpler**: On a single machine with a brand-new repository and no existing memory to migrate, pointing `autoMemoryDirectory` at a fixed absolute or `~/` path avoids managing any symlinks under `~/.claude/projects/`.
- **When `claude-repo-memory-setup` is the better fit**:
  1. **Multi-machine sharing across different clone paths**: Claude Code requires `autoMemoryDirectory` to be an **absolute path or `~/` path** (repository-relative paths such as `.agents/memory` are not accepted), so a committed `.claude/settings.json` cannot express one path that works across every machine's checkout location. Running `claude-repo-memory-setup` once per clone links each machine's default `<claude-dir>/projects/<encoded>/memory` to the versioned `<repo>/.agents/memory`.
  2. **Safe migration of existing project memory**: Changing `autoMemoryDirectory` only redirects future reads/writes and does not migrate existing `~/.claude/projects/<encoded>/memory/` files. This script migrates them with `cp -a`, `diff -rq` verification, non-clobbering per-run backups, collision detection, write-through probe, and pathspec-scoped VCS commits.
  3. **Zero local config drift & automatic Git worktree sharing**: No per-clone `.claude/settings.local.json` needed. Claude Code v2.1.63+ automatically shares the main repository root's default auto-memory across all subdirectories and linked Git worktrees, so setting up the main repository root once covers every subdirectory and temporary worktree.
- **Prerequisites & verification**: Designed for Claude Code's default memory layout (without `CLAUDE_CODE_PROJECT_DIR_NAME` or a custom `autoMemoryDirectory` override). Pause active Claude Code memory writes in the target project while running, and verify the active directory afterward in Claude Code with `/memory`.

## Installation

Copy or symlink this `claude-repo-memory-setup` directory into your agent's skills directory:

| Agent | Skill Directory |
| --- | --- |
| Claude Code | `~/.claude/skills/claude-repo-memory-setup` |
| Codex | `~/.agents/skills/claude-repo-memory-setup` |
| Antigravity (`agy`) | `~/.gemini/config/skills/claude-repo-memory-setup` |

Example:

```bash
mkdir -p ~/.claude/skills
cp -R skills/claude-repo-memory-setup ~/.claude/skills/claude-repo-memory-setup
```

- **Codex users**: keep `agents/openai.yaml` inside the skill directory so `policy.allow_implicit_invocation: false` stays active (preventing implicit auto-triggering).
- **Optional CLI symlink on `PATH`**:

```bash
mkdir -p ~/.local/bin
ln -s "$HOME/.claude/skills/claude-repo-memory-setup/claude-repo-memory-setup.sh" ~/.local/bin/claude-repo-memory-setup
```

## Usage

### Direct CLI

```bash
# Run on the current directory or an explicit repository path
claude-repo-memory-setup
claude-repo-memory-setup /path/to/repo

# Pass '--' before paths starting with '-'
claude-repo-memory-setup -- -my-repo

# Read-only preflight check (zero filesystem writes)
claude-repo-memory-setup --check-only /path/to/repo

# Inspect raw path encoding (no symlink resolution; exits 1 with empty stdout if invalid or >200 chars)
claude-repo-memory-setup --encode /path/to/repo

# Override Claude config directory (precedence: CLAUDE_HOME > CLAUDE_CONFIG_DIR > $HOME/.claude)
CLAUDE_CONFIG_DIR=/custom/claude claude-repo-memory-setup /path/to/repo
```

### Explicit Skill Invocation

This skill disables model auto-invocation across all three agents and is invoked explicitly:

- Claude Code / Antigravity (`agy`): `/claude-repo-memory-setup`
- Codex: `$claude-repo-memory-setup`

### Migrating from `repo-memory-setup`

If you previously used `repo-memory-setup`:

- CLI command renamed: `repo-memory-setup` -> `claude-repo-memory-setup`
- Skill invocation renamed: `/repo-memory-setup` & `$repo-memory-setup` -> `/claude-repo-memory-setup` & `$claude-repo-memory-setup`
- Existing `<claude-dir>/projects/<encoded>/memory -> <repo>/.agents/memory` symlinks remain completely valid and do **not** need to be re-run.

## Safety Guardrails & Boundaries

- **Git & SVN boundary enforcement (fail-closed, zero writes before rejecting)**:
  - **Allowed**: Standard Git main repository roots (`.git/` at the repo root), SVN working copy roots, and plain non-VCS directories.
  - **Rejected (exit `1`)**: Git subdirectories, linked Git worktrees (root or subdirectory), bare repositories, `.git/` metadata directories, Git submodules (both gitfile and legacy real-`.git/` layouts), `--separate-git-dir` repositories, any directory with a `.git` ancestor marker that `git` cannot verify, SVN working copy subdirectories / `.svn/` metadata directories (or ancestor `.svn` paths that `svn` cannot verify), and physical paths containing newline characters. For Git/SVN subdirectories and verifiable linked Git worktrees, the error prints the root path to re-run on.
- **Encoding, locale, and length guards**:
  - Replaces every non-ASCII-alphanumeric character with `-` (case preserved, consecutive `-` kept).
  - Refuses paths exceeding 200 encoded characters, paths containing 4-byte UTF-8 characters (e.g., emoji, which encode as two UTF-16 code units in JavaScript), and non-ASCII paths under a byte-oriented `C`/`POSIX` locale.
  - Refuses to run when `CLAUDE_CODE_PROJECT_DIR_NAME` is set.
- **Data integrity, rollback, and single-run backup cleanup**:
  - Rejects symlinks escaping `.agents` / `.agents/memory`, overlapping global/repo paths (including case-insensitive aliases and `.` / `..` after non-existent segments), symlinks/special files inside either memory directory (including on fresh clones, reruns, and `--check-only`), unreadable directories, and conflicting non-empty directories on both sides (using `diff -rq` so memory contents are never printed to `stderr`).
  - If symlink creation fails or is interrupted right after moving global `memory` to `memory.bak*`, the `EXIT` handler restores the backup back to global `memory` when that path is still unoccupied.
  - Never touches pre-existing `memory.bak*` directories from prior runs; cleans only this run's backup: `rmdir` for an empty global directory, or `rm -rf` after a final post-migration `diff -rq` check (for plain non-VCS directories, once `diff -rq` passes; for Git/SVN repositories, only after VCS tracking of regular files is also verified). If `add` is blocked (e.g., ignored directory), the backup is preserved with `WARN` and exit `0`; if commit fails, 0 regular files are tracked under ignore rules, or post-migration `diff -rq` mismatches, the backup is preserved and the script exits `1`.
  - Scopes Git commits strictly with `git commit -m ... -- .agents/memory/` so unrelated staged or unstaged changes are untouched. SVN commits `.agents/memory` (and a newly added `.agents` directory first with `--depth empty`) directly to the central repository. On non-migration runs (such as idempotent reruns), pre-existing uncommitted changes inside `.agents/memory/` are left untouched with a `WARN` rather than auto-staged or committed.

## Optional Snippet for Codex & Antigravity (`agy`) Users

Neither Codex nor Antigravity (`agy`) loads `.agents/memory/` automatically. If you want them to consult the versioned repository memory on demand, add a short pointer to your repository `AGENTS.md` (or global rules):

```markdown
## Project Memory (Claude Code)

Long-term project notes are versioned in `<repo>/.agents/memory/MEMORY.md` (with topic files alongside it). Scan the `MEMORY.md` index first and read matching topic files on demand. If a repository has not migrated its memory into `.agents/memory/` yet, check `${CLAUDE_CONFIG_DIR:-~/.claude}/projects/` or verify the active path in Claude Code via `/memory`.
```

## Running Tests

```bash
# Run the self-contained test suite directly
python3 skills/claude-repo-memory-setup/tests/test_claude_repo_memory_setup.py -v

# Or via unittest discovery
python3 -m unittest discover -s skills/claude-repo-memory-setup/tests -v
```

For other modules in this kit, see [harness-kit](https://github.com/xiaohuaxi/harness-kit).
