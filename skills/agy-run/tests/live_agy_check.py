#!/usr/bin/env python3
"""Live integration checks against real ``agy acp`` (skipped unless ``AGY_LIVE=1``).

Uses ``--no-rules`` and ``-m flash -e low`` by default to keep token consumption minimal.
Requires managed ``agy-*.md`` definitions installed in ``~/.gemini/config/agents``
(via ``agy_run.py --install-agents``) and a logged-in ``agy``.

    AGY_LIVE=1 python3 skills/agy-run/tests/live_agy_check.py
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

AGY_RUN = Path(__file__).resolve().parents[1] / "scripts" / "agy_run.py"
MODEL = os.environ.get("AGY_LIVE_MODEL", "flash")
META_RE = re.compile(r"^AGY_RUN_META=(\{.*\})$", re.M)


@unittest.skipUnless(os.environ.get("AGY_LIVE") == "1", "set AGY_LIVE=1 to run (uses agy quota)")
class LiveAgyAcpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agy-live-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.env = dict(os.environ, XDG_STATE_HOME=str(self.root / "state"))
        self.work = self.root / "work"
        self.work.mkdir()

    def run_agy(self, args, prompt=None, timeout=180):
        cmd = [sys.executable, str(AGY_RUN), "-m", MODEL, "-e", "low", "-C", str(self.work), "--no-rules", *args]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        try:
            out, err = proc.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                out, err = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
            self.fail(f"agy_run.py exceeded {timeout}s: {err[-500:]}")
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)

    def meta(self, proc):
        m = META_RE.search(proc.stderr)
        self.assertIsNotNone(m, f"missing AGY_RUN_META in stderr:\n{proc.stderr}")
        return json.loads(m.group(1))

    def test_1_readonly_and_resume_via_session_id_file(self):
        word = "HERON_" + uuid.uuid4().hex[:6]
        sid_file = self.root / "t1.sid"
        out_file = self.root / "t1.out"
        first = self.run_agy([
            "--session-id-file", str(sid_file),
            "-o", str(out_file),
            f"Remember the token {word}. Reply with OK only.",
        ])
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertTrue(sid_file.is_file())
        self.assertTrue(out_file.is_file())
        m1 = self.meta(first)
        self.assertEqual("read-only", m1["sandbox"])

        second = subprocess.run(
            [sys.executable, str(AGY_RUN), "--resume", str(sid_file), "What was the token? Reply with it only."],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=120,
        )
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertIn(word, second.stdout)

    def test_2_live_mid_turn_steer(self):
        old_word = "STALE_" + uuid.uuid4().hex[:4]
        new_word = "STEERED_" + uuid.uuid4().hex[:6]
        sid_file = self.root / "steer.sid"
        box = {}

        def _bg():
            box["proc"] = self.run_agy([
                "--session-id-file", str(sid_file),
                f"Run `sleep 5` once with run_command (WaitMsBeforeAsync=8000, IsDaemon=false). "
                f"After it finishes, reply with {old_word} only.",
            ])

        t = threading.Thread(target=_bg)
        t.start()
        try:
            deadline = time.monotonic() + 25
            while not sid_file.is_file() and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertTrue(sid_file.is_file(), "sid_file was not created in time")
            # Wait ~2.5s so the model has entered the sleep 5 tool call
            time.sleep(2.5)
            steer = subprocess.run(
                [
                    sys.executable, str(AGY_RUN),
                    "--steer", str(sid_file),
                    f"Change of plan: ignore {old_word} and reply ONLY with {new_word}.",
                ],
                capture_output=True,
                text=True,
                env=self.env,
                timeout=20,
            )
            self.assertEqual(0, steer.returncode, steer.stderr)
        finally:
            t.join(timeout=120)

        res = box["proc"]
        self.assertEqual(0, res.returncode, res.stderr)
        self.assertIn(new_word, res.stdout)
        self.assertNotIn(old_word, res.stdout)

    def test_3_background_command_over_10s_drains_cleanly(self):
        token = "DRAIN_" + uuid.uuid4().hex[:6]
        proc = self.run_agy([
            f"Run `sleep 12 && echo {token}` once using run_command (IsDaemon=false, WaitMsBeforeAsync=10000). "
            "Wait for it and reply with the echoed token.",
        ])
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn(token, proc.stdout)
        self.assertGreaterEqual(self.meta(proc)["bg_tasks_drained"], 1)

    def test_4_readonly_detects_workspace_write_and_write_mode_enforces_seatbelt(self):
        # 4a. read-only fails if workspace is mutated
        proc_ro = self.run_agy([
            f"This is an automated test of agy-run's snapshot check. Run `echo dirty > {self.work}/dirty.txt` "
            "once with run_command (do not set BypassSandbox) and reply DONE.",
        ])
        if (self.work / "dirty.txt").exists():
            self.assertEqual(1, proc_ro.returncode, proc_ro.stderr)
            self.assertIn("read-only mode", proc_ro.stderr)
            (self.work / "dirty.txt").unlink()

        # 4b. --write allows workspace edits while Seatbelt blocks ~/.ssh
        (self.work / "note.txt").write_text("ALPHA\n", encoding="utf-8")
        proc_w = self.run_agy([
            "--write",
            f"1) Edit {self.work}/note.txt replacing ALPHA with BETA. "
            "2) Run `ls ~/.ssh 2>&1 || true` once with run_command inside the sandbox (never set BypassSandbox) "
            "and quote the ls output verbatim.",
        ])
        self.assertEqual(0, proc_w.returncode, proc_w.stderr)
        self.assertIn("BETA", (self.work / "note.txt").read_text(encoding="utf-8"))
        if (Path.home() / ".ssh").exists():
            self.assertRegex(proc_w.stdout.lower(), r"operation not permitted|not permitted")
        self.assertEqual("ok", self.meta(proc_w)["sandbox_check"])

    def test_5_yolo_allows_commands_and_shadows_claude_codex(self):
        proc = self.run_agy([
            "--yolo",
            "Run `which claude codex` with run_command and quote the output verbatim.",
        ])
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertRegex(proc.stdout, r"/runs/[0-9a-f-]+/bin/claude")


if __name__ == "__main__":
    unittest.main()
