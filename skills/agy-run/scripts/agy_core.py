#!/usr/bin/env python3
"""ACP (Agent Client Protocol) runtime core for agy_run.py.

Drives ``agy acp`` over stdio JSON-RPC in its own process group, supporting:
  - Immediate session creation/resumption and ``AGY_SESSION_ID`` publication
  - Model short-name / full-slug and reasoning effort configuration
  - Live mid-turn steering through a local Unix domain socket (``SteerServer``)
  - Automatic convergence wait when ``run_command`` exceeds ``WaitMsBeforeAsync``
    (10s max) and moves to agy's internal background task queue

Standard library only.
"""

import fcntl
import json
import os
import queue
import re
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

DEFAULT_AGY_BIN = "agy"
DELEGATE_MARKER = "AGY_RUN_ACTIVE"

# Environment handed to agy. A strict whitelist keeps GEMINI_API_KEY and
# AGY_LLM_GATEWAY_* out so runs stay on the user's subscription login.
ENV_KEEP = {
    "HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR", "TERM",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
}

FILTER_NOTICE = "This request was blocked by Gemini's filters"
INTERNAL_TOOLS = frozenset({"manage_task", "finish", "enter_plan_mode", "exit_plan_mode"})
STOP_GRACE = 5.0

ACTION_TOOLS = {
    "read_file": ("view_file", "list_dir", "grep_search", "find_by_name", "read_file"),
    "write_file": (
        "write_to_file", "replace_file_content", "multi_replace_file_content",
        "notebook_edit", "write_file", "edit_file",
    ),
    "command": ("run_command",),
    "read_url": ("read_url_content",),
    "mcp": ("call_mcp_tool",),
}

EFFORT_SUFFIX_RE = re.compile(r"^(?P<base>.+)-(?P<effort>low|medium|high|xhigh|max)\Z")
AGY_FALLBACK_DIRS = (Path("/usr/local/bin"), Path("/opt/homebrew/bin"))


class AgyError(Exception):
    """A user-facing failure; ``kind`` selects the process exit code."""

    def __init__(self, message, kind="error"):
        super().__init__(message)
        self.kind = kind


class Cancelled(BaseException):
    """SIGINT/SIGTERM arrived; unwinds past normal error handling."""

    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum


def raise_cancelled(signum, _frame):
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, signal.SIG_IGN)
    raise Cancelled(signum)


def clip(text, limit=1500):
    text = str(text)
    return text if len(text) <= limit else "..." + text[-limit:]


def home_dir(environment=None):
    env = os.environ if environment is None else environment
    home = env.get("HOME")
    return Path(home) if home else Path.home()


def agy_bin(environment=None):
    env = os.environ if environment is None else environment
    if env.get("AGY_BIN"):
        return env["AGY_BIN"]
    found = shutil.which("agy", path=env.get("PATH", ""))
    if found:
        return found
    home = home_dir(env)
    candidates = [home / ".local" / "bin" / "agy"] + [d / "agy" for d in AGY_FALLBACK_DIRS]
    for candidate in candidates:
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    return DEFAULT_AGY_BIN


def agy_env(extra=None, environment=None):
    src = os.environ if environment is None else environment
    env = {k: v for k, v in src.items() if k in ENV_KEEP or k.startswith("LC_")}
    all_proxy = env.get("ALL_PROXY") or env.get("all_proxy")
    if all_proxy:
        if not (env.get("HTTP_PROXY") or env.get("http_proxy")):
            env["HTTP_PROXY"] = all_proxy
        if not (env.get("HTTPS_PROXY") or env.get("https_proxy")):
            env["HTTPS_PROXY"] = all_proxy
    env[DELEGATE_MARKER] = "1"
    env.update(extra or {})
    return env


def validate_session_id(value):
    try:
        return str(uuid.UUID(str(value).strip()))
    except (AttributeError, TypeError, ValueError) as exc:
        raise AgyError(f"invalid session UUID: {value!r}") from exc


def resolve_session_id_arg(value):
    candidate = str(value or "").strip()
    try:
        return str(uuid.UUID(candidate))
    except ValueError:
        pass
    path = Path(candidate).expanduser()
    if path.is_file():
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        return validate_session_id(content)
    return validate_session_id(candidate)


def atomic_write(path, text):
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix="." + target.name + ".")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        tmp = None
    finally:
        if fd is not None:
            os.close(fd)
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------- agent files

