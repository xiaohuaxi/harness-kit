#!/usr/bin/env python3
"""Run, resume, or live-steer a local agy (Antigravity CLI) agent via ``agy acp``.

Provides 3-tier sandbox permissions:
  - ``read-only`` (default): file read + web search + terminal commands in Seatbelt
    ``--sandbox``, plus pre/post zero-change workspace snapshot verification.
  - ``workspace-write`` (``--write``): workspace file edits + web search + terminal
    commands in Seatbelt ``--sandbox``, blocking outside-workspace writes and BypassSandbox.
  - ``danger-full-access`` (``--yolo``): full tools (including ``generate_image``)
    without ``--sandbox``, while still blocking recursive ``claude``/``codex``/``agy_run.py``.

Standard library only.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_core as core  # noqa: E402

SESSION_ID_PREFIX = "AGY_SESSION_ID="
META_PREFIX = "AGY_RUN_META="
DEFAULT_TIMEOUT = 3600.0
LIST_MODELS_TIMEOUT = 45.0
SANDBOX_CHOICES = ("read-only", "workspace-write", "danger-full-access")

READ_TOOLS = ("view_file", "list_dir", "grep_search", "find_by_name")
WEB_TOOLS = ("search_web", "read_url_content")
EDIT_TOOLS = ("write_to_file", "replace_file_content", "multi_replace_file_content")
COMMAND_TOOLS = ("run_command",)

# agy 1.3.5 custom-agent tool names -> agy 1.5.x built-in names reported in ACP/streams.
AGY15_NAMES = {
    "view_file": "read_file",
    "write_to_file": "write_file",
    "replace_file_content": "edit_file",
}

READONLY_TOOLS = READ_TOOLS + COMMAND_TOOLS + WEB_TOOLS
WRITE_MODE_TOOLS = READ_TOOLS + EDIT_TOOLS + ("notebook_edit",) + COMMAND_TOOLS + WEB_TOOLS
FULL_TOOLS = WRITE_MODE_TOOLS + ("generate_image",)

# Seatbelt --sandbox blocks ~/.gitconfig; GIT_CONFIG_GLOBAL=/dev/null keeps git working.
# In read-only mode we also suppress optional .git/index locks and .pyc files.
READONLY_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_OPTIONAL_LOCKS": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
}
WRITE_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
}

TASK_BODY = (
    "# Delegated task\n\n"
    "You are running a bounded task delegated by another agent through agy-run. "
    "Nobody can answer interactive questions or approve prompts during the run, so execute "
    "autonomously and finish with your conclusion. Use only the tools granted to you. "
    "If the task requires capabilities outside your current mode, report that clearly instead "
    "of looking for workarounds."
)
COMMANDS_NOTE = (
    "\n\nWorkspace directories are listed at the top of the prompt. "
    "Do the work yourself: never invoke Claude Code, Codex, or nested agy-run "
    "(`claude`, `codex`, `agy_run.py`, or wrapper scripts)."
)
READONLY_BODY = TASK_BODY + COMMANDS_NOTE + (
    "\n\nThis is a read-only run: every workspace directory must end completely unchanged, "
    "and any file creation, modification, or deletion inside the workspace fails the run. "
    "Use the system temporary directory (`$TMPDIR` / `/tmp`) if you need scratch files. "
    "Commands run inside agy's terminal sandbox; never set `BypassSandbox`."
)
WRITE_BODY = TASK_BODY + COMMANDS_NOTE + (
    "\n\nYou may create and edit files inside the workspace and run commands inside agy's "
    "terminal sandbox. Keep all file writes strictly inside the workspace (use `$TMPDIR` only "
    "for temporary scratch data) and never set `BypassSandbox`."
)
YOLO_BODY = TASK_BODY + COMMANDS_NOTE + (
    "\n\nYou have full tool access (including `generate_image`) without terminal sandbox "
    "restrictions. Put deliverables in the requested target path or inside the workspace and "
    "report their absolute paths; generated images are first written under agy's brain directory, "
    "so copy them to the requested output path when needed."
)

# Why both read-only and workspace-write pass ("--dangerously-skip-permissions", "--sandbox"):
# In agy 1.5.x ACP mode, omitting --dangerously-skip-permissions triggers session/request_permission
# on run_command, and granting permission via ACP executes the command OUTSIDE macOS Seatbelt.
# Passing both flags runs every non-BypassSandbox command strictly inside Seatbelt; read-only
# additionally omits write/edit tools and verifies an unchanged workspace snapshot afterwards.
MODES = {
    "read-only": {
        "slug": "readonly",
        "tools": READONLY_TOOLS,
        "flags": ("--dangerously-skip-permissions", "--sandbox"),
        "env": READONLY_ENV,
        "body": READONLY_BODY,
    },
    "workspace-write": {
        "slug": "write",
        "tools": WRITE_MODE_TOOLS,
        "flags": ("--dangerously-skip-permissions", "--sandbox"),
        "env": WRITE_ENV,
        "body": WRITE_BODY,
    },
    "danger-full-access": {
        "slug": "yolo",
        "tools": FULL_TOOLS,
        "flags": ("--dangerously-skip-permissions",),
        "env": {},
        "body": YOLO_BODY,
    },
}

WRITABLE_MODES = ("workspace-write", "danger-full-access")
AGENT_CLIS = ("claude", "codex")
AGENT_STUB = (
    '#!/bin/sh\n'
    'echo "agy-run: $(basename "$0") is blocked here: agy runs as a callee and must not '
    'start Claude Code or Codex" >&2\n'
    'exit 126\n'
)
WORKSPACE_SCAN_LIMIT = 300_000
EFFORT_PATTERN = re.compile(r"[a-z]+\Z")
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class RunError(core.AgyError):
    pass


def allowed_names(tools):
    """Agent-file tool names plus agy 1.5.x mapped built-in names."""
    return tuple(tools) + tuple(
        sorted({AGY15_NAMES[t] for t in tools if t in AGY15_NAMES} - set(tools))
    )


def agent_name(sandbox_mode, rules):
    slug = MODES[sandbox_mode]["slug"]
    return f"agy-{slug}" + ("-rules" if rules else "")


def agent_definitions():
    """Return ``{filename: markdown}`` for all 6 managed custom agent definitions."""
    defs = {}
    for sandbox_mode, spec in MODES.items():
        for rules in (False, True):
            name = agent_name(sandbox_mode, rules)
            desc = (
                f"agy-run mode '{sandbox_mode}' ({'with' if rules else 'without'} rules). "
                "Not a subagent."
            )
            defs[name + ".md"] = core.agent_markdown(
                name, desc, spec["tools"], rules, spec["body"]
            )
    return defs


def default_agents_dir(environment=None):
    return core.home_dir(environment) / ".gemini" / "config" / "agents"


def _normalize_text(text):
    return text.replace("\r\n", "\n")


def _script_cmd():
    return f"python3 {shlex.quote(str(Path(__file__).resolve()))}"


def ensure_agent_installed(name, environment=None):
    """Pre-flight check that the selected managed agent file exists and matches expected content."""
    agents_dir = default_agents_dir(environment)
    fname = f"{name}.md"
    want = _normalize_text(agent_definitions()[fname])
    fpath = agents_dir / fname
    is_sym = agents_dir.is_symlink()
    physical_target = os.path.realpath(str(agents_dir)) if is_sym else str(agents_dir)
    cmd = _script_cmd()

    if not fpath.exists():
        if is_sym:
            raise RunError(
                f"managed agent definition is missing: {fpath} "
                f"(symlink directory target: {physical_target}). "
                f"Refresh it with: {cmd} --write-agent-files {shlex.quote(physical_target)}"
            )
        raise RunError(
            f"managed agent definition is missing: {fpath}. "
            f"Install all 6 agent definitions with: {cmd} --install-agents"
        )

    actual = None
    if fpath.is_file():
        try:
            actual = _normalize_text(fpath.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            actual = None

    if actual != want:
        if is_sym:
            raise RunError(
                f"managed agent definition has local modifications or is outdated: {fpath} "
                f"(symlink directory target: {physical_target}). "
                f"Refresh it with: {cmd} --write-agent-files {shlex.quote(physical_target)}"
            )
        raise RunError(
            f"managed agent definition has local modifications or is outdated: {fpath}. "
            f"Overwrite and refresh with: {cmd} --write-agent-files {shlex.quote(str(agents_dir))}"
        )


def install_agents(stdout, stderr, environment=None):
    """Safely install missing managed agent files into ~/.gemini/config/agents."""
    agents_dir = default_agents_dir(environment)
    expected = agent_definitions()
    cmd = _script_cmd()

    if agents_dir.is_symlink():
        physical_target = os.path.realpath(str(agents_dir))
        refresh_cmd = f"{cmd} --write-agent-files {shlex.quote(physical_target)}"
        if not agents_dir.is_dir():
            print(
                f"agy-run: error: {agents_dir} is a symlink to {physical_target}, which is not an "
                f"existing directory (refusing to write through symlink). Refresh target with: {refresh_cmd}",
                file=stderr,
                flush=True,
            )
            return 1

        missing, conflicting = [], []
        for fname, want in sorted(expected.items()):
            fpath = agents_dir / fname
            if not fpath.exists():
                missing.append(fname)
                continue
            if not fpath.is_file():
                conflicting.append(fname)
                continue
            try:
                actual = _normalize_text(fpath.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                conflicting.append(fname)
                continue
            if actual != _normalize_text(want):
                conflicting.append(fname)

        if not missing and not conflicting:
            stdout.write(
                f"All {len(expected)} agy-run agent definitions are already up to date in "
                f"{agents_dir} (-> {physical_target}).\n"
            )
            stdout.flush()
            return 0

        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if conflicting:
            details.append("outdated/conflicting: " + ", ".join(conflicting))
        print(
            f"agy-run: error: {agents_dir} is a symlink to {physical_target} and will not be written "
            f"through ({'; '.join(details)}). Refresh target with: {refresh_cmd}",
            file=stderr,
            flush=True,
        )
        return 1

    if agents_dir.exists() and not agents_dir.is_dir():
        print(
            f"agy-run: error: {agents_dir} exists and is not a directory.",
            file=stderr,
            flush=True,
        )
        return 1

    up_to_date, missing, conflicting = [], [], []
    for fname, want in sorted(expected.items()):
        fpath = agents_dir / fname
        if fpath.is_symlink():
            conflicting.append(fname)
            continue
        if not fpath.exists():
            missing.append(fname)
            continue
        if not fpath.is_file():
            conflicting.append(fname)
            continue
        try:
            actual = _normalize_text(fpath.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            conflicting.append(fname)
            continue
        if actual == _normalize_text(want):
            up_to_date.append(fname)
        else:
            conflicting.append(fname)

    if conflicting:
        refresh_cmd = f"{cmd} --write-agent-files {shlex.quote(str(agents_dir))}"
        print(
            f"agy-run: error: refusing to overwrite conflicting agent file(s) in {agents_dir}: "
            f"{', '.join(conflicting)}. No files were modified. "
            f"To overwrite all 6 definitions, run: {refresh_cmd}",
            file=stderr,
            flush=True,
        )
        return 1

    if missing:
        agents_dir.mkdir(parents=True, exist_ok=True)
        for fname in missing:
            core.atomic_write(agents_dir / fname, expected[fname])
        stdout.write(
            f"Installed {len(missing)} agy-run agent definition(s) into {agents_dir} "
            f"({len(up_to_date)} already up to date).\n"
        )
        stdout.flush()
        return 0

    stdout.write(f"All {len(expected)} agy-run agent definitions are already up to date in {agents_dir}.\n")
    stdout.flush()
    return 0


def list_models(stdout, stderr, environment=None, timeout=None):
    """Run ``agy models`` and emit only stdout on success (filtering stderr noise)."""
    effective_timeout = LIST_MODELS_TIMEOUT if timeout is None else float(timeout)
    cmd = [core.agy_bin(environment), "models"]
    child_env = core.agy_env(environment=environment)
    proc = None
    launch_err = None
    held = core.signals_held()
    try:
        with held:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=child_env,
                    start_new_session=True,
                    text=True,
                )
            except (FileNotFoundError, OSError) as exc:
                launch_err = exc
        held.release()
        if launch_err is not None:
            if isinstance(launch_err, FileNotFoundError):
                print(
                    f"agy-run: error: {cmd[0]} not found on PATH (Antigravity CLI)",
                    file=stderr,
                    flush=True,
                )
            else:
                print(
                    f"agy-run: error: failed to launch `{cmd[0]} models`: {launch_err}",
                    file=stderr,
                    flush=True,
                )
            return 1

        out, err = proc.communicate(timeout=effective_timeout)
    except subprocess.TimeoutExpired:
        with core.signals_ignored():
            try:
                core.stop_group(proc, grace=0.5)
            except core.AgyError:
                pass
        print(
            f"agy-run: error: `{cmd[0]} models` timed out after {effective_timeout:g}s",
            file=stderr,
            flush=True,
        )
        return EXIT_CODES["timeout"]
    except core.Cancelled:
        if proc is not None:
            with core.signals_ignored():
                try:
                    core.stop_group(proc, grace=0.5)
                except core.AgyError:
                    pass
        raise
    except BaseException:
        if proc is not None:
            with core.signals_ignored():
                try:
                    core.stop_group(proc, grace=0.5)
                except core.AgyError:
                    pass
        raise

    if proc.returncode == 0:
        stdout.write(out)
        stdout.flush()
        return 0

    detail = (err or out or "").strip()
    print(
        f"agy-run: error: `{cmd[0]} models` failed with exit code {proc.returncode}"
        + (f": {core.clip(detail)}" if detail else ""),
        file=stderr,
        flush=True,
    )
    return proc.returncode if proc.returncode > 0 else 1


def state_dir(environment=None):
    env = os.environ if environment is None else environment
    base = env.get("XDG_STATE_HOME", "")
    home = core.home_dir(env)
    root = Path(base) if base and os.path.isabs(base) else home / ".local" / "state"
    return root / "agy-run"


def ensure_state(environment=None):
    root = state_dir(environment)
    for sub in ("conversations", "runs", "active"):
        (root / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def validate_not_nested(environment=None):
    env = os.environ if environment is None else environment
    if env.get(core.DELEGATE_MARKER) == "1":
        raise RunError(
            f"Refusing nested agy_run.py invocation ({core.DELEGATE_MARKER}=1): "
            "delegated agy sessions must not spawn agy-run recursively."
        )


def resolve_dir(value, what):
    expanded = os.path.abspath(os.path.expanduser(str(value)))
    if not Path(expanded).is_dir():
        raise RunError(f"{what} is not an existing directory: {value}")
    return expanded


def read_task_input(task_arg, prompt_file, stdin):
    if task_arg is not None and prompt_file is not None:
        raise RunError("Cannot combine positional task and --prompt-file.", kind="usage")
    if prompt_file is not None:
        pf = Path(prompt_file).expanduser()
        if not pf.is_file():
            raise RunError(f"--prompt-file does not exist: {prompt_file}")
        text = pf.read_text(encoding="utf-8")
        if not text.strip():
            raise RunError(f"--prompt-file is empty: {prompt_file}")
        return text
    if task_arg is not None:
        if not task_arg.strip():
            raise RunError("Task argument cannot be empty.")
        return task_arg
    if stdin is None or stdin.isatty():
        raise RunError("Provide a task argument, --prompt-file, or pipe prompt text via stdin.")
    text = stdin.read()
    if not text.strip():
        raise RunError("Task on stdin is empty.")
    return text


# ---------------------------------------------------------------- Claude project memory protection

def memory_plan(cwd, add_dirs, sandbox_mode, run_dir, environment=None):
    """Give rules-enabled runs read-only visibility into Claude project memory."""
    memory = core.claude_memory_dir(cwd, environment=environment)
    if memory is None:
        return [], "", None
    real = os.path.realpath(str(memory))
    inside = any(core.is_within(real, d) for d in [cwd, *add_dirs])
    if sandbox_mode not in WRITABLE_MODES:
        extra = [] if inside else [real]
        return (
            extra,
            f"Claude project memory index for this directory (read-only): {memory}/MEMORY.md",
            real,
        )
    if inside:
        return (
            [],
            (
                f"Claude project memory index for this directory: {memory}/MEMORY.md. "
                "Read it if useful; never edit anything in that directory."
            ),
            real,
        )
    copy = Path(run_dir) / "claude-memory"
    copy.mkdir(parents=True)
    for item in Path(real).iterdir():
        if item.suffix == ".md" and item.is_file():
            shutil.copy2(item, copy / item.name)
    return (
        [str(copy)],
        (
            f"Claude project memory for this directory is copied to {copy}/MEMORY.md "
            f"(the original {memory} is outside your workspace). Read the copy; do not edit it."
        ),
        real,
    )


def check_memory_untouched(tool_calls, protected):
    hits = [
        c for c in tool_calls
        if c.get("name") in core.ACTION_TOOLS["write_file"]
        and any(core.is_within(core.tool_target(c), p) for p in protected if p)
    ]
    if hits:
        raise RunError(
            "the agent edited Claude's project memory, which is read-only for agy: "
            + "; ".join(core.tool_target(c) for c in hits)
        )


def memory_digest(directory):
    def fail(exc):
        raise RunError(f"cannot scan Claude's project memory {directory} completely: {exc}")

    digest, seen = {}, set()
    for base, dirs, files in os.walk(directory, onerror=fail, followlinks=True):
        real = os.path.realpath(base)
        if real in seen:
            dirs[:] = []
            continue
        seen.add(real)
        for name in files:
            path = Path(base) / name
            try:
                digest[str(path.relative_to(directory))] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                fail(exc)
    return digest


def memory_changes(before):
    changed = []
    for directory, old in before.items():
        new = memory_digest(directory)
        changed += [
            str(Path(directory) / k)
            for k in sorted(set(old) | set(new))
            if old.get(k) != new.get(k)
        ]
    return changed


# ---------------------------------------------------------------- workspace & sandbox checks

def workspace_roots(dirs):
    roots = sorted({os.path.realpath(d) for d in dirs})
    return [r for r in roots if not any(r != o and core.is_within(r, o) for o in roots)]


def entry_state(path):
    try:
        st = os.lstat(path)
    except OSError:
        return ("gone",)
    if stat.S_ISDIR(st.st_mode):
        return ("dir", st.st_mode, st.st_uid, st.st_gid)
    if stat.S_ISLNK(st.st_mode):
        return ("link", os.readlink(path))
    return ("file", st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode, st.st_uid, st.st_gid)


def workspace_snapshot(dirs):
    snap = {}
    for root in workspace_roots(dirs):
        snap[root] = entry_state(root)

        def unreadable(exc):
            snap[exc.filename] = ("unreadable",)

        for base, subdirs, files in os.walk(root, onerror=unreadable):
            for name in subdirs + files:
                path = os.path.join(base, name)
                snap[path] = entry_state(path)
            if len(snap) > WORKSPACE_SCAN_LIMIT:
                raise RunError(
                    f"the workspace has more than {WORKSPACE_SCAN_LIMIT} entries, too many for "
                    "read-only mode's change check; point -C/--cwd at the project directory itself"
                )
    return snap


def snapshot_changes(before, dirs):
    after = workspace_snapshot(dirs)
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


COMMAND_WRAPPERS = {
    "exec", "env", "nohup", "xargs", "command", "builtin", "time", "sudo", "nice", "timeout",
    "eval", "if", "elif", "while", "until", "then", "do", "else", "!", "{",
    "caffeinate", "script", "stdbuf", "arch",
}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
OPERATOR = re.compile(r"[;&|()<>\n]+\Z")
ARG_LIKE = re.compile(r"(-.*|[A-Za-z_][A-Za-z0-9_]*=.*|\d+(\.\d+)?[smhd]?)\Z")


def command_segments(line):
    lexer = shlex.shlex(
        line.replace("`", ";").replace("$(", ";("),
        posix=True,
        punctuation_chars=";&|()<>",
    )
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        tokens = re.split(r"\s+|([;&|()])", line)
    segment = []
    for token in [t for t in tokens if t] + [";"]:
        if OPERATOR.match(token):
            if segment:
                yield segment
            segment = []
        else:
            segment.append(token)


def starts_agent_cli(words, depth=0):
    words = list(words)
    if words[:2] in (["command", "-v"], ["command", "-V"], ["type", "-a"]):
        return False
    while words:
        name = os.path.basename(words[0])
        if name in SHELLS:
            flags = [
                i for i, w in enumerate(words[1:], 1)
                if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", w)
            ]
            if flags and flags[0] + 1 < len(words) and depth < 3:
                inner = words[flags[0] + 1]
                return any(starts_agent_cli(seg, depth + 1) for seg in command_segments(inner))
            words.pop(0)
            while words and words[0].startswith("-"):
                words.pop(0)
            continue
        if name in COMMAND_WRAPPERS or ARG_LIKE.match(words[0]):
            words.pop(0)
            continue
        return name in AGENT_CLIS
    return False


def agent_cli_calls(tool_calls):
    hits = []
    for call in tool_calls:
        if call.get("name") not in COMMAND_TOOLS:
            continue
        line = str((call.get("parameters") or {}).get("CommandLine") or "")
        if any(starts_agent_cli(seg) for seg in command_segments(line)):
            hits.append(line)
    return hits


def writes_outside(tool_calls, cwd, roots):
    out = []
    for call in tool_calls:
        if call.get("name") not in core.ACTION_TOOLS["write_file"]:
            continue
        target = core.tool_target(call)
        path = target if os.path.isabs(target) else os.path.join(cwd, target)
        if not any(core.is_within(path, r) for r in roots):
            out.append(target)
    return out


# Protobuf length-prefixed JSON arguments for run_command in agy's SQLite store (up to 5-byte varint).
STORED_CALL = re.compile(rb"(call_\d+)\x12\x0brun_command\x1a([\x80-\xff]{0,4}[\x00-\x7f])(?=\{)")


def _conversations_db_dir(environment=None):
    env = os.environ if environment is None else environment
    if env.get("AGY_CONVERSATIONS_DIR"):
        return Path(env["AGY_CONVERSATIONS_DIR"]).expanduser()
    home = core.home_dir(env)
    return home / ".gemini" / "antigravity-cli" / "conversations"


def stored_commands(session_id, environment=None):
    """Return ``{call_id: (CommandLine, BypassSandbox)}`` from agy's SQLite store, or None."""
    base = _conversations_db_dir(environment) / f"{session_id}.db"
    calls, seen = {}, False
    for path in (base, Path(str(base) + "-wal")):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            continue
        except OSError:
            return None
        seen = True
        for match in STORED_CALL.finditer(data):
            size = sum((byte & 0x7f) << (7 * i) for i, byte in enumerate(match.group(2)))
            try:
                args = json.loads(data[match.end():match.end() + size].decode("utf-8"))
            except ValueError:
                continue
            if isinstance(args, dict) and isinstance(args.get("CommandLine"), str):
                calls[match.group(1).decode()] = (
                    args["CommandLine"],
                    args.get("BypassSandbox") is True,
                )
    return calls if seen else None


