#!/usr/bin/env python3
"""Unit tests for claude-statusline."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent / "claude-statusline"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def run_statusline(
    payload: str | dict | None,
    *,
    home_dir: Path,
    state_dir: Path | None = None,
    config_dir: Path | None = None,
    extra_env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home_dir),
    }
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    if state_dir is not None:
        env["CLAUDE_STATUSLINE_STATE_DIR"] = str(state_dir)
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


class ClaudeStatuslineTests(unittest.TestCase):
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

            now_ts = time.time()
            reset_7d_ts = int(now_ts + 86400 * 5.5)
            reset_5h_ts = int(now_ts + 3600 * 2)
            expected_5h_lt = time.localtime(reset_5h_ts)
            same_day = time.strftime("%Y%m%d", expected_5h_lt) == time.strftime("%Y%m%d", time.localtime(now_ts))
            expected_5h_str = time.strftime("%H:%M" if same_day else "%m-%d %H:%M", expected_5h_lt)

            payload = {
                "workspace": {"current_dir": str(repo_dir)},
                "cwd": str(home_dir),
                "model": {"display_name": "Opus 4.6 (1M context) (High)", "id": "claude-opus-4-6"},
                "effort": {"level": "high"},
                "context_window": {
                    "current_usage": {
                        "input_tokens": 12000,
                        "cache_creation_input_tokens": 8000,
                        "cache_read_input_tokens": 22000,
                    },
                    "context_window_size": 200000,
                    "used_percentage": 21.2,
                },
                "rate_limits": {
                    "seven_day": {"used_percentage": 12.4, "resets_at": reset_7d_ts},
                    "five_hour": {"used_percentage": 8.1, "resets_at": reset_5h_ts},
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
            # Segments: cwd, model, effort, ctx, 7d, 5h, [git branch], cost
            self.assertEqual(parts[0], "~/work/demo")
            self.assertEqual(parts[1], "Opus 4.6 (1M context)")
            self.assertEqual(parts[2], "high")
            self.assertEqual(parts[3], "42k/200k 21%")
            self.assertEqual(parts[4], "7d 12% ↻ 5.5d")
            self.assertEqual(parts[5], f"5h 8% ↻ {expected_5h_str}")
            if has_git:
                self.assertEqual(parts[6], "feat/statusline")
                self.assertEqual(parts[7], "$0.42")
            else:
                self.assertEqual(parts[6], "$0.42")

            # Opt-in quota log must NOT be created by default.
            self.assertFalse((state_dir / "quota-log.jsonl").exists())

    def test_api_key_and_third_party_proxy_minimal_payload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            home_dir = root / "home"
            proj_dir = home_dir / "projects" / "api-app"
            proj_dir.mkdir(parents=True)

            # No rate_limits, no effort, no top-level cwd, no display_name (fallback to model.id).
            payload = {
                "workspace": {"current_dir": str(proj_dir)},
                "model": {"id": "deepseek-reasoner"},
                "context_window": {
                    "current_usage": {
                        "input_tokens": 5000,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 15000,
                    },
                    "context_window_size": 128000,
                    "used_percentage": 15.6,
                },
                "cost": {"total_cost_usd": 0.08},
            }

            proc = run_statusline(payload, home_dir=home_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr, "")
            self.assertEqual(
                strip_ansi(proc.stdout),
                "~/projects/api-app · deepseek-reasoner · 20k/128k 16% · $0.08",
            )

    def test_model_parenthetical_preservation_and_effort_stripping(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            home_dir = root / "home"
            home_dir.mkdir()

            cases = [
                ("Opus 4.6 (1M context)", "Opus 4.6 (1M context)"),
                ("Opus 4.6 (1M context) (High)", "Opus 4.6 (1M context)"),
                ("Claude Sonnet 4.6 (low)", "Claude Sonnet 4.6"),
                ("Claude Opus 4.6 (MEDIUM)", "Claude Opus 4.6"),
                ("Claude Opus 4.6 (XHigh)", "Claude Opus 4.6"),
                ("Claude Opus 4.6 (Max)", "Claude Opus 4.6"),
                ("Claude Sonnet 4.5 (200k)", "Claude Sonnet 4.5 (200k)"),
            ]
            for display_name, expected in cases:
                proc = run_statusline(
                    {"cwd": str(home_dir), "model": {"display_name": display_name}},
                    home_dir=home_dir,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(strip_ansi(proc.stdout), f"~ · {expected}", f"display_name={display_name!r}")

    def test_context_window_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            home_dir = root / "home"
            home_dir.mkdir()

            # 1. current_usage is null -> fallback to total_input_tokens
            p1 = run_statusline(
                {
                    "cwd": str(home_dir),
                    "context_window": {
                        "current_usage": None,
                        "total_input_tokens": 1_500_000,
                        "context_window_size": 2_000_000,
                        "used_percentage": 75,
                    },
                },
                home_dir=home_dir,
            )
            self.assertEqual(p1.returncode, 0, p1.stderr)
            self.assertEqual(strip_ansi(p1.stdout), "~ · 1M/2M 75%")

            # 2. current_usage sums to 0 -> fallback to total_input_tokens (without pct)
            p2 = run_statusline(
                {
                    "cwd": str(home_dir),
                    "context_window": {
                        "current_usage": {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                        "total_input_tokens": 35000,
                        "context_window_size": 200000,
                    },
                },
                home_dir=home_dir,
            )
            self.assertEqual(p2.returncode, 0, p2.stderr)
            self.assertEqual(strip_ansi(p2.stdout), "~ · 35k/200k")

            # 3. Missing token counts / window size, but valid used_percentage -> standalone percentage
            p3 = run_statusline(
                {
                    "cwd": str(home_dir),
                    "context_window": {
                        "current_usage": None,
                        "used_percentage": 24.4,
                    },
                },
                home_dir=home_dir,
            )
            self.assertEqual(p3.returncode, 0, p3.stderr)
            self.assertEqual(strip_ansi(p3.stdout), "~ · 24%")

            # 4. Missing context_window_size even when tokens exist, with used_percentage -> standalone percentage
            p4 = run_statusline(
                {
                    "cwd": str(home_dir),
                    "context_window": {
                        "current_usage": {"input_tokens": 10000},
                        "used_percentage": 10,
                    },
                },
                home_dir=home_dir,
            )
            self.assertEqual(p4.returncode, 0, p4.stderr)
            self.assertEqual(strip_ansi(p4.stdout), "~ · 10%")

    def test_rate_limits_independent_and_edge_resets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            home_dir = root / "home"
            home_dir.mkdir()

            now_ts = time.time()
            # Expired 7d resets_at clamps to 0.0d (never negative); numeric string resets_at accepted.
            p1 = run_statusline(
                {
                    "cwd": str(home_dir),
                    "rate_limits": {
                        "seven_day": {"used_percentage": 50, "resets_at": str(int(now_ts - 3600))},
                        "five_hour": None,
                    },
                },
                home_dir=home_dir,
            )
            self.assertEqual(p1.returncode, 0, p1.stderr)
            self.assertEqual(strip_ansi(p1.stdout), "~ · 7d 50% ↻ 0.0d")

            # Only 5h present with numeric string epoch resets_at (next day -> MM-DD HH:MM).
            tomorrow_ts = int(now_ts + 86400 * 2)
            expected_5h = time.strftime("%m-%d %H:%M", time.localtime(tomorrow_ts))
            p2 = run_statusline(
                {
                    "cwd": str(home_dir),
                    "rate_limits": {
                        "five_hour": {"used_percentage": 33.7, "resets_at": str(tomorrow_ts)},
                    },
                },
                home_dir=home_dir,
            )
            self.assertEqual(p2.returncode, 0, p2.stderr)
            self.assertEqual(strip_ansi(p2.stdout), f"~ · 5h 34% ↻ {expected_5h}")

    def test_path_aliases_longest_prefix_and_directory_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            home_dir = root / "home"
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
                    {"workspace": {"current_dir": target_cwd}},
                    home_dir=home_dir,
                    extra_env={"CLAUDE_STATUSLINE_PATH_ALIASES": aliases},
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(strip_ansi(proc.stdout), expected_disp, f"cwd={target_cwd}")

    def test_quota_log_opt_in_dedup_custom_path_and_config_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            home_dir = root / "home"
            config_dir = root / "custom-claude-cfg"
            home_dir.mkdir()

            reset_7d = int(time.time() + 86400 * 4)
            reset_5h = int(time.time() + 7200)
            payload_a = {
                "cwd": str(home_dir),
                "rate_limits": {
                    "seven_day": {"used_percentage": 10, "resets_at": reset_7d},
                    "five_hour": {"used_percentage": 5, "resets_at": reset_5h},
                },
            }
            payload_b = {
                "cwd": str(home_dir),
                "rate_limits": {
                    "seven_day": {"used_percentage": 11, "resets_at": reset_7d},
                    "five_hour": {"used_percentage": 5, "resets_at": reset_5h},
                },
            }

            cfg_log = config_dir / "quota-log.jsonl"
            for disabled_val in (None, "", "0", "false", "FALSE"):
                env = {} if disabled_val is None else {"CLAUDE_STATUSLINE_QUOTA_LOG": disabled_val}
                proc = run_statusline(payload_a, home_dir=home_dir, config_dir=config_dir, extra_env=env)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertFalse(cfg_log.exists(), f"Log should stay disabled for {disabled_val!r}")

            # Respects CLAUDE_CONFIG_DIR when CLAUDE_STATUSLINE_STATE_DIR is unset.
            p1 = run_statusline(
                payload_a, home_dir=home_dir, config_dir=config_dir, extra_env={"CLAUDE_STATUSLINE_QUOTA_LOG": "1"}
            )
            p2 = run_statusline(
                payload_a, home_dir=home_dir, config_dir=config_dir, extra_env={"CLAUDE_STATUSLINE_QUOTA_LOG": "TRUE"}
            )
            p3 = run_statusline(
                payload_b, home_dir=home_dir, config_dir=config_dir, extra_env={"CLAUDE_STATUSLINE_QUOTA_LOG": "true"}
            )
            self.assertEqual((p1.returncode, p2.returncode, p3.returncode), (0, 0, 0))
            self.assertFalse((home_dir / ".claude" / "quota-log.jsonl").exists())

            lines = cfg_log.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            rec1, rec2 = json.loads(lines[0]), json.loads(lines[1])
            self.assertEqual(rec1["7d"], 10)
            self.assertEqual(rec1["7d_reset"], reset_7d)
            self.assertEqual(rec1["5h"], 5)
            self.assertEqual(rec1["5h_reset"], reset_5h)
            self.assertIn("ts", rec1)
            self.assertEqual(rec2["7d"], 11)

            # Custom path with leading ~/ creates parent dirs automatically.
            custom_rel = "~/logs/nested/custom-quota.jsonl"
            p4 = run_statusline(
                payload_b,
                home_dir=home_dir,
                config_dir=config_dir,
                extra_env={"CLAUDE_STATUSLINE_QUOTA_LOG": custom_rel},
            )
            self.assertEqual(p4.returncode, 0, p4.stderr)
            custom_file = home_dir / "logs" / "nested" / "custom-quota.jsonl"
            self.assertTrue(custom_file.is_file())
            self.assertEqual(len(custom_file.read_text(encoding="utf-8").strip().splitlines()), 1)

            # Unwritable log target (directory path instead of file) fails silently without breaking rendering.
            blocker = root / "is-a-dir"
            blocker.mkdir()
            p5 = run_statusline(
                payload_b,
                home_dir=home_dir,
                extra_env={"CLAUDE_STATUSLINE_QUOTA_LOG": str(blocker)},
            )
            self.assertEqual(p5.returncode, 0, p5.stderr)
            self.assertEqual(p5.stderr, "")
            self.assertIn("7d 11%", strip_ansi(p5.stdout))

    def test_malformed_json_invalid_types_and_shell_injection_safety(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            home_dir = root / "home"
            home_dir.mkdir()

            # 1. Malformed / non-object JSON
            for bad_input in (None, "", "{not-valid-json", "null", "[1, 2, 3]", '"just a string"'):
                proc = run_statusline(bad_input, home_dir=home_dir, cwd=home_dir)
                self.assertEqual(proc.returncode, 0, f"input={bad_input!r}, stderr={proc.stderr!r}")
                self.assertEqual(proc.stderr, "")
                self.assertEqual(strip_ansi(proc.stdout), "~")

            # 2. Invalid field types (bool, NaN, Inf, negative, non-numeric resets_at, huge epoch)
            weird_payload = {
                "workspace": {"current_dir": ["not", "a", "str"]},
                "cwd": 123,
                "model": {"display_name": 456, "id": {"nested": True}},
                "effort": {"level": 999},
                "context_window": {
                    "current_usage": {
                        "input_tokens": True,
                        "cache_creation_input_tokens": "NaN",
                        "cache_read_input_tokens": "inf",
                    },
                    "total_input_tokens": False,
                    "context_window_size": "200k",
                    "used_percentage": "NaN",
                },
                "rate_limits": {
                    "seven_day": {"used_percentage": True, "resets_at": "not-a-timestamp"},
                    "five_hour": {"used_percentage": "inf", "resets_at": 1e30},
                },
                "cost": {"total_cost_usd": -1.5},
            }
            p_weird = run_statusline(weird_payload, home_dir=home_dir, cwd=home_dir)
            self.assertEqual(p_weird.returncode, 0, p_weird.stderr)
            self.assertEqual(p_weird.stderr, "")
            self.assertEqual(strip_ansi(p_weird.stdout), "~")

            # Also verify boolean/NaN/Inf cost and huge out-of-range five_hour resets_at degrade cleanly.
            p_weird2 = run_statusline(
                {
                    "cwd": str(home_dir),
                    "rate_limits": {
                        "seven_day": {"used_percentage": 20, "resets_at": True},
                        "five_hour": {"used_percentage": 15, "resets_at": 1e30},
                    },
                    "cost": {"total_cost_usd": True},
                },
                home_dir=home_dir,
            )
            self.assertEqual(p_weird2.returncode, 0, p_weird2.stderr)
            self.assertEqual(p_weird2.stderr, "")
            self.assertEqual(strip_ansi(p_weird2.stdout), "~ · 7d 20% · 5h 15%")

            # 3. Shell injection characters in cwd, alias, model, and effort
            marker = root / "pwned"
            evil_cwd = str(home_dir / f"a'b$(touch {marker})`touch {marker}`%s")
            evil_alias = f"~/alias'$(touch {marker})`touch {marker}`"
            p_inj = run_statusline(
                {
                    "workspace": {"current_dir": evil_cwd},
                    "model": {"display_name": f"Opus'$(touch {marker})"},
                    "effort": {"level": f"high'$(touch {marker})"},
                },
                home_dir=home_dir,
                extra_env={"CLAUDE_STATUSLINE_PATH_ALIASES": f"{evil_cwd}={evil_alias}"},
            )
            self.assertEqual(p_inj.returncode, 0, p_inj.stderr)
            self.assertEqual(p_inj.stderr, "")
            self.assertFalse(marker.exists(), "Shell injection executed!")
            plain_inj = strip_ansi(p_inj.stdout)
            self.assertIn(evil_alias, plain_inj)
            self.assertIn(f"Opus'$(touch {marker})", plain_inj)
            self.assertIn(f"high'$(touch {marker})", plain_inj)

    def test_isolated_python_ignores_cwd_shadow_modules(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            home_dir = root / "home"
            work_dir = home_dir / "shadow-test"
            work_dir.mkdir(parents=True)
            (work_dir / "json.py").write_text('raise RuntimeError("shadowed stdlib json")\n', encoding="utf-8")

            proc = run_statusline(
                {"workspace": {"current_dir": str(work_dir)}, "model": {"display_name": "Opus 4.6"}},
                home_dir=home_dir,
                cwd=work_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr, "")
            self.assertEqual(strip_ansi(proc.stdout), "~/shadow-test · Opus 4.6")


if __name__ == "__main__":
    unittest.main()
