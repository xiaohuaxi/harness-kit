#!/usr/bin/env python3
"""End-to-end unit tests for hooks/codex-agent-depth-guard/codex-agent-depth-guard.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

SCRIPT_PATH = Path(__file__).resolve().parent / "codex-agent-depth-guard.py"
MARKER = "CODEX MULTI-AGENT DEPTH GUARD: BLOCKED spawn_agent:"


class CodexDepthGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def write_transcript(self, content: str, name: str = "session.jsonl") -> Path:
        path = self.tmpdir / name
        path.write_text(content, encoding="utf-8")
        return path

    def write_session_meta(self, payload: dict[str, Any], name: str = "session.jsonl") -> Path:
        first_line = json.dumps({"type": "session_meta", "payload": payload})
        return self.write_transcript(first_line + "\n", name=name)

    def make_root_transcript(self, name: str = "root.jsonl", source: str = "cli") -> Path:
        return self.write_session_meta({"source": source}, name=name)

    def make_subagent_transcript(self, depth: Any, name: str = "subagent.jsonl") -> Path:
        return self.write_session_meta(
            {"source": {"subagent": {"thread_spawn": {"depth": depth}}}},
            name=name,
        )

    def run_guard(
        self,
        stdin_payload: Any,
        *,
        env_overrides: dict[str, str | None] | None = None,
        direct_exec: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("CODEX_MAX_AGENT_DEPTH", None)
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
        return subprocess.run(
            cmd,
            input=stdin_text,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def make_request(
        self,
        transcript_path: Path | str | None,
        *,
        tool_name: Any = "spawn_agent",
        hook_event_name: Any = "PreToolUse",
        agent_id: Any = None,
        agent_type: Any = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "hook_event_name": hook_event_name,
            "tool_name": tool_name,
        }
        if transcript_path is not None:
            payload["transcript_path"] = str(transcript_path)
        if agent_id is not None:
            payload["agent_id"] = agent_id
        if agent_type is not None:
            payload["agent_type"] = agent_type
        return payload

    def test_script_is_executable_and_runs_directly(self) -> None:
        self.assertTrue(os.access(SCRIPT_PATH, os.X_OK), "guard script must be executable (+x)")
        transcript = self.make_root_transcript()
        proc = self.run_guard(self.make_request(transcript), direct_exec=True)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, "")

    def test_default_limit_allows_root_and_child_blocks_grandchild(self) -> None:
        root_t = self.make_root_transcript("root.jsonl")
        child_t = self.make_subagent_transcript(1, "child.jsonl")
        grandchild_t = self.make_subagent_transcript(2, "grandchild.jsonl")

        proc_root = self.run_guard(self.make_request(root_t))
        self.assertEqual(proc_root.returncode, 0)
        self.assertEqual(proc_root.stderr, "")

        proc_child = self.run_guard(self.make_request(child_t, agent_id="agent-1"))
        self.assertEqual(proc_child.returncode, 0)
        self.assertEqual(proc_child.stderr, "")

        proc_grandchild = self.run_guard(self.make_request(grandchild_t, agent_id="agent-2"))
        self.assertEqual(proc_grandchild.returncode, 2)
        self.assertIn(MARKER, proc_grandchild.stderr)
        self.assertIn("caller depth 2", proc_grandchild.stderr)
        self.assertIn("depth 3", proc_grandchild.stderr)
        self.assertIn("max agent depth 2", proc_grandchild.stderr)
        self.assertIn("3 levels total: depth 0..2", proc_grandchild.stderr)

    def test_tool_name_and_hook_event_matching(self) -> None:
        child_t = self.make_subagent_transcript(1, "child.jsonl")
        grandchild_t = self.make_subagent_transcript(2, "grandchild.jsonl")

        for tool in ("spawn_agent", "multi_agent_v2.spawn_agent"):
            with self.subTest(allowed_tool=tool):
                proc_ok = self.run_guard(self.make_request(child_t, tool_name=tool, agent_id="a1"))
                self.assertEqual(proc_ok.returncode, 0)

                proc_block = self.run_guard(
                    self.make_request(grandchild_t, tool_name=tool, agent_id="a2")
                )
                self.assertEqual(proc_block.returncode, 2)
                self.assertIn(MARKER, proc_block.stderr)

        for bad_event in ("PostToolUse", "SessionStart", "", None):
            with self.subTest(bad_event=bad_event):
                proc = self.run_guard(
                    self.make_request(child_t, hook_event_name=bad_event, agent_id="a1")
                )
                self.assertEqual(proc.returncode, 2)
                self.assertIn("unexpected hook event", proc.stderr)

        for bad_tool in ("shell_command", "spawn_agent_extra", "", None, 123):
            with self.subTest(bad_tool=bad_tool):
                proc = self.run_guard(
                    self.make_request(child_t, tool_name=bad_tool, agent_id="a1")
                )
                self.assertEqual(proc.returncode, 2)
                self.assertIn("unexpected tool name", proc.stderr)

    def test_malformed_and_spoofed_inputs_fail_closed(self) -> None:
        # Non-JSON and non-object stdin
        for raw_stdin in ("", "not json", "[]", "123", '"hello"'):
            with self.subTest(raw_stdin=raw_stdin):
                proc = self.run_guard(raw_stdin)
                self.assertEqual(proc.returncode, 2)
                self.assertIn(MARKER, proc.stderr)

        # Missing / empty / non-existent transcript_path
        for bad_path in (None, "", str(self.tmpdir / "does-not-exist.jsonl")):
            with self.subTest(bad_path=bad_path):
                proc = self.run_guard(self.make_request(bad_path))
                self.assertEqual(proc.returncode, 2)
                self.assertIn(MARKER, proc.stderr)

        # Empty transcript file
        empty_t = self.write_transcript("", "empty.jsonl")
        proc_empty = self.run_guard(self.make_request(empty_t))
        self.assertEqual(proc_empty.returncode, 2)
        self.assertIn("transcript is empty", proc_empty.stderr)

        # First line is not session_meta
        non_meta_t = self.write_transcript(
            json.dumps({"type": "event_msg", "payload": {}}) + "\n",
            "non_meta.jsonl",
        )
        proc_non_meta = self.run_guard(self.make_request(non_meta_t))
        self.assertEqual(proc_non_meta.returncode, 2)
        self.assertIn("not session_meta", proc_non_meta.stderr)

        # Root metadata conflicts with subagent hook fields (agent_id or agent_type non-null)
        root_t = self.make_root_transcript("root_conflict.jsonl")
        proc_root_id = self.run_guard(self.make_request(root_t, agent_id="spoofed-id"))
        self.assertEqual(proc_root_id.returncode, 2)
        self.assertIn("root metadata conflicts", proc_root_id.stderr)

        proc_root_type = self.run_guard(self.make_request(root_t, agent_type="worker"))
        self.assertEqual(proc_root_type.returncode, 2)
        self.assertIn("root metadata conflicts", proc_root_type.stderr)

        # Missing, null, empty, or unrecognized session_meta source must fail closed
        for label, payload in (
            ("missing_source", {}),
            ("null_source", {"source": None}),
            ("empty_source", {"source": ""}),
            ("whitespace_source", {"source": "   "}),
            ("unknown_dict_source", {"source": {"cli": True}}),
            ("empty_dict_source", {"source": {}}),
            ("int_source", {"source": 123}),
            ("list_source", {"source": ["cli"]}),
            ("bool_source", {"source": True}),
        ):
            with self.subTest(bad_source=label):
                bad_src_t = self.write_session_meta(payload, f"{label}.jsonl")
                proc_src = self.run_guard(self.make_request(bad_src_t))
                self.assertEqual(proc_src.returncode, 2)
                self.assertIn(MARKER, proc_src.stderr)
                self.assertIn("session source", proc_src.stderr)

        # Valid non-empty string root sources are allowed
        for root_source in ("cli", "exec", "vscode"):
            with self.subTest(root_source=root_source):
                valid_root_t = self.make_root_transcript(f"root_{root_source}.jsonl", source=root_source)
                proc_valid_root = self.run_guard(self.make_request(valid_root_t))
                self.assertEqual(proc_valid_root.returncode, 0)
                self.assertEqual(proc_valid_root.stderr, "")

        # Subagent transcript missing agent_id in hook input
        sub_ok_t = self.make_subagent_transcript(1, "sub_no_id.jsonl")
        proc_no_id = self.run_guard(self.make_request(sub_ok_t))
        self.assertEqual(proc_no_id.returncode, 2)
        self.assertIn("missing agent_id", proc_no_id.stderr)

        # Subagent transcript with invalid depth values
        for bad_depth in (0, -1, True, False, "1", 1.5, None):
            with self.subTest(bad_depth=bad_depth):
                bad_t = self.make_subagent_transcript(bad_depth, f"bad_depth_{type(bad_depth).__name__}.jsonl")
                proc = self.run_guard(self.make_request(bad_t, agent_id="agent-x"))
                self.assertEqual(proc.returncode, 2)
                self.assertIn("positive integer", proc.stderr)

        # Subagent transcript missing thread_spawn object
        bad_sub_t = self.write_session_meta({"source": {"subagent": {}}}, "missing_spawn.jsonl")
        proc_missing_spawn = self.run_guard(self.make_request(bad_sub_t, agent_id="agent-x"))
        self.assertEqual(proc_missing_spawn.returncode, 2)
        self.assertIn("thread_spawn", proc_missing_spawn.stderr)

    def test_max_agent_depth_env_var(self) -> None:
        root_t = self.make_root_transcript("env_root.jsonl")
        depth1_t = self.make_subagent_transcript(1, "env_d1.jsonl")
        depth2_t = self.make_subagent_transcript(2, "env_d2.jsonl")
        depth3_t = self.make_subagent_transcript(3, "env_d3.jsonl")

        # CODEX_MAX_AGENT_DEPTH=0 -> root only (1 level total); even root is blocked
        proc_0_root = self.run_guard(
            self.make_request(root_t), env_overrides={"CODEX_MAX_AGENT_DEPTH": "0"}
        )
        self.assertEqual(proc_0_root.returncode, 2)
        self.assertIn("caller depth 0", proc_0_root.stderr)
        self.assertIn("depth 1", proc_0_root.stderr)
        self.assertIn("max agent depth 0", proc_0_root.stderr)
        self.assertIn("1 level total: depth 0 only", proc_0_root.stderr)

        proc_0_d1 = self.run_guard(
            self.make_request(depth1_t, agent_id="a1"),
            env_overrides={"CODEX_MAX_AGENT_DEPTH": "0"},
        )
        self.assertEqual(proc_0_d1.returncode, 2)
        self.assertIn("caller depth 1", proc_0_d1.stderr)
        self.assertIn("max agent depth 0", proc_0_d1.stderr)

        # CODEX_MAX_AGENT_DEPTH=1 -> root (depth 0) allowed, depth 1 blocked (2 levels total)
        proc_1_root = self.run_guard(
            self.make_request(root_t), env_overrides={"CODEX_MAX_AGENT_DEPTH": "1"}
        )
        self.assertEqual(proc_1_root.returncode, 0)

        proc_1_d1 = self.run_guard(
            self.make_request(depth1_t, agent_id="a1"),
            env_overrides={"CODEX_MAX_AGENT_DEPTH": "1"},
        )
        self.assertEqual(proc_1_d1.returncode, 2)
        self.assertIn("caller depth 1", proc_1_d1.stderr)
        self.assertIn("depth 2", proc_1_d1.stderr)
        self.assertIn("max agent depth 1", proc_1_d1.stderr)
        self.assertIn("2 levels total: depth 0..1", proc_1_d1.stderr)

        # CODEX_MAX_AGENT_DEPTH=2 -> depth 1 allowed, depth 2 blocked (3 levels total)
        proc_2_d1 = self.run_guard(
            self.make_request(depth1_t, agent_id="a1"),
            env_overrides={"CODEX_MAX_AGENT_DEPTH": "2"},
        )
        self.assertEqual(proc_2_d1.returncode, 0)

        proc_2_d2 = self.run_guard(
            self.make_request(depth2_t, agent_id="a2"),
            env_overrides={"CODEX_MAX_AGENT_DEPTH": "2"},
        )
        self.assertEqual(proc_2_d2.returncode, 2)
        self.assertIn("caller depth 2", proc_2_d2.stderr)
        self.assertIn("depth 3", proc_2_d2.stderr)
        self.assertIn("max agent depth 2", proc_2_d2.stderr)
        self.assertIn("3 levels total: depth 0..2", proc_2_d2.stderr)

        # CODEX_MAX_AGENT_DEPTH=3 -> depth 2 allowed, depth 3 blocked (4 levels total)
        proc_3_d2 = self.run_guard(
            self.make_request(depth2_t, agent_id="a2"),
            env_overrides={"CODEX_MAX_AGENT_DEPTH": "3"},
        )
        self.assertEqual(proc_3_d2.returncode, 0)

        proc_3_d3 = self.run_guard(
            self.make_request(depth3_t, agent_id="a3"),
            env_overrides={"CODEX_MAX_AGENT_DEPTH": "3"},
        )
        self.assertEqual(proc_3_d3.returncode, 2)
        self.assertIn("caller depth 3", proc_3_d3.stderr)
        self.assertIn("depth 4", proc_3_d3.stderr)
        self.assertIn("max agent depth 3", proc_3_d3.stderr)
        self.assertIn("4 levels total: depth 0..3", proc_3_d3.stderr)

        # Empty string falls back to default 2
        proc_empty_d1 = self.run_guard(
            self.make_request(depth1_t, agent_id="a1"),
            env_overrides={"CODEX_MAX_AGENT_DEPTH": ""},
        )
        self.assertEqual(proc_empty_d1.returncode, 0)

        proc_empty_d2 = self.run_guard(
            self.make_request(depth2_t, agent_id="a2"),
            env_overrides={"CODEX_MAX_AGENT_DEPTH": ""},
        )
        self.assertEqual(proc_empty_d2.returncode, 2)
        self.assertIn("max agent depth 2", proc_empty_d2.stderr)

        # Invalid values strictly fail closed
        for bad_env in ("-1", "abc", "1.5", " 1", "1 ", "+1", "²"):
            with self.subTest(bad_env=bad_env):
                proc_bad = self.run_guard(
                    self.make_request(root_t),
                    env_overrides={"CODEX_MAX_AGENT_DEPTH": bad_env},
                )
                self.assertEqual(proc_bad.returncode, 2)
                self.assertIn(MARKER, proc_bad.stderr)
                self.assertIn("CODEX_MAX_AGENT_DEPTH", proc_bad.stderr)


if __name__ == "__main__":
    unittest.main()