def _is_bypass_param(call):
    val = (call.get("parameters") or {}).get("BypassSandbox")
    return val is True or (isinstance(val, str) and val.lower() == "true")


def unsandboxed_commands(session_id, tool_calls, before, environment=None):
    """Return commands executed with ``BypassSandbox`` in this turn, ``[]`` if clean, or ``None``."""
    cmd_calls = [c for c in tool_calls if c.get("name") in COMMAND_TOOLS]
    if not cmd_calls:
        return []
    direct = {
        str((c.get("parameters") or {}).get("CommandLine") or "<command>")
        for c in cmd_calls
        if _is_bypass_param(c)
    }
    stored = stored_commands(session_id, environment=environment)
    if stored is None:
        return sorted(direct) if direct else None
    new = [v for k, v in stored.items() if k not in before]
    bypassed = direct | {cmd for cmd, bypass in new if bypass}
    if bypassed:
        return sorted(bypassed)
    lines = [str((c.get("parameters") or {}).get("CommandLine") or "") for c in cmd_calls]
    if Counter(lines) - Counter(cmd for cmd, _ in new):
        return None
    return []


def install_agent_stubs(run_dir):
    bin_dir = Path(run_dir) / "bin"
    bin_dir.mkdir()
    for name in AGENT_CLIS:
        stub = bin_dir / name
        stub.write_text(AGENT_STUB, encoding="utf-8")
        stub.chmod(0o755)
    return str(bin_dir)


