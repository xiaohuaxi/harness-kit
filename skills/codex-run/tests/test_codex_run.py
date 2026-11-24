import importlib.util
import io
import json
import os
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

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "codex_run.py"
SPEC = importlib.util.spec_from_file_location("codex_run", SCRIPT_PATH)
codex_run = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(codex_run)

ROOT_TID = "019d7bdf-b702-7be2-8c96-11971d29bf78"
SUB_TID = "019d7bdf-c111-7be2-8c96-222222222222"


FAKE_APP_SERVER = """#!/usr/bin/env python3
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

log_file = os.environ.get("FAKE_CODEX_LOG")
mode = os.environ.get("FAKE_CODEX_MODE", "normal")
root_tid = os.environ.get("FAKE_ROOT_TID", "019d7bdf-b702-7be2-8c96-11971d29bf78")
sub_tid = os.environ.get("FAKE_SUB_TID", "019d7bdf-c111-7be2-8c96-222222222222")

def record(event):
    if not log_file:
        return
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\\n")

def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\\n")
    sys.stdout.flush()

record({"pid": os.getpid(), "argv": sys.argv[1:], "cwd": os.getcwd(), "zdotdir": os.environ.get("ZDOTDIR"), "active_marker": os.environ.get("CODEX_RUN_ACTIVE")})

if mode == "hold_timeout":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    if os.environ.get("FAKE_CHILD_PID_FILE"):
        Path(os.environ["FAKE_CHILD_PID_FILE"]).write_text(str(child.pid), encoding="utf-8")

turn_count = 0
steered_texts = []

for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    msg = json.loads(line)
    record({"rpc_in": msg})
    rid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}

    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"userAgent": "fake-codex"}})
    elif method == "initialized":
        pass
    elif method in ("thread/start", "thread/resume"):
        tid = params.get("threadId") or root_tid
        eff_model = params.get("model") or "deepseek-v4-pro"
        eff_effort = "high" if "--profile" in sys.argv else None
        res_obj = {"thread": {"id": tid}, "model": eff_model}
        if eff_effort:
            res_obj["reasoningEffort"] = eff_effort
        send({"jsonrpc": "2.0", "id": rid, "result": res_obj})
    elif method == "turn/steer":
        txt = ((params.get("input") or [{}])[0]).get("text", "")
        steered_texts.append(txt)
        send({"jsonrpc": "2.0", "id": rid, "result": {"turnId": params.get("expectedTurnId") or "turn-1"}})
        if mode == "wait_steer":
            send({"jsonrpc": "2.0", "method": "item/completed", "params": {"threadId": root_tid, "turnId": "turn-1", "item": {"type": "agentMessage", "id": "m1", "text": "STEERED:" + txt}}})
            send({"jsonrpc": "2.0", "method": "turn/completed", "params": {"threadId": root_tid, "turn": {"id": "turn-1", "status": "completed", "items": [], "error": None}}})
    elif method == "turn/start":
        turn_count += 1
        t_id = "turn-%d" % turn_count
        prompt = ((params.get("input") or [{}])[0]).get("text", "")
        send({"jsonrpc": "2.0", "id": rid, "result": {"turn": {"id": t_id, "status": "inProgress", "items": [], "error": None}}})
        send({"jsonrpc": "2.0", "method": "turn/started", "params": {"threadId": root_tid, "turn": {"id": t_id, "status": "inProgress", "items": [], "error": None}}})

        if mode == "hold_timeout":
            time.sleep(60)
        elif mode == "wait_steer":
            # Wait for turn/steer on stdin
            continue
        elif mode == "server_request":
            send({"jsonrpc": "2.0", "id": 999, "method": "item/tool/requestUserInput", "params": {"questions": []}})
            continue
        elif mode == "subagent_early_exit":
            if turn_count == 1:
                send({"jsonrpc": "2.0", "method": "turn/started", "params": {"threadId": sub_tid, "turn": {"id": "sub-1", "status": "inProgress", "items": [], "error": None}}})
                send({"jsonrpc": "2.0", "method": "item/completed", "params": {"threadId": root_tid, "turnId": t_id, "item": {"type": "agentMessage", "id": "m1", "text": "Spawned subagent, ending root turn early."}}})
                send({"jsonrpc": "2.0", "method": "turn/completed", "params": {"threadId": root_tid, "turn": {"id": t_id, "status": "completed", "items": [], "error": None}}})
                time.sleep(0.15)
                send({"jsonrpc": "2.0", "method": "turn/completed", "params": {"threadId": sub_tid, "turn": {"id": "sub-1", "status": "completed", "items": [], "error": None}}})
            else:
                send({"jsonrpc": "2.0", "method": "item/completed", "params": {"threadId": root_tid, "turnId": t_id, "item": {"type": "agentMessage", "id": "m2", "text": "FINAL_AFTER_SUBAGENT_DRAIN"}}})
                send({"jsonrpc": "2.0", "method": "turn/completed", "params": {"threadId": root_tid, "turn": {"id": t_id, "status": "completed", "items": [], "error": None}}})
        elif mode == "infinite_subagent_loop":
            sub_id_here = "%s-%d" % (sub_tid[:-2], turn_count)
            send({"jsonrpc": "2.0", "method": "turn/started", "params": {"threadId": sub_id_here, "turn": {"id": "sub", "status": "inProgress", "items": [], "error": None}}})
            send({"jsonrpc": "2.0", "method": "item/completed", "params": {"threadId": root_tid, "turnId": t_id, "item": {"type": "agentMessage", "id": "m", "text": "LOOP_TURN_%d" % turn_count}}})
            send({"jsonrpc": "2.0", "method": "turn/completed", "params": {"threadId": root_tid, "turn": {"id": t_id, "status": "completed", "items": [], "error": None}}})
            time.sleep(0.05)
            send({"jsonrpc": "2.0", "method": "turn/completed", "params": {"threadId": sub_id_here, "turn": {"id": "sub", "status": "completed", "items": [], "error": None}}})
        elif mode == "turn_failed":
            send({"jsonrpc": "2.0", "method": "turn/completed", "params": {"threadId": root_tid, "turn": {"id": t_id, "status": "failed", "items": [], "error": {"message": "Auth failed with Bearer secret-token-value"}}}})
        elif mode == "crash_mid_turn":
            send({"jsonrpc": "2.0", "method": "item/completed", "params": {"threadId": root_tid, "turnId": t_id, "item": {"type": "agentMessage", "id": "m1", "text": "Partial progress before crash"}}})
            sys.exit(9)
        else:
            send({"jsonrpc": "2.0", "method": "thread/tokenUsage/updated", "params": {"threadId": root_tid, "turnId": t_id, "tokenUsage": {"last": {"inputTokens": 100, "cachedInputTokens": 20, "outputTokens": 10, "reasoningOutputTokens": 5, "totalTokens": 110}, "modelContextWindow": 400000}}})
            reply_text = os.environ.get("FAKE_REPLY_TEXT", "OK_RESULT")
            send({"jsonrpc": "2.0", "method": "item/completed", "params": {"threadId": root_tid, "turnId": t_id, "item": {"type": "agentMessage", "id": "m1", "text": reply_text}}})
            send({"jsonrpc": "2.0", "method": "turn/completed", "params": {"threadId": root_tid, "turn": {"id": t_id, "status": "completed", "items": [], "error": None}}})
    elif "error" in msg and msg.get("id") == 999:
        # Response to our ServerRequest
        send({"jsonrpc": "2.0", "method": "item/completed", "params": {"threadId": root_tid, "turnId": "turn-1", "item": {"type": "agentMessage", "id": "m1", "text": "HANDLED_SERVER_REQUEST"}}})
        send({"jsonrpc": "2.0", "method": "turn/completed", "params": {"threadId": root_tid, "turn": {"id": "turn-1", "status": "completed", "items": [], "error": None}}})
"""


class CodexRunTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.codex_home = self.root / "codex_home"
        self.codex_home.mkdir()
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.log_file = self.root / "fake_codex.jsonl"

        (self.codex_home / "deepseek.config.toml").write_text(
            'model_provider = "deepseek"\n', encoding="utf-8"
        )
        models_cache = {
            "models": [
                {
                    "slug": "gpt-6-alpha",
                    "visibility": "list",
                    "supported_reasoning_levels": [
                        {"effort": "medium"},
                        {"effort": "high"},
                        {"effort": "xhigh"},
                    ],
                },
                {
                    "slug": "gpt-5.6-beta",
                    "visibility": "list",
                    "supported_reasoning_levels": [{"effort": "medium"}],
                },
                {
                    "slug": "gpt-6-beta",
                    "visibility": "list",
                    "supported_reasoning_levels": [
                        {"effort": "low"},
                        {"effort": "medium"},
                        {"effort": "high"},
                    ],
                },
                {
                    "slug": "gpt-6-gamma",
                    "visibility": "list",
                    "supported_reasoning_levels": [
                        {"effort": "low"},
                        {"effort": "medium"},
                    ],
                },
            ]
        }
        (self.codex_home / "models_cache.json").write_text(
            json.dumps(models_cache), encoding="utf-8"
        )

        self.fake_codex = self.bin_dir / "codex"
        self.fake_codex.write_text(FAKE_APP_SERVER, encoding="utf-8")
        self.fake_codex.chmod(self.fake_codex.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        self.temp_dir.cleanup()

    def base_env(self, **extra):
        env = os.environ.copy()
        env.pop("CODEX_RUN_ACTIVE", None)
        env.pop("CODEX_BIN", None)
        env["PATH"] = "%s:%s" % (self.bin_dir, env.get("PATH", ""))
        env["CODEX_HOME"] = str(self.codex_home)
        env["CODEX_RUN_STATE_DIR"] = str(self.state_dir)
        env["FAKE_CODEX_LOG"] = str(self.log_file)
        env.update(extra)
        return env

    def read_logs(self):
        if not self.log_file.exists():
            return []
        return [
            json.loads(line)
            for line in self.log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_start_resolves_short_model_and_writes_outputs(self):
        tid_file = self.root / "out.tid"
        out_file = self.root / "out.txt"
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "-m",
                "beta",
                "-e",
                "medium",
                "-C",
                str(self.workspace),
                "--write",
                "--thread-id-file",
                str(tid_file),
                "-o",
                str(out_file),
                "Inspect the repo",
            ],
            env=self.base_env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "OK_RESULT")
        self.assertEqual(tid_file.read_text(encoding="utf-8").strip(), ROOT_TID)
        self.assertEqual(out_file.read_text(encoding="utf-8").strip(), "OK_RESULT")
        self.assertIn("CODEX_THREAD_ID=" + ROOT_TID, proc.stderr)
        self.assertIn('"model": "gpt-6-beta"', proc.stderr)
        self.assertIn('"sandbox": "workspace-write"', proc.stderr)

        logs = self.read_logs()
        argv = logs[0]["argv"]
        self.assertIn("app-server", argv)
        self.assertIn("--stdio", argv)
        self.assertIn("gpt-6-beta", argv)
        self.assertIn("workspace-write", argv)
        self.assertIn("tools.experimental_request_user_input.enabled=false", argv)
        self.assertEqual(logs[0]["active_marker"], "1")
        self.assertFalse(Path(logs[0]["zdotdir"]).exists())

    def test_resume_restores_saved_thread_metadata(self):
        tid_file = self.root / "session.tid"
        first = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "-m",
                "alpha",
                "-e",
                "high",
                "-C",
                str(self.workspace),
                "--write",
                "--thread-id-file",
                str(tid_file),
                "First turn",
            ],
            env=self.base_env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.log_file.unlink()

        resumed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--resume",
                str(tid_file),
                "Follow up turn",
            ],
            env=self.base_env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertIn('"model": "gpt-6-alpha"', resumed.stderr)
        self.assertIn('"effort": "high"', resumed.stderr)
        self.assertIn('"sandbox": "workspace-write"', resumed.stderr)
        logs = self.read_logs()
        methods = [entry["rpc_in"].get("method") for entry in logs if "rpc_in" in entry]
        self.assertIn("thread/resume", methods)

    def test_live_mid_turn_steer_injects_message(self):
        tid_file = self.root / "steer.tid"
        env = self.base_env(FAKE_CODEX_MODE="wait_steer")
        bg = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "-m",
                "gamma",
                "-e",
                "low",
                "--thread-id-file",
                str(tid_file),
                "Initial task",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if tid_file.is_file():
                    tid = tid_file.read_text(encoding="utf-8").strip()
                    if codex_run.active_meta_path(tid, env).is_file():
                        break
                time.sleep(0.05)

            steer_proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "--steer",
                    str(tid_file),
                    "Switch to BANANA",
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(steer_proc.returncode, 0, steer_proc.stderr)
            steer_resp = json.loads(steer_proc.stdout.strip())
            self.assertTrue(steer_resp.get("ok"))

            out, err = bg.communicate(timeout=5.0)
            self.assertEqual(bg.returncode, 0, err)
            self.assertEqual(out.strip(), "STEERED:Switch to BANANA")
        finally:
            if bg.poll() is None:
                bg.kill()

    def test_anti_early_exit_waits_for_subagent_and_auto_continues_once(self):
        out_file = self.root / "early_exit.out"
        env = self.base_env(FAKE_CODEX_MODE="subagent_early_exit")
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "-m",
                "gamma",
                "-e",
                "low",
                "-o",
                str(out_file),
                "Spawn subagent",
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        expected_notice = (
            "[codex-run notice: The delegated Codex agent ended early without wait_agent 1 time(s) "
            "(1 subagent(s) spawned total); codex-run waited for drain and auto-continued to completion. "
            "Report this to the user when summarizing the result.]"
        )
        out_text = out_file.read_text(encoding="utf-8")
        self.assertIn("FINAL_AFTER_SUBAGENT_DRAIN", proc.stdout)
        self.assertIn(expected_notice, proc.stdout)
        self.assertIn(expected_notice, proc.stderr)
        self.assertIn("FINAL_AFTER_SUBAGENT_DRAIN", out_text)
        self.assertIn(expected_notice, out_text)
        self.assertIn("Subagent started", proc.stderr)
        self.assertIn("Root turn ended early without wait_agent: 1 subagent(s) still active", proc.stderr)
        self.assertIn("Subagent finished", proc.stderr)
        self.assertIn("All subagents drained; triggering root auto-continue (1/3)...", proc.stderr)
        self.assertIn('"auto_continues": 1', proc.stderr)

    def test_auto_continue_hard_cap_prevents_infinite_loop(self):
        env = self.base_env(FAKE_CODEX_MODE="infinite_subagent_loop")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "-m", "gamma", "-e", "low", "Loop test"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Reached MAX_AUTO_CONTINUES=3", proc.stderr)

    def test_crash_mid_turn_is_reported_as_failure(self):
        env = self.base_env(FAKE_CODEX_MODE="crash_mid_turn")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "-m", "gamma", "-e", "low", "Crash test"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("exited unexpectedly before turn completion", proc.stderr)

    def test_concurrent_resume_same_thread_is_rejected_by_lock(self):
        env = self.base_env()
        codex_run.save_thread_meta(ROOT_TID, {"thread_id": ROOT_TID, "model": "gpt-6-alpha"}, env)
        srv = codex_run.SteerServer(ROOT_TID, lambda req: {"ok": True}, env)
        srv.start()
        try:
            proc = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--resume", ROOT_TID, "-m", "gamma", "Second caller"],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("already has an active codex-run turn running", proc.stderr)
            self.assertTrue(srv.sock_path.exists())
            self.assertEqual(self.read_logs(), [])
            self.assertEqual(codex_run.load_thread_meta(ROOT_TID, env).get("model"), "gpt-6-alpha")
        finally:
            srv.close()
        lock_file = codex_run.state_root(env) / "active" / ("%s.lock" % ROOT_TID)
        self.assertTrue(lock_file.exists())

    def test_server_request_receives_immediate_error_response(self):
        env = self.base_env(FAKE_CODEX_MODE="server_request")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "-m", "gamma", "-e", "low", "Ask user input"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "HANDLED_SERVER_REQUEST")

    def test_deepseek_profile_injects_guard_and_strict_config(self):
        env = self.base_env()
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--profile",
                "deepseek",
                "Analyze module",
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('"model": "deepseek-v4-pro"', proc.stderr)
        self.assertIn('"effort": "high"', proc.stderr)
        saved = codex_run.load_thread_meta(ROOT_TID, env)
        self.assertEqual(saved.get("model"), "deepseek-v4-pro")
        self.assertEqual(saved.get("effort"), "high")
        logs = self.read_logs()
        argv = logs[0]["argv"]
        self.assertIn("--profile", argv)
        self.assertIn("deepseek", argv)
        self.assertIn("--strict-config", argv)
        turn_starts = [
            e["rpc_in"]
            for e in logs
            if "rpc_in" in e and e["rpc_in"].get("method") == "turn/start"
        ]
        prompt_sent = turn_starts[0]["params"]["input"][0]["text"]
        self.assertIn("Provider-specific guard: do not call spawn_agent", prompt_sent)

    def test_stop_process_group_reaps_grandchild_after_leader_reaped(self):
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess, sys; c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); print(c.pid, flush=True)",
            ],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        grandchild_pid = int(proc.stdout.readline().strip())
        proc.stdout.close()
        proc.wait(timeout=5.0)
        self.assertIsNotNone(proc.poll())
        codex_run.stop_process_group(proc, grace_seconds=0.5)
        with self.assertRaises(ProcessLookupError):
            os.kill(grandchild_pid, 0)

    def test_nested_invocation_and_invalid_inputs_are_rejected(self):
        nested = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "Task"],
            env=self.base_env(CODEX_RUN_ACTIVE="1"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(nested.returncode, 1)
        self.assertIn("Nested codex-run invocation is not allowed", nested.stderr)

        missing_profile = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--profile", "nonexistent", "Task"],
            env=self.base_env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(missing_profile.returncode, 1)
        self.assertIn("install or create the profile config at", missing_profile.stderr)
        self.assertNotIn("harness", missing_profile.stderr.lower())

        bad_effort = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "-m", "gamma", "-e", "xhigh", "Task"],
            env=self.base_env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(bad_effort.returncode, 1)
        self.assertIn("does not support effort 'xhigh'", bad_effort.stderr)

        both_flags = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--write", "--yolo", "Task"],
            env=self.base_env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(both_flags.returncode, 2)
        self.assertIn("Cannot combine --sandbox, --write, and --yolo.", both_flags.stderr)

        sandbox_and_yolo = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--sandbox", "read-only", "--yolo", "Task"],
            env=self.base_env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(sandbox_and_yolo.returncode, 2)
        self.assertIn("Cannot combine --sandbox, --write, and --yolo.", sandbox_and_yolo.stderr)

    def test_yolo_flag_maps_to_danger_full_access_and_persists_on_resume(self):
        tid_file = self.root / "yolo.sid"
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "-m",
                "beta",
                "-e",
                "medium",
                "-C",
                str(self.workspace),
                "--yolo",
                "--session-id-file",
                str(tid_file),
                "Cross-directory task",
            ],
            env=self.base_env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('"sandbox": "danger-full-access"', proc.stderr)
        logs = self.read_logs()
        self.assertIn("danger-full-access", logs[0]["argv"])
        start_rpc = next(
            e["rpc_in"]
            for e in logs
            if "rpc_in" in e and e["rpc_in"].get("method") == "thread/start"
        )
        self.assertEqual(start_rpc["params"]["sandbox"], "danger-full-access")

        self.log_file.unlink()
        resumed = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--resume", str(tid_file), "Continue yolo"],
            env=self.base_env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertIn('"sandbox": "danger-full-access"', resumed.stderr)
        resume_logs = self.read_logs()
        self.assertIn("danger-full-access", resume_logs[0]["argv"])
        resume_rpc = next(
            e["rpc_in"]
            for e in resume_logs
            if "rpc_in" in e and e["rpc_in"].get("method") == "thread/resume"
        )
        self.assertEqual(resume_rpc["params"]["sandbox"], "danger-full-access")

        self.log_file.unlink()
        downgraded = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--resume",
                str(tid_file),
                "--write",
                "Switch to workspace-write",
            ],
            env=self.base_env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(downgraded.returncode, 0, downgraded.stderr)
        self.assertIn('"sandbox": "workspace-write"', downgraded.stderr)

    def test_turn_failure_redacts_secrets(self):
        env = self.base_env(
            FAKE_CODEX_MODE="turn_failed",
            DEEPSEEK_API_KEY="super-secret-deepseek-key",
            FAKE_REPLY_TEXT="leaked super-secret-deepseek-key",
        )
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "-m", "gamma", "Task"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Bearer [REDACTED]", proc.stderr)
        self.assertNotIn("secret-token-value", proc.stderr)

    def test_timeout_reaps_process_group(self):
        child_pid_file = self.root / "child.pid"
        env = self.base_env(
            FAKE_CODEX_MODE="hold_timeout",
            FAKE_CHILD_PID_FILE=str(child_pid_file),
        )
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--timeout", "0.3", "-m", "gamma", "Slow task"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 124)
        self.assertIn("Timed out after", proc.stderr)
        if child_pid_file.exists():
            child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_sigterm_cancels_and_reaps_process_group(self):
        child_pid_file = self.root / "sigterm_child.pid"
        tid_file = self.root / "sigterm.tid"
        env = self.base_env(
            FAKE_CODEX_MODE="hold_timeout",
            FAKE_CHILD_PID_FILE=str(child_pid_file),
        )
        proc = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--timeout",
                "30",
                "-m",
                "gamma",
                "--session-id-file",
                str(tid_file),
                "Blocking task",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        server_pid = None
        child_pid = None
        try:
            meta_file = None
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                if child_pid_file.is_file() and tid_file.is_file():
                    child_raw = child_pid_file.read_text(encoding="utf-8").strip()
                    tid = tid_file.read_text(encoding="utf-8").strip()
                    if child_raw.isdigit() and tid:
                        candidate_meta = codex_run.active_meta_path(tid, env)
                        if candidate_meta.is_file():
                            child_pid = int(child_raw)
                            meta_file = candidate_meta
                            break
                time.sleep(0.05)

            self.assertIsNone(proc.poll(), "codex_run.py exited prematurely before SIGTERM")
            self.assertIsNotNone(child_pid, "child PID file did not appear before timeout")
            self.assertIsNotNone(meta_file, "active steer metadata did not appear before timeout")

            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            sock_path = Path(meta["socket_path"])
            self.assertTrue(sock_path.exists(), "active steer socket should exist before SIGTERM")

            logs = self.read_logs()
            self.assertTrue(logs, "expected fake app-server startup log")
            server_pid = logs[0]["pid"]
            zdotdir_path = Path(logs[0]["zdotdir"])

            proc.send_signal(signal.SIGTERM)
            _, err = proc.communicate(timeout=10.0)

            self.assertEqual(proc.returncode, 128 + signal.SIGTERM, err)
            self.assertIn("cancelled by signal %d" % signal.SIGTERM, err)

            for label, pid in (("app-server", server_pid), ("grandchild", child_pid)):
                with self.assertRaises(ProcessLookupError, msg="%s (pid=%d) still alive" % (label, pid)):
                    os.kill(pid, 0)

            self.assertFalse(sock_path.exists(), "steer socket file was not cleaned up")
            self.assertFalse(meta_file.exists(), "active meta file was not cleaned up")
            self.assertFalse(zdotdir_path.exists(), "temporary ZDOTDIR was not cleaned up")
            active_dir = codex_run.state_root(env) / "active"
            self.assertEqual(list(active_dir.glob("*.sock")), [])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5.0)
            for pid in (server_pid, child_pid):
                if pid is not None:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_list_models_default_cache_formats_efforts_and_hides_long_fields(self):
        models_cache = {
            "models": [
                {
                    "slug": "gpt-6-alpha",
                    "visibility": "list",
                    "supported_reasoning_levels": [
                        {"effort": "medium"},
                        {"effort": "high"},
                        {"effort": "xhigh"},
                    ],
                    "model_messages": {
                        "instructions_template": "VERY_LONG_SECRET_TEMPLATE_SHOULD_NOT_APPEAR"
                    },
                    "base_instructions": "VERY_LONG_BASE_INSTRUCTIONS_SHOULD_NOT_APPEAR",
                },
                {
                    "slug": "gpt-6-hidden",
                    "visibility": "hide",
                    "supported_reasoning_levels": [{"effort": "low"}],
                },
                {
                    "slug": "gpt-6-plain",
                    "visibility": "list",
                },
            ]
        }
        (self.codex_home / "models_cache.json").write_text(
            json.dumps(models_cache), encoding="utf-8"
        )

        # Also verifies --list-models bypasses CODEX_RUN_ACTIVE recursion guard and stdin.
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--list-models"],
            env=self.base_env(CODEX_RUN_ACTIVE="1"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.strip().splitlines()
        self.assertEqual(
            lines,
            [
                "gpt-6-alpha\tefforts: medium, high, xhigh",
                "gpt-6-plain",
            ],
        )
        self.assertNotIn("gpt-6-hidden", proc.stdout)
        self.assertNotIn("VERY_LONG_SECRET_TEMPLATE", proc.stdout)
        self.assertNotIn("VERY_LONG_BASE_INSTRUCTIONS", proc.stdout)

        # Verify HOME fallback when CODEX_HOME is unset.
        fake_home = self.root / "fake_home"
        (fake_home / ".codex").mkdir(parents=True)
        (fake_home / ".codex" / "models_cache.json").write_text(
            json.dumps({"models": [{"slug": "gpt-6-home", "visibility": "list"}]}),
            encoding="utf-8",
        )
        home_env = self.base_env(HOME=str(fake_home))
        home_env.pop("CODEX_HOME", None)
        home_proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--list-models"],
            env=home_env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(home_proc.returncode, 0, home_proc.stderr)
        self.assertEqual(home_proc.stdout.strip(), "gpt-6-home")

    def test_list_models_default_cache_missing_corrupt_or_empty_errors(self):
        cache_path = self.codex_home / "models_cache.json"
        cache_path.unlink()
        missing = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--list-models"],
            env=self.base_env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(missing.returncode, 1)
        self.assertIn("Run 'codex' once to refresh the local model cache", missing.stderr)
        self.assertIn("omit -m", missing.stderr)

        cache_path.write_text("{not-json", encoding="utf-8")
        corrupt = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--list-models"],
            env=self.base_env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(corrupt.returncode, 1)
        self.assertIn("No visible models found", corrupt.stderr)

        cache_path.write_text(
            json.dumps({"models": [{"slug": "gpt-6-hidden", "visibility": "hide"}]}),
            encoding="utf-8",
        )
        empty = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--list-models"],
            env=self.base_env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(empty.returncode, 1)
        self.assertIn("No visible models found", empty.stderr)

    def test_list_models_profile_catalog_default_relative_and_no_gpt_fallback(self):
        fake_home = self.root / "profile_home"
        home_codex = fake_home / ".codex"
        home_codex.mkdir(parents=True)
        catalog_data = {
            "models": [
                {
                    "slug": "deepseek-v4-flash",
                    "visibility": "list",
                    "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}],
                    "model_messages": {"instructions_template": "HUGE_TEMPLATE_DO_NOT_PRINT"},
                },
                {
                    "slug": "deepseek-v4-pro",
                    "supported_reasoning_levels": [
                        {"effort": "low"},
                        {"effort": "high"},
                        {"effort": "max"},
                    ],
                },
                {
                    "slug": "deepseek-hidden",
                    "visibility": "hide",
                    "supported_reasoning_levels": [{"effort": "low"}],
                },
            ]
        }
        (home_codex / "deepseek-models.json").write_text(
            json.dumps(catalog_data), encoding="utf-8"
        )
        (self.codex_home / "deepseek.config.toml").write_text(
            'model = "deepseek-v4-pro"\n'
            'model_provider = "deepseek"\n'
            'model_reasoning_effort = "high"\n'
            'model_catalog_json = "~/.codex/deepseek-models.json"\n\n'
            "[model_providers.deepseek]\n"
            'name = "DeepSeek"\n',
            encoding="utf-8",
        )

        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--list-models", "--profile", "deepseek"],
            env=self.base_env(HOME=str(fake_home)),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.strip().splitlines(),
            [
                "deepseek-v4-flash\tefforts: low, high",
                "deepseek-v4-pro\tefforts: low, high, max\t[default]",
            ],
        )
        self.assertNotIn("deepseek-hidden", proc.stdout)
        self.assertNotIn("HUGE_TEMPLATE_DO_NOT_PRINT", proc.stdout)

        # Relative model_catalog_json resolved against profile_path.parent
        rel_dir = self.codex_home / "catalogs"
        rel_dir.mkdir()
        (rel_dir / "custom.json").write_text(
            json.dumps({"models": [{"slug": "custom-1", "visibility": "list"}]}),
            encoding="utf-8",
        )
        (self.codex_home / "relprof.config.toml").write_text(
            'model = "custom-1"\nmodel_catalog_json = "catalogs/custom.json"\n',
            encoding="utf-8",
        )
        rel_proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--list-models", "--profile", "relprof"],
            env=self.base_env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(rel_proc.returncode, 0, rel_proc.stderr)
        self.assertEqual(rel_proc.stdout.strip(), "custom-1\t[default]")

        # Top-level model only (no model_catalog_json)
        (self.codex_home / "simple.config.toml").write_text(
            'model = "vendor-model-x"\nmodel_reasoning_effort = "medium"\n',
            encoding="utf-8",
        )
        simple_proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--list-models", "--profile", "simple"],
            env=self.base_env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(simple_proc.returncode, 0, simple_proc.stderr)
        self.assertEqual(simple_proc.stdout.strip(), "vendor-model-x\teffort: medium\t[default]")

        # Neither model_catalog_json nor top-level model -> error
        (self.codex_home / "emptyprof.config.toml").write_text(
            'model_provider = "other"\n', encoding="utf-8"
        )
        empty_prof = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--list-models", "--profile", "emptyprof"],
            env=self.base_env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(empty_prof.returncode, 1)
        self.assertIn("defines neither 'model_catalog_json' nor a top-level 'model'", empty_prof.stderr)

        # Corrupt catalog -> error, MUST NOT fall back to GPT models_cache.json
        (self.codex_home / "broken.json").write_text("{bad json", encoding="utf-8")
        (self.codex_home / "brokenprof.config.toml").write_text(
            'model = "broken-1"\nmodel_catalog_json = "broken.json"\n',
            encoding="utf-8",
        )
        broken_proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--list-models", "--profile", "brokenprof"],
            env=self.base_env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(broken_proc.returncode, 1)
        self.assertIn("Failed to load model_catalog_json", broken_proc.stderr)
        self.assertEqual(broken_proc.stdout.strip(), "")
        self.assertNotIn("gpt-6-alpha", broken_proc.stdout + broken_proc.stderr)

    def test_profile_toml_fallback_parser_without_tomllib(self):
        sample_toml = (
            "# top-level comment\n"
            'model = "deepseek-v4-pro"  # inline comment\n'
            "model_reasoning_effort = 'high'\n"
            'model_catalog_json = "~/.codex/deepseek-models.json"\n'
            "\n"
            "[model_providers.deepseek]\n"
            'model = "must-ignore-nested-table-key"\n'
            'model_catalog_json = "must-ignore.json"\n'
        )
        parsed = codex_run._parse_top_level_toml_strings_fallback(sample_toml)
        self.assertEqual(
            parsed,
            {
                "model": "deepseek-v4-pro",
                "model_reasoning_effort": "high",
                "model_catalog_json": "~/.codex/deepseek-models.json",
            },
        )

        profile_file = self.codex_home / "fallback.config.toml"
        profile_file.write_text(sample_toml, encoding="utf-8")
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            via_loader = codex_run.parse_profile_top_level_config(profile_file)
        self.assertEqual(via_loader, parsed)

    def test_profile_toml_fallback_skips_multiline_triple_quoted_strings(self):
        multiline_toml = (
            'developer_instructions = """\n'
            "[not_a_table_header]\n"
            'model = "wrong-from-multiline-double"\n'
            'model_catalog_json = "example.json"\n'
            'escaped_quotes = \\"\\"\\" still inside\n'
            '"""\n'
            "literal_notes = '''\n"
            "[another_fake_table]\n"
            'model_reasoning_effort = "low"\n'
            "'''\n"
            'inline_triple = """single line triple"""\n'
            'model = "deepseek-v4-pro"\n'
            'model_reasoning_effort = "high"\n'
            'model_catalog_json = "~/.codex/deepseek-models.json"\n'
            "\n"
            "[model_providers.deepseek]\n"
            'model = "must-ignore-nested"\n'
        )
        profile_file = self.codex_home / "multiline.config.toml"
        profile_file.write_text(multiline_toml, encoding="utf-8")

        expected = {
            "model": "deepseek-v4-pro",
            "model_reasoning_effort": "high",
            "model_catalog_json": "~/.codex/deepseek-models.json",
        }
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            via_fallback = codex_run.parse_profile_top_level_config(profile_file)
        self.assertEqual(via_fallback, expected)

        try:
            import tomllib  # noqa: F401
        except ImportError:
            tomllib = None
        if tomllib is not None:
            via_tomllib = codex_run.parse_profile_top_level_config(profile_file)
            self.assertEqual(via_tomllib, via_fallback)


if __name__ == "__main__":
    unittest.main()
