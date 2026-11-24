#!/usr/bin/env python3
"""Run, resume, or live-steer a local Codex agent via codex app-server --stdio.

See ../DESIGN.md for architecture, state machine (anti-early-exit subagent drain,
bounded auto-continue, live turn/steer socket), and concurrency lock invariants.
"""

import argparse
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
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 3600.0
DEFAULT_SANDBOX = "read-only"
SANDBOX_CHOICES = ("read-only", "workspace-write", "danger-full-access")
PROCESS_KILL_GRACE_SECONDS = 2.0
MAX_AUTO_CONTINUES = 3

DELEGATE_MARKER = "CODEX_RUN_ACTIVE"

PROFILE_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")
EFFORT_PATTERN = re.compile(r"[a-z]+\Z")
_TOML_TOP_LEVEL_STR_RE = re.compile(
    r"""^\s*(model|model_reasoning_effort|model_catalog_json)\s*=\s*(?:"((?:\\.|[^"\\])*)"|'([^']*)')\s*(?:#.*)?$"""
)
_TOML_MULTILINE_START_RE = re.compile(r"""^\s*[^=#\s][^=]*=\s*(\"\"\"|''')(.*)$""")
_TOML_UNESCAPED_TRIPLE_DQUOTE_RE = re.compile(r'(?<!\\)(?:\\\\)*"""')
SECRET_NAME_PATTERN = re.compile(
    r"(?:^|_)(?:API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|"
    r"COOKIE|AUTH|BEARER|DATABASE_URL|DSN|SESSION)(?:_|$)",
    re.IGNORECASE,
)
SHELL_ENV_EXCLUDES = (
    "*PASSWORD*",
    "*PASSWD*",
    "*CREDENTIAL*",
    "*COOKIE*",
    "*AUTH*",
    "*BEARER*",
    "*DATABASE_URL*",
    "*DSN*",
    "*PRIVATE*",
    "*SESSION*",
)

DELEGATE_GUARD = (
    "You are running inside a non-interactive codex-run session. "
    "Obey every loaded AGENTS.md rule and the original task's authorization. "
    "Access only resources required for the task; do not inspect or expose "
    "credentials or unrelated secrets. Complete the task before ending your turn: "
    "if you start background commands or built-in subagents, wait for them "
    "(via write_stdin or wait_agent) and integrate their results in this turn. "
    "Only end your turn early if you are genuinely blocked on a decision that "
    "requires the caller's input. Do not invoke codex-run again."
)

DEEPSEEK_GUARD = (
    "Provider-specific guard: do not call spawn_agent or any other built-in "
    "subagent tool in this DeepSeek root. Complete the task in this root. "
    "Current Codex V2 external-provider agent messages do not deliver the "
    "plaintext child task reliably, and full-history forks can replay the root "
    "delegation instruction recursively. If the task explicitly requires a "
    "DeepSeek subagent, report that limitation instead of attempting it."
)

AUTO_CONTINUE_PROMPT = (
    "[System notice: The internal subagent(s) you spawned were still running when your "
    "turn ended and have now finished. Call wait_agent (or inspect their completed status) "
    "to incorporate their results. If you only paused to wait for the subagent(s), "
    "finish the task now; if you still need a decision or clarification from the caller "
    "before proceeding, include the collected subagent findings and clearly restate your "
    "question for the caller, then end your turn.]"
)

OPT_OUT_NOTIFICATIONS = [
    "item/agentMessage/delta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/summaryPartAdded",
    "item/reasoning/textDelta",
]


class CodexRunError(Exception):
    """A user-facing codex-run failure."""


class Cancelled(BaseException):
    """SIGTERM arrived; unwinds past normal error handling into finally blocks."""

    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum


def _raise_cancelled(signum, _frame):
    signal.signal(signum, signal.SIG_IGN)
    raise Cancelled(signum)


def known_secret_values(environment=None):
    values = []
    environment = os.environ if environment is None else environment
    for name, value in environment.items():
        if SECRET_NAME_PATTERN.search(name) and value and len(value) >= 6:
            values.append(value)
    return sorted(set(values), key=len, reverse=True)


def sanitize(message, secrets=()):
    text = str(message)
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")

    text = re.sub(r"(?i)\bBearer\s+[^\s,;\"']+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)\bsk-[A-Za-z0-9._*~-]{4,}", "sk-[REDACTED]", text)
    text = re.sub(
        r"(?i)(?<![A-Za-z0-9_])([\"']?(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|"
        r"password|credential|secret|token|key)[\"']?\s*[:=]\s*)([\"'])(.*?)\2",
        lambda m: "%s%s[REDACTED]%s" % (m.group(1), m.group(2), m.group(2)),
        text,
    )
    text = re.sub(
        r"(?i)(?<![A-Za-z0-9_])([\"']?(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|"
        r"password|credential|secret|token|key)[\"']?\s*[:=]\s*)[^\s,;\"']+",
        lambda m: "%s[REDACTED]" % m.group(1),
        text,
    )
    text = re.sub(
        r"(?<![A-Za-z0-9])(?=[A-Za-z0-9._*~-]{8,}(?![A-Za-z0-9]))"
        r"[A-Za-z0-9._~-]*\*{3,}[A-Za-z0-9._*~-]*(?![A-Za-z0-9])",
        "[REDACTED]",
        text,
    )
    return text


def resolve_home_dir(environment=None):
    env = os.environ if environment is None else environment
    home = env.get("HOME")
    if home:
        return Path(home)
    return Path.home()


def expand_path_with_env(raw_path, environment=None, base_dir=None):
    text = str(raw_path)
    if text == "~":
        path = resolve_home_dir(environment)
    elif text.startswith("~/"):
        path = resolve_home_dir(environment) / text[2:]
    else:
        path = Path(text).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = Path(base_dir) / path
    return path


def resolve_codex_home(environment=None):
    env = os.environ if environment is None else environment
    raw = env.get("CODEX_HOME")
    if raw:
        return expand_path_with_env(raw, env)
    return resolve_home_dir(env) / ".codex"