AGENT_DIRS = (".agents", ".agent", "_agents", "_agent")


def agent_file_name(path):
    target = path / "agent.md" if path.is_dir() else path
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = re.search(r"^name:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", text.split("\n---", 1)[0], re.M)
    return match.group(1) if match else None


def shadowing_agents(cwd):
    hits, here = [], Path(cwd)
    while True:
        for dot in AGENT_DIRS:
            folder = here / dot / "agents"
            if folder.is_dir():
                hits += [
                    str(p) for p in folder.iterdir()
                    if p.name.split(".")[0].startswith("agy-")
                    or (agent_file_name(p) or "").startswith("agy-")
                ]
        if (here / ".git").exists() or here.parent == here:
            return hits
        here = here.parent


# ---------------------------------------------------------------- turn orchestration

def requested_sandbox_from_opts(opts):
    if opts.sandbox is not None:
        return opts.sandbox
    if opts.yolo:
        return "danger-full-access"
    if opts.write:
        return "workspace-write"
    return None


def validate_opts(opts):
    if opts.effort and not EFFORT_PATTERN.match(opts.effort):
        raise RunError(f"invalid --effort {opts.effort!r}")
    if opts.model and not MODEL_PATTERN.match(opts.model):
        raise RunError(f"invalid --model {opts.model!r}")
    if opts.timeout <= 0:
        raise RunError("--timeout must be positive")


