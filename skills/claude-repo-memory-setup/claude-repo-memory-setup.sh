#!/usr/bin/env bash
set -euo pipefail

warns=()
tmp_files=()
git_canonical_root=""
rollback_global_memory=""
needs_link_rollback=0
repo_memory_existed_before=0
migrated_nonempty_global=0
vcs_action="not detected"
vcs_kind="none"
vcs_blocked=""
commit_state=""
commit_note="not run"
bak_note="none"
backup_path=""
backup_was_empty=0
post_backup_mismatch=0

readonly MAX_ENCODED_LEN=200
readonly COMMIT_MSG="chore: localize Claude Code auto-memory into repo (claude-repo-memory-setup)"

say() { printf '%s\n' "$*"; }
step() { say "==> $*"; }
warn() { warns+=("$*"); say "WARN: $*" >&2; }
die() { say "ERROR: $*" >&2; exit 1; }

rollback_pending_link() {
  if (( ! needs_link_rollback )); then
    return 0
  fi
  needs_link_rollback=0
  if [[ -n "$backup_path" && -n "$rollback_global_memory" && ( -e "$backup_path" || -L "$backup_path" ) ]]; then
    if [[ ! -e "$rollback_global_memory" && ! -L "$rollback_global_memory" ]]; then
      if mv -- "$backup_path" "$rollback_global_memory" 2>/dev/null; then
        say "Restored original global memory from backup: $rollback_global_memory" >&2
      else
        say "WARN: Failed to restore backup ($backup_path) to $rollback_global_memory; backup preserved." >&2
      fi
    else
      say "WARN: Global memory path ($rollback_global_memory) is now occupied; preserved both paths (backup: $backup_path)." >&2
    fi
  fi
}

