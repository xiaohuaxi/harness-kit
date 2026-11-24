#!/usr/bin/env python3
"""Offline unit & integration tests for agy_core and agy_run (ACP v1 over stdio)."""

import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
REPO_ROOT = TESTS_DIR.parents[2]
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(TESTS_DIR))

import agy_core as core  # noqa: E402
import agy_run  # noqa: E402
import fake_agy as fa  # noqa: E402


class PipeStdin(io.StringIO):
    def isatty(self):
        return False


class TtyStdin(io.StringIO):
    def isatty(self):
        return True


def _encode_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def write_fake_db_calls(home, session_id, records, wal=False):
    """Write protobuf-framed ``run_command`` records into fake SQLite store."""
    conv_dir = Path(home) / ".gemini" / "antigravity-cli" / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    target = conv_dir / (f"{session_id}.db-wal" if wal else f"{session_id}.db")
    chunks = [b"SQLite format 3\x00"]
    for call_id, cmd, bypass in records:
        payload = {"CommandLine": cmd}
        if bypass is not None:
            payload["BypassSandbox"] = bypass
        raw_json = json.dumps(payload).encode("utf-8")
        chunks.append(
            call_id.encode("ascii")
            + b"\x12\x0brun_command\x1a"
            + _encode_varint(len(raw_json))
            + raw_json
        )
    target.write_bytes(b"\x00\xff".join(chunks))


class BaseTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agy-run-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.home = str(self.root / "home")
        self.work = str(self.root / "work")
        self.state = str(self.root / "state")
        Path(self.home).mkdir()
        Path(self.work).mkdir()
        self.agents_dir = Path(self.home) / ".gemini" / "config" / "agents"
        agy_run.write_agent_files(self.agents_dir)
        self.fake = fa.FakeAgy(self.home)
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "HOME": self.home,
                "XDG_STATE_HOME": self.state,
                "PATH": self.fake.path_env(),
                "GEMINI_API_KEY": "must-not-leak",
                "AGY_LLM_GATEWAY_URL": "https://leak.invalid",
            },
            clear=False,
        )
        self.env_patch.start()
        os.environ.pop(core.DELEGATE_MARKER, None)
        os.environ.pop("AGY_BIN", None)
        self.addCleanup(self.env_patch.stop)

    def invoke(self, argv, stdin_text=None, tty=False):
        out, err = io.StringIO(), io.StringIO()
        stdin = TtyStdin() if tty else PipeStdin(stdin_text or "")
        code = agy_run.main(argv, stdin=stdin, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def parse_meta(self, err_text):
        lines = [l for l in err_text.splitlines() if l.startswith(agy_run.META_PREFIX)]
        self.assertTrue(lines, f"no {agy_run.META_PREFIX} in stderr:\n{err_text}")
        return json.loads(lines[-1][len(agy_run.META_PREFIX):])

    def saved_session(self, sid):
        p = agy_run.state_dir() / "conversations" / f"{sid}.json"
        return json.loads(p.read_text(encoding="utf-8"))


class ModelAndEffortResolutionTests(unittest.TestCase):
    def test_short_aliases_and_full_slugs(self):
        opts = fa.DEFAULT_CONFIG_OPTIONS
        self.assertEqual(("gemini-4-carbon", None), core.match_acp_model(None, opts))
        self.assertEqual(("gemini-4-carbon", None), core.match_acp_model("carbon", opts))
        self.assertEqual(("gemini-3.8-flash", None), core.match_acp_model("flash", opts))
        self.assertEqual(("gemini-4-hydrogen", None), core.match_acp_model("hydrogen", opts))
        self.assertEqual(("gemini-4-argon", None), core.match_acp_model("argon", opts))
        self.assertEqual(("gemini-pro-orchestrator", None), core.match_acp_model("orchestrator", opts))
        self.assertEqual(("gpt-oss-120b-medium", None), core.match_acp_model("gpt-oss-120b-medium", opts))
        self.assertEqual(
            ("gemini-4-hydrogen", "xhigh"),
            core.match_acp_model("gemini-4-hydrogen-xhigh", opts),
        )
        self.assertEqual(("gemini-4-carbon", "high"), core.match_acp_model("carbon-high", opts))

        with self.assertRaises(core.AgyError):
            core.match_acp_model("nonexistent-model", opts)

    def test_version_tuple_prefers_highest_semver(self):
        opts = [
            {
                "id": "model",
                "currentValue": "gemini-3.8-flash",
                "options": [
                    {"value": "gemini-3.8-flash"},
                    {"value": "gemini-4.10-flash"},
                    {"value": "gemini-4.2-flash"},
                ],
            }
        ]
        self.assertEqual(("gemini-4.10-flash", None), core.match_acp_model("flash", opts))

    def test_effort_validation_and_orchestrator_omission(self):
        opts = fa.DEFAULT_CONFIG_OPTIONS
        self.assertEqual("medium", core.match_acp_effort("gemini-4-carbon", None, False, opts))
        self.assertEqual("high", core.match_acp_effort("gemini-4-carbon", "high", True, opts))
        with self.assertRaises(core.AgyError):
            core.match_acp_effort("gemini-4-carbon", "xhigh", True, opts)

        no_tl = [opts[0]]
        self.assertIsNone(core.match_acp_effort("gemini-pro-orchestrator", None, False, no_tl))
        with self.assertRaises(core.AgyError):
            core.match_acp_effort("gemini-pro-orchestrator", "high", True, no_tl)


def _has_deployed_agents_mapping():
    if os.environ.get("AGY_CHECK_DEPLOYED_AGENTS") == "1":
        return True
    links = REPO_ROOT / "deploy" / "links.tsv"
    if not links.is_file():
        return False
    for raw in links.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        src = line.split("\t", 1)[0].strip()
        if src == "gemini/agents":
            return True
    return False


class AgentFilesAndDefinitionsTests(unittest.TestCase):
    def test_agent_definitions_structure_and_tool_permissions(self):
        expected = agy_run.agent_definitions()
        self.assertEqual(
            {
                "agy-readonly.md",
                "agy-readonly-rules.md",
                "agy-write.md",
                "agy-write-rules.md",
                "agy-yolo.md",
                "agy-yolo-rules.md",
            },
            set(expected),
        )

        for fname, text in expected.items():
            fm = core.parse_agent_frontmatter(text)
            self.assertFalse(fm["subagent"])
            is_rules = fname.endswith("-rules.md")
            self.assertEqual(is_rules, fm["inheritCustomizations"])
            self.assertEqual(not is_rules, fm["excludeDefaultComponents"])

        # read-only must have zero file-write tools; only yolo gets generate_image
        self.assertFalse(
            set(agy_run.allowed_names(agy_run.MODES["read-only"]["tools"]))
            & set(core.ACTION_TOOLS["write_file"])
        )
        self.assertNotIn("generate_image", agy_run.MODES["read-only"]["tools"])
        self.assertNotIn("generate_image", agy_run.MODES["workspace-write"]["tools"])
        self.assertIn("generate_image", agy_run.MODES["danger-full-access"]["tools"])

    def test_deployed_gemini_agents_match_definitions_exactly(self):
        if not _has_deployed_agents_mapping():
            self.skipTest("no repo deploy/links.tsv gemini/agents mapping in this checkout")
        agents_dir = REPO_ROOT / "gemini" / "agents"
        self.assertTrue(agents_dir.is_dir(), f"expected {agents_dir} to exist when mapped in deploy/links.tsv")
        expected = agy_run.agent_definitions()
        actual_files = {p.name: p.read_text(encoding="utf-8") for p in agents_dir.glob("*.md")}
        self.assertEqual(expected, actual_files)


class CliAndTurnExecutionTests(BaseTestCase):
    def test_help_shows_session_id_file_and_hides_legacy_aliases(self):
        help_text = agy_run.build_parser().format_help()
        self.assertIn("--session-id-file", help_text)
        self.assertNotIn("--thread-id-file", help_text)
        self.assertNotIn("--conversation-file", help_text)

    def test_cli_mutual_exclusions_and_input_modes(self):
        code, _, err = self.invoke(["--resume", fa.new_id(), "--steer", fa.new_id(), "hi"])
        self.assertEqual(2, code)
        self.assertIn("Cannot use --resume and --steer together", err)

        code, _, err = self.invoke(["--write", "--yolo", "hi"])
        self.assertEqual(2, code)
        self.assertIn("Cannot combine --sandbox, --write, and --yolo", err)

        code, _, err = self.invoke(["--timeout", "0", "hi"])
        self.assertEqual(2, code)

        pf = self.root / "prompt.md"
        pf.write_text("from prompt file\n", encoding="utf-8")
        code, _, err = self.invoke(["--prompt-file", str(pf), "positional"])
        self.assertEqual(2, code)

        code, _, err = self.invoke(["-C", self.work], tty=True)
        self.assertEqual(1, code)
        self.assertIn("Provide a task argument", err)

    def test_default_readonly_turn_and_resume_with_legacy_id_flag_fallback(self):
        sid = fa.new_id()
        self.fake.queue(response="Turn 1 conclusion", session_id=sid)
        sid_file = self.root / "s1.sid"
        out_file = self.root / "s1.out"
        pf = self.root / "p1.md"
        pf.write_text("inspect repo", encoding="utf-8")

        # Exercise hidden --thread-id-file alias as well
        code, out, err = self.invoke([
            "-m", "flash",
            "-e", "low",
            "-C", self.work,
            "--thread-id-file", str(sid_file),
            "-o", str(out_file),
            "--prompt-file", str(pf),
        ])
        self.assertEqual(0, code, err)
        self.assertEqual("Turn 1 conclusion\n", out)
        self.assertEqual("Turn 1 conclusion\n", out_file.read_text(encoding="utf-8"))
        self.assertEqual(sid, sid_file.read_text(encoding="utf-8").strip())
        self.assertIn(f"AGY_SESSION_ID={sid}", err)

        meta = self.parse_meta(err)
        self.assertEqual(sid, meta["session_id"])
        self.assertEqual("gemini-3.8-flash", meta["model"])
        self.assertEqual("low", meta["effort"])
        self.assertEqual("read-only", meta["sandbox"])
        self.assertTrue(meta["rules"])

        call = self.fake.calls()[0]
        self.assertEqual(["acp", "--agent", "agy-readonly-rules", "--dangerously-skip-permissions", "--sandbox"], call["argv"])
        self.assertNotIn("GEMINI_API_KEY", call["env_keys"])
        self.assertNotIn("AGY_LLM_GATEWAY_URL", call["env_keys"])
        self.assertEqual("1", call["env"][core.DELEGATE_MARKER])
        self.assertEqual("/dev/null", call["env"]["GIT_CONFIG_GLOBAL"])

        # Resume via session-id file without repeating -C/-m/-e
        self.fake.queue(response="Turn 2 conclusion")
        code2, out2, err2 = self.invoke(["--resume", str(sid_file), "follow up question"])
        self.assertEqual(0, code2, err2)
        self.assertEqual("Turn 2 conclusion\n", out2)
        meta2 = self.parse_meta(err2)
        self.assertEqual("gemini-3.8-flash", meta2["model"])
        self.assertEqual("low", meta2["effort"])
        rpcs2 = self.fake.calls()[1]["rpcs"]
        self.assertTrue(any(r.get("method") == "session/resume" and r["params"]["sessionId"] == sid for r in rpcs2))

    def test_orchestrator_model_skips_effort_unless_explicit(self):
        self.fake.queue(response="Orch reply")
        code, out, err = self.invoke(["-m", "orchestrator", "-C", self.work, "--no-rules", "plan this"])
        self.assertEqual(0, code, err)
        meta = self.parse_meta(err)
        self.assertEqual("gemini-pro-orchestrator", meta["model"])
        self.assertIsNone(meta["effort"])

        self.fake.queue(response="never")
        code2, _, err2 = self.invoke(["-m", "orchestrator", "-e", "high", "-C", self.work, "plan"])
        self.assertEqual(1, code2)
        self.assertIn("does not support --effort", err2)


class SteerAndBackgroundDrainTests(BaseTestCase):
    def test_live_mid_turn_steer_via_unix_socket(self):
        # Also test >95-byte XDG_STATE_HOME fallback to /tmp/agy-run-<uid>/
        long_state = self.root / ("very_long_state_segment_" * 5)
        sid = fa.new_id()
        self.fake.queue(
            response="Stale initial reply",
            steered_response="Fresh steered reply!",
            session_id=sid,
            wait_for_steer=True,
        )
        sid_file = self.root / "active.sid"
        result_box = {}

        def run_main_turn():
            out, err = io.StringIO(), io.StringIO()
            env = dict(os.environ, XDG_STATE_HOME=str(long_state))
            rc = agy_run.main(
                ["-C", self.work, "--no-rules", "--session-id-file", str(sid_file), "initial slow prompt"],
                stdin=PipeStdin(""),
                stdout=out,
                stderr=err,
                environment=env,
            )
            result_box["rc"] = rc
            result_box["out"] = out.getvalue()
            result_box["err"] = err.getvalue()

        t = threading.Thread(target=run_main_turn)
        t.start()
        try:
            deadline = time.monotonic() + 5.0
            while not sid_file.is_file() and time.monotonic() < deadline:
                time.sleep(0.03)
            self.assertTrue(sid_file.is_file(), "session-id-file was not published promptly")
            # Wait briefly for prompt 1 to enter tool wait
            time.sleep(0.15)

            s_out, s_err = io.StringIO(), io.StringIO()
            env = dict(os.environ, XDG_STATE_HOME=str(long_state))
            s_rc = agy_run.main(
                ["--steer", str(sid_file), "Switch focus to module B"],
                stdin=PipeStdin(""),
                stdout=s_out,
                stderr=s_err,
                environment=env,
            )
            self.assertEqual(0, s_rc, s_err.getvalue())
            steer_resp = json.loads(s_out.getvalue())
            self.assertTrue(steer_resp["ok"])
            self.assertEqual("live_steer", steer_resp["mode"])
        finally:
            t.join(timeout=10.0)

        self.assertEqual(0, result_box["rc"], result_box.get("err"))
        self.assertEqual("Fresh steered reply!\n", result_box["out"])
        self.assertNotIn("Stale initial reply", result_box["out"])

    def test_background_run_command_drain_converges_from_transcript(self):
        sid = fa.new_id()
        self.fake.queue(
            response="Command moved to background, waiting...",
            session_id=sid,
            tools=[{
                "name": "run_command",
                "params": {"CommandLine": "pytest -q", "WaitMsBeforeAsync": 10000},
                "status": "in_progress",
            }],
            bg_drain={
                "delay": 0.2,
                "wakeup_tool_calls": [{
                    "name": "read_file",
                    "args": {"Path": json.dumps(str(Path(self.work) / "report.txt"))},
                }],
                "final_text": "All 42 tests passed after background drain.",
            },
        )
        write_fake_db_calls(self.home, sid, [("call_1", "pytest -q", False)])

        code, out, err = self.invoke(["-C", self.work, "--no-rules", "run suite"])
        self.assertEqual(0, code, err)
        self.assertEqual("All 42 tests passed after background drain.\n", out)
        self.assertIn("Background command running", err)
        meta = self.parse_meta(err)
        self.assertEqual(1, meta["bg_tasks_drained"])
        self.assertEqual(2, meta["tool_calls"])
        self.assertEqual("ok", meta["sandbox_check"])

    def test_daemon_command_is_rejected(self):
        self.fake.queue(
            response="Started server",
            tools=[{
                "name": "run_command",
                "params": {"CommandLine": "npm run dev", "IsDaemon": True},
                "status": "in_progress",
            }],
        )
        code, _, err = self.invoke(["--yolo", "-C", self.work, "--no-rules", "start dev server"])
        self.assertEqual(1, code)
        self.assertIn("IsDaemon=true", err)


class SandboxAndSecurityEnforcementTests(BaseTestCase):
    def test_nested_agy_run_blocked_by_env_marker(self):
        with mock.patch.dict(os.environ, {core.DELEGATE_MARKER: "1"}):
            code, _, err = self.invoke(["-C", self.work, "hello"])
        self.assertEqual(1, code)
        self.assertIn("Refusing nested agy_run.py", err)
        self.assertEqual([], self.fake.calls())

    def test_readonly_workspace_mutation_fails_run(self):
        dirty = Path(self.work) / "sneaky.txt"
        self.fake.queue(
            response="Done",
            shell=[f"echo oops > {dirty}"],
            tools=[{"name": "run_command", "params": {"CommandLine": f"echo oops > {dirty}"}}],
        )
        code, _, err = self.invoke(["-C", self.work, "--no-rules", "check status"])
        self.assertEqual(1, code)
        self.assertIn("read-only mode", err)
        self.assertIn("sneaky.txt", err)

    def test_bypass_sandbox_caught_via_acp_params_and_multibyte_sqlite_varint(self):
        # 1. Direct ACP tool_call parameter BypassSandbox=True
        self.fake.queue(
            response="Ran outside",
            tools=[{
                "name": "run_command",
                "params": {"CommandLine": "ls ~/.ssh", "BypassSandbox": True},
            }],
        )
        code, _, err = self.invoke(["--write", "-C", self.work, "--no-rules", "check ssh"])
        self.assertEqual(1, code)
        self.assertIn("BypassSandbox", err)

        # 2. Multi-byte protobuf varint (>16KB command) in SQLite store
        sid = fa.new_id()
        long_cmd = "echo " + ("x" * 20000)
        write_fake_db_calls(self.home, sid, [("call_99", long_cmd, True)], wal=True)
        stored = agy_run.stored_commands(sid)
        self.assertEqual({"call_99": (long_cmd, True)}, stored)
        self.assertEqual(
            [long_cmd],
            agy_run.unsandboxed_commands(
                sid,
                [{"name": "run_command", "parameters": {"CommandLine": long_cmd}}],
                {},
            ),
        )

    def test_write_mode_blocks_outside_writes_while_yolo_allows(self):
        outside = self.root / "outside.txt"
        self.fake.queue(
            response="Wrote outside",
            tools=[{"name": "write_file", "params": {"Path": str(outside)}}],
        )
        code, _, err = self.invoke(["--write", "-C", self.work, "--no-rules", "write file"])
        self.assertEqual(1, code)
        self.assertIn("wrote files outside the workspace", err)

        self.fake.queue(
            response="Wrote outside in yolo",
            tools=[{"name": "write_file", "params": {"Path": str(outside)}}],
        )
        code2, _, err2 = self.invoke(["--yolo", "-C", self.work, "--no-rules", "write file"])
        self.assertEqual(0, code2, err2)

    def test_claude_memory_protected_in_write_mode(self):
        mem_dir = Path(self.work) / ".agents" / "memory"
        mem_dir.mkdir(parents=True)
        mem_file = mem_dir / "MEMORY.md"
        mem_file.write_text("# Memory\n", encoding="utf-8")

        self.fake.queue(
            response="Edited memory",
            shell=[f"echo bad >> {mem_file}"],
        )
        code, _, err = self.invoke(["--write", "-C", self.work, "update repo"])
        self.assertEqual(1, code)
        self.assertIn("Claude's project memory", err)

    def test_agent_cli_calls_and_path_stubs_block_claude_and_codex(self):
        self.fake.queue(
            response="Ran stubs",
            shell=["claude --version", "codex --version"],
        )
        code, _, err = self.invoke(["--yolo", "-C", self.work, "--no-rules", "probe"])
        self.assertEqual(0, code, err)
        results = self.fake.shell_results()
        self.assertEqual([126, 126], [r["code"] for r in results])
        self.assertTrue(all("blocked here" in r["stderr"] for r in results))

        # Command-line scanner catches direct and sh -c invocations
        for bad_cmd in ("claude -p hi", "env FOO=1 codex exec x", "zsh -lc 'echo ok && claude'"):
            self.assertEqual(
                [bad_cmd],
                agy_run.agent_cli_calls([{"name": "run_command", "parameters": {"CommandLine": bad_cmd}}]),
            )
        # Inspecting agy_run.py as a file argument must NOT trigger false positives
        self.assertEqual(
            [],
            agy_run.agent_cli_calls([
                {"name": "run_command", "parameters": {"CommandLine": "git diff skills/agy-run/scripts/agy_run.py"}},
            ]),
        )

    def test_permission_request_and_safety_filter_and_disallowed_tool(self):
        self.fake.queue(request_permission={"name": "read_file", "params": {"Path": "/etc/passwd"}})
        code, _, err = self.invoke(["-C", self.work, "--no-rules", "read passwd"])
        self.assertEqual(1, code)
        self.assertIn("interactive permission", err)

        self.fake.queue(response=fa.FILTER_TEXT)
        code2, _, err2 = self.invoke(["-C", self.work, "--no-rules", "filtered"])
        self.assertEqual(1, code2)
        self.assertIn("safety filter", err2)

        # write_file in read-only mode is outside allowed_tools
        self.fake.queue(
            response="Edited",
            tools=[{"name": "write_file", "params": {"Path": str(Path(self.work) / "a.txt")}}],
        )
        code3, _, err3 = self.invoke(["-C", self.work, "--no-rules", "edit"])
        self.assertEqual(1, code3)
        self.assertIn("outside this sandbox mode", err3)

    def test_timeout_exit_124_and_process_group_reaping_and_lock_contention(self):
        self.fake.queue(response="never", sleep=5.0, ignore_sigint=True, leave_child=True)
        code, _, err = self.invoke(["-C", self.work, "--no-rules", "--timeout", "1.0", "slow"])
        self.assertEqual(124, code)
        self.assertIn("did not finish", err)
        child_pid_file = self.fake.base / "child.pid"
        if child_pid_file.exists():
            cpid = int(child_pid_file.read_text().strip())
            with self.assertRaises(ProcessLookupError):
                os.kill(cpid, 0)

        # Concurrent --resume lock contention
        sid = fa.new_id()
        self.fake.queue(response="ok", session_id=sid)
        rc, _, _ = self.invoke(["-C", self.work, "--no-rules", "first"])
        self.assertEqual(0, rc)
        lock_fd = core.acquire_session_lock(agy_run.ensure_state(), sid)
        try:
            rc2, _, err2 = self.invoke(["--resume", sid, "concurrent"])
            self.assertEqual(1, rc2)
            self.assertIn("already has an active agy-run turn running", err2)
        finally:
            core.release_session_lock(lock_fd)

    def test_session_lock_stays_held_across_post_turn_checks(self):
        sid = fa.new_id()
        self.fake.queue(response="checked", session_id=sid)
        real_snapshot_changes = agy_run.snapshot_changes
        lock_verified = []

        def verify_locked_during_check(before, dirs):
            with self.assertRaises(core.AgyError):
                core.acquire_session_lock(agy_run.ensure_state(), sid)
            lock_verified.append(True)
            return real_snapshot_changes(before, dirs)

        with mock.patch.object(agy_run, "snapshot_changes", side_effect=verify_locked_during_check):
            rc, _, err = self.invoke(["-C", self.work, "--no-rules", "check lock"])
        self.assertEqual(0, rc, err)
        self.assertEqual([True], lock_verified)
        # After run_turn completes, lock must be released cleanly
        fd = core.acquire_session_lock(agy_run.ensure_state(), sid)
        core.release_session_lock(fd)

    def test_readonly_rejects_launcher_output_files_inside_workspace(self):
        inside_sid = str(Path(self.work) / "inside.sid")
        code, _, err = self.invoke(["-C", self.work, "--session-id-file", inside_sid, "hi"])
        self.assertEqual(1, code)
        self.assertIn("points inside the read-only workspace", err)
        self.assertEqual([], self.fake.calls())

        inside_out = str(Path(self.work) / "sub" / "inside.out")
        code2, _, err2 = self.invoke(["-C", self.work, "-o", inside_out, "hi"])
        self.assertEqual(1, code2)
        self.assertIn("points inside the read-only workspace", err2)
        self.assertEqual([], self.fake.calls())

        # Trailing symlink inside read-only workspace pointing outside must ALSO be rejected
        ext_target = self.root / "external.out"
        ws_symlink = Path(self.work) / "link_to_ext.out"
        ws_symlink.symlink_to(ext_target)
        code3, _, err3 = self.invoke(["-C", self.work, "-o", str(ws_symlink), "hi"])
        self.assertEqual(1, code3)
        self.assertIn("points inside the read-only workspace", err3)
        self.assertTrue(ws_symlink.is_symlink())
        self.assertEqual([], self.fake.calls())

    def test_steer_server_start_failure_does_not_leak_new_session_lock(self):
        sid = fa.new_id()
        self.fake.queue(response="never", session_id=sid)
        with mock.patch("socket.socket.bind", side_effect=OSError("simulated bind failure")):
            code, _, err = self.invoke(["-C", self.work, "--no-rules", "hi"])
        self.assertEqual(1, code)
        self.assertIn("simulated bind failure", err)
        # Lock for sid must NOT remain held after mid-startup failure
        fd = core.acquire_session_lock(agy_run.ensure_state(), sid)
        core.release_session_lock(fd)

    def test_live_steer_does_not_mask_unfinished_pre_steer_command(self):
        sid = fa.new_id()
        self.fake.queue(
            response="waiting",
            steered_response="Premature steered reply",
            session_id=sid,
            wait_for_steer=True,
            keep_tool_in_progress=True,
        )
        sid_file = self.root / "mask_steer.sid"
        box = {}

        def _run():
            box["res"] = self.invoke(["-C", self.work, "--no-rules", "--session-id-file", str(sid_file), "go"])

        t = threading.Thread(target=_run)
        t.start()
        try:
            deadline = time.monotonic() + 5.0
            while not sid_file.is_file() and time.monotonic() < deadline:
                time.sleep(0.03)
            time.sleep(0.15)
            s_code, _, s_err = self.invoke(["--steer", str(sid_file), "try early exit"])
            self.assertEqual(0, s_code, s_err)
        finally:
            t.join(timeout=15.0)

        rc_main, out_main, err_main = box["res"]
        self.assertEqual(1, rc_main)
        self.assertEqual("", out_main)
        self.assertIn("still in_progress", err_main)

    def test_untracked_in_progress_run_command_fails_loudly_and_empty_steered_reply_fails(self):
        # 1. run_command ends turn still in_progress with no transcript bg task -> must fail!
        self.fake.queue(
            response="Still running...",
            tools=[{
                "name": "run_command",
                "params": {"CommandLine": "sleep 20"},
                "status": "in_progress",
            }],
        )
        # Patch time.monotonic only for grace check? Even simpler: let 5s grace expire or shorten via mock?
        # Wait: 5s is fast enough or we can run it directly! Let's run it directly (5.1s).
        code, _, err = self.invoke(["-C", self.work, "--no-rules", "run untracked bg"])
        self.assertEqual(1, code)
        self.assertIn("still in_progress", err)

        # 2. Steered prompt returning empty text must NOT return superseded pre-steer draft
        sid = fa.new_id()
        self.fake.queue(
            pre_steer_chunk="Superseded draft that must never leak",
            response="",
            steered_response="",
            session_id=sid,
            wait_for_steer=True,
        )
        sid_file = self.root / "empty_steer.sid"
        box = {}

        def _run():
            box["res"] = self.invoke(["-C", self.work, "--no-rules", "--session-id-file", str(sid_file), "go"])

        t = threading.Thread(target=_run)
        t.start()
        try:
            deadline = time.monotonic() + 5.0
            while not sid_file.is_file() and time.monotonic() < deadline:
                time.sleep(0.03)
            time.sleep(0.15)
            s_code, _, s_err = self.invoke(["--steer", str(sid_file), "abort old draft"])
            self.assertEqual(0, s_code, s_err)
        finally:
            t.join(timeout=10.0)

        rc_main, out_main, err_main = box["res"]
        self.assertEqual(1, rc_main)
        self.assertEqual("", out_main)
        self.assertIn("empty answer", err_main)


class TranscriptRegexAndEnvTests(BaseTestCase):
    def test_bg_task_regex_ignores_source_literals_and_other_sessions(self):
        sid = fa.new_id()
        other_sid = fa.new_id()
        logs = (
            Path(self.home)
            / ".gemini"
            / "antigravity-cli"
            / "brain"
            / sid
            / ".system_generated"
            / "logs"
        )
        logs.mkdir(parents=True)
        tf = logs / "transcript_full.jsonl"

        noise_steps = [
            {
                "step_index": 1,
                "type": "GENERIC",
                "status": "DONE",
                "content": (
                    "Created At: 2026-11-24T20:00:00+08:00\n"
                    "Showing lines 50-60 of 100\n"
                    '57: BG_TASK_SPAWN_RE = re.compile(r"Tool is running as a background task with task id:\\s*(\\S+)")\n'
                ),
            },
            {
                "step_index": 2,
                "type": "GENERIC",
                "status": "DONE",
                "content": f"Tool is running as a background task with task id: {other_sid}/task-1\n",
            },
            {
                "step_index": 3,
                "type": "GENERIC",
                "status": "DONE",
                "content": f"quoted: Tool is running as a background task with task id: {sid}/task-9\n",
            },
        ]
        tf.write_text(
            "".join(json.dumps(s) + "\n" for s in noise_steps),
            encoding="utf-8",
        )
        env = dict(os.environ, HOME=self.home)
        res = core.inspect_transcript_drain(sid, 0, environment=env)
        self.assertEqual(set(), res["spawned"])
        self.assertEqual(set(), res["pending"])
        self.assertFalse(res["converged"])

        # Real background task line matching current session_id/task-<N>
        real_spawn = {
            "step_index": 4,
            "type": "GENERIC",
            "status": "DONE",
            "content": (
                "Created At: 2026-11-24T20:01:00+08:00\n"
                f"Tool is running as a background task with task id: {sid}/task-7\n"
                "Task Description: pytest -q"
            ),
        }
        with tf.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(real_spawn) + "\n")
        res2 = core.inspect_transcript_drain(sid, 0, environment=env)
        self.assertEqual({f"{sid}/task-7"}, res2["spawned"])
        self.assertEqual({f"{sid}/task-7"}, res2["pending"])
        self.assertFalse(res2["converged"])

        # Real SYSTEM_MESSAGE wrapper with [Message] ... content=Task id "..." finished with result:
        real_done = [
            {
                "step_index": 5,
                "type": "SYSTEM_MESSAGE",
                "status": "DONE",
                "content": (
                    "The following is a <SYSTEM_MESSAGE>\n\n<SYSTEM_MESSAGE>\n"
                    f'[Message] timestamp=2026-11-24T12:01:20Z sender={sid}/task-7 '
                    f'priority=MESSAGE_PRIORITY_HIGH content=Task id "{sid}/task-7" '
                    "finished with result: exit 0"
                ),
            },
            {
                "step_index": 6,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "Final converged output",
            },
        ]
        with tf.open("a", encoding="utf-8") as fh:
            for s in real_done:
                fh.write(json.dumps(s) + "\n")
        res3 = core.inspect_transcript_drain(sid, 0, environment=env)
        self.assertEqual({f"{sid}/task-7"}, res3["spawned"])
        self.assertEqual(set(), res3["pending"])
        self.assertTrue(res3["converged"])
        self.assertEqual("Final converged output", res3["final_text"])

    def test_bg_done_header_anchor_ignores_quoted_second_task_completion_in_body(self):
        sid = fa.new_id()
        logs = (
            Path(self.home)
            / ".gemini"
            / "antigravity-cli"
            / "brain"
            / sid
            / ".system_generated"
            / "logs"
        )
        logs.mkdir(parents=True)
        tf = logs / "transcript_full.jsonl"
        task1 = f"{sid}/task-1"
        task2 = f"{sid}/task-2"
        steps = [
            {
                "step_index": 1,
                "type": "GENERIC",
                "status": "DONE",
                "content": f"Created At: 2026-11-24T20:01:00+08:00\nTool is running as a background task with task id: {task1}\n",
            },
            {
                "step_index": 2,
                "type": "GENERIC",
                "status": "DONE",
                "content": f"Created At: 2026-11-24T20:01:01+08:00\nTool is running as a background task with task id: {task2}\n",
            },
            {
                "step_index": 3,
                "type": "SYSTEM_MESSAGE",
                "status": "DONE",
                "content": (
                    "The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n\n"
                    "<SYSTEM_MESSAGE>\n"
                    f'[Message] timestamp=2026-11-24T12:01:10Z sender={task1} '
                    f'priority=MESSAGE_PRIORITY_HIGH content=Task id "{task1}" finished with result:\n'
                    f'stdout quoted: Task id "{task2}" finished with result: fake\n'
                    f'[Message] sender={task2} content=Task id "{task2}" finished with result: fake2\n'
                ),
            },
            {
                "step_index": 4,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "Still waiting on task 2",
            },
        ]
        tf.write_text("".join(json.dumps(s) + "\n" for s in steps), encoding="utf-8")
        res = core.inspect_transcript_drain(sid, 0, environment={"HOME": self.home})
        self.assertEqual({task1, task2}, res["spawned"])
        self.assertEqual({task2}, res["pending"])
        self.assertFalse(res["converged"])

    def test_home_env_propagation_and_all_proxy_bridge_and_agy_bin_discovery(self):
        custom_home = self.root / "alt-home"
        custom_home.mkdir()
        sid = fa.new_id()
        env_alt = {"HOME": str(custom_home), "PATH": ""}

        # brain_transcript_path honors passed environment["HOME"]
        tpath = core.brain_transcript_path(sid, environment=env_alt)
        self.assertEqual(
            custom_home / ".gemini" / "antigravity-cli" / "brain" / sid / ".system_generated" / "logs" / "transcript.jsonl",
            tpath,
        )

        # claude_memory_dir honors passed environment["HOME"]
        encoded = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(self.work))
        mem_dir = custom_home / ".claude" / "projects" / encoded / "memory"
        mem_dir.mkdir(parents=True)
        (mem_dir / "MEMORY.md").write_text("# Alt memory\n", encoding="utf-8")
        self.assertEqual(mem_dir, core.claude_memory_dir(self.work, environment=env_alt))
        self.assertIsNone(core.claude_memory_dir(self.work, environment={"HOME": self.home}))

        # ALL_PROXY / all_proxy bridging
        bridged = core.agy_env(environment={"ALL_PROXY": "socks5://127.0.0.1:1080"})
        self.assertEqual("socks5://127.0.0.1:1080", bridged["HTTP_PROXY"])
        self.assertEqual("socks5://127.0.0.1:1080", bridged["HTTPS_PROXY"])

        partial = core.agy_env(
            environment={"HTTP_PROXY": "http://127.0.0.1:8080", "all_proxy": "socks5://127.0.0.1:1080"}
        )
        self.assertEqual("http://127.0.0.1:8080", partial["HTTP_PROXY"])
        self.assertEqual("socks5://127.0.0.1:1080", partial["HTTPS_PROXY"])

        # agy_bin priority: AGY_BIN > PATH > <HOME>/.local/bin/agy > "agy"
        local_bin = custom_home / ".local" / "bin"
        local_bin.mkdir(parents=True)
        local_agy = local_bin / "agy"
        local_agy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        local_agy.chmod(local_agy.stat().st_mode | stat.S_IXUSR)

        with mock.patch.object(core, "AGY_FALLBACK_DIRS", ()):
            self.assertEqual("/custom/agy", core.agy_bin({"AGY_BIN": "/custom/agy", "HOME": str(custom_home), "PATH": ""}))
            self.assertEqual(str(local_agy), core.agy_bin({"HOME": str(custom_home), "PATH": ""}))
            self.assertEqual("agy", core.agy_bin({"HOME": self.home, "PATH": ""}))