def agent_markdown(name, description, tools, inherit, body):
    """Markdown custom agent as agy reads it (frontmatter + system prompt body)."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise ValueError(f"bad agent name {name!r}")
    lines = [
        "---",
        f"name: {name}",
        "description: " + json.dumps(description),
        "inheritCustomizations: " + ("true" if inherit else "false"),
        "excludeDefaultComponents: " + ("false" if inherit else "true"),
        "subagent: false",
        "tools: [" + ", ".join(tools) + "]",
        "---",
        body.rstrip("\n"),
        "",
    ]
    return "\n".join(lines)


def parse_agent_frontmatter(text):
    """Minimal reader for the frontmatter written by agent_markdown (tests, checks)."""
    if not text.startswith("---\n"):
        raise ValueError("no frontmatter")
    head = text[4:].split("\n---\n", 1)[0]
    out = {}
    for line in head.splitlines():
        key, _, value = line.partition(":")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            out[key.strip()] = [v.strip() for v in value[1:-1].split(",") if v.strip()]
        elif value in ("true", "false"):
            out[key.strip()] = value == "true"
        else:
            out[key.strip()] = value.strip('"')
    return out


# ---------------------------------------------------------------- process & signals

def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _close_proc_pipes(proc):
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def stop_group(proc, grace=STOP_GRACE):
    """SIGINT first, then SIGTERM, then SIGKILL across the child session group."""
    if proc is None:
        return
    try:
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError):
            pgid = proc.pid
        for sig, wait in ((signal.SIGINT, grace), (signal.SIGTERM, 3.0), (signal.SIGKILL, 5.0)):
            if proc.poll() is not None and not group_alive(pgid):
                return
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                pass
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if proc.poll() is not None and not group_alive(pgid):
                    return
                time.sleep(0.05)
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        if group_alive(pgid):
            raise AgyError(f"could not stop agy process group {pgid}")
    finally:
        try:
            if proc.poll() is not None:
                proc.wait(timeout=0.1)
        except Exception:
            pass
        _close_proc_pipes(proc)


class signals_ignored:
    """Ignore SIGINT/SIGTERM inside the block (main thread only); restore afterwards."""

    def __enter__(self):
        self.saved = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self.saved[sig] = signal.signal(sig, signal.SIG_IGN)
            except ValueError:
                pass
        return self

    def __exit__(self, *exc):
        for sig, handler in self.saved.items():
            signal.signal(sig, handler)


class signals_held:
    """Hold SIGINT/SIGTERM during Popen start; release() re-raises any queued signal."""

    def __enter__(self):
        self.saved, self.pending = {}, []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self.saved[sig] = signal.signal(sig, lambda signum, _f: self.pending.append(signum))
            except ValueError:
                pass
        return self

    def __exit__(self, *exc):
        for sig, handler in self.saved.items():
            signal.signal(sig, handler)

    def release(self):
        pending, self.pending = self.pending, []
        for sig in dict.fromkeys(pending):
            signal.raise_signal(sig)


# ---------------------------------------------------------------- locks & steer socket

def acquire_session_lock(state_dir, session_id):
    """Acquire an exclusive non-blocking flock for ``session_id``.

    The ``.lock`` file is intentionally never unlinked so concurrent processes
    cannot race on inode replacement.
    """
    active_dir = Path(state_dir) / "active"
    active_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = active_dir / f"{session_id}.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise AgyError(
            f"session {session_id} already has an active agy-run turn running; "
            "use --steer or wait for it to finish"
        ) from exc
    return fd


def release_session_lock(lock_fd):
    if lock_fd is None:
        return
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(lock_fd)
    except OSError:
        pass


def _private_short_sock_dir():
    short_dir = Path("/tmp") / f"agy-run-{os.getuid()}"
    short_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(str(short_dir))
    if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid():
        raise AgyError(f"unsafe socket directory ownership or symlink at {short_dir}")
    os.chmod(str(short_dir), 0o700)
    return short_dir


class SteerServer:
    """Per-session Unix domain socket server accepting live ``--steer`` messages."""

    def __init__(self, state_dir, session_id, handler, lock_fd=None, release_lock_on_close=True):
        self.state_dir = Path(state_dir)
        self.session_id = session_id
        self.handler = handler
        self.meta_path = self.state_dir / "active" / f"{session_id}.json"
        preferred = self.state_dir / "active" / f"{session_id}.sock"
        if len(str(preferred).encode("utf-8")) <= 95:
            self.sock_path = preferred
        else:
            self.sock_path = _private_short_sock_dir() / f"{session_id}.sock"
        self._lock_fd = lock_fd
        self._release_lock_on_close = release_lock_on_close
        self._server = None
        self._thread = None
        self._closed = False

    def start(self, initial_meta=None):
        self.sock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.meta_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._lock_fd is None:
            self._lock_fd = acquire_session_lock(self.state_dir, self.session_id)
        try:
            if self.sock_path.exists() or self.sock_path.is_symlink():
                self.sock_path.unlink()
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(str(self.sock_path))
            try:
                os.chmod(self.sock_path, 0o600)
            except OSError:
                pass
            srv.listen(4)
            srv.settimeout(0.2)
        except OSError as exc:
            srv.close()
            raise AgyError(f"failed to start steer socket at {self.sock_path}: {exc}") from exc
        self._server = srv
        meta = dict(initial_meta or {})
        meta.update({
            "session_id": self.session_id,
            "pid": os.getpid(),
            "socket_path": str(self.sock_path),
            "started_at": time.time(),
        })
        try:
            atomic_write(self.meta_path, json.dumps(meta, ensure_ascii=False) + "\n")
        except OSError as exc:
            raise AgyError(f"failed to write active session metadata {self.meta_path}: {exc}") from exc
        self._thread = threading.Thread(target=self._serve_loop, daemon=True)
        self._thread.start()
        return self._lock_fd

    def _serve_loop(self):
        while not self._closed and self._server is not None:
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    conn.settimeout(5.0)
                    raw = b""
                    while len(raw) < 1024 * 1024:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        raw += chunk
                        if b"\n" in raw:
                            break
                    req = json.loads(raw.decode("utf-8", errors="replace").strip())
                    resp = self.handler(req)
                except Exception as exc:
                    resp = {"ok": False, "error": str(exc)}
                try:
                    conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                except OSError:
                    pass

    def close(self):
        self._closed = True
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._lock_fd is not None:
            for p in (self.sock_path, self.meta_path):
                try:
                    if p.exists() or p.is_symlink():
                        p.unlink()
                except OSError:
                    pass
            if self._release_lock_on_close:
                release_session_lock(self._lock_fd)
            self._lock_fd = None


def steer_active_session(state_dir, session_id, message, timeout=10.0):
    """Deliver a live steer message to a running agy_run.py process."""
    session_id = validate_session_id(session_id)
    if not message or not message.strip():
        raise AgyError("steer message cannot be empty")
    state_dir = Path(state_dir)
    meta_file = state_dir / "active" / f"{session_id}.json"
    sock_file = state_dir / "active" / f"{session_id}.sock"
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if meta.get("socket_path"):
                sock_file = Path(meta["socket_path"])
        except Exception:
            pass
    if not sock_file.exists():
        raise AgyError(
            f"no active agy-run turn found for session {session_id} (socket not present); "
            "use --resume instead"
        )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(sock_file))
            payload = json.dumps({"action": "steer", "text": message}, ensure_ascii=False) + "\n"
            sock.sendall(payload.encode("utf-8"))
            raw = b""
            while len(raw) < 1024 * 1024:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                raw += chunk
                if b"\n" in raw:
                    break
    except OSError as exc:
        raise AgyError(
            f"failed to connect to active turn socket for session {session_id}: {exc} "
            "(use --resume if the turn already finished)"
        ) from exc
    if not raw.strip():
        raise AgyError(f"empty response from active turn socket for session {session_id}")
    resp = json.loads(raw.decode("utf-8", errors="replace").strip())
    if not resp.get("ok"):
        raise AgyError(f"steer rejected for session {session_id}: {resp.get('error') or 'unknown error'}")
    return resp


# ---------------------------------------------------------------- model & effort resolution

def _parse_family_version(slug, short_name):
    """Extract numeric version tuple from ``gemini-<ver>-<short>`` (e.g. (3, 8) or (4,))."""
    suffix = "-" + short_name
    if not slug.endswith(suffix):
        return None
    prefix = slug[:-len(suffix)]
    m = re.search(r"-(\d+(?:\.\d+)*)\Z", prefix)
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def match_acp_model(requested_model, config_options):
    """Resolve ``requested_model`` against ACP's ``model`` configOption.

    Returns ``(target_model_id, implied_effort_or_None)``.
    Supports:
      - ``None``: keep current ACP model
      - Exact ACP model option value (e.g. ``gemini-4-carbon``, ``gemini-3.8-flash``)
      - Full ``agy models`` slug with effort suffix (e.g. ``gemini-4-hydrogen-xhigh``)
      - Short tier/family alias (e.g. ``carbon``, ``hydrogen``, ``argon``, ``flash``, ``orchestrator``)
    """
    model_opt = next((c for c in (config_options or []) if c.get("id") == "model"), None)
    current = (model_opt or {}).get("currentValue")
    values = [
        o["value"]
        for o in ((model_opt or {}).get("options") or [])
        if isinstance(o, dict) and o.get("value")
    ]
    if not requested_model:
        return current, None
    if not values:
        return requested_model, None

    if requested_model in values:
        return requested_model, None

    m = EFFORT_SUFFIX_RE.match(requested_model)
    if m:
        if m.group("base") in values:
            return m.group("base"), m.group("effort")
        try:
            resolved_base, _ = match_acp_model(m.group("base"), config_options)
            return resolved_base, m.group("effort")
        except AgyError:
            pass

    if requested_model == "orchestrator" and "gemini-pro-orchestrator" in values:
        return "gemini-pro-orchestrator", None

    # Short name (e.g. carbon, flash, hydrogen, argon): prefer versioned slugs
    # like gemini-4-carbon / gemini-3.8-flash over composite names like
    # gemini-pro-orchestrator-argon, selecting the highest version tuple.
    tail_hits = [v for v in values if v.endswith("-" + requested_model)]
    versioned = [(ver, v) for v in tail_hits if (ver := _parse_family_version(v, requested_model)) is not None]
    if versioned:
        versioned.sort(key=lambda item: item[0] + (0,) * (6 - len(item[0])), reverse=True)
        return versioned[0][1], None
    if len(tail_hits) == 1:
        return tail_hits[0], None
    if len(tail_hits) > 1:
        raise AgyError(
            f"model {requested_model!r} is ambiguous ({', '.join(tail_hits)}); pass the full model name"
        )

    raise AgyError(f"unknown model {requested_model!r}; available: {', '.join(values)}")


def match_acp_effort(target_model, requested_effort, effort_explicit, config_options):
    """Validate and normalize ``requested_effort`` against ACP's ``thought_level`` option."""
    tl_opt = next((c for c in (config_options or []) if c.get("id") == "thought_level"), None)
    options = [
        o["value"]
        for o in ((tl_opt or {}).get("options") or [])
        if isinstance(o, dict) and o.get("value")
    ]
    if not options:
        if effort_explicit:
            raise AgyError(f"model {target_model!r} does not support --effort ({requested_effort!r})")
        return None

    effort = requested_effort or "medium"
    if effort == "extra" and "xhigh" in options:
        effort = "xhigh"
    if effort not in options:
        raise AgyError(
            f"model {target_model!r} does not support effort {effort!r}; "
            f"supported: {', '.join(options)}"
        )
    return effort


