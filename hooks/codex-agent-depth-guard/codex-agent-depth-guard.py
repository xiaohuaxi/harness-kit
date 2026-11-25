#!/usr/bin/env python3
"""Block Codex subagent spawning beyond the configured max agent depth."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


MARKER = "CODEX MULTI-AGENT DEPTH GUARD"
ENV_MAX_AGENT_DEPTH = "CODEX_MAX_AGENT_DEPTH"
DEFAULT_MAX_AGENT_DEPTH = 2


def reject(reason: str) -> int:
    print(f"{MARKER}: BLOCKED spawn_agent: {reason}", file=sys.stderr, flush=True)
    return 2


def resolve_max_agent_depth() -> int:
    raw = os.environ.get(ENV_MAX_AGENT_DEPTH)
    if raw is None or raw == "":
        return DEFAULT_MAX_AGENT_DEPTH
    if not (raw.isascii() and raw.isdigit()):
        raise ValueError(
            f"invalid {ENV_MAX_AGENT_DEPTH}={raw!r}: "
            "must be a non-negative base-10 integer"
        )
    return int(raw)


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def read_session_meta(transcript_path: str) -> dict[str, Any]:
    path = Path(transcript_path)
    with path.open("r", encoding="utf-8") as transcript:
        first_line = transcript.readline()
    if not first_line:
        raise ValueError("transcript is empty")
    entry = require_object(json.loads(first_line), "transcript first line")
    if entry.get("type") != "session_meta":
        raise ValueError("transcript first line is not session_meta")
    return require_object(entry.get("payload"), "session_meta payload")


def caller_depth(request: dict[str, Any]) -> int:
    transcript_path = request.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        raise ValueError("transcript_path is missing")

    meta = read_session_meta(transcript_path)
    source = meta.get("source")
    if isinstance(source, str) and source.strip():
        if request.get("agent_id") is not None or request.get("agent_type") is not None:
            raise ValueError("root metadata conflicts with subagent hook fields")
        return 0

    if not isinstance(source, dict) or "subagent" not in source:
        raise ValueError("unable to determine session source from session_meta")

    subagent = require_object(source.get("subagent"), "session source subagent")
    thread_spawn = require_object(subagent.get("thread_spawn"), "thread_spawn source")
    depth = thread_spawn.get("depth")
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
        raise ValueError("thread_spawn depth must be a positive integer")
    if request.get("agent_id") is None:
        raise ValueError("subagent hook input is missing agent_id")
    return depth


def main() -> int:
    try:
        max_agent_depth = resolve_max_agent_depth()
    except ValueError as error:
        return reject(str(error))

    request = require_object(json.load(sys.stdin), "hook input")
    if request.get("hook_event_name") != "PreToolUse":
        return reject("unexpected hook event")

    tool_name = request.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name.endswith("spawn_agent"):
        return reject("unexpected tool name")

    depth = caller_depth(request)
    next_depth = depth + 1
    if next_depth > max_agent_depth:
        total_levels = max_agent_depth + 1
        if max_agent_depth == 0:
            range_desc = "1 level total: depth 0 only"
        else:
            range_desc = f"{total_levels} levels total: depth 0..{max_agent_depth}"
        return reject(
            f"caller depth {depth} may not create depth {next_depth} "
            f"(exceeds max agent depth {max_agent_depth}; {range_desc})"
        )
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except BaseException as error:
        # Codex fails open on hook errors. Convert every uncaught failure into a
        # protocol-level denial (exit 2 plus a loud stderr reason).
        exit_code = reject(f"guard failure ({type(error).__name__}): {error}")
    os._exit(exit_code)