def resolve_turn_config(opts, root, resume_id):
    add_dirs = [resolve_dir(d, "--add-dir") for d in opts.add_dir]
    req_sandbox = requested_sandbox_from_opts(opts)

    if not resume_id:
        raw_cwd = opts.cwd if opts.cwd is not None else os.getcwd()
        cwd = resolve_dir(raw_cwd, "--cwd")
        sandbox = req_sandbox or "read-only"
        rules = not opts.no_rules
        return {
            "cwd": cwd,
            "sandbox": sandbox,
            "rules": rules,
            "model": opts.model,
            "effort": opts.effort,
            "effort_explicit": bool(opts.effort),
            "add_dirs": add_dirs,
        }

    state_path = root / "conversations" / f"{resume_id}.json"
    if not state_path.is_file():
        raise RunError(f"session {resume_id} was not started by agy-run (no {state_path})")
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    saved_cwd = saved["cwd"]
    if opts.cwd is not None:
        given_cwd = resolve_dir(opts.cwd, "--cwd")
        if os.path.realpath(given_cwd) != os.path.realpath(saved_cwd):
            raise RunError(f"--cwd {given_cwd} differs from session's {saved_cwd}")
    cwd = resolve_dir(saved_cwd, "saved cwd")

    saved_rules = saved.get("rules")
    if isinstance(saved_rules, str):
        saved_rules = saved_rules == "on"
    else:
        saved_rules = bool(saved_rules)
    if opts.no_rules and saved_rules:
        raise RunError("--no-rules cannot disable rules on an existing session started with rules enabled")

    saved_sandbox = saved.get("sandbox") or "read-only"
    sandbox = req_sandbox or saved_sandbox
    merged_add_dirs = sorted(set(saved.get("add_dirs", [])) | set(add_dirs))
    return {
        "cwd": cwd,
        "sandbox": sandbox,
        "rules": saved_rules,
        "model": opts.model or saved.get("model"),
        "effort": opts.effort or saved.get("effort"),
        "effort_explicit": bool(opts.effort),
        "add_dirs": merged_add_dirs,
    }