# ---------------------------------------------------------------- transcript background drain

def brain_transcript_path(session_id, environment=None):
    env = os.environ if environment is None else environment
    brain_root = env.get("AGY_BRAIN_DIR")
    if brain_root:
        base = Path(brain_root).expanduser() / session_id
    else:
        base = home_dir(env) / ".gemini" / "antigravity-cli" / "brain" / session_id
    logs = base / ".system_generated" / "logs"
    full = logs / "transcript_full.jsonl"
    return full if full.exists() else (logs / "transcript.jsonl")


def count_complete_lines(path):
    try:
        data = Path(path).read_bytes()
    except OSError:
        return 0
    lines = data.splitlines(keepends=True)
    if lines and not lines[-1].endswith(b"\n"):
        lines.pop()
    return len(lines)


def _decode_transcript_args(raw_args):
    if not isinstance(raw_args, dict):
        return {}
    out = {}
    for k, v in raw_args.items():
        if isinstance(v, str):
            try:
                out[k] = json.loads(v)
                continue
            except ValueError:
                pass
        out[k] = v
    return out


def inspect_transcript_drain(session_id, start_line, environment=None):
    """Inspect transcript steps appended since ``start_line`` for background task convergence.

    Returns a dict:
      - ``spawned``: set of ``<sid>/task-<N>`` IDs started in this turn
      - ``pending``: set of spawned task IDs without a completion SYSTEM_MESSAGE yet
      - ``converged``: True once ``spawned`` is non-empty, ``pending`` is empty, and
        agy has written the final post-wakeup ``PLANNER_RESPONSE`` (status DONE, no tool_calls)
      - ``final_text``: content of that final wakeup ``PLANNER_RESPONSE``
      - ``wakeup_tool_calls``: tool calls issued during silent wakeup steps
      - ``wakeup_usage``: token counts summed over silent wakeup ``PLANNER_RESPONSE`` steps
    """
    path = brain_transcript_path(session_id, environment)
    empty = {
        "spawned": set(),
        "pending": set(),
        "converged": False,
        "final_text": "",
        "wakeup_tool_calls": [],
        "wakeup_usage": {},
    }
    try:
        raw_bytes = path.read_bytes()
    except OSError:
        return empty

    raw_lines = raw_bytes.splitlines(keepends=True)
    if raw_lines and not raw_lines[-1].endswith(b"\n"):
        raw_lines.pop()
    if len(raw_lines) <= start_line:
        return empty

    steps = []
    for raw in raw_lines[start_line:]:
        try:
            steps.append(json.loads(raw.decode("utf-8", errors="replace")))
        except ValueError:
            continue

    sid_pat = re.escape(session_id)
    spawn_re = re.compile(
        rf"(?m)^Tool is running as a background task with task id:\s*({sid_pat}/task-\d+)\s*$"
    )
    done_re = re.compile(
        rf'\A\s*(?:(?:(?:The following is a <SYSTEM_MESSAGE>[^\n]*\n\s*)?<SYSTEM_MESSAGE>\s*)?'
        rf'\[Message\]\s+(?:(?!content=)\S+\s+)*content=)?'
        rf'Task id "({sid_pat}/task-\d+)" finished with result:'
    )

    spawned = set()
    completed = set()
    first_bg_pos = None
    last_sys_pos = -1
    wakeup_tool_calls = []
    wakeup_usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}

    for idx, step in enumerate(steps):
        stype = step.get("type")
        content = step.get("content") or ""
        if stype == "GENERIC":
            for m in spawn_re.finditer(content):
                spawned.add(m.group(1))
                if first_bg_pos is None:
                    first_bg_pos = idx
        elif stype == "SYSTEM_MESSAGE":
            m = done_re.match(content)
            if m:
                completed.add(m.group(1))
                last_sys_pos = idx
        elif stype == "PLANNER_RESPONSE" and first_bg_pos is not None and idx > last_sys_pos >= 0:
            for k in ("input_tokens", "output_tokens", "cache_read_tokens"):
                val = step.get(k)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    wakeup_usage[k] += int(val)
            for tc in step.get("tool_calls") or ():
                if isinstance(tc, dict) and tc.get("name"):
                    wakeup_tool_calls.append({
                        "name": tc["name"],
                        "parameters": _decode_transcript_args(tc.get("args") or {}),
                        "state": "DONE",
                    })

    pending = spawned - completed
    converged = False
    final_text = ""
    if spawned and not pending and last_sys_pos >= 0 and len(steps) > last_sys_pos + 1:
        last_step = steps[-1]
        if (
            last_step.get("type") == "PLANNER_RESPONSE"
            and last_step.get("status") == "DONE"
            and not last_step.get("tool_calls")
        ):
            converged = True
            final_text = last_step.get("content") or ""

    return {
        "spawned": spawned,
        "pending": pending,
        "converged": converged,
        "final_text": final_text,
        "wakeup_tool_calls": wakeup_tool_calls,
        "wakeup_usage": wakeup_usage,
    }


