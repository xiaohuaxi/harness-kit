#!/usr/bin/env python3
"""Block Antigravity CLI (agy) subagent spawning beyond the configured max agent depth."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any


MARKER = "AGY AGENT DEPTH GUARD"
ENV_MAX_AGENT_DEPTH = "AGY_MAX_AGENT_DEPTH"
DEFAULT_MAX_AGENT_DEPTH = 2

MESSAGE_HEADER_SENDER_RE = re.compile(
    r"\[Message\]\s+timestamp=\S+\s+sender=([^\s]+)(?=\s|$)"
)
UUID_FULL_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def emit(response: dict[str, Any]) -> None:
    json.dump(response, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


def allow() -> None:
    emit({"decision": "allow"})


def deny(reason: str) -> None:
    emit({"decision": "deny", "reason": f"{MARKER}: BLOCKED invoke_subagent: {reason}"})


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


def is_valid_conversation_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in (".", "..")
        and "/" not in value
        and "\\" not in value
        and "\x00" not in value
    )


def validate_conversation_id(value: Any) -> str:
    if not is_valid_conversation_id(value):
        raise ValueError("conversationId is missing or invalid")
    return value


def resolve_brain_root(payload: dict[str, Any], cid: str) -> Path:
    explicit_path_provided = False
    candidates: list[Path] = []
    seen: set[Path] = set()

    def add_candidate(path: Path) -> None:
        if path not in seen:
            seen.add(path)
            candidates.append(path)

    artifact_dir = payload.get("artifactDirectoryPath")
    if artifact_dir is not None and artifact_dir != "":
        explicit_path_provided = True
        if isinstance(artifact_dir, str):
            p = Path(artifact_dir)
            if p.name == cid and p.parent != p:
                add_candidate(p.parent)

    transcript_path = payload.get("transcriptPath")
    if transcript_path is not None and transcript_path != "":
        explicit_path_provided = True
        if isinstance(transcript_path, str):
            p = Path(transcript_path)
            if len(p.parents) >= 4 and p.parents[2].name == cid:
                add_candidate(p.parents[3])

    if candidates:
        for candidate in candidates:
            if (candidate / cid / ".system_generated").is_dir():
                return candidate
        for candidate in candidates:
            if (candidate / cid).is_dir():
                return candidate
        for candidate in candidates:
            if candidate.is_dir():
                return candidate
        raise ValueError("cannot locate agy brain root from hook payload")

    if explicit_path_provided:
        raise ValueError("unsupported or unrecognized agy brain layout in hook payload")

    return Path.home() / ".gemini/antigravity-cli/brain"


def find_parent_id(
    brain_root: Path,
    cur: str,
    *,
    require_conv_dir: bool,
) -> str | None:
    conv_dir = brain_root / cur / ".system_generated"
    try:
        st = os.stat(conv_dir)
    except (FileNotFoundError, NotADirectoryError):
        if require_conv_dir:
            raise RuntimeError(
                f"broken conversation ancestry: missing metadata for ancestor {cur}"
            )
        return None
    if not stat.S_ISDIR(st.st_mode):
        if require_conv_dir:
            raise RuntimeError(
                f"broken conversation ancestry: missing metadata for ancestor {cur}"
            )
        raise RuntimeError(f"invalid conversation metadata path for {cur}: not a directory")

    # 1. Primary channel: .system_generated/messages/*.json (skip read.json).
    # Use os.scandir instead of Path.glob so PermissionError is not swallowed on Python 3.8-3.9.
    msg_dir = conv_dir / "messages"
    messages_error: Exception | None = None
    entries: list[os.DirEntry[str]] = []
    try:
        with os.scandir(msg_dir) as iterator:
            entries = sorted(
                (
                    entry
                    for entry in iterator
                    if entry.name.endswith(".json") and entry.name != "read.json"
                ),
                key=lambda entry: entry.name,
            )
    except (FileNotFoundError, NotADirectoryError):
        entries = []
    except OSError as error:
        messages_error = error

    for entry in entries:
        try:
            if not entry.is_file():
                continue
            with open(entry.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception as error:
            if messages_error is None:
                messages_error = error
            continue

        if not isinstance(data, dict) or data.get("recipient") != cur:
            continue
        sender = data.get("sender")
        if not is_valid_conversation_id(sender):
            continue
        source_meta = data.get("sourceMetadata")
        if not isinstance(source_meta, dict):
            continue
        tool = source_meta.get("tool")
        if not isinstance(tool, dict):
            continue
        tool_call = tool.get("toolCall")
        if not isinstance(tool_call, dict):
            continue
        if tool_call.get("name") == "invoke_subagent":
            return sender

    # 2. Fallback channel: step_index == 0 in .system_generated/logs/transcript.jsonl.
    transcript = conv_dir / "logs" / "transcript.jsonl"
    transcript_error: Exception | None = None
    proven_root = False
    first_line = ""
    try:
        with open(transcript, "r", encoding="utf-8") as handle:
            first_line = handle.readline()
    except (FileNotFoundError, NotADirectoryError):
        first_line = ""
    except OSError as error:
        transcript_error = error

    if first_line:
        step0: Any = None
        try:
            step0 = json.loads(first_line)
        except Exception as error:
            transcript_error = error

        if step0 is not None and not isinstance(step0, dict):
            transcript_error = ValueError("transcript first line must be a JSON object")
        elif isinstance(step0, dict):
            step_index = step0.get("step_index")
            if not isinstance(step_index, bool) and step_index == 0:
                source = step0.get("source")
                if source == "USER_EXPLICIT":
                    proven_root = True
                elif source == "SYSTEM" and step0.get("type") == "SYSTEM_MESSAGE":
                    content = step0.get("content")
                    if isinstance(content, str):
                        match = MESSAGE_HEADER_SENDER_RE.search(content)
                        if match:
                            sender = match.group(1)
                            if UUID_FULL_RE.match(sender) and is_valid_conversation_id(sender):
                                return sender

    if proven_root:
        return None
    if messages_error is not None:
        raise RuntimeError(f"failed to inspect messages for {cur}: {messages_error}")
    if transcript_error is not None:
        raise RuntimeError(f"failed to inspect transcript for {cur}: {transcript_error}")
    return None


def caller_depth(payload: dict[str, Any], max_agent_depth: int) -> int:
    cid = validate_conversation_id(payload.get("conversationId"))
    brain_root = resolve_brain_root(payload, cid)

    depth = 0
    cur = cid
    visited = {cid}
    while depth < max_agent_depth:
        parent = find_parent_id(brain_root, cur, require_conv_dir=(depth > 0))
        if parent is None:
            return depth
        if parent in visited:
            raise ValueError(f"cycle detected in conversation ancestry at {parent}")
        visited.add(parent)
        cur = parent
        depth += 1
    return depth


def main() -> None:
    max_agent_depth = resolve_max_agent_depth()

    raw_stdin = sys.stdin.read()
    if not raw_stdin.strip():
        deny("hook input is empty")
        return

    try:
        payload = json.loads(raw_stdin)
    except Exception as error:
        deny(f"invalid hook input JSON: {error}")
        return

    if not isinstance(payload, dict):
        deny("hook input must be a JSON object")
        return

    hook_event = payload.get("hookEventName")
    if hook_event is not None and hook_event != "PreToolUse":
        deny("unexpected hook event")
        return

    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict) or tool_call.get("name") != "invoke_subagent":
        deny("unexpected tool name")
        return

    depth = caller_depth(payload, max_agent_depth)
    next_depth = depth + 1
    if next_depth > max_agent_depth:
        total_levels = max_agent_depth + 1
        if max_agent_depth == 0:
            range_desc = "1 level total: depth 0 only"
            guidance = "Complete the remaining work directly without delegating to subagents."
        else:
            range_desc = f"{total_levels} levels total: depth 0..{max_agent_depth}"
            guidance = "Complete the remaining work directly and return your results to your caller."
        deny(
            f"caller depth {depth} may not create depth {next_depth} "
            f"(exceeds max agent depth {max_agent_depth}; {range_desc}). "
            f"{guidance}"
        )
        return

    allow()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as error:
        deny(str(error))
    except Exception as error:
        # agy fails open on hook crashes. Convert every uncaught failure into a
        # protocol-level denial (exit 0 plus a JSON deny response on stdout).
        deny(f"guard failure ({type(error).__name__}): {error}")