def _lexical_within(resolved_path, resolved_root):
    try:
        Path(resolved_path).relative_to(Path(resolved_root))
        return True
    except ValueError:
        return False


def _check_launcher_output_paths(opts, sandbox, cwd, add_dirs, protected_dirs):
    ro_roots = workspace_roots([cwd, *add_dirs]) if sandbox == "read-only" else []
    mem_roots = [os.path.realpath(d) for d in protected_dirs if d]
    for flag_name, raw_val in (
        ("--session-id-file", opts.session_id_file),
        ("-o/--output", opts.output),
    ):
        if not raw_val:
            continue
        abs_p = os.path.abspath(os.path.expanduser(str(raw_val)))
        # atomic_write creates its temp file and calls os.replace in dirname(abs_p)
        # (replacing a trailing symlink in-place). Check both the parent-resolved
        # lexical target (without following a trailing symlink) and realpath(abs_p).
        replace_loc = os.path.join(os.path.realpath(os.path.dirname(abs_p)), os.path.basename(abs_p))
        target_real = os.path.realpath(abs_p)
        for candidate in (replace_loc, target_real):
            if any(_lexical_within(candidate, r) for r in ro_roots):
                raise RunError(
                    f"{flag_name} ({raw_val}) points inside the read-only workspace; "
                    "place launcher output files outside the workspace (e.g. under /tmp)"
                )
            if any(_lexical_within(candidate, r) for r in mem_roots):
                raise RunError(
                    f"{flag_name} ({raw_val}) points inside Claude's read-only project memory"
                )