def state_root(environment=None):
    env = os.environ if environment is None else environment
    custom = env.get("CODEX_RUN_STATE_DIR")
    if custom:
        out = expand_path_with_env(custom, env)
    else:
        base = env.get("XDG_STATE_HOME", "")
        root = (
            Path(base)
            if base and os.path.isabs(base)
            else resolve_home_dir(env) / ".local" / "state"
        )
        out = root / "codex-run"
    for sub in ("threads", "active"):
        (out / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
    return out


def active_meta_path(thread_id, environment=None):
    return state_root(environment) / "active" / ("%s.json" % thread_id)


def active_socket_path(thread_id, environment=None):
    return state_root(environment) / "active" / ("%s.sock" % thread_id)


def thread_meta_path(thread_id, environment=None):
    return state_root(environment) / "threads" / ("%s.json" % thread_id)


def atomic_write_text(target_path, content):
    target = Path(target_path).expanduser()
    try:
        parent = target.parent.resolve(strict=True)
    except OSError as exc:
        raise CodexRunError("Target directory does not exist: %s" % target.parent) from exc
    if not parent.is_dir() or not target.name:
        raise CodexRunError("Invalid target file path: %s" % target_path)
    target = parent / target.name
    temp_fd = None
    temp_path = None
    try:
        temp_fd, temp_path = tempfile.mkstemp(prefix=".%s." % target.name, dir=str(parent))
        os.fchmod(temp_fd, 0o600)
        with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
            temp_fd = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        temp_path = None
    except OSError as exc:
        raise CodexRunError("Could not write file: %s" % target) from exc
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def validate_not_nested(environment=None):
    env = os.environ if environment is None else environment
    if env.get(DELEGATE_MARKER) == "1":
        raise CodexRunError("Nested codex-run invocation is not allowed inside a delegated session.")


def validate_profile(profile, environment=None):
    if not PROFILE_PATTERN.fullmatch(profile):
        raise CodexRunError(
            "Profile names may contain only letters, numbers, underscores, and hyphens."
        )
    profile_path = resolve_codex_home(environment) / ("%s.config.toml" % profile)
    if not profile_path.is_file():
        raise CodexRunError(
            "Codex profile '%s' was not found; install or create the profile config at %s."
            % (profile, profile_path)
        )
    return profile_path


def validate_thread_id(value):
    try:
        return str(uuid.UUID(str(value).strip()))
    except (AttributeError, TypeError, ValueError) as exc:
        raise CodexRunError("Resume/steer requires an exact Codex thread UUID.") from exc


def resolve_thread_id_arg(value):
    candidate = str(value or "").strip()
    try:
        return str(uuid.UUID(candidate))
    except ValueError:
        pass
    path = Path(candidate).expanduser()
    if path.is_file():
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        return validate_thread_id(content)
    return validate_thread_id(candidate)


def validate_cwd(value):
    candidate = Path(value).expanduser() if value else Path.cwd()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise CodexRunError("The requested working directory does not exist: %s" % value) from exc
    if not resolved.is_dir():
        raise CodexRunError("The requested working path is not a directory: %s" % value)
    return resolved


def find_codex_binary(environment=None):
    env = os.environ if environment is None else environment
    custom = env.get("CODEX_BIN")
    if custom:
        return custom
    found = shutil.which("codex", path=env.get("PATH"))
    if found:
        return found
    for candidate in (
        resolve_home_dir(env) / ".local" / "bin" / "codex",
        Path("/usr/local/bin/codex"),
        Path("/opt/homebrew/bin/codex"),
    ):
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    raise CodexRunError("Could not find 'codex' binary on PATH.")


def build_child_env(environment, zdotdir):
    child_env = dict(environment)
    for var in ("BASH_ENV", "ENV", "PROMPT_COMMAND", "PYTHONSTARTUP"):
        child_env.pop(var, None)
    child_env["ZDOTDIR"] = str(zdotdir)
    child_env[DELEGATE_MARKER] = "1"
    return child_env


def load_model_catalog(environment=None):
    cache_path = resolve_codex_home(environment) / "models_cache.json"
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        listed = [
            m
            for m in (data.get("models") or [])
            if isinstance(m, dict)
            and isinstance(m.get("slug"), str)
            and m.get("slug").strip()
            and m.get("visibility") == "list"
        ]
        return listed or None
    except Exception:
        return None


def _extract_supported_efforts(entry):
    return [
        lvl.get("effort")
        for lvl in (entry.get("supported_reasoning_levels") or [])
        if isinstance(lvl, dict) and isinstance(lvl.get("effort"), str) and lvl.get("effort")
    ]


def _format_model_line(slug, efforts=(), is_default=False, default_effort=None):
    parts = [slug]
    if efforts:
        parts.append("efforts: %s" % ", ".join(efforts))
    elif default_effort:
        parts.append("effort: %s" % default_effort)
    if is_default:
        parts.append("[default]")
    return "\t".join(parts)


def _parse_top_level_toml_strings_fallback(text):
    result = {}
    in_multiline = None
    for line in text.splitlines():
        if in_multiline is not None:
            if in_multiline == "'''":
                if "'''" in line:
                    in_multiline = None
            elif _TOML_UNESCAPED_TRIPLE_DQUOTE_RE.search(line):
                in_multiline = None
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            break
        ml_match = _TOML_MULTILINE_START_RE.match(line)
        if ml_match:
            delim, rest = ml_match.groups()
            closed = (
                "'''" in rest
                if delim == "'''"
                else bool(_TOML_UNESCAPED_TRIPLE_DQUOTE_RE.search(rest))
            )
            if not closed:
                in_multiline = delim
            continue
        match = _TOML_TOP_LEVEL_STR_RE.match(line)
        if match:
            key, double_val, single_val = match.groups()
            if double_val is not None:
                try:
                    result[key] = json.loads('"%s"' % double_val)
                except ValueError:
                    result[key] = double_val
            else:
                result[key] = single_val
    return result


def parse_profile_top_level_config(profile_path):
    try:
        text = profile_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CodexRunError("Failed to read profile config %s: %s" % (profile_path, exc)) from exc
    try:
        import tomllib
    except ImportError:
        return _parse_top_level_toml_strings_fallback(text)
    try:
        data = tomllib.loads(text)
    except Exception as exc:
        raise CodexRunError("Failed to parse profile config %s: %s" % (profile_path, exc)) from exc
    return {
        key: data[key]
        for key in ("model", "model_reasoning_effort", "model_catalog_json")
        if isinstance(data.get(key), str)
    }


def list_models(profile=None, environment=None):
    env = os.environ if environment is None else environment
    if not profile:
        cache_path = resolve_codex_home(env) / "models_cache.json"
        catalog = load_model_catalog(env)
        if not catalog:
            raise CodexRunError(
                "No visible models found in %s. Run 'codex' once to refresh the local model cache, "
                "or omit -m to use the default model / pass a full model slug." % cache_path
            )
        return [
            _format_model_line(m["slug"].strip(), efforts=_extract_supported_efforts(m))
            for m in catalog
        ]

    profile_path = validate_profile(profile, env)
    cfg = parse_profile_top_level_config(profile_path)
    default_model = (cfg.get("model") or "").strip() or None
    default_effort = (cfg.get("model_reasoning_effort") or "").strip() or None
    catalog_raw = (cfg.get("model_catalog_json") or "").strip() or None

    if catalog_raw:
        catalog_path = expand_path_with_env(catalog_raw, env, base_dir=profile_path.parent)
        if not catalog_path.is_file():
            raise CodexRunError(
                "Profile '%s' references missing model_catalog_json: %s"
                % (profile, catalog_path)
            )
        try:
            data = json.loads(catalog_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CodexRunError(
                "Failed to load model_catalog_json for profile '%s' at %s: %s"
                % (profile, catalog_path, exc)
            ) from exc
        models_raw = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models_raw, list):
            raise CodexRunError(
                "Invalid model_catalog_json for profile '%s' at %s: missing 'models' list."
                % (profile, catalog_path)
            )
        listed = [
            m
            for m in models_raw
            if isinstance(m, dict)
            and isinstance(m.get("slug"), str)
            and m.get("slug").strip()
            and (m.get("visibility") is None or m.get("visibility") == "list")
        ]
        if not listed:
            raise CodexRunError(
                "No visible models found in model_catalog_json for profile '%s' at %s."
                % (profile, catalog_path)
            )
        return [
            _format_model_line(
                m["slug"].strip(),
                efforts=_extract_supported_efforts(m),
                is_default=bool(default_model and m["slug"].strip() == default_model),
            )
            for m in listed
        ]

    if default_model:
        return [
            _format_model_line(
                default_model,
                is_default=True,
                default_effort=default_effort,
            )
        ]

    raise CodexRunError(
        "Profile '%s' at %s defines neither 'model_catalog_json' nor a top-level 'model'."
        % (profile, profile_path)
    )


def _parse_version_tuple(slug, short_name):
    prefix = slug[: -(len(short_name) + 1)]
    m = re.search(r"-(\d+(?:\.\d+)*)\Z", prefix)
    if not m:
        return None
    return tuple(int(part) for part in m.group(1).split("."))


def _compare_versions(ver_a, ver_b):
    max_len = max(len(ver_a), len(ver_b))
    for idx in range(max_len):
        da = ver_a[idx] if idx < len(ver_a) else 0
        db = ver_b[idx] if idx < len(ver_b) else 0
        if da != db:
            return da - db
    return 0


def resolve_model_and_effort(model, effort, profile=None, environment=None):
    if effort is not None and not EFFORT_PATTERN.fullmatch(effort):
        raise CodexRunError("Invalid reasoning effort %r." % effort)
    if not profile and effort is None:
        effort = "medium"
    if profile or not model:
        return model, effort

    catalog = load_model_catalog(environment)
    if not catalog:
        return model, effort

    hit = next((m for m in catalog if m.get("slug") == model), None)
    if hit is None:
        tail = [m for m in catalog if str(m.get("slug", "")).endswith("-" + model)]
        if len(tail) > 1:
            versions = [_parse_version_tuple(m["slug"], model) for m in tail]
            if not all(versions):
                raise CodexRunError(
                    "Model '%s' matches several slugs (%s); pass the full slug."
                    % (model, ", ".join(m["slug"] for m in tail))
                )
            paired = sorted(
                zip(versions, tail),
                key=lambda item: item[0] + (0,) * (8 - len(item[0])),
                reverse=True,
            )
            if _compare_versions(paired[0][0], paired[1][0]) == 0:
                raise CodexRunError(
                    "Model '%s' matches several slugs (%s); pass the full slug."
                    % (model, ", ".join(m["slug"] for m in tail))
                )
            hit = paired[0][1]
        elif len(tail) == 1:
            hit = tail[0]

    if hit is None:
        raise CodexRunError(
            "Unknown model '%s'; available in models_cache.json: %s"
            % (model, ", ".join(m["slug"] for m in catalog))
        )

    if effort:
        levels = _extract_supported_efforts(hit)
        if levels and effort not in levels:
            raise CodexRunError(
                "Model '%s' does not support effort '%s'; supported: %s"
                % (hit["slug"], effort, ", ".join(levels))
            )

    return hit["slug"], effort


def read_task(stdin, stderr, prompt_file=None):
    if prompt_file:
        p = Path(prompt_file).expanduser()
        if not p.is_file():
            raise CodexRunError("Prompt file does not exist: %s" % prompt_file)
        task = p.read_text(encoding="utf-8")
    else:
        if stdin.isatty():
            print("Enter the bounded task, then send EOF:", file=stderr)
        task = stdin.read()
    if not task.strip():
        raise CodexRunError("No task was provided.")
    return task


def build_prompt(profile, task):
    if profile == "deepseek":
        return "%s\n\n%s\n\nTask:\n%s" % (DELEGATE_GUARD, DEEPSEEK_GUARD, task)
    return "%s\n\nTask:\n%s" % (DELEGATE_GUARD, task)


def build_app_server_command(
    codex_bin,
    cwd,
    sandbox,
    profile=None,
    model=None,
    effort=None,
):
    exclude_toml = "[" + ", ".join(json.dumps(item) for item in SHELL_ENV_EXCLUDES) + "]"
    command = [codex_bin]
    if profile:
        command.extend(["--profile", profile, "--strict-config"])
    command.extend(
        [
            "-c",
            "allow_login_shell=false",
            "-c",
            "shell_environment_policy.ignore_default_excludes=false",
            "-c",
            "shell_environment_policy.exclude=%s" % exclude_toml,
            "-c",
            "tools.experimental_request_user_input.enabled=false",
            "--sandbox",
            sandbox,
            "--cd",
            str(cwd),
            "-a",
            "never",
        ]
    )
    if model:
        command.extend(["--model", model])
    if effort:
        command.extend(["-c", "model_reasoning_effort=%s" % json.dumps(effort)])
    command.extend(["app-server", "--stdio"])
    return command


def save_thread_meta(thread_id, meta, environment=None):
    try:
        path = thread_meta_path(thread_id, environment)
        atomic_write_text(path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    except Exception:
        pass


def load_thread_meta(thread_id, environment=None):
    try:
        path = thread_meta_path(thread_id, environment)
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def process_group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_process_group(proc, grace_seconds=PROCESS_KILL_GRACE_SECONDS):
    if proc is None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        pgid = proc.pid

    if proc.poll() is not None and not process_group_alive(pgid):
        return

    for sig, wait_window in ((signal.SIGTERM, grace_seconds), (signal.SIGKILL, grace_seconds)):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            if proc.poll() is not None and not process_group_alive(pgid):
                return
        deadline = time.monotonic() + wait_window
        while time.monotonic() < deadline:
            if proc.poll() is not None and not process_group_alive(pgid):
                return
            time.sleep(0.02)


def acquire_thread_lock(thread_id, environment=None):
    """Acquire an exclusive non-blocking flock for `thread_id`.

    The `.lock` file is intentionally kept on disk after unlock to avoid the
    classic POSIX `flock + unlink` inode replacement race.
    """
    lock_path = state_root(environment) / "active" / ("%s.lock" % thread_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(lock_fd)
        raise CodexRunError(
            "Thread %s already has an active codex-run turn running; use --steer or wait for it to finish."
            % thread_id
        ) from exc
    return lock_fd


def release_thread_lock(lock_fd):
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


def _ensure_private_short_sock_dir():
    """Return a private `/tmp/codex-run-<uid>` directory safe on multi-user Linux/macOS."""
    short_dir = Path("/tmp") / ("codex-run-%d" % os.getuid())
    short_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(str(short_dir))
    if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid():
        raise CodexRunError("Unsafe socket directory ownership or symlink at %s" % short_dir)
    os.chmod(str(short_dir), 0o700)
    return short_dir


class SteerServer:
    """Per-thread Unix domain socket server allowing mid-turn `codex_run.py --steer`."""

    def __init__(self, thread_id, handler, environment=None, lock_fd=None):
        self.thread_id = thread_id
        self.handler = handler
        self.environment = environment
        self.meta_path = active_meta_path(thread_id, environment)
        preferred_sock = active_socket_path(thread_id, environment)
        # Keep AF_UNIX path well below macOS (104B) and Linux (108B) sun_path limits.
        if len(str(preferred_sock).encode("utf-8")) <= 95:
            self.sock_path = preferred_sock
        else:
            self.sock_path = _ensure_private_short_sock_dir() / ("%s.sock" % thread_id)
        self._lock_fd = lock_fd
        self._server = None
        self._thread = None
        self._closed = False

    def start(self, initial_meta=None):
        self.sock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.meta_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._lock_fd is None:
            self._lock_fd = acquire_thread_lock(self.thread_id, self.environment)
        try:
            if self.sock_path.exists() or self.sock_path.is_symlink():
                self.sock_path.unlink()
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(self.sock_path))
        try:
            os.chmod(self.sock_path, 0o600)
        except OSError:
            pass
        srv.listen(4)
        srv.settimeout(0.2)
        self._server = srv
        meta = dict(initial_meta or {})
        meta.update(
            {
                "thread_id": self.thread_id,
                "pid": os.getpid(),
                "socket_path": str(self.sock_path),
                "started_at": time.time(),
            }
        )
        atomic_write_text(self.meta_path, json.dumps(meta, ensure_ascii=False) + "\n")
        self._thread = threading.Thread(target=self._serve_loop, daemon=True)
        self._thread.start()

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
            release_thread_lock(self._lock_fd)
            self._lock_fd = None


def steer_active_run(thread_id, message, timeout=10.0, environment=None):
    """Send a mid-turn steer message to a currently running codex_run.py process."""
    validate_thread_id(thread_id)
    if not message or not message.strip():
        raise CodexRunError("Steer message cannot be empty.")
    meta_file = active_meta_path(thread_id, environment)
    sock_file = active_socket_path(thread_id, environment)
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if meta.get("socket_path"):
                sock_file = Path(meta["socket_path"])
        except Exception:
            pass
    if not sock_file.exists():
        raise CodexRunError(
            "No active codex-run turn found for thread %s (socket not present); use --resume instead."
            % thread_id
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
        raise CodexRunError(
            "Failed to connect to active turn socket for thread %s: %s (use --resume if the turn already finished)."
            % (thread_id, exc)
        )
    if not raw.strip():
        raise CodexRunError("Empty response from active turn socket for thread %s." % thread_id)
    resp = json.loads(raw.decode("utf-8", errors="replace").strip())
    if not resp.get("ok"):
        raise CodexRunError(
            "Steer rejected for thread %s: %s" % (thread_id, resp.get("error") or "unknown error")
        )
    return resp


def _start_stream_reader(stream, tag, out_queue):
    def _reader():
        try:
            for line in iter(stream.readline, ""):
                out_queue.put((tag, line))
        except Exception:
            pass
        finally:
            out_queue.put(("eof_" + tag, ""))

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    return t


def run_codex_app_server(
    task,
    cwd,
    sandbox=DEFAULT_SANDBOX,
    profile=None,
    model=None,
    effort=None,
    resume_thread_id=None,
    timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    thread_id_file=None,
    output_file=None,
    stderr=sys.stderr,
    environment=None,
):
    environment = os.environ if environment is None else environment
    validate_not_nested(environment)

    pre_lock_fd = None
    try:
        if resume_thread_id:
            resume_thread_id = resolve_thread_id_arg(resume_thread_id)
            pre_lock_fd = acquire_thread_lock(resume_thread_id, environment)
            saved_meta = load_thread_meta(resume_thread_id, environment)
            if profile is None and saved_meta.get("profile"):
                profile = saved_meta.get("profile")
            if model is None and saved_meta.get("model"):
                model = saved_meta.get("model")
            if effort is None and saved_meta.get("effort"):
                effort = saved_meta.get("effort")
            if sandbox is None and saved_meta.get("sandbox"):
                sandbox = saved_meta.get("sandbox")
            if cwd is None and saved_meta.get("cwd"):
                cwd = saved_meta.get("cwd")

        sandbox = sandbox or DEFAULT_SANDBOX
        resolved_cwd = validate_cwd(cwd or os.getcwd())
        if profile:
            validate_profile(profile, environment)
        resolved_model, resolved_effort = resolve_model_and_effort(
            model, effort, profile=profile, environment=environment
        )
        codex_bin = find_codex_binary(environment)
        prompt_text = build_prompt(profile, task)
    except BaseException:
        release_thread_lock(pre_lock_fd)
        raise

    cmd = build_app_server_command(
        codex_bin=codex_bin,
        cwd=resolved_cwd,
        sandbox=sandbox,
        profile=profile,
        model=resolved_model,
        effort=resolved_effort,
    )

    zdotdir = tempfile.mkdtemp(prefix="codex-run-zdotdir-")
    child_env = build_child_env(environment, zdotdir)
    secrets = known_secret_values(environment)

    event_q = queue.Queue()
    steer_server = None
    proc = None
    watchdog = None
    watchdog_fired = threading.Event()
    next_rpc_id = 10

    def send_rpc(payload):
        if proc is None or proc.stdin is None:
            raise CodexRunError("codex app-server stdin is closed.")
        try:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except OSError as exc:
            raise CodexRunError("Failed to write to codex app-server stdin: %s" % exc) from exc

    def handle_socket_request(req):
        reply_q = queue.Queue(maxsize=1)
        event_q.put(("steer", req, reply_q))
        try:
            return reply_q.get(timeout=4.5)
        except queue.Empty:
            return {"ok": False, "error": "Timed out waiting for event loop to process steer."}

    def _on_watchdog_timeout():
        watchdog_fired.set()
        if proc is not None:
            stop_process_group(proc)
        event_q.put(("timeout", ""))

    try:
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=str(resolved_cwd),
                env=child_env,
                start_new_session=True,
            )
        except OSError as exc:
            raise CodexRunError("Failed to start codex app-server: %s" % exc)

        watchdog = threading.Timer(float(timeout_seconds), _on_watchdog_timeout)
        watchdog.daemon = True
        watchdog.start()

        _start_stream_reader(proc.stdout, "stdout", event_q)
        _start_stream_reader(proc.stderr, "stderr", event_q)

        send_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "codex-run", "version": "1.0"},
                    "capabilities": {
                        "experimentalApi": False,
                        "optOutNotificationMethods": OPT_OUT_NOTIFICATIONS,
                    },
                },
            }
        )

        root_tid = None
        effective_model = resolved_model
        effective_effort = resolved_effort
        active_root_turn_id = None
        active_subagent_threads = set()
        seen_subagent_threads = set()
        waiting_for_sub_drain = False
        auto_continues = 0
        current_turn_messages = []
        all_root_messages = []
        queued_steer_texts = []
        pending_live_steers = []
        pending_steer_rpcs = {}
        token_summary = {}
        stderr_lines = []
        last_error_msg = None
        turn_failed_msg = None
        stdout_eof = False
        completed_cleanly = False
        done = False

        deadline = time.monotonic() + float(timeout_seconds)

        def note_subagent_started(sub_tid):
            if sub_tid not in active_subagent_threads:
                active_subagent_threads.add(sub_tid)
                seen_subagent_threads.add(sub_tid)
                print(
                    "[codex-run] Subagent started (%s): %d active, %d spawned total"
                    % (sub_tid[:8], len(active_subagent_threads), len(seen_subagent_threads)),
                    file=stderr,
                    flush=True,
                )

        def note_subagent_finished(sub_tid):
            if sub_tid in active_subagent_threads:
                active_subagent_threads.discard(sub_tid)
                print(
                    "[codex-run] Subagent finished (%s): %d active, %d spawned total"
                    % (sub_tid[:8], len(active_subagent_threads), len(seen_subagent_threads)),
                    file=stderr,
                    flush=True,
                )

        def start_turn(text_to_send):
            nonlocal next_rpc_id
            rpc_id = next_rpc_id
            next_rpc_id += 1
            turn_params = {
                "threadId": root_tid,
                "input": [{"type": "text", "text": text_to_send, "text_elements": []}],
            }
            if resolved_effort:
                turn_params["effort"] = resolved_effort
            send_rpc({"jsonrpc": "2.0", "id": rpc_id, "method": "turn/start", "params": turn_params})
            return rpc_id

        def maybe_trigger_auto_continue():
            nonlocal waiting_for_sub_drain, auto_continues, current_turn_messages
            if not waiting_for_sub_drain or len(active_subagent_threads) > 0:
                return
            waiting_for_sub_drain = False
            auto_continues += 1
            current_turn_messages = []
            extra = ""
            if queued_steer_texts:
                extra = "\n\nAdditional caller instructions received:\n" + "\n".join(queued_steer_texts)
                queued_steer_texts.clear()
            print(
                "[codex-run] All subagents drained; triggering root auto-continue (%d/%d)..."
                % (auto_continues, MAX_AUTO_CONTINUES),
                file=stderr,
                flush=True,
            )
            start_turn(AUTO_CONTINUE_PROMPT + extra)

        def flush_pending_live_steers():
            nonlocal next_rpc_id
            if not active_root_turn_id or not pending_live_steers:
                return
            while pending_live_steers:
                s_text, r_q = pending_live_steers.pop(0)
                rpc_id = next_rpc_id
                next_rpc_id += 1
                pending_steer_rpcs[rpc_id] = (s_text, r_q)
                send_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "method": "turn/steer",
                        "params": {
                            "threadId": root_tid,
                            "expectedTurnId": active_root_turn_id,
                            "input": [{"type": "text", "text": s_text, "text_elements": []}],
                        },
                    }
                )

        while not done:
            if watchdog_fired.is_set() or time.monotonic() >= deadline:
                stop_process_group(proc)
                print(
                    "Timed out after %s seconds." % timeout_seconds,
                    file=stderr,
                    flush=True,
                )
                return 124

            try:
                item = event_q.get(timeout=0.05)
            except queue.Empty:
                if stdout_eof and proc.poll() is not None:
                    break
                continue

            kind = item[0]
            if kind == "timeout":
                stop_process_group(proc)
                print(
                    "Timed out after %s seconds." % timeout_seconds,
                    file=stderr,
                    flush=True,
                )
                return 124
            if kind == "eof_stdout":
                stdout_eof = True
                if proc.poll() is not None and event_q.empty():
                    break
                continue
            if kind == "eof_stderr":
                continue
            if kind == "stderr":
                stderr_lines.append(item[1])
                continue
            if kind == "steer":
                _, req, reply_q = item
                steer_text = (req.get("text") or "").strip()
                if not steer_text:
                    reply_q.put({"ok": False, "error": "Empty steer message."})
                elif active_root_turn_id and not waiting_for_sub_drain:
                    rpc_id = next_rpc_id
                    next_rpc_id += 1
                    pending_steer_rpcs[rpc_id] = (steer_text, reply_q)
                    send_rpc(
                        {
                            "jsonrpc": "2.0",
                            "id": rpc_id,
                            "method": "turn/steer",
                            "params": {
                                "threadId": root_tid,
                                "expectedTurnId": active_root_turn_id,
                                "input": [{"type": "text", "text": steer_text, "text_elements": []}],
                            },
                        }
                    )
                elif waiting_for_sub_drain:
                    queued_steer_texts.append(steer_text)
                    reply_q.put(
                        {
                            "ok": True,
                            "thread_id": root_tid,
                            "mode": "queued_for_subagent_drain",
                        }
                    )
                else:
                    pending_live_steers.append((steer_text, reply_q))
                continue

            if kind != "stdout":
                continue

            raw_line = item[1].strip()
            if not raw_line:
                continue
            try:
                msg = json.loads(raw_line)
            except ValueError:
                continue

            if "id" in msg and "method" in msg:
                send_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "error": {
                            "code": -32601,
                            "message": "ServerRequest %s is not supported in non-interactive codex-run."
                            % msg.get("method"),
                        },
                    }
                )
                continue

            if "id" in msg and "method" not in msg:
                rid = msg.get("id")
                err = msg.get("error")
                res = msg.get("result") or {}
                if rid in pending_steer_rpcs:
                    s_text, r_q = pending_steer_rpcs.pop(rid)
                    if err:
                        if waiting_for_sub_drain:
                            queued_steer_texts.append(s_text)
                            r_q.put(
                                {
                                    "ok": True,
                                    "thread_id": root_tid,
                                    "mode": "queued_for_subagent_drain",
                                }
                            )
                        else:
                            r_q.put({"ok": False, "error": err.get("message") or str(err)})
                    else:
                        r_q.put(
                            {
                                "ok": True,
                                "thread_id": root_tid,
                                "turn_id": res.get("turnId") or active_root_turn_id,
                                "mode": "turn/steer",
                            }
                        )
                    continue

                if err:
                    turn_failed_msg = err.get("message") or json.dumps(err, ensure_ascii=False)
                    done = True
                    continue

                if rid == 1:
                    send_rpc({"jsonrpc": "2.0", "method": "initialized", "params": {}})
                    if resume_thread_id:
                        send_rpc(
                            {
                                "jsonrpc": "2.0",
                                "id": 2,
                                "method": "thread/resume",
                                "params": {
                                    "threadId": resume_thread_id,
                                    "cwd": str(resolved_cwd),
                                    "model": resolved_model,
                                    "approvalPolicy": "never",
                                    "sandbox": sandbox,
                                },
                            }
                        )
                    else:
                        send_rpc(
                            {
                                "jsonrpc": "2.0",
                                "id": 2,
                                "method": "thread/start",
                                "params": {
                                    "cwd": str(resolved_cwd),
                                    "model": resolved_model,
                                    "approvalPolicy": "never",
                                    "sandbox": sandbox,
                                },
                            }
                        )
                elif rid == 2:
                    tid_candidate = (res.get("thread") or {}).get("id")
                    root_tid = validate_thread_id(tid_candidate)
                    effective_model = res.get("model") or resolved_model
                    effective_effort = res.get("reasoningEffort") or resolved_effort
                    meta_obj = {
                        "thread_id": root_tid,
                        "cwd": str(resolved_cwd),
                        "sandbox": sandbox,
                        "profile": profile,
                        "model": effective_model,
                        "effort": effective_effort,
                        "updated_at": time.time(),
                    }
                    lock_to_use = pre_lock_fd if resume_thread_id == root_tid else None
                    pre_lock_fd = None if lock_to_use is not None else pre_lock_fd
                    steer_server = SteerServer(
                        root_tid,
                        handle_socket_request,
                        environment,
                        lock_fd=lock_to_use,
                    )
                    steer_server.start(initial_meta=meta_obj)
                    save_thread_meta(root_tid, meta_obj, environment)
                    if thread_id_file:
                        atomic_write_text(thread_id_file, root_tid + "\n")
                    print("CODEX_THREAD_ID=%s" % root_tid, file=stderr, flush=True)
                    start_turn(prompt_text)
                else:
                    started_turn_id = (res.get("turn") or {}).get("id")
                    if started_turn_id and active_root_turn_id is None and not waiting_for_sub_drain:
                        active_root_turn_id = started_turn_id
                        flush_pending_live_steers()
                continue

            method = msg.get("method")
            params = msg.get("params") or {}
            tid = params.get("threadId")

            if method == "turn/started":
                t_id = (params.get("turn") or {}).get("id")
                if tid == root_tid or root_tid is None:
                    active_root_turn_id = t_id
                    flush_pending_live_steers()
                elif tid and root_tid and tid != root_tid:
                    note_subagent_started(tid)
            elif method == "item/completed":
                if tid == root_tid:
                    item_obj = params.get("item") or {}
                    if item_obj.get("type") == "agentMessage":
                        txt = item_obj.get("text") or ""
                        if txt.strip():
                            current_turn_messages.append(txt)
                            all_root_messages.append(txt)
            elif method == "thread/tokenUsage/updated":
                if tid == root_tid:
                    tu = params.get("tokenUsage") or {}
                    last_u = tu.get("last") or {}
                    total_u = tu.get("total") or {}
                    token_summary = {
                        "input": last_u.get("inputTokens"),
                        "cached_input": last_u.get("cachedInputTokens"),
                        "output": last_u.get("outputTokens"),
                        "reasoning": last_u.get("reasoningOutputTokens"),
                        "total": last_u.get("totalTokens"),
                        "session_total": total_u.get("totalTokens"),
                        "context_window": tu.get("modelContextWindow"),
                    }
            elif method == "error":
                if (tid == root_tid or root_tid is None) and not params.get("willRetry"):
                    err_obj = params.get("error") or {}
                    last_error_msg = err_obj.get("message") or str(err_obj)
            elif method == "thread/status/changed":
                if tid and root_tid and tid != root_tid:
                    st_type = (params.get("status") or {}).get("type")
                    if st_type in ("idle", "notLoaded", "systemError"):
                        note_subagent_finished(tid)
                        maybe_trigger_auto_continue()
            elif method == "thread/closed":
                if tid and root_tid and tid != root_tid:
                    note_subagent_finished(tid)
                    maybe_trigger_auto_continue()
            elif method == "turn/completed":
                if tid and root_tid and tid != root_tid:
                    note_subagent_finished(tid)
                    maybe_trigger_auto_continue()
                elif tid == root_tid:
                    active_root_turn_id = None
                    turn_obj = params.get("turn") or {}
                    t_status = turn_obj.get("status")
                    if t_status == "failed":
                        t_err = turn_obj.get("error") or {}
                        turn_failed_msg = (
                            t_err.get("message") or last_error_msg or "Codex turn failed."
                        )
                        done = True
                    elif t_status == "interrupted":
                        turn_failed_msg = last_error_msg or "Codex turn was interrupted."
                        done = True
                    else:
                        if len(active_subagent_threads) > 0 and auto_continues < MAX_AUTO_CONTINUES:
                            waiting_for_sub_drain = True
                            print(
                                "[codex-run] Root turn ended early without wait_agent: %d subagent(s) still active; waiting for drain..."
                                % len(active_subagent_threads),
                                file=stderr,
                                flush=True,
                            )
                        elif queued_steer_texts and auto_continues < MAX_AUTO_CONTINUES:
                            waiting_for_sub_drain = True
                            maybe_trigger_auto_continue()
                        elif len(active_subagent_threads) > 0:
                            turn_failed_msg = (
                                "Reached MAX_AUTO_CONTINUES=%d with %d subagent(s) still active; exiting to prevent infinite loop."
                                % (MAX_AUTO_CONTINUES, len(active_subagent_threads))
                            )
                            done = True
                        else:
                            completed_cleanly = True
                            done = True

        while pending_live_steers:
            _, r_q = pending_live_steers.pop(0)
            r_q.put({"ok": False, "error": "Turn already finished; use --resume instead."})
        for _, (_, r_q) in list(pending_steer_rpcs.items()):
            r_q.put({"ok": False, "error": "Turn already finished; use --resume instead."})
        pending_steer_rpcs.clear()

        stop_process_group(proc)

        if watchdog_fired.is_set():
            print(
                "Timed out after %s seconds." % timeout_seconds,
                file=stderr,
                flush=True,
            )
            return 124

        if turn_failed_msg or not root_tid or not completed_cleanly:
            err_detail = turn_failed_msg or last_error_msg or "".join(stderr_lines).strip()
            if not err_detail:
                err_detail = "codex app-server exited unexpectedly before turn completion (code=%s)." % (
                    proc.returncode if proc else "unknown"
                )
            print(sanitize(err_detail, secrets), file=stderr, flush=True)
            return 1

        final_messages = current_turn_messages if current_turn_messages else all_root_messages
        final_text = sanitize("\n\n".join(final_messages).strip(), secrets)
        if not final_text:
            err_detail = last_error_msg or "".join(stderr_lines).strip()
            if err_detail:
                print(sanitize(err_detail, secrets), file=stderr, flush=True)
            print("Codex completed without returning an agent message.", file=stderr, flush=True)
            return 1

        if auto_continues > 0:
            notice = (
                "[codex-run notice: The delegated Codex agent ended early without wait_agent %d time(s) "
                "(%d subagent(s) spawned total); codex-run waited for drain and auto-continued to completion. "
                "Report this to the user when summarizing the result.]"
                % (auto_continues, len(seen_subagent_threads))
            )
            print(notice, file=stderr, flush=True)
            final_text = final_text + "\n\n" + notice

        if output_file:
            atomic_write_text(output_file, final_text + "\n")

        meta_report = {
            "thread_id": root_tid,
            "model": effective_model,
            "effort": effective_effort,
            "profile": profile,
            "sandbox": sandbox,
            "subagents_spawned": len(seen_subagent_threads),
            "auto_continues": auto_continues,
            "tokens": token_summary,
        }
        print(
            "CODEX_RUN_META=%s" % json.dumps(meta_report, ensure_ascii=False),
            file=stderr,
            flush=True,
        )
        sys.stdout.write(final_text + "\n")
        sys.stdout.flush()
        return 0

    except CodexRunError:
        if watchdog_fired.is_set():
            print(
                "Timed out after %s seconds." % timeout_seconds,
                file=stderr,
                flush=True,
            )
            return 124
        raise
    except KeyboardInterrupt:
        if proc is not None:
            stop_process_group(proc)
        print("Interrupted.", file=stderr, flush=True)
        return 130
    finally:
        if watchdog is not None:
            watchdog.cancel()
        try:
            if proc is not None:
                stop_process_group(proc)
        finally:
            try:
                if steer_server is not None:
                    steer_server.close()
            finally:
                if pre_lock_fd is not None:
                    release_thread_lock(pre_lock_fd)
                    pre_lock_fd = None
                try:
                    shutil.rmtree(zdotdir)
                except OSError as exc:
                    print(
                        "Cleanup error: failed to remove temporary ZDOTDIR %s: %s"
                        % (zdotdir, exc),
                        file=stderr,
                        flush=True,
                    )