class ListModelsInstallAgentsAndPreflightTests(BaseTestCase):
    def test_list_models_filters_stderr_skips_preflight_and_leaves_acp_queue_intact(self):
        # Remove agents dir to prove --list-models does not trigger pre-flight check
        shutil.rmtree(self.agents_dir)
        expected_out = "gemini-4-carbon-medium\tGemini 4 Carbon (Medium)\ngemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"
        self.fake.set_models_response(
            stdout=expected_out,
            stderr="Fetching available models...\nI0000 glog noise\n",
            code=0,
        )
        self.fake.queue(response="First queued ACP turn")

        code, out, err = self.invoke(["--list-models"])
        self.assertEqual(0, code, err)
        self.assertEqual(expected_out, out)
        self.assertEqual("", err)
        self.assertEqual([], self.fake.calls())
        self.assertEqual(1, len(self.fake.models_calls()))

        # Restore agents and verify ACP queue index 0 was NOT consumed by --list-models
        agy_run.write_agent_files(self.agents_dir)
        rc2, out2, err2 = self.invoke(["-C", self.work, "--no-rules", "hello"])
        self.assertEqual(0, rc2, err2)
        self.assertEqual("First queued ACP turn\n", out2)

    def test_list_models_failure_and_timeout(self):
        self.fake.set_models_response(stdout="", stderr="auth token expired\n", code=2)
        code, out, err = self.invoke(["--list-models"])
        self.assertNotEqual(0, code)
        self.assertEqual("", out)
        self.assertIn("auth token expired", err)

        self.fake.set_models_response(stdout="", stderr="", sleep=5.0, ignore_sigint=True)
        with mock.patch.object(agy_run, "LIST_MODELS_TIMEOUT", 0.5):
            code2, _, err2 = self.invoke(["--list-models"])
        self.assertEqual(124, code2)
        self.assertIn("timed out", err2)

    def test_list_models_sigterm_cancels_and_reaps_child_process_group(self):
        self.fake.set_models_response(
            stdout="",
            stderr="",
            sleep=30.0,
            ignore_sigint=True,
            leave_child=True,
        )
        child_pid_file = self.fake.base / "models_child.pid"
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPTS_DIR / "agy_run.py"), "--list-models"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ),
        )
        try:
            deadline = time.monotonic() + 5.0
            while not child_pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.03)
            self.assertTrue(child_pid_file.exists(), "fake agy models child did not start in time")
            cpid = int(child_pid_file.read_text(encoding="utf-8").strip())
            proc.send_signal(signal.SIGTERM)
            _, err = proc.communicate(timeout=10.0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(130, proc.returncode, err)
        self.assertIn("cancelled by signal", err)
        with self.assertRaises(ProcessLookupError):
            os.kill(cpid, 0)

    def test_install_agents_fresh_idempotent_crlf_and_conflict_refusal(self):
        shutil.rmtree(self.agents_dir)

        # 1. Fresh install into missing directory
        code, out, err = self.invoke(["--install-agents"])
        self.assertEqual(0, code, err)
        self.assertIn("Installed 6", out)
        expected = agy_run.agent_definitions()
        for fname, content in expected.items():
            self.assertEqual(content, (self.agents_dir / fname).read_text(encoding="utf-8"))

        # 2. Idempotent second run writes nothing (mtimes unchanged)
        mtimes_before = {p.name: p.stat().st_mtime_ns for p in self.agents_dir.glob("*.md")}
        time.sleep(0.02)
        code2, out2, err2 = self.invoke(["--install-agents"])
        self.assertEqual(0, code2, err2)
        self.assertIn("already up to date", out2)
        mtimes_after = {p.name: p.stat().st_mtime_ns for p in self.agents_dir.glob("*.md")}
        self.assertEqual(mtimes_before, mtimes_after)

        # 3. CRLF line endings are accepted as up to date
        ro_file = self.agents_dir / "agy-readonly.md"
        ro_file.write_bytes(expected["agy-readonly.md"].replace("\n", "\r\n").encode("utf-8"))
        code3, out3, err3 = self.invoke(["--install-agents"])
        self.assertEqual(0, code3, err3)
        self.assertIn("already up to date", out3)

        # 4. Conflict + missing refuses partial install (zero files written)
        ro_file.write_text("# locally edited\n", encoding="utf-8")
        missing_file = self.agents_dir / "agy-yolo.md"
        missing_file.unlink()
        code4, _, err4 = self.invoke(["--install-agents"])
        self.assertEqual(1, code4)
        self.assertIn("agy-readonly.md", err4)
        self.assertIn("--write-agent-files", err4)
        self.assertIn(str(self.agents_dir), err4)
        self.assertFalse(missing_file.exists(), "must not partially install missing files on conflict")
        self.assertEqual("# locally edited\n", ro_file.read_text(encoding="utf-8"))

        # 5. Symlink file inside normal directory is treated as conflict
        ro_file.unlink()
        ext_file = self.root / "external.md"
        ext_file.write_text(expected["agy-readonly.md"], encoding="utf-8")
        ro_file.symlink_to(ext_file)
        code5, _, err5 = self.invoke(["--install-agents"])
        self.assertEqual(1, code5)
        self.assertIn("agy-readonly.md", err5)
        self.assertFalse(missing_file.exists())

    def test_install_agents_symlink_directory_handling(self):
        shutil.rmtree(self.agents_dir)
        real_dir = self.root / "managed-agents-repo"
        agy_run.write_agent_files(real_dir)
        self.agents_dir.parent.mkdir(parents=True, exist_ok=True)
        self.agents_dir.symlink_to(real_dir)

        # All 6 match -> zero writes, exit 0
        mtimes_before = {p.name: p.stat().st_mtime_ns for p in real_dir.glob("*.md")}
        time.sleep(0.02)
        code, out, err = self.invoke(["--install-agents"])
        self.assertEqual(0, code, err)
        self.assertIn("already up to date", out)
        self.assertEqual(mtimes_before, {p.name: p.stat().st_mtime_ns for p in real_dir.glob("*.md")})

        # Missing or modified file through symlink -> fail without writing, print physical target
        missing = real_dir / "agy-write.md"
        missing.unlink()
        (real_dir / "agy-yolo.md").write_text("stale\n", encoding="utf-8")
        code2, _, err2 = self.invoke(["--install-agents"])
        self.assertEqual(1, code2)
        self.assertIn(str(real_dir), err2)
        self.assertIn("--write-agent-files", err2)
        self.assertIn("agy-write.md", err2)
        self.assertIn("agy-yolo.md", err2)
        self.assertFalse(missing.exists(), "must not write through symlink directory")

    def test_preflight_single_file_check_missing_drifted_symlink_resume_and_steer(self):
        # 1. Missing selected agent fails before ACP
        (self.agents_dir / "agy-readonly.md").unlink()
        code, _, err = self.invoke(["-C", self.work, "--no-rules", "check"])
        self.assertEqual(1, code)
        self.assertIn("missing", err)
        self.assertIn("--install-agents", err)
        self.assertEqual([], self.fake.calls())

        # 2. Unrelated missing agent does NOT block a different mode
        self.fake.queue(response="Write turn OK")
        code_ok, out_ok, err_ok = self.invoke(["--write", "-C", self.work, "--no-rules", "edit"])
        self.assertEqual(0, code_ok, err_ok)
        self.assertEqual("Write turn OK\n", out_ok)

        # 3. Drifted/modified selected agent fails with distinct message + --write-agent-files
        (self.agents_dir / "agy-readonly.md").write_text("# old version\n", encoding="utf-8")
        code2, _, err2 = self.invoke(["-C", self.work, "--no-rules", "check"])
        self.assertEqual(1, code2)
        self.assertIn("local modifications or is outdated", err2)
        self.assertIn("--write-agent-files", err2)
        self.assertIn(str(self.agents_dir), err2)
        self.assertEqual(1, len(self.fake.calls()))

        # 4. Symlink directory pre-flight error points to physical target
        shutil.rmtree(self.agents_dir)
        real_dir = self.root / "sym-target"
        agy_run.write_agent_files(real_dir)
        (real_dir / "agy-readonly.md").unlink()
        self.agents_dir.symlink_to(real_dir)
        code3, _, err3 = self.invoke(["-C", self.work, "--no-rules", "check"])
        self.assertEqual(1, code3)
        self.assertIn(str(real_dir), err3)
        self.assertIn("--write-agent-files", err3)
        self.assertEqual(1, len(self.fake.calls()))

        # 5. --resume also runs pre-flight check
        self.agents_dir.unlink()
        agy_run.write_agent_files(self.agents_dir)
        sid = fa.new_id()
        self.fake.queue(response="Turn 1", session_id=sid)
        rc_init, _, err_init = self.invoke(["-C", self.work, "--no-rules", "start"])
        self.assertEqual(0, rc_init, err_init)
        (self.agents_dir / "agy-readonly.md").unlink()
        rc_res, _, err_res = self.invoke(["--resume", sid, "continue"])
        self.assertEqual(1, rc_res)
        self.assertIn("missing", err_res)
        self.assertEqual(2, len(self.fake.calls()))

        # 6. --steer is NOT blocked by missing agents dir
        shutil.rmtree(self.agents_dir)
        rc_steer, _, err_steer = self.invoke(["--steer", sid, "nudge"])
        self.assertEqual(1, rc_steer)
        self.assertIn("no active agy-run turn found", err_steer)
        self.assertNotIn("--install-agents", err_steer)


if __name__ == "__main__":
    unittest.main()