def run_turn(opts, task, root, stderr, environment=None):
    validate_not_nested(environment)
    validate_opts(opts)

    resume_id = core.resolve_session_id_arg(opts.resume) if opts.resume else None
    held_lock_fd = core.acquire_session_lock(root, resume_id) if resume_id else None
    try:
        cfg = resolve_turn_config(opts, root, resume_id)
        cwd = cfg["cwd"]
        sandbox = cfg["sandbox"]
        rules = cfg["rules"]
        add_dirs = cfg["add_dirs"]

        selected_agent = agent_name(sandbox, rules)
        ensure_agent_installed(selected_agent, environment=environment)

        shadows = shadowing_agents(cwd)
        if shadows:
            raise RunError(
                "project agents named agy-* would shadow agy-run's custom agents: "
                + ", ".join(shadows)
            )

        spec = MODES[sandbox]
        run_dir = root / "runs" / str(uuid.uuid4())
        run_dir.mkdir(parents=True, mode=0o700)
        try:
            mem_dirs, note, protected = ([], "", None)
            if rules:
                mem_dirs, note, protected = memory_plan(
                    cwd, add_dirs, sandbox, run_dir, environment=environment
                )
            _check_launcher_output_paths(opts, sandbox, cwd, add_dirs, [protected, *mem_dirs])
            workspace = [cwd, *add_dirs, *mem_dirs]
            watched = [protected, *mem_dirs] if (protected and sandbox in WRITABLE_MODES) else []
            before_mem = {d: memory_digest(d) for d in watched}
            ws_before = workspace_snapshot(workspace) if sandbox == "read-only" else None

            stub_path = install_agent_stubs(run_dir) + os.pathsep + (
                (environment or os.environ).get("PATH", "")
            )
            child_env = core.agy_env(
                {**spec.get("env", {}), "PATH": stub_path},
                environment=environment,
            )

            lead = ["[agy-run] Workspace: " + ", ".join([cwd, *add_dirs])]
            if note:
                lead.append(f"[agy-run] {note}")
            full_prompt = "".join(line + "\n\n" for line in lead) + task

            sandboxed = "--sandbox" in spec["flags"]
            calls_before = (
                (stored_commands(resume_id, environment=environment) or {})
                if (sandboxed and resume_id)
                else {}
            )

            def on_session_ready(session_id, _eff_model, _eff_effort):
                if opts.session_id_file:
                    core.atomic_write(opts.session_id_file, session_id + "\n")
                if stderr is not None:
                    print(f"{SESSION_ID_PREFIX}{session_id}", file=stderr, flush=True)

            def changes():
                found = []
                mem = memory_changes(before_mem)
                if mem:
                    found.append("Claude's project memory: " + ", ".join(mem))
                if ws_before is not None:
                    ws = snapshot_changes(ws_before, workspace)
                    if ws:
                        found.append(
                            "the workspace (read-only mode): "
                            + ", ".join(ws[:30])
                            + (f" and {len(ws) - 30} more" if len(ws) > 30 else "")
                        )
                return found

            def changes_after_failure():
                try:
                    return changes()
                except RunError as exc:
                    return [f"(could not check for changes: {exc})"]

            def on_lock_acquired(fd):
                nonlocal held_lock_fd
                held_lock_fd = fd

            try:
                result = core.run_acp_turn(
                    agent=agent_name(sandbox, rules),
                    prompt=full_prompt,
                    cwd=cwd,
                    allowed_tools=allowed_names(spec["tools"]),
                    state_dir=root,
                    model=cfg["model"],
                    effort=cfg["effort"],
                    effort_explicit=cfg["effort_explicit"],
                    resume_session_id=resume_id,
                    pre_lock_fd=held_lock_fd,
                    on_lock_acquired=on_lock_acquired,
                    add_dirs=add_dirs + mem_dirs,
                    extra_flags=spec["flags"],
                    timeout=opts.timeout,
                    env=child_env,
                    environment=environment,
                    on_session_ready=on_session_ready,
                    stderr=stderr,
                )
            except core.AgyError as exc:
                with core.signals_ignored():
                    changed = changes_after_failure()
                if changed:
                    exc.args = (str(exc) + "; also changed " + "; ".join(changed),)
                raise
            except BaseException:
                with core.signals_ignored():
                    changed = (
                        changes_after_failure()
                        if (before_mem or ws_before is not None)
                        else []
                    )
                if changed and stderr is not None:
                    print(
                        "agy-run: error: before the cancel, the agent changed "
                        + "; ".join(changed),
                        file=stderr,
                        flush=True,
                    )
                raise

            session_id = result["session_id"]
            with core.signals_ignored():
                unsandboxed = (
                    unsandboxed_commands(
                        session_id,
                        result["tool_calls"],
                        calls_before,
                        environment=environment,
                    )
                    if sandboxed
                    else None
                )
                changed = changes()
                if changed:
                    raise RunError(
                        "the agent changed "
                        + "; ".join(changed)
                        + (
                            "; commands it ran outside the sandbox: " + "; ".join(unsandboxed)
                            if unsandboxed
                            else ""
                        )
                    )
                if unsandboxed:
                    raise RunError(
                        "commands ran outside agy's sandbox (BypassSandbox): "
                        + "; ".join(unsandboxed)
                        + ". Tasks requiring unsandboxed host access must explicitly pass --yolo"
                    )
                if protected:
                    check_memory_untouched(result["tool_calls"], watched or [protected])
                clis = agent_cli_calls(result["tool_calls"])
                if clis:
                    raise RunError(
                        "agy tried to start Claude Code or Codex, which it must not do as a callee: "
                        + "; ".join(clis)
                    )
                if sandbox == "workspace-write":
                    outside = writes_outside(
                        result["tool_calls"], cwd, workspace_roots([cwd, *add_dirs])
                    )
                    if outside:
                        raise RunError(
                            "the agent wrote files outside the workspace in --write mode: "
                            + "; ".join(outside)
                        )

            state = {
                "sandbox": sandbox,
                "rules": rules,
                "cwd": cwd,
                "add_dirs": add_dirs,
                "model": result["model"],
                "effort": result["effort"],
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            core.atomic_write(
                root / "conversations" / f"{session_id}.json",
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            )
            if opts.session_id_file:
                core.atomic_write(opts.session_id_file, session_id + "\n")
            resp_text = result["response"]
            if opts.output:
                core.atomic_write(
                    opts.output,
                    resp_text if resp_text.endswith("\n") else resp_text + "\n",
                )
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

        meta = {
            "session_id": session_id,
            "model": result["model"],
            "effort": result["effort"],
            "sandbox": sandbox,
            "rules": rules,
            "add_dirs": add_dirs + mem_dirs,
            "tool_calls": len(result["tool_calls"]),
            "bg_tasks_drained": result["bg_tasks_drained"],
            "tokens": result["turn_usage"],
            "seconds": result["seconds"],
        }
        if sandboxed:
            meta["sandbox_check"] = "unverified" if unsandboxed is None else "ok"
        return resp_text, meta
    finally:
        if held_lock_fd is not None:
            core.release_session_lock(held_lock_fd)


# ---------------------------------------------------------------- CLI entry point

EXIT_CODES = {"timeout": 124, "usage": 2}


def write_agent_files(directory):
    """Write all 6 custom agent definitions (``agy-*.md``) to ``directory``."""
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    for name, text in agent_definitions().items():
        core.atomic_write(target_dir / name, text)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="agy_run.py",
        description="Run, resume, or live-steer a local agy agent via agy acp.",
    )
    parser.add_argument(
        "task",
        nargs="?",
        help="Prompt/message text. Reads from stdin or --prompt-file when omitted.",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="List available agy models (via `agy models`) and exit.",
    )
    parser.add_argument(
        "--install-agents",
        action="store_true",
        help="Install agy-run's 6 custom agent definitions into ~/.gemini/config/agents and exit.",
    )
    parser.add_argument(
        "--write-agent-files",
        metavar="DIR",
        help="Write/overwrite all 6 custom agent definitions into DIR and exit.",
    )
    parser.add_argument(
        "-m",
        "--model",
        help="Model slug from `agy models` (full or versioned suffix short name).",
    )
    parser.add_argument(
        "-e",
        "--effort",
        help="Reasoning effort (low/medium/high/xhigh; defaults to medium when supported).",
    )
    parser.add_argument(
        "-C",
        "--cwd",
        help="Working directory for the agy workspace (defaults to current directory; inherited on --resume).",
    )
    parser.add_argument(
        "-s",
        "--sandbox",
        choices=SANDBOX_CHOICES,
        default=None,
        help="Sandbox mode: read-only (default), workspace-write, or danger-full-access.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Shorthand for --sandbox workspace-write.",
    )
    parser.add_argument(
        "--yolo",
        action="store_true",
        help="Shorthand for --sandbox danger-full-access.",
    )
    parser.add_argument(
        "--add-dir",
        action="append",
        default=[],
        help="Additional directory exposed to agy workspace (repeatable).",
    )
    parser.add_argument(
        "--no-rules",
        action="store_true",
        help="Exclude user/project rules and customizations (for standalone media/image tasks).",
    )
    parser.add_argument(
        "--resume",
        metavar="SESSION_ID_OR_FILE",
        help="Resume an existing agy-run session by UUID or path to a session-id file.",
    )
    parser.add_argument(
        "--steer",
        metavar="SESSION_ID_OR_FILE",
        help="Inject a live mid-turn steer message into an active agy-run session.",
    )
    parser.add_argument(
        "--prompt-file",
        help="Read the prompt/message from a file instead of positional arg or stdin.",
    )
    parser.add_argument(
        "--session-id-file",
        dest="session_id_file",
        help="Write the active session UUID to this file as soon as the session starts/resumes.",
    )
    parser.add_argument(
        "--thread-id-file",
        "--conversation-file",
        dest="session_id_file",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Write the final agent message to this file (in addition to stdout).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Timeout in seconds (default: {DEFAULT_TIMEOUT:g}).",
    )
    return parser


