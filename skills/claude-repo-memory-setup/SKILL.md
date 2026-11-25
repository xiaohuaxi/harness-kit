---
name: claude-repo-memory-setup
description: Migrate a repository's Claude Code auto-memory into .agents/memory/ and symlink it back; explicit invocation only, never auto-trigger.
disable-model-invocation: true
---

# claude-repo-memory-setup

Run the setup script to move a project's default Claude Code auto-memory (`<claude-dir>/projects/<encoded>/memory`) into `<repo>/.agents/memory/` and symlink it back. Only `memory` is touched; sibling session transcripts, file history, and checkpoints remain untouched. Pause active Claude Code memory writes in the target project before running.

## Invocation

Prefer `claude-repo-memory-setup` if it is on `PATH`; otherwise run `claude-repo-memory-setup.sh` from this skill's directory:

```bash
claude-repo-memory-setup [project-path]
# or, when not linked onto PATH:
<skill-dir>/claude-repo-memory-setup.sh [project-path]
```

- Project path: defaults to `$PWD` and resolves to its physical path before encoding. Pass `--` before a path starting with `-`.
- Idempotent, no `--force`: safe to re-run; never overwrites conflicting files or removes backups from previous runs.
- Config directory override: resolved in order `CLAUDE_HOME` (script-specific override) > `CLAUDE_CONFIG_DIR` > `$HOME/.claude`. If `CLAUDE_CODE_PROJECT_DIR_NAME` is set, the script exits `1` without writing because Claude Code overrides the default path-encoded project key. This script manages only Claude Code's default project memory layout (if custom `autoMemoryDirectory` is configured in settings, verify the active path in Claude Code via `/memory`).
- Helper subcommands: `--encode <path>` encodes the raw string without resolving symlinks (and exits `1` with empty stdout if invalid or >200 chars); `--check-only [project-path]` runs read-only boundary, locale, and encoding preflight checks without writing.

## Git & SVN Boundary & Encoding Guards

- Supported targets: standard Git main repository roots (`.git/` at the repo root), SVN working copy roots, and plain non-VCS directories (using the target's physical path).
- Rejected targets (exit `1`, zero filesystem writes): Git subdirectories, linked Git worktrees, bare repositories, `.git` metadata directories, Git submodules (both gitfile and legacy real-`.git/` layouts), `--separate-git-dir` repositories (whose Claude Code project-key derivation varies across versions), any path with a `.git` marker that `git` cannot verify, SVN working copy subdirectories / `.svn` metadata directories (or ancestor `.svn` paths that `svn` cannot verify), and physical paths containing newline characters. For Git/SVN subdirectories and verifiable linked Git worktrees, the error prints the root path to re-run on.
- Encoding & length limits: non-ASCII-alphanumeric characters map to single `-` (case preserved, consecutive `-` kept). Paths exceeding 200 encoded characters, paths containing 4-byte UTF-8 characters (such as emoji, which encode as two UTF-16 code units in JS), or non-ASCII paths under a byte-oriented `C`/`POSIX` locale exit `1` before writing.

## Migration & Safety Guardrails

- Collision & containment checks: `.agents` and `.agents/memory` must not be symlinks escaping the repository, and the global and repo memory paths must not overlap (checked both lexically and via inode identity across case-insensitive filesystems; `.` / `..` after non-existent segments are rejected). If global `memory` is already a symlink to another existing directory, the script warns and exits `1`; same-inode symlinks are skipped, dangling symlinks are repaired.
- Regular files only: before linking or migrating (including fresh clones, reruns, and `--check-only`), if either memory directory contains symlinks or special files (FIFO/socket), the script exits `1` without copying or deleting backups.
- Existing memory migration: unreadable directories fail immediately. An empty global directory is moved to a non-clobbering backup (`memory.bak`, `memory.bak2`, ...), symlinked, and removed with `rmdir` only (preserved with a warning if concurrent writes appear). A non-empty global directory with an empty `.agents/memory/` is copied with `cp -a`, verified with `diff -rq`, backed up, symlinked, verified with a unique `mktemp` write-through probe, and re-checked against the backup before removing only this run's backup. If `ln -s` fails or is interrupted after backup rename, the `EXIT` handler restores the backup back to global `memory` whenever that path is still unoccupied. If both directories are non-empty and differ, the script exits `1` without overwriting anything.

## VCS Commit & Exit Handling

When `.git` or `.svn` is present at the repository root, the script scopes its commit to `.agents/memory/`:
- Git: `git add -- .agents/memory/` and pathspec-scoped `git commit -m ... -- .agents/memory/`, leaving other staged or unstaged changes untouched; skips empty commits.
- SVN: commits `.agents/memory` (committing a newly added `.agents` directory itself first with `--depth empty`). `svn commit` pushes directly to the central repository, and a fresh repo may still produce a directory commit even when `.agents/memory/` is empty.
- Pre-existing edits on non-migration runs: when no non-empty global memory was migrated in this run (e.g., idempotent rerun or linking a clone) and `.agents/memory/` already has uncommitted Git/SVN changes, the script leaves the index and working tree untouched, emits `WARN`, and exits `0`.

How to handle results and backups:
- Exit `0` without `WARN`: migration/idempotency succeeded (no VCS, empty Git memory with no files to commit, or tracked memory verified). Only this run's backup is removed; pre-existing `memory.bak*` directories are left untouched.
- Exit `0` with `WARN`: inspect the exact warning (commonly VCS blocked because `.agents/memory/` is ignored, `add` failed, pre-existing uncommitted changes exist in `.agents/memory/`, or `rmdir` found newly written files in an expected-empty backup). Report the warning and any preserved backup path to the user without editing ignore files automatically.
- Non-zero exit: inspect which step failed, the reported symlink state, and any preserved backup path (failure may occur before any writes, mid-migration during copy/link/probe, or after verified migration+link due to commit failure, post-backup diff mismatch, or 0 tracked memory files under ignore rules). Report the exact failure and preserved paths to the user; never force-overwrite files or delete `memory.bak*` until the user confirms memory is safely committed.