# ---------------------------------------------------------------- ACP command & turn loop

def build_acp_command(agent, add_dirs=(), extra_flags=(), environment=None):
    cmd = [agy_bin(environment), "acp", "--agent", agent]
    for d in add_dirs:
        cmd += ["--add-dir", str(d)]
    cmd += list(extra_flags)
    return cmd


def tool_target(call):
    params = call.get("parameters") or {}
    for key in (
        "AbsolutePath", "TargetFile", "NotebookPath", "DirectoryPath",
        "SearchPath", "SearchDirectory", "Path", "Url", "CommandLine",
        "query", "Query",
    ):
        if params.get(key):
            return str(params[key])
    return json.dumps(params, ensure_ascii=False)[:200] if params else ""


def _start_reader(stream, tag, out_queue):
    def _loop():
        try:
            for line in iter(stream.readline, ""):
                out_queue.put((tag, line))
        except Exception:
            pass
        finally:
            out_queue.put(("eof_" + tag, ""))

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t


def _merge_usage(dest, acp_usage):
    if not isinstance(acp_usage, dict):
        return
    mapping = {
        "input_tokens": "inputTokens",
        "output_tokens": "outputTokens",
        "cache_read_tokens": "cachedReadTokens",
        "thinking_tokens": "thoughtTokens",
        "total_tokens": "totalTokens",
    }
    for out_k, in_k in mapping.items():
        val = acp_usage.get(in_k)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            dest[out_k] = dest.get(out_k, 0) + int(val)


