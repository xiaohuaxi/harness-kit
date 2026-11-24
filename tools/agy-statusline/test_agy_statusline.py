#!/usr/bin/env python3
"""Unit tests for agy-statusline."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent / "agy-statusline"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def run_statusline(
    payload: str | dict | None,
    *,
    home_dir: Path,
    state_dir: Path | None = None,
    extra_env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home_dir),
    }
    if state_dir is not None:
        env["AGY_STATUSLINE_STATE_DIR"] = str(state_dir)
    if extra_env:
        env.update(extra_env)

    if payload is None:
        stdin_text = ""
    elif isinstance(payload, dict):
        stdin_text = json.dumps(payload)
    else:
        stdin_text = payload

    return subprocess.run(
        ["bash", str(SCRIPT_PATH)],
        input=stdin_text,
        text=True,
        capture_output=True,
        env=env,
        cwd=str(cwd or home_dir),
        check=False,
    )


class AgyStatuslineTests(unittest.TestCase):
    def test_full_seven_segments_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            home_dir = root / "home"
            state_dir = root / "state"
            repo_dir = home_dir / "work" / "demo"
            repo_dir.mkdir(parents=True)

            has_git = shutil.which("git") is not None
            if has_git:
                subprocess.run(
                    ["git", "init", "-b", "feat/statusline"],
                    cwd=str(repo_dir),
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "config", "user.name", "Test User"],
                    cwd=str(repo_dir),
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "config", "user.email", "test@example.invalid"],
                    cwd=str(repo_dir),
                    check=True,
                    capture_output=True,
                )
                (repo_dir / "README").write_text("hi\n", encoding="utf-8")
                subprocess.run(["git", "add", "README"], cwd=str(repo_dir), check=True, capture_output=True)
                subprocess.run(
                    ["git", "commit", "-m", "init"],
                    cwd=str(repo_dir),
                    check=True,
                    capture_output=True,
                )

            transcript = root / "transcript.jsonl"
            transcript.write_text(
                '{"source":"USER","type":"MESSAGE"}\n'
                '{"source":"SYSTEM","type":"CHECKPOINT"}\n'
                '{"source":"SYSTEM","type":"CHECKPOINT"}\n',
                encoding="utf-8",
            )

            now_utc = dt.datetime.now(dt.timezone.utc)
            g_5h_reset = (now_utc + dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
            payload = {
                "cwd": str(repo_dir),
                "model": {"display_name": "Claude Sonnet 4.5 (High)"},
                "context_window": {
                    "current_usage": {
                        "input_tokens": 12000,
                        "cache_creation_input_tokens": 8000,
                        "cache_read_input_tokens": 22000,
                    },
                    "context_window_size": 200000,
                    "used_percentage": 21.2,
                },
                "transcript_path": str(transcript),
                "quota": {
                    "gemini-weekly": {"used_percentage": 12.4, "reset_in_seconds": 86400 * 5.5},
                    "gemini-5h": {"remaining_fraction": 0.92, "reset_time": g_5h_reset},
                    "3p-weekly": {"remaining_fraction": 0.75, "reset_in_seconds": 86400 * 3.0},
                    "3p-5h": {"remaining_fraction": 0.5, "reset_in_seconds": 3600},
                },
                "cost": {"total_cost_usd": 0.425},
            }

            proc = run_statusline(payload, home_dir=home_dir, state_dir=state_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr, "")
            # Ensure 256-color ANSI codes are emitted.
            self.assertIn("\x1b[38;5;73m", proc.stdout)
            self.assertIn("\x1b[38;5;108m", proc.stdout)
            self.assertIn("\x1b[38;5;137m", proc.stdout)

            plain = strip_ansi(proc.stdout)
            parts = plain.split(" · ")
            # Segments: cwd, model, effort, ctx, G:, C:, [git branch], cost
            self.assertEqual(parts[0], "~/work/demo")
            self.assertEqual(parts[1], "Sonnet 4.5")
            self.assertEqual(parts[2], "high")
            self.assertEqual(parts[3], "42k/200k 21% ⛁ 2")
            self.assertIn("G: 7d 12% ↻ 5.5d 5h 8% ↻ ", parts[4])
            # 3P segment renders 7d only by design (no 5h item in C: segment).
            self.assertEqual(parts[5], "C: 7d 25% ↻ 3.0d")
            if has_git:
                self.assertEqual(parts[6], "feat/statusline")
                self.assertEqual(parts[7], "$0.42")
            else:
                self.assertEqual(parts[6], "$0.42")

            # Quota cache written, opt-in log NOT created by default.
            self.assertTrue((state_dir / "quota-cache.json").is_file())
            self.assertFalse((state_dir / "quota-log.jsonl").exists())

    def test_quota_cache_fallback_and_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            home_dir = root / "home"
            state_dir = root / "state"
            home_dir.mkdir()

            fixed_5h_rt = "2099-01-02T12:30:00Z"
            payload_with_quota = {
                "cwd": str(home_dir),
                "model": {"display_name": "Gemini 3.8 Flash (Medium)"},
                "quota": {
                    "gemini-weekly": {"used_percentage": 30, "reset_in_seconds": 86400 * 2},
                    "gemini-5h": {"used_percentage": 5, "reset_time": fixed_5h_rt},
                    "3p-weekly": {"used_percentage": 45, "reset_in_seconds": 86400 * 4},
                },
            }
            p1 = run_statusline(payload_with_quota, home_dir=home_dir, state_dir=state_dir)
            self.assertEqual(p1.returncode, 0, p1.stderr)
            cache_file = state_dir / "quota-cache.json"
            self.assertTrue(cache_file.is_file())
            # Atomic write must not leave stray temp files behind.
            leftovers = [p.name for p in state_dir.iterdir() if ".tmp" in p.name]
            self.assertEqual(leftovers, [])

            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            # Relative reset_in_seconds must be persisted as absolute UTC reset_time to prevent drift.
            self.assertNotIn("reset_in_seconds", cached["gemini-weekly"])
            self.assertRegex(cached["gemini-weekly"]["reset_time"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            self.assertNotIn("reset_in_seconds", cached["3p-weekly"])
            self.assertRegex(cached["3p-weekly"]["reset_time"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            # Existing absolute reset_time must stay untouched.
            self.assertEqual(cached["gemini-5h"]["reset_time"], fixed_5h_rt)

            # Subsequent tick without quota falls back to quota-cache.json.
            payload_no_quota = {
                "cwd": str(home_dir),
                "model": {"display_name": "Gemini 3.8 Flash (Medium)"},
            }
            p2 = run_statusline(payload_no_quota, home_dir=home_dir, state_dir=state_dir)
            self.assertEqual(p2.returncode, 0, p2.stderr)
            plain2 = strip_ansi(p2.stdout)
            self.assertIn("G: 7d 30% ↻ 2.0d", plain2)
            self.assertIn("C: 7d 45% ↻ 4.0d", plain2)

            # Simulate 24h elapsed by moving cached absolute reset_time 1 day earlier: countdown must decrement.
            g_dt = dt.datetime.strptime(cached["gemini-weekly"]["reset_time"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=dt.timezone.utc
            )
            cached["gemini-weekly"]["reset_time"] = (g_dt - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            cache_file.write_text(json.dumps(cached), encoding="utf-8")
            p2b = run_statusline(payload_no_quota, home_dir=home_dir, state_dir=state_dir)
            self.assertEqual(p2b.returncode, 0, p2b.stderr)
            plain2b = strip_ansi(p2b.stdout)
            self.assertIn("G: 7d 30% ↻ 1.0d", plain2b)
            self.assertIn("C: 7d 45% ↻ 4.0d", plain2b)

            # Corrupt cache file + missing quota degrades gracefully without error.
            cache_file.write_text("{corrupt", encoding="utf-8")
            p3 = run_statusline(payload_no_quota, home_dir=home_dir, state_dir=state_dir)
            self.assertEqual(p3.returncode, 0, p3.stderr)
            self.assertEqual(p3.stderr, "")
            self.assertNotIn("G:", strip_ansi(p3.stdout))

    def test_quota_log_opt_in_modes_and_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            home_dir = root / "home"
            state_dir = root / "state"
            home_dir.mkdir()

            payload_a = {
                "cwd": str(home_dir),
                "quota": {
                    "gemini-weekly": {"used_percentage": 10},
                    "gemini-5h": {"used_percentage": 5, "reset_time": "2099-01-02T12:30:00Z"},
                    "3p-weekly": {"used_percentage": 20},
                    "3p-5h": {"used_percentage": 15, "reset_time": "2099-01-02T13:00:00Z"},
                },
            }
            payload_b = {
                "cwd": str(home_dir),
                "quota": {
                    "gemini-weekly": {"used_percentage": 11},
                    "gemini-5h": {"used_percentage": 5, "reset_time": "2099-01-02T12:30:00Z"},
                    "3p-weekly": {"used_percentage": 20},
                    "3p-5h": {"used_percentage": 15, "reset_time": "2099-01-02T13:00:00Z"},
                },
            }

            default_log = state_dir / "quota-log.jsonl"
            for disabled_val in (None, "", "0", "false", "FALSE"):
                env = {} if disabled_val is None else {"AGY_STATUSLINE_QUOTA_LOG": disabled_val}
                proc = run_statusline(payload_a, home_dir=home_dir, state_dir=state_dir, extra_env=env)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertFalse(default_log.exists(), f"Log should stay disabled for {disabled_val!r}")

            # Enable via "1": writes first line, deduplicates identical second call, appends on change.
            p1 = run_statusline(payload_a, home_dir=home_dir, state_dir=state_dir, extra_env={"AGY_STATUSLINE_QUOTA_LOG": "1"})
            p2 = run_statusline(payload_a, home_dir=home_dir, state_dir=state_dir, extra_env={"AGY_STATUSLINE_QUOTA_LOG": "TRUE"})
            p3 = run_statusline(payload_b, home_dir=home_dir, state_dir=state_dir, extra_env={"AGY_STATUSLINE_QUOTA_LOG": "true"})
            self.assertEqual((p1.returncode, p2.returncode, p3.returncode), (0, 0, 0))

            lines = default_log.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            rec1, rec2 = json.loads(lines[0]), json.loads(lines[1])
            self.assertEqual(rec1["g_7d"], 10)
            self.assertEqual(rec1["c_5h"], 15)
            self.assertIn("ts", rec1)
            self.assertEqual(rec2["g_7d"], 11)

            # Custom path with leading ~/ creates parent dirs automatically.
            custom_rel = "~/logs/nested/custom-quota.jsonl"
            p4 = run_statusline(
                payload_b,
                home_dir=home_dir,
                state_dir=state_dir,
                extra_env={"AGY_STATUSLINE_QUOTA_LOG": custom_rel},
            )
            self.assertEqual(p4.returncode, 0, p4.stderr)
            custom_file = home_dir / "logs" / "nested" / "custom-quota.jsonl"
            self.assertTrue(custom_file.is_file())
            self.assertEqual(len(custom_file.read_text(encoding="utf-8").strip().splitlines()), 1)

    def test_path_aliases_longest_prefix_and_directory_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            home_dir = root / "home"
            state_dir = root / "state"
            home_dir.mkdir()

            aliases = f"~/work/app=~/APP:~/work/app/sub=~/SUB:{root}/external/data=DATA"
            cases = [
                (str(home_dir / "work" / "app"), "~/APP"),
                (str(home_dir / "work" / "app" / "src" / "pkg"), "~/APP/src/pkg"),
                (str(home_dir / "work" / "app" / "sub"), "~/SUB"),
                (str(home_dir / "work" / "app" / "sub" / "deep"), "~/SUB/deep"),
                # Boundary guard: must NOT match ~/work/app or $HOME
                (str(home_dir / "work" / "app2"), "~/work/app2"),
                (str(home_dir / "work" / "app-other" / "x"), "~/work/app-other/x"),
                (str(root / "external" / "data" / "v1"), "DATA/v1"),
                (str(root / "external" / "data2"), str(root / "external" / "data2")),
                (str(root / "home2" / "work"), str(root / "home2" / "work")),
                (str(home_dir), "~"),
                (str(home_dir / "misc"), "~/misc"),
            ]
            for target_cwd, expected_disp in cases:
                proc = run_statusline(
                    {"cwd": target_cwd},
                    home_dir=home_dir,
                    state_dir=state_dir,
                    extra_env={"AGY_STATUSLINE_PATH_ALIASES": aliases},
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(strip_ansi(proc.stdout), expected_disp, f"cwd={target_cwd}")

    def test_transcript_checkpoint_fallback_via_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            home_dir = root / "home"
            state_dir = root / "custom-state"
            home_dir.mkdir()

            cid = "conv-12345"
            fallback_tp = state_dir / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
            fallback_tp.parent.mkdir(parents=True)
            fallback_tp.write_text(
                '{"source":"SYSTEM","type":"CHECKPOINT"}\n'
                '{"source":"USER","type":"TEXT"}\n'
                '{"source":"SYSTEM","type":"CHECKPOINT"}\n'
                '{"source":"SYSTEM","type":"CHECKPOINT"}\n',
                encoding="utf-8",
            )

            payload = {
                "cwd": str(home_dir),
                "conversation_id": cid,
                # Non-existent path simulating missing -cli segment in agy payload.
                "transcript_path": str(root / "nonexistent" / "transcript.jsonl"),
                "context_window": {
                    "total_input_tokens": 1_500_000,
                    "context_window_size": 2_000_000,
                },
            }
            proc = run_statusline(payload, home_dir=home_dir, state_dir=state_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            plain = strip_ansi(proc.stdout)
            self.assertEqual(plain, "~ · 1M/2M ⛁ 3")

    def test_empty_and_invalid_json_graceful_degradation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            home_dir = root / "home"
            state_dir = root / "state"
            home_dir.mkdir()

            for bad_input in (None, "", "{not-valid-json", "null", "[1, 2, 3]"):
                proc = run_statusline(bad_input, home_dir=home_dir, state_dir=state_dir, cwd=home_dir)
                self.assertEqual(proc.returncode, 0, f"input={bad_input!r}, stderr={proc.stderr!r}")
                self.assertEqual(proc.stderr, "")
                self.assertEqual(strip_ansi(proc.stdout), "~")

    def test_unexpected_field_types_graceful_degradation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            home_dir = root / "home"
            state_dir = root / "state"
            home_dir.mkdir()

            weird_payload = {
                "cwd": 123,
                "workspace": {"current_dir": ["not", "a", "str"]},
                "model": {"display_name": 456, "id": {"nested": True}, "effort": 789},
                "effort": {"level": 999},
                "transcript_path": 321,
                "conversation_id": 654,
                "context_window": {
                    "current_usage": {
                        "input_tokens": "bad",
                        "cache_creation_input_tokens": {"x": 1},
                        "cache_read_input_tokens": True,
                    },
                    "total_input_tokens": "nope",
                    "context_window_size": "200k",
                    "used_percentage": "NaN%",
                },
                "cost": {"total_cost_usd": "free"},
            }
            proc = run_statusline(weird_payload, home_dir=home_dir, state_dir=state_dir, cwd=home_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr, "")
            self.assertEqual(strip_ansi(proc.stdout), "~")

    def test_effort_variants_and_model_prefix_stripping(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            home_dir = root / "home"
            state_dir = root / "state"
            home_dir.mkdir()

            p1 = run_statusline(
                {"cwd": str(home_dir), "model": {"id": "GPT 5.4"}, "effort": {"level": "xhigh"}},
                home_dir=home_dir,
                state_dir=state_dir,
            )
            self.assertEqual(p1.returncode, 0, p1.stderr)
            self.assertEqual(strip_ansi(p1.stdout), "~ · 5.4 · xhigh")

            p2 = run_statusline(
                {"cwd": str(home_dir), "model": {"display_name": "Gemini 4 Carbon", "effort": "low"}},
                home_dir=home_dir,
                state_dir=state_dir,
            )
            self.assertEqual(p2.returncode, 0, p2.stderr)
            self.assertEqual(strip_ansi(p2.stdout), "~ · 4 Carbon · low")


if __name__ == "__main__":
    unittest.main()