def build_parser():
    parser = argparse.ArgumentParser(
        description="Run, resume, or live-steer a local Codex agent via codex app-server --stdio."
    )
    parser.add_argument(
        "task",
        nargs="?",
        help="Prompt/message text. Reads from stdin or --prompt-file when omitted.",
    )
    parser.add_argument(
        "-m",
        "--model",
        help="Model short suffix name (resolved via models_cache.json) or full model slug.",
    )
    parser.add_argument(
        "-e",
        "--effort",
        help="Reasoning effort (low/medium/high/xhigh/max).",
    )
    parser.add_argument(
        "--profile",
        help="Optional external Codex profile (<name>.config.toml in $CODEX_HOME, e.g. deepseek).",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="List available models from local models_cache.json or the given --profile config, then exit.",
    )
    parser.add_argument(
        "-C",
        "--cwd",
        help="Working directory for the Codex agent (defaults to current directory).",
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
        "--resume",
        metavar="THREAD_ID_OR_FILE",
        help="Resume an existing Codex thread by UUID or path to a thread-id file.",
    )
    parser.add_argument(
        "--steer",
        metavar="THREAD_ID_OR_FILE",
        help="Inject a live mid-turn steer message into an active codex-run thread.",
    )
    parser.add_argument(
        "--prompt-file",
        help="Read the prompt/message from a file instead of positional arg or stdin.",
    )
    parser.add_argument(
        "--session-id-file",
        dest="thread_id_file",
        help="Write the active session UUID to this file as soon as the thread starts/resumes.",
    )
    parser.add_argument(
        "--thread-id-file",
        "--conversation-file",
        dest="thread_id_file",
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
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Timeout in seconds (default: %s)." % DEFAULT_TIMEOUT_SECONDS,
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_models:
        try:
            for line in list_models(profile=args.profile, environment=os.environ):
                sys.stdout.write(line + "\n")
            sys.stdout.flush()
            return 0
        except CodexRunError as exc:
            secrets = known_secret_values(os.environ)
            print(sanitize(str(exc), secrets), file=sys.stderr, flush=True)
            return 1

    if args.resume and args.steer:
        print("Cannot use --resume and --steer together.", file=sys.stderr)
        return 2
    if sum(bool(x) for x in (args.sandbox, args.write, args.yolo)) > 1:
        print("Cannot combine --sandbox, --write, and --yolo.", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("--timeout must be positive.", file=sys.stderr)
        return 2

    prev_sigterm = None
    installed_sigterm = False
    try:
        prev_sigterm = signal.signal(signal.SIGTERM, _raise_cancelled)
        installed_sigterm = True
    except ValueError:
        pass

    try:
        message = (
            args.task
            if args.task is not None and args.task.strip()
            else read_task(sys.stdin, sys.stderr, prompt_file=args.prompt_file)
        )
        if args.steer:
            tid = resolve_thread_id_arg(args.steer)
            resp = steer_active_run(
                tid,
                message,
                timeout=min(args.timeout, 15.0),
                environment=os.environ,
            )
            print(json.dumps(resp, ensure_ascii=False))
            return 0

        sandbox = args.sandbox
        if args.yolo and sandbox is None:
            sandbox = "danger-full-access"
        elif args.write and sandbox is None:
            sandbox = "workspace-write"

        return run_codex_app_server(
            task=message,
            cwd=args.cwd,
            sandbox=sandbox,
            profile=args.profile,
            model=args.model,
            effort=args.effort,
            resume_thread_id=args.resume,
            timeout_seconds=args.timeout,
            thread_id_file=args.thread_id_file,
            output_file=args.output,
            stderr=sys.stderr,
            environment=os.environ,
        )
    except CodexRunError as exc:
        secrets = known_secret_values(os.environ)
        print(sanitize(str(exc), secrets), file=sys.stderr, flush=True)
        return 1
    except Cancelled as exc:
        print("codex-run: cancelled by signal %d" % exc.signum, file=sys.stderr, flush=True)
        return 128 + exc.signum
    finally:
        if installed_sigterm:
            try:
                signal.signal(signal.SIGTERM, prev_sigterm)
            except ValueError:
                pass


if __name__ == "__main__":
    sys.exit(main())