cleanup_tmp() {
  local f
  if ((${#tmp_files[@]} > 0)); then
    for f in "${tmp_files[@]}"; do
      [[ -n "$f" ]] && rm -f -- "$f" 2>/dev/null || true
    done
  fi
}

cleanup_on_exit() {
  rollback_pending_link
  cleanup_tmp
}

trap cleanup_on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

usage() {
  cat <<'USAGE'
Usage: claude-repo-memory-setup.sh [options] [--] [project-path]

Move a repository's default Claude Code auto-memory
(<claude-dir>/projects/<encoded>/memory) into <repo>/.agents/memory/
and symlink it back. Only the memory directory is touched.

Options:
  --encode <path>        Encode raw path string without resolving symlinks
                         (exits 1 with empty stdout if invalid or >200 chars)
  --check-only [path]    Run read-only boundary, locale, encoding, and layout
                         preflight checks without modifying the filesystem
  --                     End of options (use before paths starting with '-')
  -h, --help             Show this help message

Environment:
  CLAUDE_HOME            Script-specific override for Claude config dir (highest priority)
  CLAUDE_CONFIG_DIR      Claude Code config dir override (used if CLAUDE_HOME is unset)
  HOME                   Default fallback ($HOME/.claude)
  CLAUDE_CODE_PROJECT_DIR_NAME
                         Refused when set because it overrides default path encoding
USAGE
}

resolve_dir_realpath() {
  local path=$1
  local base=${2:-$PWD}
  local nl=$'\n'
  if [[ "$path" != /* ]]; then
    path="${base%/}/$path"
  fi
  local raw=""
  raw="$(CDPATH="" cd -P -- "$path" >/dev/null 2>&1 && pwd -P && printf .)" || return 1
  raw="${raw%.}"
  raw="${raw%"$nl"}"
  if [[ "$raw" == *"$nl"* ]]; then
    return 2
  fi
  printf '%s\n' "$raw"
}

resolve_canonical_path() {
  local p=$1
  local nl=$'\n'
  if [[ "$p" == *"$nl"* ]]; then
    return 2
  fi
  if [[ "$p" != /* ]]; then
    p="${PWD%/}/$p"
  fi
  while [[ "$p" != "/" && "$p" == */ ]]; do
    p="${p%/}"
  done

  local suffix=""
  local cur="$p"
  local base=""
  local parent=""
  while [[ ! -d "$cur" ]]; do
    base="${cur##*/}"
    if [[ "$base" == ".." || "$base" == "." ]]; then
      return 3
    fi
    if [[ -n "$base" ]]; then
      suffix="/$base$suffix"
    fi
    parent="${cur%/*}"
    if [[ -z "$parent" ]]; then
      cur="/"
      break
    fi
    cur="$parent"
  done

  local resolved=""
  resolved="$(resolve_dir_realpath "$cur")" || return $?
  if [[ "$resolved" == "/" ]]; then
    printf '/%s\n' "${suffix#/}"
  else
    printf '%s%s\n' "$resolved" "$suffix"
  fi
}

paths_overlap() {
  local a=$1
  local b=$2
  [[ "$a" == "/" || "$b" == "/" || "$a" == "$b" || "$a" == "$b/"* || "$b" == "$a/"* ]]
}

path_has_ancestor_ef() {
  local probe=$1
  local target=$2
  [[ -e "$target" ]] || return 1
  local cur="$probe"
  local parent=""
  while :; do
    if [[ -e "$cur" && "$cur" -ef "$target" ]]; then
      return 0
    fi
    [[ "$cur" == "/" ]] && break
    parent="${cur%/*}"
    cur="${parent:-/}"
  done
  return 1
}

check_claude_env() {
  if [[ -n "${CLAUDE_CODE_PROJECT_DIR_NAME:-}" ]]; then
    die "CLAUDE_CODE_PROJECT_DIR_NAME is set ('${CLAUDE_CODE_PROJECT_DIR_NAME}'), which overrides Claude Code's default path-encoded project key; unset it before running or use Claude Code's native configuration."
  fi
}

resolve_claude_home() {
  local dir=""
  if [[ -n "${CLAUDE_HOME:-}" ]]; then
    dir="$CLAUDE_HOME"
  elif [[ -n "${CLAUDE_CONFIG_DIR:-}" ]]; then
    dir="$CLAUDE_CONFIG_DIR"
  else
    dir="${HOME:?HOME is not set}/.claude"
  fi
  while [[ "$dir" != "/" && "$dir" == */ ]]; do
    dir="${dir%/}"
  done
  printf '%s\n' "$dir"
}

encode_and_validate_path() {
  local path=$1
  local non_ascii=""
  non_ascii="$(printf '%s' "$path" | LC_ALL=C tr -d '\001-\177')"
  if [[ -n "$non_ascii" ]]; then
    local four_byte=""
    four_byte="$(printf '%s' "$path" | LC_ALL=C tr -cd '\360-\364')"
    if [[ -n "$four_byte" ]]; then
      die "Path contains 4-byte UTF-8 characters (such as emoji), which JavaScript encodes as two UTF-16 code units (two '-') while POSIX shells count one character: $path"
    fi
    local probe="é"
    if (( ${#probe} != 1 )); then
      die "Path contains non-ASCII characters under a byte-oriented locale (C/POSIX); re-run under a UTF-8 locale (for example LC_ALL=C.UTF-8 or en_US.UTF-8): $path"
    fi
  fi

  local encoded="${path//[!abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789]/-}"
  if (( ${#encoded} > MAX_ENCODED_LEN )); then
    die "Encoded project path exceeds Claude Code's ${MAX_ENCODED_LEN}-character limit (${#encoded} chars); use Claude Code's autoMemoryDirectory instead: $path"
  fi
  printf '%s\n' "$encoded"
}

find_ancestor_dot_git() {
  local probe=$1
  while :; do
    if [[ -e "$probe/.git" || -L "$probe/.git" ]]; then
      printf '%s\n' "$probe/.git"
      return 0
    fi
    [[ "$probe" == "/" ]] && break
    probe="$(dirname "$probe")"
  done
  return 1
}

verify_standard_main_root() {
  local candidate=$1
  local expected_common=$2
  [[ -n "$candidate" && -d "$candidate" ]] || return 1

  local c_wt="" c_top_raw="" c_top="" c_gd_raw="" c_gd="" c_gc_raw="" c_gc=""
  c_wt="$(git -C "$candidate" rev-parse --is-inside-work-tree 2>/dev/null || true)"
  [[ "$c_wt" == "true" ]] || return 1

  c_top_raw="$(git -C "$candidate" rev-parse --show-toplevel 2>/dev/null || true)"
  c_gd_raw="$(git -C "$candidate" rev-parse --git-dir 2>/dev/null || true)"
  c_gc_raw="$(git -C "$candidate" rev-parse --git-common-dir 2>/dev/null || true)"
  [[ -n "$c_top_raw" && -n "$c_gd_raw" && -n "$c_gc_raw" ]] || return 1

  c_top="$(resolve_dir_realpath "$c_top_raw" "$candidate" || true)"
  c_gd="$(resolve_dir_realpath "$c_gd_raw" "$candidate" || true)"
  c_gc="$(resolve_dir_realpath "$c_gc_raw" "$candidate" || true)"

  [[ "$c_top" -ef "$candidate" ]] || return 1
  [[ "$c_gd" -ef "$expected_common" ]] || return 1
  [[ "$c_gc" -ef "$expected_common" ]] || return 1
  [[ ! -L "$candidate/.git" && -d "$candidate/.git" && "$expected_common" -ef "$candidate/.git" ]] || return 1

  # Guard against --separate-git-dir=/elsewhere/.git where Git treats the
  # parent container directory as a worktree even though none of its tracked
  # files live there: when tracked files exist, at least one must be present.
  local tracked_any=0 tracked_present=0 rel_f=""
  while IFS= read -r -d '' rel_f; do
    tracked_any=1
    if [[ -e "$candidate/$rel_f" || -L "$candidate/$rel_f" ]]; then
      tracked_present=1
      break
    fi
  done < <(git -C "$candidate" ls-files -z 2>/dev/null)
  if (( tracked_any && ! tracked_present )); then
    return 1
  fi
  return 0
}

validate_git_boundary() {
  local repo=$1
  local dot_git_marker=""
  local has_git_marker=0
  if dot_git_marker="$(find_ancestor_dot_git "$repo")"; then
    has_git_marker=1
  fi

  if ! command -v git >/dev/null 2>&1; then
    if (( has_git_marker )); then
      die "Detected Git marker at $dot_git_marker, but 'git' is not available on PATH; refusing to fall back to non-Git mode."
    fi
    return 0
  fi

  local inside_git_dir="" inside_wt="" git_dir_raw="" git_common_raw=""
  inside_git_dir="$(git -C "$repo" rev-parse --is-inside-git-dir 2>/dev/null || true)"
  inside_wt="$(git -C "$repo" rev-parse --is-inside-work-tree 2>/dev/null || true)"

  if [[ -z "$inside_git_dir" && -z "$inside_wt" ]]; then
    if (( has_git_marker )); then
      die "Detected Git marker at $dot_git_marker, but git could not verify this repository (corrupt gitfile, safe.directory restriction, or invalid repo); refusing to fall back to non-Git mode."
    fi
    return 0
  fi

  # 1. Bare repository or inside .git metadata directory
  if [[ "$inside_git_dir" == "true" && "$inside_wt" != "true" ]]; then
    die "Target is inside a bare repository or .git metadata directory: $repo. Run on a standard Git main repository root or use Claude Code's autoMemoryDirectory."
  fi

  if [[ "$inside_wt" != "true" ]]; then
    die "Target is not inside a standard Git working tree: $repo"
  fi

  git_dir_raw="$(git -C "$repo" rev-parse --git-dir 2>/dev/null || true)"
  git_common_raw="$(git -C "$repo" rev-parse --git-common-dir 2>/dev/null || true)"
  local toplevel_raw=""
  toplevel_raw="$(git -C "$repo" rev-parse --show-toplevel 2>/dev/null || true)"
  [[ -n "$git_dir_raw" && -n "$git_common_raw" && -n "$toplevel_raw" ]] \
    || die "Failed to resolve Git repository metadata for: $repo"

  local git_dir="" git_common="" toplevel=""
  git_dir="$(resolve_dir_realpath "$git_dir_raw" "$repo" || true)"
  git_common="$(resolve_dir_realpath "$git_common_raw" "$repo" || true)"
  toplevel="$(resolve_dir_realpath "$toplevel_raw" "$repo" || true)"
  [[ -n "$git_dir" && -n "$git_common" && -n "$toplevel" ]] \
    || die "Failed to resolve physical Git paths for: $repo"

  # 2. Linked Git worktree (root or subdirectory)
  if [[ ! "$git_dir" -ef "$git_common" ]]; then
    local is_bare=""
    is_bare="$(git --git-dir="$git_common" rev-parse --is-bare-repository 2>/dev/null || true)"
    if [[ "$is_bare" == "true" ]]; then
      die "Target is inside a linked Git worktree derived from a bare repository ($git_common), which has no main working tree. Use Claude Code's autoMemoryDirectory instead."
    fi

    local wt_cfg="" candidate=""
    wt_cfg="$(git --git-dir="$git_common" config --get core.worktree 2>/dev/null || true)"
    if [[ -n "$wt_cfg" ]]; then
      candidate="$(resolve_dir_realpath "$wt_cfg" "$git_common" || true)"
    elif [[ "$(basename "$git_common")" == ".git" ]]; then
      candidate="$(resolve_dir_realpath "$(dirname "$git_common")" || true)"
    fi

    if verify_standard_main_root "$candidate" "$git_common"; then
      die "Target is inside a linked Git worktree ($repo). Claude Code shares auto-memory under the main repository root; re-run on the main root instead: claude-repo-memory-setup \"$candidate\""
    fi

    die "Target is inside a linked Git worktree ($repo) whose common git directory ($git_common) does not map to a standard main repository root. Verify the active path in Claude Code via /memory or use autoMemoryDirectory instead."
  fi

  # 3. Git submodule (modern gitfile or legacy real .git/ layout)
  local superproject=""
  superproject="$(git -C "$repo" rev-parse --show-superproject-working-tree 2>/dev/null || true)"
  if [[ -n "$superproject" ]]; then
    die "Target is inside a Git submodule (common git dir: $git_common). Claude Code's project-key derivation for submodules varies across versions; refusing to guess. Verify the active path via /memory or use autoMemoryDirectory instead."
  fi

  # 4. External git directory / --separate-git-dir (or symlinked .git)
  if [[ -L "$toplevel/.git" || ! -d "$toplevel/.git" || ! "$git_common" -ef "$toplevel/.git" ]]; then
    die "Target uses an external git directory / --separate-git-dir ($git_common). Claude Code's project-key derivation for separated git directories varies across versions; refusing to guess. Verify the active path via /memory or use autoMemoryDirectory instead."
  fi

  # 5. Standard Git subdirectory
  if [[ ! "$toplevel" -ef "$repo" ]]; then
    die "Target is a subdirectory of a Git repository ($repo). Claude Code binds auto-memory to the repository root; re-run on the repository root instead: claude-repo-memory-setup \"$toplevel\""
  fi

  git_canonical_root="$toplevel"
}

find_ancestor_dot_svn() {
  local probe=$1
  local parent=""
  while :; do
    if [[ -d "$probe/.svn" ]]; then
      printf '%s\n' "$probe"
      return 0
    fi
    [[ "$probe" == "/" ]] && break
    parent="${probe%/*}"
    probe="${parent:-/}"
  done
  return 1
}

validate_svn_boundary() {
  local repo=$1
  [[ -z "$git_canonical_root" ]] || return 0

  if [[ "$repo" == */.svn || "$repo" == */.svn/* ]]; then
    die "Target is inside an SVN .svn metadata directory: $repo. Re-run on the SVN working copy root instead."
  fi

  local ancestor_svn_root=""
  if ! ancestor_svn_root="$(find_ancestor_dot_svn "$repo")"; then
    return 0
  fi

  if [[ "$ancestor_svn_root" -ef "$repo" ]]; then
    if command -v svn >/dev/null 2>&1; then
      local wc_root_raw="" wc_root=""
      wc_root_raw="$(svn info --show-item wc-root "$repo" 2>/dev/null || true)"
      if [[ -n "$wc_root_raw" ]]; then
        wc_root="$(resolve_dir_realpath "$wc_root_raw" "$repo" || true)"
        if [[ -n "$wc_root" && ! "$wc_root" -ef "$repo" ]]; then
          die "Target is a subdirectory of an SVN working copy ($repo). Re-run on the SVN working copy root instead: claude-repo-memory-setup \"$wc_root\""
        fi
      fi
    fi
    return 0
  fi

  if command -v svn >/dev/null 2>&1; then
    local wc_root_raw="" wc_root=""
    if wc_root_raw="$(svn info --show-item wc-root "$repo" 2>/dev/null)" && [[ -n "$wc_root_raw" ]]; then
      wc_root="$(resolve_dir_realpath "$wc_root_raw" "$repo" || true)"
      if [[ -n "$wc_root" && ! "$wc_root" -ef "$repo" ]]; then
        die "Target is a subdirectory of an SVN working copy ($repo). Re-run on the SVN working copy root instead: claude-repo-memory-setup \"$wc_root\""
      fi
      return 0
    fi
    die "Detected ancestor SVN working copy at $ancestor_svn_root, but 'svn info' could not verify $repo as a standalone root; refusing to fall back to non-VCS mode. Re-run on the SVN working copy root ($ancestor_svn_root) instead."
  fi

  die "Detected ancestor SVN working copy at $ancestor_svn_root, but 'svn' is not available on PATH; refusing to fall back to non-VCS mode. Re-run on the SVN working copy root ($ancestor_svn_root) instead."
}

validate_repo_and_overlap() {
  local repo=$1
  local global_project=$2
  local agents_dir="$repo/.agents"
  local repo_memory="$repo/.agents/memory"

  if [[ -L "$agents_dir" ]]; then
    die ".agents must be a real directory, not a symlink: $agents_dir"
  fi
  if [[ -e "$agents_dir" && ! -d "$agents_dir" ]]; then
    die ".agents exists and is not a directory: $agents_dir"
  fi
  if [[ -L "$repo_memory" ]]; then
    die ".agents/memory must be a real directory, not a symlink: $repo_memory"
  fi
  if [[ -e "$repo_memory" && ! -d "$repo_memory" ]]; then
    die ".agents/memory exists and is not a directory: $repo_memory"
  fi
  if [[ -d "$repo_memory" ]]; then
    ensure_regular_tree "$repo_memory"
  fi

  local canon_global_project="" canon_global_memory="" rc=0
  canon_global_project="$(resolve_canonical_path "$global_project")" || rc=$?
  if (( rc == 2 )); then
    die "Global project path contains a newline character, which is not supported: $global_project"
  elif (( rc == 3 )); then
    die "Global project path contains '.' or '..' after a non-existent directory: $global_project"
  elif (( rc != 0 )) || [[ -z "$canon_global_project" ]]; then
    die "Failed to resolve global project path: $global_project"
  fi
  canon_global_memory="${canon_global_project%/}/memory"

  if paths_overlap "$canon_global_memory" "$repo_memory" \
    || paths_overlap "$canon_global_project" "$repo" \
    || paths_overlap "$canon_global_memory" "$repo" \
    || path_has_ancestor_ef "$canon_global_memory" "$repo" \
    || path_has_ancestor_ef "$canon_global_project" "$repo" \
    || path_has_ancestor_ef "$repo" "$canon_global_project" \
    || path_has_ancestor_ef "$repo" "$canon_global_memory"; then
    die "Global memory location ($canon_global_memory) overlaps with repository path ($repo); refusing to proceed."
  fi
}

dir_state() {
  local dir=$1
  local entries=""
  if ! entries="$(ls -A -- "$dir" 2>/dev/null && printf .)"; then
    die "Cannot read directory: $dir"
  fi
  if [[ "$entries" != "." ]]; then
    printf 'nonempty\n'
  else
    printf 'empty\n'
  fi
}

ensure_regular_tree() {
  local dir=$1
  local bad=""
  if ! bad="$(find "$dir" -mindepth 1 ! -type f ! -type d 2>/dev/null)"; then
    die "Cannot inspect directory entries: $dir"
  fi
  if [[ -n "$bad" ]]; then
    local first="${bad%%$'\n'*}"
    die "Memory directory contains symlinks or special files (only regular files and directories are supported): $first"
  fi
}

next_backup_path() {
  local parent=$1
  local candidate="$parent/memory.bak"
  local n=2
  while [[ -e "$candidate" || -L "$candidate" ]]; do
    candidate="$parent/memory.bak$n"
    n=$((n + 1))
  done
  printf '%s\n' "$candidate"
}

list_other_backups() {
  local parent=$1
  local exclude=$2
  local b="" base=""
  local others=()
  for b in "$parent"/memory.bak*; do
    [[ -e "$b" || -L "$b" ]] || continue
    [[ -n "$exclude" && "$b" == "$exclude" ]] && continue
    base="${b##*/}"
    if [[ "$base" == "memory.bak" ]] || [[ "$base" =~ ^memory\.bak[0-9]+$ ]]; then
      others+=("$b")
    fi
  done
  if ((${#others[@]} > 0)); then
    printf '%s\n' "${others[*]}"
  fi
}

run_diff_rq() {
  local left=$1
  local right=$2
  local out_file=""
  out_file="$(mktemp "${TMPDIR:-/tmp}/claude-repo-memory-setup.diff.XXXXXX")" \
    || die "Failed to create temporary diff file"
  tmp_files+=("$out_file")

  if diff -rq -- "$left" "$right" >"$out_file" 2>&1; then
    rm -f -- "$out_file"
    return 0
  else
    sed -n '1,40p' "$out_file" >&2 || true
    rm -f -- "$out_file"
    return 1
  fi
}

ensure_memory_link() {
  local global_memory=$1
  local repo_memory=$2

  if [[ -L "$global_memory" ]]; then
    if [[ "$global_memory" -ef "$repo_memory" ]]; then
      say "Global memory symlink already points to repository memory; skipping"
      return 0
    fi
    if [[ ! -e "$global_memory" ]]; then
      say "Global memory is a dangling symlink; repairing to point to repository memory"
      rm -- "$global_memory"
      ln -s -- "$repo_memory" "$global_memory"
      return 0
    fi
    warn "Global memory symlink points to another existing target: $(readlink "$global_memory")"
    die "Possible encoded path collision or manual symlink; refusing to overwrite: $global_memory"
  fi

  if [[ -e "$global_memory" ]]; then
    die "Global memory path is still occupied; refusing to create symlink: $global_memory"
  fi

  say "Creating symlink: $global_memory -> $repo_memory"
  ln -s -- "$repo_memory" "$global_memory"
  needs_link_rollback=0
}

handle_git() {
  local repo=$1
  local rel=".agents/memory/"
  vcs_kind="git"

  if ! command -v git >/dev/null 2>&1; then
    vcs_blocked="git command not found"
    warn "Found .git but 'git' is not on PATH; skipping Git integration"
    vcs_action="Git: git not found, skipped"
    return 0
  fi

  if [[ "$(git -C "$repo" rev-parse --is-inside-work-tree 2>/dev/null || true)" != "true" ]]; then
    vcs_blocked="git working tree unavailable"
    warn "Found .git but git working tree is unavailable; skipping Git integration"
    vcs_action="Git: working tree unavailable, skipped"
    return 0
  fi

  if (( ! migrated_nonempty_global && repo_memory_existed_before )); then
    local preexisting=""
    preexisting="$(git -C "$repo" status --porcelain -- "$rel" 2>/dev/null || true)"
    if [[ -n "$preexisting" ]]; then
      vcs_blocked="pre-existing uncommitted changes in .agents/memory/"
      warn "Pre-existing uncommitted changes detected in $rel; leaving Git index and working tree untouched. Review and commit them manually."
      vcs_action="Git: pre-existing uncommitted changes in $rel; skipped git add"
      return 0
    fi
  fi

  if git -C "$repo" check-ignore -q "$rel"; then
    vcs_blocked="path ignored by Git"
    warn "$rel is ignored by Git; not forcing git add. Adjust ignore rules and re-run."
    vcs_action="Git: path ignored, skipped git add"
    return 0
  fi

  if git -C "$repo" add -- "$rel"; then
    vcs_action="Git: staged $rel"
  else
    vcs_blocked="git add failed"
    warn "git add -- $rel failed"
    vcs_action="Git: git add failed"
  fi
}

handle_svn() {
  local repo=$1
  local memory="$repo/.agents/memory"
  vcs_kind="svn"

  if ! command -v svn >/dev/null 2>&1; then
    vcs_blocked="svn command not found"
    warn "Found .svn but 'svn' is not on PATH; skipping SVN integration"
    vcs_action="SVN: svn not found, skipped"
    return 0
  fi

  if (( ! migrated_nonempty_global && repo_memory_existed_before )); then
    local preexisting=""
    preexisting="$(svn status "$memory" 2>/dev/null || true)"
    if [[ -n "$preexisting" ]]; then
      vcs_blocked="pre-existing uncommitted changes in .agents/memory/"
      warn "Pre-existing uncommitted changes detected in .agents/memory/; leaving SVN working copy untouched. Review and commit them manually."
      vcs_action="SVN: pre-existing uncommitted changes in .agents/memory/; skipped svn add"
      return 0
    fi
  fi

  local status=""
  status="$(svn status --no-ignore --depth empty "$memory" 2>/dev/null || true)"
  if [[ "$status" == I* ]]; then
    vcs_blocked="path ignored by SVN"
    warn ".agents/memory/ is ignored by SVN; not forcing svn add. Adjust ignore rules and re-run."
    vcs_action="SVN: path ignored, skipped svn add"
    return 0
  fi

  if svn add --force --parents "$memory" >/dev/null 2>&1; then
    vcs_action="SVN: ensured .agents/memory/ is under version control"
  else
    vcs_blocked="svn add failed"
    warn "svn add --force --parents .agents/memory/ failed"
    vcs_action="SVN: svn add failed"
  fi
}

integrate_vcs() {
  local repo=$1
  if [[ -e "$repo/.git" ]]; then
    handle_git "$repo"
  elif [[ -e "$repo/.svn" ]]; then
    handle_svn "$repo"
  else
    vcs_action="No .git or .svn detected; skipped VCS integration"
    say "$vcs_action"
  fi
}

probe_link() {
  local global_memory=$1
  local repo_memory=$2

  [[ -L "$global_memory" && "$global_memory" -ef "$repo_memory" ]] \
    || die "Global memory symlink check failed: $global_memory does not resolve to $repo_memory"

  say "Files visible through global memory symlink:"
  local listing=""
  listing="$(ls -A -- "$global_memory" 2>/dev/null | sort || true)"
  if [[ -n "$listing" ]]; then
    printf '%s\n' "$listing" | sed 's/^/  /'
  else
    say "  (empty)"
  fi

  local probe_file=""
  probe_file="$(mktemp "$global_memory/.claude-repo-memory-setup-probe.XXXXXX")" \
    || die "Symlink write probe failed: unable to write through $global_memory"
  tmp_files+=("$probe_file")

  local probe_base="${probe_file##*/}"
  local repo_probe="$repo_memory/$probe_base"
  if [[ -f "$repo_probe" && "$probe_file" -ef "$repo_probe" ]]; then
    rm -f -- "$probe_file"
    say "Symlink write probe passed"
  else
    rm -f -- "$probe_file"
    die "Symlink write probe failed: write through $global_memory did not land at $repo_probe"
  fi
}

commit_die() {
  say "ERROR: $*" >&2
  say "Migration and symlink succeeded: memory is stored in .agents/memory/ and the global symlink is active; only the automatic commit failed." >&2
  if [[ -n "$backup_path" ]]; then
    say "Backup preserved: $backup_path" >&2
  fi
  say "Resolve the VCS error, commit .agents/memory/ manually, and remove the backup once verified." >&2
  exit 1
}

commit_git() {
  local repo=$1
  local rel=".agents/memory/"

  local pending=""
  pending="$(git -C "$repo" status --porcelain -- "$rel" 2>/dev/null)" || commit_die "git status check failed"
  if [[ -z "$pending" ]]; then
    commit_state="empty"
    commit_note="No memory changes to commit; skipped"
    say "$commit_note"
    return 0
  fi

  if git -C "$repo" commit -m "$COMMIT_MSG" -- "$rel"; then
    commit_state="committed"
    commit_note="Git: committed locally (pathspec-scoped to ${rel})"
    say "$commit_note"
  else
    commit_state="failed"
    commit_die "git commit failed (common causes: missing user.name/user.email or repository mid-merge)"
  fi
}

commit_svn() {
  local repo=$1
  local agents="$repo/.agents"
  local memory="$repo/.agents/memory"

  local pending="" agents_st=""
  pending="$(svn status -q "$memory" 2>/dev/null)" || commit_die "svn status check failed"
  agents_st="$(svn status -q --depth empty "$agents" 2>/dev/null)" || commit_die "svn status check for .agents failed"

  if [[ -z "$pending" && "$agents_st" != A* ]]; then
    commit_state="empty"
    commit_note="No memory changes to commit; skipped"
    say "$commit_note"
    return 0
  fi

  # Note: svn commit pushes directly to the central repository.
  if [[ "$agents_st" == A* ]]; then
    if ! svn commit --depth empty "$agents" -m "$COMMIT_MSG"; then
      commit_state="failed"
      commit_die "svn commit of .agents directory failed (check permissions/network)"
    fi
  fi

  if svn commit "$memory" -m "$COMMIT_MSG"; then
    commit_state="committed"
    commit_note="SVN: committed to central repository (scoped to .agents/memory)"
    say "$commit_note"
  else
    commit_state="failed"
    commit_die "svn commit of .agents/memory failed (check permissions/network)"
  fi
}

commit_vcs() {
  local repo=$1

  if [[ "$vcs_kind" == "none" ]]; then
    commit_state="none"
    commit_note="No VCS; skipped"
    say "$commit_note"
    return 0
  fi

  if [[ -n "$vcs_blocked" ]]; then
    commit_state="blocked"
    if [[ -n "$backup_path" ]]; then
      commit_note="Skipped (blocked: ${vcs_blocked}); backup preserved"
      warn "VCS integration blocked (${vcs_blocked}); skipped automatic commit and preserved backup."
    else
      commit_note="Skipped (${vcs_blocked})"
    fi
    return 0
  fi

  if [[ "$vcs_kind" == "git" ]]; then
    commit_git "$repo"
  else
    commit_svn "$repo"
  fi
}

verify_memory_tracked() {
  local repo=$1
  local memory="$repo/.agents/memory"

  [[ "$commit_state" == "committed" || "$commit_state" == "empty" ]] || return 0
  [[ "$vcs_kind" == "git" || "$vcs_kind" == "svn" ]] || return 0

  if [[ "$vcs_kind" == "git" ]]; then
    local has_file=""
    has_file="$(find "$memory" -type f 2>/dev/null | head -n 1 || true)"
    [[ -n "$has_file" ]] || return 0

    local tracked=""
    tracked="$(git -C "$repo" ls-files -- .agents/memory/ 2>/dev/null || true)"
    if [[ -n "$tracked" ]]; then
      return 0
    fi
  else
    local f="" st="" has_regular=0 tracked_regular=0
    while IFS= read -r -d '' f; do
      has_regular=1
      st="$(svn status -v --no-ignore --depth empty "$f" 2>/dev/null || true)"
      if [[ -n "$st" && "$st" != \?* && "$st" != I* ]]; then
        tracked_regular=1
        break
      fi
    done < <(find "$memory" -type f -print0 2>/dev/null)

    if (( ! has_regular || tracked_regular )); then
      return 0
    fi
  fi

  commit_state="untracked"
  commit_note="Memory files are not tracked by VCS (likely ignored); backup preserved"
  warn "Memory files under .agents/memory/ exist on disk, but 0 regular files are tracked by VCS (likely matched by ignore rules). Backup preserved; adjust ignore rules and re-run before running cleanup commands like git clean."
}

remove_run_backup() {
  local parent=$1
  local repo_memory=$2

  local other_baks=""
  other_baks="$(list_other_backups "$parent" "$backup_path")"

  if [[ -z "$backup_path" ]]; then
    if [[ -n "$other_baks" ]]; then
      bak_note="none this run (pre-existing backups untouched: $other_baks)"
    else
      bak_note="none"
    fi
    return 0
  fi

  case "$commit_state" in
    committed|empty|none)
      if (( backup_was_empty )); then
        if rmdir -- "$backup_path" 2>/dev/null; then
          say "Removed empty backup directory with rmdir: $backup_path"
          bak_note="removed (empty backup cleaned with rmdir)"
        else
          warn "Expected-empty backup directory is no longer empty (concurrent writes detected?); preserved: $backup_path"
          bak_note="preserved (rmdir failed; not empty): $backup_path"
        fi
      else
        if run_diff_rq "$backup_path" "$repo_memory"; then
          rm -rf -- "$backup_path"
          say "Removed this run's backup: $backup_path"
          bak_note="removed (verified)"
        else
          warn "Post-migration verification detected differences between backup ($backup_path) and repository memory ($repo_memory); backup preserved."
          bak_note="preserved (post-migration diff mismatch): $backup_path"
          post_backup_mismatch=1
        fi
      fi
      ;;
    untracked)
      bak_note="preserved (memory untracked, likely ignored): $backup_path"
      say "Memory not tracked; preserved backup: $backup_path"
      ;;
    *)
      bak_note="preserved (commit skipped/blocked): $backup_path"
      say "Commit not completed; preserved backup: $backup_path"
      ;;
  esac

  if [[ -n "$other_baks" ]]; then
    bak_note="$bak_note (pre-existing backups untouched: $other_baks)"
  fi
}

main() {
  local mode="run"
  local encode_arg=""
  local positional=()
  local end_of_opts=0

  while [[ $# -gt 0 ]]; do
    if (( ! end_of_opts )); then
      case "$1" in
        -h|--help)
          usage
          exit 0
          ;;
        --encode)
          [[ $# -eq 2 && "$mode" == "run" && ${#positional[@]} -eq 0 ]] \
            || die "--encode requires exactly one path argument"
          mode="encode"
          encode_arg=$2
          shift 2
          continue
          ;;
        --check-only)
          [[ "$mode" == "run" ]] || die "Conflicting options"
          mode="check-only"
          shift
          continue
          ;;
        --)
          end_of_opts=1
          shift
          continue
          ;;
        -*)
          die "Unknown option: $1 (use -- before a path starting with '-')"
          ;;
      esac
    fi
    positional+=("$1")
    shift
  done

  if [[ "$mode" == "encode" ]]; then
    encode_and_validate_path "$encode_arg"
    exit 0
  fi

  if ((${#positional[@]} > 1)); then
    die "Too many arguments; expected at most one project path."
  fi

  check_claude_env

  local input="$PWD"
  if ((${#positional[@]} == 1)); then
    input="${positional[0]}"
  fi
  [[ -d "$input" ]] || die "Project path does not exist or is not a directory: $input"

  local repo="" rc=0
  repo="$(resolve_dir_realpath "$input")" || rc=$?
  if (( rc == 2 )); then
    die "Project physical path contains a newline character, which is not supported: $input"
  elif (( rc != 0 )) || [[ -z "$repo" ]]; then
    die "Failed to resolve physical path for: $input"
  fi

  validate_git_boundary "$repo"
  if [[ -n "$git_canonical_root" ]]; then
    repo="$git_canonical_root"
  fi
  validate_svn_boundary "$repo"

  local encoded=""
  if ! encoded="$(encode_and_validate_path "$repo")"; then
    exit 1
  fi

  local claude_home=""
  claude_home="$(resolve_claude_home)"
  local global_project="$claude_home/projects/$encoded"
  local global_memory="$global_project/memory"
  local repo_memory="$repo/.agents/memory"

  validate_repo_and_overlap "$repo" "$global_project"

  if [[ "$mode" == "check-only" ]]; then
    say "Preflight OK: $repo (encoded: $encoded)"
    exit 0
  fi

  say "Project: $repo"
  say "Encoded: $encoded"
  say "Repo memory: $repo_memory"
  say "Global memory: $global_memory"

  step "Ensuring .agents/memory/ exists in repository"
  if [[ -d "$repo_memory" ]]; then
    repo_memory_existed_before=1
  fi
  mkdir -p -- "$repo_memory"
  local verified_repo_memory=""
  verified_repo_memory="$(resolve_dir_realpath "$repo_memory")" || die "Failed to resolve created .agents/memory/"
  [[ "$verified_repo_memory" == "$repo/.agents/memory" ]] \
    || die ".agents/memory resolved outside the expected repository path: $verified_repo_memory"
  ensure_regular_tree "$repo_memory"

  step "Processing global memory directory"
  mkdir -p -- "$global_project"

  if [[ ! -L "$global_memory" && -d "$global_memory" ]]; then
    local global_state="" repo_state=""
    global_state="$(dir_state "$global_memory")"
    repo_state="$(dir_state "$repo_memory")"

    ensure_regular_tree "$global_memory"

    if [[ "$global_state" == "empty" ]]; then
      backup_was_empty=1
      backup_path="$(next_backup_path "$global_project")"
      mv -- "$global_memory" "$backup_path"
      rollback_global_memory="$global_memory"
      needs_link_rollback=1
      say "Global memory is an empty directory; moved to backup: $backup_path"
    elif [[ "$repo_state" == "nonempty" ]]; then
      if run_diff_rq "$global_memory" "$repo_memory"; then
        say "Repository memory already matches global directory; skipping copy"
      else
        die "Both .agents/memory/ and global memory are non-empty and differ; merge manually before re-running (no files were overwritten)."
      fi
      migrated_nonempty_global=1
      backup_was_empty=0
      backup_path="$(next_backup_path "$global_project")"
      mv -- "$global_memory" "$backup_path"
      rollback_global_memory="$global_memory"
      needs_link_rollback=1
      say "Backed up global directory to: $backup_path"
    else
      say "Global memory is a real directory; copying existing memory into repository"
      cp -a "$global_memory/." "$repo_memory/"
      if ! run_diff_rq "$global_memory" "$repo_memory"; then
        die "Post-copy diff verification failed; stopped before renaming global memory."
      fi
      say "Post-copy diff verification passed"
      migrated_nonempty_global=1
      backup_was_empty=0
      backup_path="$(next_backup_path "$global_project")"
      mv -- "$global_memory" "$backup_path"
      rollback_global_memory="$global_memory"
      needs_link_rollback=1
      say "Backed up global directory to: $backup_path"
    fi
  elif [[ ! -L "$global_memory" && -e "$global_memory" ]]; then
    die "Global memory path exists but is neither a directory nor a symlink: $global_memory"
  fi

  ensure_memory_link "$global_memory" "$repo_memory"

  step "Integrating with VCS"
  integrate_vcs "$repo"

  step "Verifying symlink write-through"
  probe_link "$global_memory" "$repo_memory"

  step "Committing .agents/memory/"
  commit_vcs "$repo"

  step "Verifying memory tracking"
  verify_memory_tracked "$repo"

  step "Cleaning up this run's backup"
  remove_run_backup "$global_project" "$repo_memory"

  local final_link=""
  final_link="$(readlink "$global_memory" 2>/dev/null || true)"

  say ""
  say "Summary"
  say "  encoded: $encoded"
  say "  repo memory: $repo_memory"
  say "  global memory: $global_memory"
  say "  symlink target: ${final_link:-not a symlink}"
  say "  VCS action: $vcs_action"
  say "  commit status: $commit_note"
  say "  backup: $bak_note"
  if ((${#warns[@]} > 0)); then
    say "  warnings:"
    local w
    for w in "${warns[@]}"; do
      say "    - $w"
    done
  else
    say "  warnings: none"
  fi

  if [[ "$commit_state" == "untracked" ]] || (( post_backup_mismatch )); then
    exit 1
  fi
}

main "$@"
