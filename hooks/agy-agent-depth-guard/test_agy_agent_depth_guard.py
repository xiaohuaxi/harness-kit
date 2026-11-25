#!/usr/bin/env python3
"""End-to-end unit tests for hooks/agy-agent-depth-guard/agy-agent-depth-guard.py."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

SCRIPT_PATH = Path(__file__).resolve().parent / "agy-agent-depth-guard.py"
MARKER = "AGY AGENT DEPTH GUARD: BLOCKED invoke_subagent:"

UUID_ROOT = "11111111-1111-1111-1111-111111111111"
UUID_CHILD = "22222222-2222-2222-2222-222222222222"
UUID_GRANDCHILD = "33333333-3333-3333-3333-333333333333"
UUID_GREAT_GRANDCHILD = "44444444-4444-4444-4444-444444444444"


def make_uuid(index: int) -> str:
    return f"{index:08x}-0000-4000-8000-000000000000"


class AgyDepthGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmpdir.name)
        self.fake_home = self.tmpdir / "home"
        self.fake_home.mkdir()
        self.brain_root = self.tmpdir / "brain"
        self.brain_root.mkdir()
        self._chmod_restore: list[tuple[Path, int]] = []

    def tearDown(self) -> None:
        for path, mode in reversed(self._chmod_restore):
            try:
                os.chmod(path, mode)
            except OSError:
                pass
        self._tmpdir.cleanup()

    def conv_system_dir(self, cid: str, *, brain_root: Path | None = None) -> Path:
        root = brain_root if brain_root is not None else self.brain_root
        path = root / cid / ".system_generated"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_message(
        self,
        cid: str,
        filename: str,
        content: dict[str, Any] | str,
        *,
        brain_root: Path | None = None,
    ) -> Path:
        msg_dir = self.conv_system_dir(cid, brain_root=brain_root) / "messages"
        msg_dir.mkdir(parents=True, exist_ok=True)
        path = msg_dir / filename
        text = content if isinstance(content, str) else json.dumps(content)
        path.write_text(text, encoding="utf-8")
        return path

    def write_invoke_message(
        self,
        cid: str,
        parent_id: str,
        *,
        filename: str = "0001-create.json",
        tool_name: str = "invoke_subagent",
        brain_root: Path | None = None,
    ) -> Path:
        return self.write_message(
            cid,
            filename,
            {
                "sender": parent_id,
                "recipient": cid,
                "sourceMetadata": {"tool": {"toolCall": {"name": tool_name}}},
            },
            brain_root=brain_root,
        )

    def write_transcript_first_line(
        self,
        cid: str,
        entry: dict[str, Any] | str,
        *,
        brain_root: Path | None = None,
    ) -> Path:
        logs_dir = self.conv_system_dir(cid, brain_root=brain_root) / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        path = logs_dir / "transcript.jsonl"
        line = entry if isinstance(entry, str) else json.dumps(entry)
        path.write_text(line + "\n", encoding="utf-8")
        return path

    def write_user_root_transcript(
        self,
        cid: str,
        *,
        brain_root: Path | None = None,
    ) -> Path:
        return self.write_transcript_first_line(
            cid,
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "hi"},
            brain_root=brain_root,
        )

    def write_system_spawn_transcript(
        self,
        cid: str,
        sender_token: str,
        *,
        step_index: Any = 0,
        brain_root: Path | None = None,
    ) -> Path:
        return self.write_transcript_first_line(
            cid,
            {
                "step_index": step_index,
                "source": "SYSTEM",
                "type": "SYSTEM_MESSAGE",
                "content": (
                    f"[Message] timestamp=2026-11-24T12:00:00Z sender={sender_token} "
                    "priority=MESSAGE_PRIORITY_HIGH content=Do the task"
                ),
            },
            brain_root=brain_root,
        )

    def make_payload(
        self,
        cid: Any,
        *,
        hook_event_name: Any = "PreToolUse",
        tool_name: Any = "invoke_subagent",
        include_tool_call: bool = True,
        artifact_dir: Any = ...,
        transcript_path: Any = ...,
        brain_root: Path | None = None,
    ) -> dict[str, Any]:
        root = brain_root if brain_root is not None else self.brain_root
        payload: dict[str, Any] = {"hookEventName": hook_event_name}
        if cid is not None:
            payload["conversationId"] = cid
        if include_tool_call:
            payload["toolCall"] = {"id": "call-1", "name": tool_name, "args": {}}
        if artifact_dir is ...:
            if isinstance(cid, str) and cid:
                payload["artifactDirectoryPath"] = str(root / cid)
        elif artifact_dir is not None:
            payload["artifactDirectoryPath"] = artifact_dir
        if transcript_path is ...:
            if isinstance(cid, str) and cid:
                payload["transcriptPath"] = str(
                    root / cid / ".system_generated" / "logs" / "transcript.jsonl"
                )
        elif transcript_path is not None:
            payload["transcriptPath"] = transcript_path
        return payload

    def run_guard(
        self,
        stdin_payload: Any,
        *,
        env_overrides: dict[str, str | None] | None = None,
        direct_exec: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        env = os.environ.copy()
        env["HOME"] = str(self.fake_home)
        env.pop("AGY_MAX_AGENT_DEPTH", None)
        if env_overrides:
            for key, value in env_overrides.items():
                if value is None:
                    env.pop(key, None)
                else:
                    env[key] = value

        stdin_text = (
            stdin_payload
            if isinstance(stdin_payload, str)
            else json.dumps(stdin_payload)
        )
        cmd = [str(SCRIPT_PATH)] if direct_exec else [sys.executable, str(SCRIPT_PATH)]
        proc = subprocess.run(
            cmd,
            input=stdin_text,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"hook must always exit 0, got {proc.returncode}; stderr={proc.stderr!r}",
        )
        parsed = json.loads(proc.stdout)
        self.assertIsInstance(parsed, dict)
        return proc, parsed

    def assert_allow(
        self,
        stdin_payload: Any,
        *,
        env_overrides: dict[str, str | None] | None = None,
        direct_exec: bool = False,
    ) -> dict[str, Any]:
        _, parsed = self.run_guard(
            stdin_payload, env_overrides=env_overrides, direct_exec=direct_exec
        )
        self.assertEqual(parsed, {"decision": "allow"})
        return parsed

    def assert_deny(
        self,
        stdin_payload: Any,
        *,
        expected_substring: str = "",
        env_overrides: dict[str, str | None] | None = None,
        direct_exec: bool = False,
    ) -> dict[str, Any]:
        _, parsed = self.run_guard(
            stdin_payload, env_overrides=env_overrides, direct_exec=direct_exec
        )
        self.assertEqual(parsed.get("decision"), "deny", f"expected deny, got {parsed!r}")
        reason = parsed.get("reason", "")
        self.assertIsInstance(reason, str)
        self.assertIn(MARKER, reason)
        if expected_substring:
            self.assertIn(expected_substring, reason)
        return parsed

    def test_script_is_executable_and_runs_directly(self) -> None:
        self.assertTrue(os.access(SCRIPT_PATH, os.X_OK), "guard script must be executable (+x)")
        self.write_user_root_transcript(UUID_ROOT)
        self.assert_allow(self.make_payload(UUID_ROOT), direct_exec=True)

    def test_default_limit_allows_root_and_child_blocks_grandchild(self) -> None:
        # Root without messages/ (only USER_EXPLICIT transcript)
        self.write_user_root_transcript(UUID_ROOT)
        self.assert_allow(self.make_payload(UUID_ROOT))

        # Root with no .system_generated on disk yet (early root turn)
        early_root = make_uuid(99)
        self.assert_allow(self.make_payload(early_root))

        # Root with only a non-invoke_subagent send_message file
        root_with_msg = make_uuid(98)
        self.write_invoke_message(root_with_msg, UUID_ROOT, tool_name="send_message")
        self.assert_allow(self.make_payload(root_with_msg))

        # Child (depth 1) -> allowed under default max_agent_depth=2
        self.write_invoke_message(UUID_CHILD, UUID_ROOT)
        self.assert_allow(self.make_payload(UUID_CHILD))

        # Grandchild (depth 2) -> denied under default max_agent_depth=2
        self.write_invoke_message(UUID_GRANDCHILD, UUID_CHILD)
        res = self.assert_deny(self.make_payload(UUID_GRANDCHILD))
        reason = res["reason"]
        self.assertIn("caller depth 2", reason)
        self.assertIn("depth 3", reason)
        self.assertIn("max agent depth 2", reason)
        self.assertIn("3 levels total: depth 0..2", reason)

    def test_messages_only_transcript_only_and_mixed_ancestry(self) -> None:
        # Transcript-only chain: root -> child -> grandchild
        r1, c1, g1 = make_uuid(10), make_uuid(11), make_uuid(12)
        self.write_user_root_transcript(r1)
        self.write_system_spawn_transcript(c1, r1)
        self.write_system_spawn_transcript(g1, c1)
        self.assert_allow(self.make_payload(c1))
        self.assert_deny(self.make_payload(g1), expected_substring="caller depth 2")

        # Mixed chain: grandchild via messages, child via transcript, root via USER_EXPLICIT
        r2, c2, g2 = make_uuid(20), make_uuid(21), make_uuid(22)
        self.write_user_root_transcript(r2)
        self.write_system_spawn_transcript(c2, r2)
        self.write_invoke_message(g2, c2)
        self.assert_allow(self.make_payload(c2))
        self.assert_deny(self.make_payload(g2), expected_substring="caller depth 2")

    def test_counterexamples_and_concurrency_robustness(self) -> None:
        # Background task sender (<UUID>/task-7) must NOT be misclassified as parent session
        bg_root = make_uuid(30)
        self.write_system_spawn_transcript(bg_root, f"{UUID_ROOT}/task-7")
        self.assert_allow(self.make_payload(bg_root))

        # First line with step_index != 0 (including bool False) must NOT be treated as step 0
        non_zero = make_uuid(31)
        self.write_system_spawn_transcript(non_zero, UUID_ROOT, step_index=1)
        self.assert_allow(self.make_payload(non_zero))

        bool_step = make_uuid(32)
        self.write_system_spawn_transcript(bool_step, UUID_ROOT, step_index=False)
        self.assert_allow(self.make_payload(bool_step))

        # Corrupt/half-written message alongside a valid invoke_subagent message still traces depth
        self.write_user_root_transcript(UUID_ROOT)
        c_corrupt = make_uuid(33)
        g_corrupt = make_uuid(34)
        self.write_message(c_corrupt, "0000-half.json", "{not-finished")
        self.write_invoke_message(c_corrupt, UUID_ROOT, filename="0001-create.json")
        self.write_invoke_message(g_corrupt, c_corrupt)
        self.assert_allow(self.make_payload(c_corrupt))
        self.assert_deny(self.make_payload(g_corrupt), expected_substring="caller depth 2")

        # Corrupt message in a proven USER_EXPLICIT root does not false-deny the root
        root_half = make_uuid(35)
        self.write_message(root_half, "0000-half.json", "{not-finished")
        self.write_user_root_transcript(root_half)
        self.assert_allow(self.make_payload(root_half))

        # Corrupt message with NO valid creation message and NO USER_EXPLICIT proof fails closed
        unproven_half = make_uuid(36)
        self.write_message(unproven_half, "0000-half.json", "{not-finished")
        self.assert_deny(self.make_payload(unproven_half), expected_substring="failed to inspect messages")

    def test_max_agent_depth_env_var_and_deep_chain(self) -> None:
        # Build a 14-node chain: depth 0 .. depth 13 (verifies no 10-level truncation)
        nodes = [make_uuid(100 + i) for i in range(14)]
        self.write_user_root_transcript(nodes[0])
        for idx in range(1, len(nodes)):
            self.write_invoke_message(nodes[idx], nodes[idx - 1])

        # AGY_MAX_AGENT_DEPTH=0 -> root only (1 level total); even root is blocked
        res_0_root = self.assert_deny(
            self.make_payload(nodes[0]),
            env_overrides={"AGY_MAX_AGENT_DEPTH": "0"},
        )
        self.assertIn("caller depth 0", res_0_root["reason"])
        self.assertIn("depth 1", res_0_root["reason"])
        self.assertIn("max agent depth 0", res_0_root["reason"])
        self.assertIn("1 level total: depth 0 only", res_0_root["reason"])

        early_root = make_uuid(199)
        self.assert_deny(
            self.make_payload(early_root),
            expected_substring="max agent depth 0",
            env_overrides={"AGY_MAX_AGENT_DEPTH": "0"},
        )

        # AGY_MAX_AGENT_DEPTH=1 -> depth 0 allowed, depth 1 blocked (2 levels total)
        self.assert_allow(
            self.make_payload(nodes[0]), env_overrides={"AGY_MAX_AGENT_DEPTH": "1"}
        )
        res_1 = self.assert_deny(
            self.make_payload(nodes[1]),
            env_overrides={"AGY_MAX_AGENT_DEPTH": "1"},
        )
        self.assertIn("caller depth 1", res_1["reason"])
        self.assertIn("depth 2", res_1["reason"])
        self.assertIn("max agent depth 1", res_1["reason"])
        self.assertIn("2 levels total: depth 0..1", res_1["reason"])

        # AGY_MAX_AGENT_DEPTH=2 -> depth 1 allowed, depth 2 blocked (3 levels total)
        self.assert_allow(
            self.make_payload(nodes[1]), env_overrides={"AGY_MAX_AGENT_DEPTH": "2"}
        )
        res_2 = self.assert_deny(
            self.make_payload(nodes[2]),
            env_overrides={"AGY_MAX_AGENT_DEPTH": "2"},
        )
        self.assertIn("caller depth 2", res_2["reason"])
        self.assertIn("depth 3", res_2["reason"])
        self.assertIn("max agent depth 2", res_2["reason"])
        self.assertIn("3 levels total: depth 0..2", res_2["reason"])

        # AGY_MAX_AGENT_DEPTH=3 -> depth 2 allowed, depth 3 blocked (4 levels total)
        self.assert_allow(
            self.make_payload(nodes[2]), env_overrides={"AGY_MAX_AGENT_DEPTH": "3"}
        )
        res_3 = self.assert_deny(
            self.make_payload(nodes[3]),
            env_overrides={"AGY_MAX_AGENT_DEPTH": "3"},
        )
        self.assertIn("caller depth 3", res_3["reason"])
        self.assertIn("depth 4", res_3["reason"])
        self.assertIn("max agent depth 3", res_3["reason"])
        self.assertIn("4 levels total: depth 0..3", res_3["reason"])

        # Empty string falls back to default 2
        self.assert_allow(
            self.make_payload(nodes[1]), env_overrides={"AGY_MAX_AGENT_DEPTH": ""}
        )
        self.assert_deny(
            self.make_payload(nodes[2]),
            expected_substring="max agent depth 2",
            env_overrides={"AGY_MAX_AGENT_DEPTH": ""},
        )

        # Deep chain AGY_MAX_AGENT_DEPTH=13 -> depth 12 allowed (creates 13), depth 13 blocked
        self.assert_allow(
            self.make_payload(nodes[12]), env_overrides={"AGY_MAX_AGENT_DEPTH": "13"}
        )
        res_13 = self.assert_deny(
            self.make_payload(nodes[13]),
            env_overrides={"AGY_MAX_AGENT_DEPTH": "13"},
        )
        self.assertIn("caller depth 13", res_13["reason"])
        self.assertIn("depth 14", res_13["reason"])
        self.assertIn("max agent depth 13", res_13["reason"])
        self.assertIn("14 levels total: depth 0..13", res_13["reason"])

        # Invalid values fail closed
        for bad_env in ("-1", "abc", "1.5", " 1", "1 ", "+1", "²"):
            with self.subTest(bad_env=bad_env):
                self.assert_deny(
                    self.make_payload(nodes[0]),
                    expected_substring="AGY_MAX_AGENT_DEPTH",
                    env_overrides={"AGY_MAX_AGENT_DEPTH": bad_env},
                )

    def test_brain_root_resolution_and_non_cli_layout_rejection(self) -> None:
        self.write_user_root_transcript(UUID_ROOT)
        self.write_invoke_message(UUID_CHILD, UUID_ROOT)
        self.write_invoke_message(UUID_GRANDCHILD, UUID_CHILD)

        # Prefer candidate containing .system_generated over an empty decoy artifact root
        decoy_root = self.tmpdir / "decoy-brain"
        (decoy_root / UUID_GRANDCHILD).mkdir(parents=True)
        payload = self.make_payload(
            UUID_GRANDCHILD,
            artifact_dir=str(decoy_root / UUID_GRANDCHILD),
            transcript_path=str(
                self.brain_root
                / UUID_GRANDCHILD
                / ".system_generated"
                / "logs"
                / "transcript.jsonl"
            ),
        )
        self.assert_deny(payload, expected_substring="caller depth 2")

        # Non-CLI explicit paths (e.g. IDE / Antigravity 2.0) must fail closed, not fall back
        ide_payload = self.make_payload(
            UUID_ROOT,
            artifact_dir=str(self.tmpdir / "workspace" / "artifacts"),
            transcript_path=str(self.tmpdir / "workspace" / "transcript.jsonl"),
        )
        self.assert_deny(ide_payload, expected_substring="unsupported or unrecognized")

        # Missing both path hints falls back to ~/.gemini/antigravity-cli/brain
        default_brain = self.fake_home / ".gemini/antigravity-cli/brain"
        default_brain.mkdir(parents=True)
        r_def, c_def, g_def = make_uuid(40), make_uuid(41), make_uuid(42)
        self.write_user_root_transcript(r_def, brain_root=default_brain)
        self.write_invoke_message(c_def, r_def, brain_root=default_brain)
        self.write_invoke_message(g_def, c_def, brain_root=default_brain)

        self.assert_allow(
            self.make_payload(c_def, artifact_dir=None, transcript_path=None)
        )
        self.assert_deny(
            self.make_payload(g_def, artifact_dir=None, transcript_path=None),
            expected_substring="caller depth 2",
        )

    def test_protocol_and_fail_closed_boundaries(self) -> None:
        self.write_user_root_transcript(UUID_ROOT)

        # Malformed / non-object stdin
        for raw in ("", "   ", "{bad", "[]", "123", '"hello"'):
            with self.subTest(raw=raw):
                self.assert_deny(raw)

        # Wrong hookEventName (present and non-null, not PreToolUse)
        for bad_event in ("PostToolUse", "SessionStart", "", 123):
            with self.subTest(bad_event=bad_event):
                self.assert_deny(
                    self.make_payload(UUID_ROOT, hook_event_name=bad_event),
                    expected_substring="unexpected hook event",
                )

        # Missing or null hookEventName is accepted for backward compatibility with older agy,
        # while still enforcing toolCall.name and caller depth checks.
        self.write_invoke_message(UUID_CHILD, UUID_ROOT)
        self.write_invoke_message(UUID_GRANDCHILD, UUID_CHILD)
        for omit_or_none in ("omit", "none"):
            with self.subTest(hook_event_compat=omit_or_none):
                root_payload = self.make_payload(UUID_ROOT, hook_event_name=None)
                child_payload = self.make_payload(UUID_CHILD, hook_event_name=None)
                grandchild_payload = self.make_payload(UUID_GRANDCHILD, hook_event_name=None)
                wrong_tool_payload = self.make_payload(
                    UUID_ROOT, hook_event_name=None, tool_name="run_command"
                )
                if omit_or_none == "omit":
                    for p in (root_payload, child_payload, grandchild_payload, wrong_tool_payload):
                        p.pop("hookEventName", None)
                self.assert_allow(root_payload)
                self.assert_allow(child_payload)
                self.assert_deny(grandchild_payload, expected_substring="caller depth 2")
                self.assert_deny(wrong_tool_payload, expected_substring="unexpected tool name")

        # Wrong or missing toolCall.name
        for bad_tool in ("run_command", "invoke_subagent_extra", "", None, 123):
            with self.subTest(bad_tool=bad_tool):
                self.assert_deny(
                    self.make_payload(UUID_ROOT, tool_name=bad_tool),
                    expected_substring="unexpected tool name",
                )
        self.assert_deny(
            self.make_payload(UUID_ROOT, include_tool_call=False),
            expected_substring="unexpected tool name",
        )

        # Missing / invalid / path-traversal conversationId
        for bad_cid in (None, "", 123, ".", "..", "../evil", "a/b", "a\\b", "bad\x00id"):
            with self.subTest(bad_cid=bad_cid):
                self.assert_deny(
                    self.make_payload(
                        bad_cid,
                        artifact_dir=None,
                        transcript_path=None,
                    ),
                    expected_substring="conversationId",
                )

        # Broken ancestry: grandchild points to child whose metadata directory is missing
        missing_parent = make_uuid(50)
        orphan_grandchild = make_uuid(51)
        self.write_invoke_message(orphan_grandchild, missing_parent)
        self.assert_deny(
            self.make_payload(orphan_grandchild),
            expected_substring="broken conversation ancestry",
        )

        # Self-cycle: single creation message points to itself
        self_loop = make_uuid(52)
        self.write_invoke_message(self_loop, self_loop)
        self.assert_deny(
            self.make_payload(self_loop),
            expected_substring="cycle detected",
        )

        # 2-cycle: A -> B -> A
        loop_a, loop_b = make_uuid(53), make_uuid(54)
        self.write_invoke_message(loop_a, loop_b)
        self.write_invoke_message(loop_b, loop_a)
        self.assert_deny(
            self.make_payload(loop_a),
            expected_substring="cycle detected",
        )

    @unittest.skipIf(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        "chmod 0o000 does not deny reads for root",
    )
    def test_unreadable_messages_dir_fails_closed(self) -> None:
        locked_cid = make_uuid(60)
        msg_dir = self.conv_system_dir(locked_cid) / "messages"
        msg_dir.mkdir(parents=True, exist_ok=True)
        self.write_invoke_message(locked_cid, UUID_ROOT)
        old_mode = stat.S_IMODE(os.stat(msg_dir).st_mode)
        os.chmod(msg_dir, 0o000)
        self._chmod_restore.append((msg_dir, old_mode))

        self.assert_deny(
            self.make_payload(locked_cid),
            expected_substring="failed to inspect messages",
        )


if __name__ == "__main__":
    unittest.main()