def main(argv=None, stdin=None, stdout=None, stderr=None, environment=None):
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    env = os.environ if environment is None else environment
    argv = sys.argv[1:] if argv is None else list(argv)

    opts = build_parser().parse_args(argv)
    standalone_modes = sum(
        bool(x) for x in (opts.list_models, opts.install_agents, opts.write_agent_files)
    )
    if standalone_modes > 1:
        print(
            "agy-run: error: Cannot combine --list-models, --install-agents, and --write-agent-files.",
            file=stderr,
            flush=True,
        )
        return 2
    if standalone_modes and (opts.resume or opts.steer):
        print(
            "agy-run: error: Cannot combine --resume or --steer with info/setup flags.",
            file=stderr,
            flush=True,
        )
        return 2
    if opts.write_agent_files:
        write_agent_files(opts.write_agent_files)
        return 0
    if opts.install_agents:
        return install_agents(stdout=stdout, stderr=stderr, environment=env)

    if opts.resume and opts.steer:
        print("agy-run: error: Cannot use --resume and --steer together.", file=stderr, flush=True)
        return 2
    if sum(bool(x) for x in (opts.sandbox, opts.write, opts.yolo)) > 1:
        print("agy-run: error: Cannot combine --sandbox, --write, and --yolo.", file=stderr, flush=True)
        return 2
    if opts.timeout <= 0:
        print("agy-run: error: --timeout must be positive.", file=stderr, flush=True)
        return 2

    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, core.raise_cancelled)
        except ValueError:
            pass
    try:
        if opts.list_models:
            return list_models(stdout=stdout, stderr=stderr, environment=env)
        validate_not_nested(env)
        message = read_task_input(opts.task, opts.prompt_file, stdin)
        root = ensure_state(env)

        if opts.steer:
            sid = core.resolve_session_id_arg(opts.steer)
            resp = core.steer_active_session(
                root,
                sid,
                message,
                timeout=min(opts.timeout, 15.0),
            )
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()
            return 0

        text, meta = run_turn(opts, message, root, stderr=stderr, environment=env)
    except core.AgyError as exc:
        print("agy-run: error: " + str(exc), file=stderr, flush=True)
        return EXIT_CODES.get(exc.kind, 1)
    except core.Cancelled as exc:
        print(f"agy-run: error: cancelled by signal {exc.signum}", file=stderr, flush=True)
        return 130
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    stdout.write(text if text.endswith("\n") else text + "\n")
    stdout.flush()
    print(META_PREFIX + json.dumps(meta, ensure_ascii=False), file=stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