def _is_daemon_call(call):
    if call.get("name") != "run_command":
        return False
    val = (call.get("parameters") or {}).get("IsDaemon")
    return val is True or (isinstance(val, str) and val.lower() == "true")


def run_acp_turn(
    *,
    agent,
    prompt,
    cwd,
    allowed_tools,
    state_dir,
    model=None,
    effort=None,
    effort_explicit=False,
    resume_session_id=None,
    pre_lock_fd=None,
    on_lock_acquired=None,
    add_dirs=(),
    extra_flags=(),
    timeout=3600.0,
    env=None,
    environment=None,
    on_session_ready=None,
    stderr=None,
):
    """Run one bounded turn (including live steers and background command drain) via ``agy acp``."""
    cmd = build_acp_command(agent, add_dirs=add_dirs, extra_flags=extra_flags, environment=environment)
    child_env = env if env is not None else agy_env(environment=environment)
    caller_owns_lock = on_lock_acquired is not None
    started = time.monotonic()
    deadline = started + float(timeout)

    event_q = queue.Queue()
    proc = None
    steer_server = None
    watchdog = None
    watchdog_fired = threading.Event()
    next_rpc_id = 10

    def send_rpc(payload):
        if proc is None or proc.stdin is None:
            raise AgyError("agy acp stdin is closed")
        try:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except OSError as exc:
            raise AgyError(f"failed to write to agy acp stdin: {exc}") from exc

    def on_socket_request(req):
        reply_q = queue.Queue(maxsize=1)
        event_q.put(("steer", req, reply_q))
        try:
            return reply_q.get(timeout=4.5)
        except queue.Empty:
            return {"ok": False, "error": "timed out waiting for agy-run event loop"}

    def on_watchdog():
        watchdog_fired.set()
        if proc is not None:
            try:
                stop_group(proc)
            except Exception:
                pass
        event_q.put(("timeout", ""))

    held = signals_held()
    try:
        with held:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=str(cwd),
                    env=child_env,
                    start_new_session=True,
                    text=True,
                    bufsize=1,
                )
            except FileNotFoundError:
                pass
        held.release()
        if proc is None:
            raise AgyError(f"{cmd[0]} not found on PATH (Antigravity CLI)")

        watchdog = threading.Timer(float(timeout), on_watchdog)
        watchdog.daemon = True
        watchdog.start()

        _start_reader(proc.stdout, "stdout", event_q)
        _start_reader(proc.stderr, "stderr", event_q)

        stderr_lines = []

        def wait_rpc_response(expected_id, step_name):
            while True:
                if watchdog_fired.is_set() or time.monotonic() >= deadline:
                    raise AgyError(f"agy timed out after {timeout:.0f}s during {step_name}", kind="timeout")
                wait_slice = max(0.05, min(0.5, deadline - time.monotonic()))
                try:
                    item = event_q.get(timeout=wait_slice)
                except queue.Empty:
                    if proc.poll() is not None:
                        detail = "".join(stderr_lines).strip()
                        raise AgyError(
                            f"agy acp exited ({proc.returncode}) during {step_name}"
                            + (f": {clip(detail)}" if detail else "")
                        )
                    continue
                tag = item[0]
                if tag == "timeout":
                    raise AgyError(f"agy timed out after {timeout:.0f}s during {step_name}", kind="timeout")
                if tag == "stderr":
                    stderr_lines.append(item[1])
                    continue
                if tag == "eof_stdout":
                    detail = "".join(stderr_lines).strip()
                    raise AgyError(
                        f"agy acp closed stdout during {step_name}"
                        + (f": {clip(detail)}" if detail else "")
                    )
                if tag == "steer":
                    # Queue early steer until prompt starts
                    early_steers.append((item[1], item[2]))
                    continue
                if tag != "stdout":
                    continue
                line = item[1].strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == expected_id and "method" not in msg:
                    if "error" in msg and msg["error"]:
                        err = msg["error"]
                        em = err.get("message") if isinstance(err, dict) else str(err)
                        raise AgyError(f"agy acp {step_name} error: {clip(em)}")
                    return msg.get("result") or {}
                # Buffer any notifications arriving during handshake
                buffered_stdout.append(msg)

        early_steers = []
        buffered_stdout = []

        # 1. ACP handshake
        send_rpc({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientInfo": {"name": "agy-run", "version": "2.0"},
                "capabilities": {},
            },
        })
        wait_rpc_response(1, "initialize")

        # 2. Create or resume session
        if resume_session_id:
            send_rpc({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/resume",
                "params": {"sessionId": resume_session_id, "cwd": str(cwd), "mcpServers": []},
            })
            sess_res = wait_rpc_response(2, "session/resume")
        else:
            send_rpc({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/new",
                "params": {"cwd": str(cwd), "mcpServers": []},
            })
            sess_res = wait_rpc_response(2, "session/new")

        session_id = validate_session_id(sess_res.get("sessionId"))
        if resume_session_id and session_id != resume_session_id:
            raise AgyError(f"agy resumed session {session_id}, expected {resume_session_id}")

        config_options = sess_res.get("configOptions") or []

        # 3. Configure model & reasoning effort
        target_model, implied_effort = match_acp_model(model, config_options)
        current_model = next(
            (c.get("currentValue") for c in config_options if c.get("id") == "model"),
            None,
        )
        if target_model and target_model != current_model:
            send_rpc({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/set_config_option",
                "params": {"sessionId": session_id, "configId": "model", "value": target_model},
            })
            mod_res = wait_rpc_response(3, "set model")
            config_options = mod_res.get("configOptions") or config_options

        desired_effort = effort if effort_explicit else (implied_effort or effort or "medium")
        effective_effort = match_acp_effort(target_model, desired_effort, effort_explicit, config_options)
        current_effort = next(
            (c.get("currentValue") for c in config_options if c.get("id") == "thought_level"),
            None,
        )
        if effective_effort and effective_effort != current_effort:
            send_rpc({
                "jsonrpc": "2.0",
                "id": 4,
                "method": "session/set_config_option",
                "params": {
                    "sessionId": session_id,
                    "configId": "thought_level",
                    "value": effective_effort,
                },
            })
            wait_rpc_response(4, "set effort")

        effective_model = target_model or current_model or model

        # 4. Acquire/transfer session lock, start SteerServer, publish session ID
        caller_owns_lock = on_lock_acquired is not None
        caller_had_pre_lock = caller_owns_lock and pre_lock_fd is not None
        lock_to_pass = pre_lock_fd
        pre_lock_fd = None
        steer_server = SteerServer(
            state_dir,
            session_id,
            on_socket_request,
            lock_fd=lock_to_pass,
            release_lock_on_close=not caller_had_pre_lock,
        )
        acquired_fd = steer_server.start(initial_meta={
            "cwd": str(cwd),
            "model": effective_model,
            "effort": effective_effort,
        })
        if on_lock_acquired is not None:
            on_lock_acquired(acquired_fd)
            steer_server._release_lock_on_close = False
        if on_session_ready is not None:
            on_session_ready(session_id, effective_model, effective_effort)

        # 5. Send initial prompt and run main event + background-drain loop
        transcript_start = count_complete_lines(brain_transcript_path(session_id, environment))

        in_flight_prompts = []
        superseded_prompts = set()
        current_chunks = []
        tools_by_id = {}
        tool_order = []
        denied_calls = []
        turn_usage = {}
        queued_steers = []
        waiting_bg_drain = False
        bg_grace_until = 0.0
        untracked_in_prog_cmds = []
        bg_announced = set()
        drained_bg_tasks = set()
        final_wakeup_text = None
        wakeup_tool_calls = []
        stdout_eof = False

        def mark_in_progress_commands_drained():
            for tid in tool_order:
                if (
                    tools_by_id[tid].get("name") == "run_command"
                    and tools_by_id[tid].get("state") in ("pending", "in_progress")
                ):
                    tools_by_id[tid]["state"] = "drained"

        def send_prompt_rpc(text_body):
            nonlocal next_rpc_id
            pid = next_rpc_id
            next_rpc_id += 1
            in_flight_prompts.append(pid)
            send_rpc({
                "jsonrpc": "2.0",
                "id": pid,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": text_body}],
                },
            })
            return pid

        send_prompt_rpc(prompt)

        # Flush any steers that arrived between SteerServer.start() and first prompt
        for s_req, s_reply in early_steers:
            event_q.put(("steer", s_req, s_reply))
        early_steers.clear()

        def handle_acp_message(msg):
            nonlocal waiting_bg_drain, bg_grace_until, final_wakeup_text, transcript_start
            # ServerRequest (permission prompt or elicitation form)
            if "id" in msg and "method" in msg:
                method = msg.get("method")
                params = msg.get("params") or {}
                if method == "session/request_permission":
                    tc = params.get("toolCall") or {}
                    opts = params.get("options") or []
                    rej_id = next(
                        (
                            o["optionId"]
                            for o in opts
                            if isinstance(o, dict)
                            and o.get("optionId")
                            and (
                                "reject" in str(o.get("kind", "")).lower()
                                or "reject" in str(o["optionId"]).lower()
                                or "deny" in str(o["optionId"]).lower()
                            )
                        ),
                        opts[-1]["optionId"] if opts and isinstance(opts[-1], dict) else "deny",
                    )
                    denied_calls.append({
                        "name": tc.get("name") or "?",
                        "parameters": tc.get("args") or tc.get("rawInput") or {},
                    })
                    send_rpc({
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "result": {"outcome": {"outcome": "selected", "optionId": rej_id}},
                    })
                else:
                    send_rpc({
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "error": {
                            "code": -32601,
                            "message": f"ServerRequest {method} is not supported in headless agy-run",
                        },
                    })
                return False

            # JSON-RPC Response to session/prompt
            if "id" in msg and "method" not in msg:
                rid = msg.get("id")
                if rid not in in_flight_prompts:
                    return False
                in_flight_prompts.remove(rid)
                if "error" in msg and msg["error"]:
                    err = msg["error"]
                    em = err.get("message") if isinstance(err, dict) else str(err)
                    code = err.get("code") if isinstance(err, dict) else None
                    raise AgyError(f"agy prompt error: {clip(em)}", kind="api" if code == -32000 else "error")
                res = msg.get("result") or {}
                _merge_usage(turn_usage, res.get("usage"))
                stop_reason = res.get("stopReason")
                if rid in superseded_prompts:
                    superseded_prompts.discard(rid)
                    return False
                if stop_reason and stop_reason not in ("end_turn",):
                    raise AgyError(f"agy ended turn with stopReason={stop_reason!r}")
                if in_flight_prompts:
                    return False

                # Check for forbidden IsDaemon=true before waiting on background tasks
                daemon_hits = [
                    tool_target(tools_by_id[tid])
                    for tid in tool_order
                    if _is_daemon_call(tools_by_id[tid])
                ]
                if daemon_hits:
                    raise AgyError(
                        "agy started a daemon command (IsDaemon=true), which is forbidden in bounded agy-run tasks: "
                        + "; ".join(daemon_hits)
                    )

                # Did any run_command turn into a background task (>WaitMsBeforeAsync)?
                in_prog_cmds = [
                    tid for tid in tool_order
                    if tools_by_id[tid].get("name") == "run_command"
                    and tools_by_id[tid].get("state") in ("pending", "in_progress")
                ]
                drain_state = inspect_transcript_drain(session_id, transcript_start, environment)
                if drain_state["spawned"] and not drain_state["converged"]:
                    waiting_bg_drain = True
                    return False
                if drain_state["converged"]:
                    mark_in_progress_commands_drained()
                    drained_bg_tasks.update(drain_state["spawned"])
                    wakeup_tool_calls.extend(drain_state["wakeup_tool_calls"])
                    for uk, uv in drain_state["wakeup_usage"].items():
                        turn_usage[uk] = turn_usage.get(uk, 0) + uv
                    if drain_state["final_text"].strip():
                        final_wakeup_text = drain_state["final_text"]
                elif in_prog_cmds:
                    untracked_in_prog_cmds[:] = [
                        tool_target(tools_by_id[tid]) or "run_command"
                        for tid in in_prog_cmds
                    ]
                    waiting_bg_drain = True
                    bg_grace_until = time.monotonic() + 5.0
                    return False

                if queued_steers:
                    combined = "\n\n".join(queued_steers)
                    queued_steers.clear()
                    transcript_start = count_complete_lines(brain_transcript_path(session_id, environment))
                    current_chunks.clear()
                    final_wakeup_text = None
                    send_prompt_rpc("[Caller steer]\n" + combined)
                    return False
                return True

            # Notification (session/update)
            if msg.get("method") == "session/update":
                upd = (msg.get("params") or {}).get("update") or {}
                utype = upd.get("sessionUpdate")
                if utype == "agent_message_chunk":
                    txt = (upd.get("content") or {}).get("text") or ""
                    if txt:
                        current_chunks.append(txt)
                elif utype in ("tool_call", "tool_call_update"):
                    tc_id = upd.get("toolCallId") or f"anon_{len(tool_order)}"
                    if tc_id not in tools_by_id:
                        tools_by_id[tc_id] = {}
                        tool_order.append(tc_id)
                    entry = tools_by_id[tc_id]
                    if upd.get("name"):
                        entry["name"] = upd["name"]
                    params = upd.get("args") if upd.get("args") is not None else upd.get("rawInput")
                    if isinstance(params, dict) and params:
                        entry["parameters"] = params
                    if upd.get("status"):
                        entry["state"] = upd["status"]
            return False

        done = False
        for buffered in buffered_stdout:
            if handle_acp_message(buffered):
                done = True
                break
        buffered_stdout.clear()

        while not done:
            if watchdog_fired.is_set() or time.monotonic() >= deadline:
                raise AgyError(f"agy did not finish within {timeout:.0f}s; killed", kind="timeout")

            if waiting_bg_drain and not in_flight_prompts:
                drain_state = inspect_transcript_drain(session_id, transcript_start, environment)
                new_bg = drain_state["spawned"] - bg_announced
                if new_bg and stderr is not None:
                    bg_announced.update(new_bg)
                    names = ", ".join(t.split("/")[-1] for t in sorted(new_bg))
                    print(
                        f"[agy-run] Background command running ({names}), waiting for convergence...",
                        file=stderr,
                        flush=True,
                    )
                if drain_state["converged"]:
                    # Brief settle re-check to ensure transcript flush is complete
                    time.sleep(0.1)
                    drain_state = inspect_transcript_drain(session_id, transcript_start, environment)
                    if drain_state["converged"]:
                        waiting_bg_drain = False
                        mark_in_progress_commands_drained()
                        drained_bg_tasks.update(drain_state["spawned"])
                        wakeup_tool_calls.extend(drain_state["wakeup_tool_calls"])
                        for uk, uv in drain_state["wakeup_usage"].items():
                            turn_usage[uk] = turn_usage.get(uk, 0) + uv
                        if drain_state["final_text"].strip():
                            final_wakeup_text = drain_state["final_text"]
                        if queued_steers:
                            combined = "\n\n".join(queued_steers)
                            queued_steers.clear()
                            transcript_start = count_complete_lines(brain_transcript_path(session_id, environment))
                            current_chunks.clear()
                            final_wakeup_text = None
                            send_prompt_rpc("[Caller steer]\n" + combined)
                            continue
                        done = True
                        break
                elif not drain_state["spawned"] and bg_grace_until and time.monotonic() >= bg_grace_until:
                    raise AgyError(
                        "agy ended turn with run_command still in_progress, and no background task appeared in transcript: "
                        + "; ".join(untracked_in_prog_cmds or ["<run_command>"])
                    )

            try:
                item = event_q.get(timeout=0.1)
            except queue.Empty:
                if stdout_eof and proc.poll() is not None:
                    detail = "".join(stderr_lines).strip()
                    raise AgyError(
                        f"agy acp exited unexpectedly ({proc.returncode}) before turn converged"
                        + (f": {clip(detail)}" if detail else "")
                    )
                continue

            tag = item[0]
            if tag == "timeout":
                raise AgyError(f"agy did not finish within {timeout:.0f}s; killed", kind="timeout")
            if tag == "stderr":
                stderr_lines.append(item[1])
                continue
            if tag == "eof_stderr":
                continue
            if tag == "eof_stdout":
                stdout_eof = True
                continue
            if tag == "steer":
                _, req, reply_q = item
                steer_text = (req.get("text") or "").strip()
                if not steer_text:
                    reply_q.put({"ok": False, "error": "empty steer message"})
                elif in_flight_prompts:
                    for old_id in in_flight_prompts:
                        superseded_prompts.add(old_id)
                    current_chunks.clear()
                    final_wakeup_text = None
                    new_pid = send_prompt_rpc("[Caller steer]\n" + steer_text)
                    reply_q.put({
                        "ok": True,
                        "session_id": session_id,
                        "prompt_id": new_pid,
                        "mode": "live_steer",
                    })
                elif waiting_bg_drain:
                    queued_steers.append(steer_text)
                    reply_q.put({
                        "ok": True,
                        "session_id": session_id,
                        "mode": "queued_for_bg_drain",
                    })
                else:
                    reply_q.put({"ok": False, "error": "turn already finished; use --resume instead"})
                continue

            if tag == "stdout":
                raw = item[1].strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if handle_acp_message(msg):
                    done = True

        # Clean shutdown of ACP server
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        with signals_ignored():
            stop_group(proc)

        # Post-turn stream & tool validations
        all_tool_calls = [tools_by_id[tid] for tid in tool_order] + wakeup_tool_calls
        for tc in wakeup_tool_calls:
            if _is_daemon_call(tc):
                raise AgyError(
                    "agy started a daemon command (IsDaemon=true) during background wakeup: "
                    + tool_target(tc)
                )

        if denied_calls:
            attempted = "; ".join(f"{c.get('name')} {tool_target(c)}".strip() for c in denied_calls)
            raise AgyError(
                f"agy requested interactive permission (denied in headless mode): {attempted}",
                kind="denied",
            )

        allowed = set(allowed_tools) | INTERNAL_TOOLS
        bad_tools = [c for c in all_tool_calls if c.get("name") and c.get("name") not in allowed]
        if bad_tools:
            raise AgyError(
                "agy called tools outside this sandbox mode: "
                + "; ".join(f"{c.get('name')} {tool_target(c)}".strip() for c in bad_tools),
                kind="tool",
            )

        response = (
            final_wakeup_text
            if final_wakeup_text is not None
            else "".join(current_chunks)
        )
        if FILTER_NOTICE in response:
            raise AgyError(
                "Gemini's safety filter blocked the answer. Retry or rephrase the prompt.",
                kind="filtered",
            )
        if not response.strip():
            detail = "".join(stderr_lines).strip()
            raise AgyError("agy returned an empty answer" + (f": {clip(detail, 500)}" if detail else ""))

        return {
            "response": response,
            "session_id": session_id,
            "model": effective_model,
            "effort": effective_effort,
            "turn_usage": turn_usage,
            "tool_calls": all_tool_calls,
            "bg_tasks_drained": len(drained_bg_tasks),
            "seconds": round(time.monotonic() - started, 1),
        }

    except BaseException:
        if proc is not None:
            with signals_ignored():
                stop_group(proc)
        raise
    finally:
        if watchdog is not None:
            watchdog.cancel()
        try:
            if proc is not None:
                with signals_ignored():
                    stop_group(proc)
        finally:
            try:
                if steer_server is not None:
                    steer_server.close()
            finally:
                if pre_lock_fd is not None and not caller_owns_lock:
                    release_session_lock(pre_lock_fd)


# ---------------------------------------------------------------- files & memory helpers

def claude_memory_dir(cwd, environment=None):
    """Claude Code's project memory directory for cwd, or None when there is none."""
    # 1. Repo-local .agents/memory/MEMORY.md
    local_mem = Path(cwd) / ".agents" / "memory"
    if (local_mem / "MEMORY.md").is_file():
        return local_mem
    # 2. ~/.claude/projects/<encoded-path>/memory/MEMORY.md
    projects = home_dir(environment) / ".claude" / "projects"
    seen = []
    for form in (os.path.abspath(str(cwd)), os.path.realpath(str(cwd))):
        name = re.sub(r"[^A-Za-z0-9]", "-", form)
        if name in seen:
            continue
        seen.append(name)
        candidate = projects / name / "memory"
        if (candidate / "MEMORY.md").is_file():
            return candidate
    return None


def is_within(path, root):
    try:
        Path(os.path.realpath(str(path))).relative_to(os.path.realpath(str(root)))
        return True
    except ValueError:
        return False
