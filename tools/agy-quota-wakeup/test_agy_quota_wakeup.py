#!/usr/bin/env python3
"""Unit tests for agy-quota-wakeup."""

from __future__ import annotations

import datetime as dt
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

SCRIPT_PATH = Path(__file__).resolve().parent / "agy-quota-wakeup"
loader = importlib.machinery.SourceFileLoader("agy_quota_wakeup", str(SCRIPT_PATH))
spec = importlib.util.spec_from_loader("agy_quota_wakeup", loader)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


SAMPLE_USAGE = {
    "conversation_id": "",
    "status": "SUCCESS",
    "command": {
        "name": "usage",
        "data": {
            "groups": [
                {
                    "name": "Gemini Models",
                    "buckets": [
                        {
                            "id": "gemini-weekly",
                            "name": "Weekly Limit Remaining",
                            "description": "You have used some of your weekly limit, it will fully refresh in 6 days, 22 hours.",
                            "window": "weekly",
                            "remaining_fraction": 0.9855,
                            "reset_time": "2026-11-25T02:02:41Z",
                        },
                        {
                            "id": "gemini-5h",
                            "window": "5h",
                            "remaining_fraction": 0.95,
                            "reset_time": "2026-11-18T07:02:41Z",
                        },
                    ],
                },
                {
                    "name": "Claude and GPT models",
                    "buckets": [
                        {
                            "id": "3p-weekly",
                            "name": "Weekly Limit Remaining",
                            "window": "weekly",
                            "remaining_fraction": 1,
                            "reset_time": "2026-11-25T03:31:51Z",
                        },
                        {
                            "id": "3p-5h",
                            "window": "5h",
                            "remaining_fraction": 1,
                            "reset_time": "2026-11-18T08:31:51Z",
                        },
                    ],
                },
            ]
        },
    },
}

SAMPLE_MODELS_STDOUT = """\
gemini-4-carbon-medium\tGemini 4 Carbon (Medium)
gemini-4-carbon-high\tGemini 4 Carbon (High)
gemini-4-carbon-low\tGemini 4 Carbon (Low)
gemini-pro-orchestrator\tOrchestrator (Carbon)
gemini-3.8-flash-high\tGemini 3.8 Flash (High)
gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)
gemini-3.8-flash-low\tGemini 3.8 Flash (Low)
claude-opus-5-5-low\tClaude Opus 5.5 (Low)
claude-sonnet-5-5-low\tClaude Sonnet 5.5 (Low)
claude-sonnet-5-5-medium\tClaude Sonnet 5.5 (Medium)
claude-sonnet-5-5-high\tClaude Sonnet 5.5 (High)
gpt-oss-120b-medium\tGPT-OSS 120B (Medium)
"""


class AgyQuotaWakeupTests(unittest.TestCase):
    def test_parse_iso_utc_python310_fractional_compat(self) -> None:
        t_nano = mod.parse_iso_utc("2026-11-25T02:02:41.123456789Z")
        self.assertIsNotNone(t_nano)
        assert t_nano is not None
        self.assertEqual(t_nano.microsecond, 123456)

        t_short = mod.parse_iso_utc("2026-11-25T10:02:41.5+0800")
        self.assertIsNotNone(t_short)
        assert t_short is not None
        self.assertEqual(t_short.hour, 2)
        self.assertEqual(t_short.microsecond, 500000)

    def test_extract_weekly_buckets_and_activity(self) -> None:
        now_utc = dt.datetime(2026, 11, 18, 3, 31, 51, tzinfo=dt.timezone.utc)
        weekly = mod.extract_weekly_buckets(SAMPLE_USAGE)
        self.assertEqual([w["pool"] for w in weekly], ["gemini", "3p"])

        g_active, g_reason, _ = mod.evaluate_bucket_activity(weekly[0]["raw"], now_utc)
        self.assertTrue(g_active)
        self.assertEqual(g_reason, "remaining_below_100pct")

        c_active, c_reason, _ = mod.evaluate_bucket_activity(weekly[1]["raw"], now_utc)
        self.assertFalse(c_active)
        self.assertEqual(c_reason, "unactivated_100pct")

    def test_reset_timer_already_counting_down_even_if_fraction_rounds_to_1(self) -> None:
        now_utc = dt.datetime(2026, 11, 18, 5, 0, 0, tzinfo=dt.timezone.utc)
        bucket = {
            "id": "gemini-weekly",
            "window": "weekly",
            "remaining_fraction": 1,
            "reset_time": "2026-11-25T04:00:00Z",
        }
        active, reason, _ = mod.evaluate_bucket_activity(bucket, now_utc)
        self.assertTrue(active)
        self.assertEqual(reason, "reset_timer_counting_down")

    def test_pinned_reset_time_within_5m_vs_fresh_bonus_reset(self) -> None:
        activated_utc = dt.datetime(2026, 11, 18, 4, 0, 0, tzinfo=dt.timezone.utc)
        last_state = {
            "last_probe_epoch": activated_utc.timestamp(),
            "locked_reset_time": "2026-11-25T04:00:00Z",
            "verified_active": True,
        }
        two_min_later = activated_utc + dt.timedelta(minutes=2)
        pinned_bucket = {
            "id": "gemini-weekly",
            "window": "weekly",
            "remaining_fraction": 1,
            "reset_time": "2026-11-25T04:00:00Z",
        }
        active, reason, _ = mod.evaluate_bucket_activity(pinned_bucket, two_min_later, last_state)
        self.assertTrue(active)
        self.assertEqual(reason, "pinned_reset_time_just_activated")

        three_min_later = activated_utc + dt.timedelta(minutes=3)
        bonus_reset_bucket = {
            "id": "gemini-weekly",
            "window": "weekly",
            "remaining_fraction": 1,
            "reset_time": "2026-11-25T04:03:00Z",
        }
        active2, reason2, _ = mod.evaluate_bucket_activity(bonus_reset_bucket, three_min_later, last_state)
        self.assertFalse(active2)
        self.assertEqual(reason2, "unactivated_100pct")

    def test_unverified_cooldown_prevents_hourly_spin(self) -> None:
        t0 = dt.datetime(2026, 11, 18, 4, 0, 0, tzinfo=dt.timezone.utc)
        last_state = {
            "last_probe_epoch": t0.timestamp(),
            "locked_reset_time": "2026-11-25T04:00:00Z",
            "verified_active": False,
        }
        one_hour_later = t0 + dt.timedelta(hours=1)
        drifting_bucket = {
            "id": "3p-weekly",
            "window": "weekly",
            "remaining_fraction": 1,
            "reset_time": "2026-11-25T05:00:00Z",
        }
        active, reason, _ = mod.evaluate_bucket_activity(drifting_bucket, one_hour_later, last_state)
        self.assertTrue(active)
        self.assertEqual(reason, "unverified_probe_cooldown")

    def test_chat_only_wakeup_agent_and_flags(self) -> None:
        models = mod.parse_models_output(SAMPLE_MODELS_STDOUT)
        self.assertEqual(mod.pick_wakeup_model("gemini", models), "gemini-3.8-flash-low")
        self.assertEqual(mod.pick_wakeup_model("3p", models), "claude-sonnet-5-5-low")

        future_models = mod.parse_models_output(
            SAMPLE_MODELS_STDOUT
            + "gemini-4-flash-low\tGemini 4 Flash (Low)\n"
            + "gemini-4.2-flash-low\tGemini 4.2 Flash (Low)\n"
            + "claude-sonnet-6-low\tClaude Sonnet 6 (Low)\n"
            + "claude-sonnet-6-1-low\tClaude Sonnet 6.1 (Low)\n"
        )
        self.assertEqual(mod.pick_wakeup_model("gemini", future_models), "gemini-4.2-flash-low")
        self.assertEqual(mod.pick_wakeup_model("3p", future_models), "claude-sonnet-6-1-low")

        cmd = mod.build_wakeup_command("/usr/local/bin/agy", "claude-sonnet-5-5-low")
        self.assertIn("--agent", cmd)
        self.assertIn("quota-wakeup", cmd)
        self.assertIn("--effort", cmd)
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        self.assertNotIn("--sandbox", cmd)

        cmd_gpt = mod.build_wakeup_command("/usr/local/bin/agy", "gpt-oss-120b-medium")
        self.assertNotIn("--effort", cmd_gpt)

        with tempfile.TemporaryDirectory() as td:
            agent_file = mod.write_chat_only_agent(Path(td))
            text = agent_file.read_text(encoding="utf-8")
            self.assertIn("tools: []", text)
            self.assertIn("inheritCustomizations: false", text)
            self.assertIn("excludeDefaultComponents: true", text)

        fb_cmd = mod.build_fallback_wakeup_command("/usr/local/bin/agy", "claude-sonnet-5-5-low")
        self.assertNotIn("--agent", fb_cmd)
        self.assertNotIn("--dangerously-skip-permissions", fb_cmd)
        self.assertNotIn("--sandbox", fb_cmd)

    def test_send_wakeup_request_primary_and_fallback(self) -> None:
        calls = []

        def fake_runner_ok(cmd, env, timeout_s=75, with_chat_only_agent=False):
            calls.append((list(cmd), with_chat_only_agent))
            return {"status": "SUCCESS", "response": "OK"}

        res, mode, perr = mod.send_wakeup_request("agy", "gemini-3.8-flash-low", {}, runner=fake_runner_ok)
        self.assertEqual(mode, "chat_only")
        self.assertIsNone(perr)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1])

        calls.clear()

        def fake_runner_fallback(cmd, env, timeout_s=75, with_chat_only_agent=False):
            calls.append((list(cmd), with_chat_only_agent))
            if with_chat_only_agent:
                raise RuntimeError("unknown frontmatter key excludeDefaultComponents")
            return {"status": "SUCCESS", "response": "OK"}

        res2, mode2, perr2 = mod.send_wakeup_request("agy", "gemini-3.8-flash-low", {}, runner=fake_runner_fallback)
        self.assertEqual(mode2, "standard_fallback")
        self.assertIn("excludeDefaultComponents", perr2 or "")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][1])
        self.assertFalse(calls[1][1])
        self.assertNotIn("--dangerously-skip-permissions", calls[1][0])

        calls.clear()

        def fake_runner_agent_missing(cmd, env, timeout_s=75, with_chat_only_agent=False):
            calls.append((list(cmd), with_chat_only_agent))
            if with_chat_only_agent:
                raise RuntimeError('agy exited with 1: unknown agent "quota-wakeup" in /tmp/agy-quota-wakeup-x')
            return {"status": "SUCCESS", "response": "OK"}

        res3, mode3, _ = mod.send_wakeup_request("agy", "gemini-3.8-flash-low", {}, runner=fake_runner_agent_missing)
        self.assertEqual(mode3, "standard_fallback")
        self.assertEqual(len(calls), 2)

        calls.clear()
        huge_usage_tail = "\nUsage of agy:\n" + ("  --some-flag  help text\n" * 160)
        compact = mod._compact_cli_error("flags provided but not defined: -disable-slash-commands" + huge_usage_tail)
        self.assertIn("flags provided but not defined", compact)
        self.assertNotIn("Usage of agy:", compact)

        def fake_runner_unknown_flag_plural(cmd, env, timeout_s=75, with_chat_only_agent=False):
            calls.append((list(cmd), with_chat_only_agent))
            if with_chat_only_agent:
                raise RuntimeError(f"agy exited with 2: {compact}")
            return {"status": "SUCCESS", "response": "OK"}

        _, mode4, _ = mod.send_wakeup_request("agy", "gemini-3.8-flash-low", {}, runner=fake_runner_unknown_flag_plural)
        self.assertEqual(mode4, "standard_fallback")
        self.assertEqual(len(calls), 2)

        # Timeouts, network errors, or RPC Unknown/InvalidArgument server errors must NOT send a duplicate fallback request.
        for bad_err in (
            subprocess.TimeoutExpired(cmd=["agy"], timeout=75),
            RuntimeError("agy exited with 1: connection reset by peer"),
            RuntimeError("agy exited with 1: rpc error: code = Unknown desc = internal server error"),
            RuntimeError("agy exited with 1: rpc error: code = InvalidArgument desc = invalid argument"),
            RuntimeError("agy exited with 3: AGY_ERROR: invalid value in model response"),
        ):
            calls.clear()

            def fake_runner_bad(cmd, env, timeout_s=75, with_chat_only_agent=False, _exc=bad_err):
                calls.append((list(cmd), with_chat_only_agent))
                raise _exc

            with self.assertRaises(type(bad_err)):
                mod.send_wakeup_request("agy", "gemini-3.8-flash-low", {}, runner=fake_runner_bad)
            self.assertEqual(len(calls), 1)

    def test_clean_env_and_systemd_unit_escaping(self) -> None:
        old = os.environ.get("GEMINI_API_KEY")
        old_gw = os.environ.get("AGY_LLM_GATEWAY_URL")
        old_path = os.environ.get("PATH")
        try:
            os.environ["GEMINI_API_KEY"] = "secret"
            os.environ["AGY_LLM_GATEWAY_URL"] = "https://example.invalid"
            os.environ["PATH"] = "/usr/bin:.:relative/dir:/custom/abs/bin"
            clean = mod.build_clean_env()
            self.assertNotIn("GEMINI_API_KEY", clean)
            self.assertNotIn("AGY_LLM_GATEWAY_URL", clean)
            path_entries = clean["PATH"].split(":")
            self.assertIn("/custom/abs/bin", path_entries)
            self.assertNotIn(".", path_entries)
            self.assertNotIn("relative/dir", path_entries)
            self.assertTrue(any(p.endswith("/.local/bin") for p in path_entries))
        finally:
            if old is None:
                os.environ.pop("GEMINI_API_KEY", None)
            else:
                os.environ["GEMINI_API_KEY"] = old
            if old_gw is None:
                os.environ.pop("AGY_LLM_GATEWAY_URL", None)
            else:
                os.environ["AGY_LLM_GATEWAY_URL"] = old_gw
            if old_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = old_path

        self.assertEqual(mod.DEFAULT_INTERVAL_SECONDS, 1800)
        self.assertEqual(mod.calendar_minutes_for_interval(1800), [3, 33])
        self.assertEqual(mod.calendar_minutes_for_interval(1800, offset_minute=18), [18, 48])
        self.assertEqual(mod.calendar_minutes_for_interval(3600), [3])
        self.assertIsNone(mod.calendar_minutes_for_interval(2700))

        _, timer_offset = mod.build_systemd_units(1800, "all", offset_minute=18)
        self.assertIn("OnCalendar=*-*-* *:18,48:00", timer_offset)
        plist_offset = mod.build_launchd_plist(1800, "all", Path("/var/tmp/custom-agy-state"), offset_minute=18)
        self.assertEqual(plist_offset["StartCalendarInterval"], [{"Minute": 18}, {"Minute": 48}])
        with redirect_stderr(io.StringIO()):
            self.assertEqual(mod.main(["install", "--minute-offset", "60"]), 2)

        service_txt, timer_txt = mod.build_systemd_units(
            1800,
            "all",
            agy_bin="/opt/agy/bin/agy",
            custom_state_dir=Path("/var/tmp/custom-agy-state"),
        )
        self.assertIn("Type=oneshot", service_txt)
        self.assertIn("TimeoutStartSec=1320", service_txt)
        self.assertIn("--min-uptime 300", service_txt)
        self.assertIn("--agy-bin /opt/agy/bin/agy", service_txt)
        self.assertIn('Environment="AGY_QUOTA_WAKEUP_STATE_DIR=/var/tmp/custom-agy-state"', service_txt)
        self.assertIn("OnCalendar=*-*-* *:03,33:00", timer_txt)
        self.assertNotIn("OnActiveSec=", timer_txt)
        self.assertNotIn("OnStartupSec=", timer_txt)
        self.assertNotIn("Persistent=true", timer_txt)
        self.assertEqual(mod._systemd_env_assignment("PATH", "/a b/%u"), 'Environment="PATH=/a b/%%u"')
        self.assertEqual(mod._systemd_exec_arg("/opt/agy%u/bin/agy"), '"/opt/agy%%u/bin/agy"')

        with mock.patch.dict(os.environ, {"AGY_QUOTA_WAKEUP_STATE_DIR": ""}):
            svc_default, timer_odd = mod.build_systemd_units(2700, "all")
            self.assertNotIn("AGY_QUOTA_WAKEUP_STATE_DIR=", svc_default)
            self.assertIn("--min-uptime 300", svc_default)
            self.assertIn("OnActiveSec=2700s", timer_odd)
            self.assertIn("OnUnitActiveSec=2700s", timer_odd)
            self.assertNotIn("OnStartupSec=", timer_odd)
            self.assertNotIn("Persistent=true", timer_odd)

            plist = mod.build_launchd_plist(
                1800,
                "gemini",
                Path("/tmp/st"),
                agy_bin="/opt/agy/bin/agy",
                custom_state_dir=Path("/tmp/st"),
            )
            self.assertIs(plist["RunAtLoad"], False)
            self.assertIn("--min-uptime", plist["ProgramArguments"])
            self.assertEqual(
                plist["ProgramArguments"][plist["ProgramArguments"].index("--min-uptime") + 1],
                "300",
            )
            self.assertEqual(plist["ProgramArguments"][-2:], ["--agy-bin", "/opt/agy/bin/agy"])
            self.assertEqual(plist["EnvironmentVariables"].get("AGY_QUOTA_WAKEUP_STATE_DIR"), "/tmp/st")
            self.assertEqual(plist["StartCalendarInterval"], [{"Minute": 3}, {"Minute": 33}])
            self.assertNotIn("StartInterval", plist)

            plist_odd = mod.build_launchd_plist(2700, "gemini", Path("/tmp/st"))
            self.assertNotIn("AGY_QUOTA_WAKEUP_STATE_DIR", plist_odd["EnvironmentVariables"])
            self.assertEqual(plist_odd["StartInterval"], 2700)
            self.assertNotIn("StartCalendarInterval", plist_odd)

    def test_resolve_agy_bin_validates_explicit_and_preserves_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td).resolve()
            real_bin = d / "real-agy"
            real_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            real_bin.chmod(0o755)
            link_bin = d / "agy-link"
            link_bin.symlink_to(real_bin)
            non_exec = d / "not-exec"
            non_exec.write_text("plain", encoding="utf-8")
            non_exec.chmod(0o644)

            old_cwd = os.getcwd()
            try:
                os.chdir(d)
                resolved = mod.resolve_agy_bin("./agy-link", {"PATH": "/usr/bin", "HOME": str(d)})
                # Must return an absolute path while keeping the symlink entrypoint intact.
                self.assertEqual(resolved, str(link_bin))
            finally:
                os.chdir(old_cwd)

            with self.assertRaises(FileNotFoundError):
                mod.resolve_agy_bin(str(d / "missing-agy"), {"PATH": "/usr/bin", "HOME": str(d)})
            with self.assertRaises(FileNotFoundError):
                mod.resolve_agy_bin(str(non_exec), {"PATH": "/usr/bin", "HOME": str(d)})

    def test_dry_run_skips_log_file_and_surfaces_model_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            st_dir = Path(td)
            fake_agy = st_dir / "agy"
            fake_agy.write_text("#!/bin/sh\necho '1.5.11'\n", encoding="utf-8")
            fake_agy.chmod(0o755)

            all_active_usage = {
                "status": "SUCCESS",
                "command": {
                    "name": "usage",
                    "data": {
                        "groups": [
                            {
                                "name": "Gemini Models",
                                "buckets": [
                                    {
                                        "id": "gemini-weekly",
                                        "window": "weekly",
                                        "remaining_fraction": 0.9,
                                        "reset_time": "2026-11-25T02:02:41Z",
                                    }
                                ],
                            }
                        ]
                    },
                },
            }

            with mock.patch.dict(os.environ, {"AGY_QUOTA_WAKEUP_STATE_DIR": str(st_dir)}):
                # 1) All active + --dry-run => status=="dry_run", exit 0, no wakeup.jsonl created.
                out_buf = io.StringIO()
                with mock.patch.object(mod, "fetch_usage_with_retries", return_value=(all_active_usage, 1)), redirect_stdout(out_buf):
                    rc = mod.main(["run", "--dry-run", "--pools", "gemini", "--agy-bin", str(fake_agy)])
                self.assertEqual(rc, 0)
                rec = json.loads(out_buf.getvalue().strip())
                self.assertEqual(rec["status"], "dry_run")
                self.assertFalse((st_dir / "wakeup.jsonl").exists())

                # 2) --dry-run + --force with failing `agy models` => exit 1, status=="error", still no wakeup.jsonl.
                err_buf = io.StringIO()
                with mock.patch.object(mod, "fetch_usage_with_retries", return_value=(all_active_usage, 1)):
                    with mock.patch.object(mod, "fetch_available_models", side_effect=RuntimeError("models boom")):
                        with redirect_stderr(err_buf):
                            rc_err = mod.main(["run", "--dry-run", "--force", "--pools", "gemini", "--agy-bin", str(fake_agy)])
                self.assertEqual(rc_err, 1)
                rec_err = json.loads(err_buf.getvalue().strip())
                self.assertEqual(rec_err["status"], "error")
                self.assertFalse((st_dir / "wakeup.jsonl").exists())

    def test_proxy_persistence_all_proxy_bridge_and_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            st_dir = d / "state"
            fake_agy = d / "agy"
            fake_agy.write_text("#!/bin/sh\necho '1.5.11'\n", encoding="utf-8")
            fake_agy.chmod(0o755)

            # 1) Install with lowercase all_proxy + credentialed URL -> bridges to https_proxy/http_proxy,
            # writes 0600 proxy.json, and redacts credentials in stdout.
            out_buf = io.StringIO()
            env_install = {
                "AGY_QUOTA_WAKEUP_STATE_DIR": str(st_dir),
                "all_proxy": "socks5://alice:supersecret@127.0.0.1:1080",
            }
            with mock.patch.dict(os.environ, env_install, clear=False):
                for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
                    os.environ.pop(k, None)
                with mock.patch.object(mod.platform, "system", return_value="Linux"):
                    with mock.patch.object(
                        mod,
                        "_install_linux",
                        side_effect=lambda *a, **kw: (0, *mod.persist_install_proxy_env(st_dir, bool(kw.get("disable_proxy")))),
                    ):
                        with redirect_stdout(out_buf):
                            rc = mod.main(["install", "--agy-bin", str(fake_agy)])
            self.assertEqual(rc, 0)
            printed = out_buf.getvalue()
            self.assertIn("socks5://***@127.0.0.1:1080", printed)
            self.assertNotIn("supersecret", printed)

            pfile = st_dir / "proxy.json"
            self.assertTrue(pfile.is_file())
            self.assertEqual(pfile.stat().st_mode & 0o777, 0o600)

            # 2) Background run (even when ambient NO_PROXY is set by OS) still loads saved proxy.json.
            clean_bg = {k: v for k, v in os.environ.items() if k not in mod.PROXY_ENV_KEYS}
            clean_bg["NO_PROXY"] = "localhost,127.0.0.1"
            with mock.patch.dict(os.environ, clean_bg, clear=True):
                built = mod.build_clean_env(state_dir=st_dir)
            self.assertEqual(built.get("https_proxy"), "socks5://alice:supersecret@127.0.0.1:1080")
            self.assertEqual(built.get("http_proxy"), "socks5://alice:supersecret@127.0.0.1:1080")
            self.assertEqual(built.get("NO_PROXY"), "localhost,127.0.0.1")

            # 3) Re-install with --no-proxy removes proxy.json.
            with mock.patch.dict(os.environ, env_install, clear=False):
                with mock.patch.object(mod.platform, "system", return_value="Linux"):
                    with mock.patch.object(
                        mod,
                        "_install_linux",
                        side_effect=lambda *a, **kw: (0, *mod.persist_install_proxy_env(st_dir, bool(kw.get("disable_proxy")))),
                    ):
                        with redirect_stdout(io.StringIO()):
                            rc2 = mod.main(["install", "--no-proxy", "--agy-bin", str(fake_agy)])
            self.assertEqual(rc2, 0)
            self.assertFalse(pfile.exists())

    def test_state_persisted_immediately_before_recheck_failure_and_uninstall_guard(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            st_dir = d / "state"
            xdg_dir = d / "xdg"
            fake_agy = d / "agy"
            fake_agy.write_text("#!/bin/sh\necho '1.5.11'\n", encoding="utf-8")
            fake_agy.chmod(0o755)

            real_now = dt.datetime.now(dt.timezone.utc)
            drift_t1 = (real_now + dt.timedelta(days=7, seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
            drift_t2 = (real_now + dt.timedelta(days=7, seconds=13)).strftime("%Y-%m-%dT%H:%M:%SZ")
            unactivated_t1 = {
                "status": "SUCCESS",
                "command": {
                    "name": "usage",
                    "data": {
                        "groups": [
                            {
                                "name": "Gemini Models",
                                "buckets": [
                                    {
                                        "id": "gemini-weekly",
                                        "window": "weekly",
                                        "remaining_fraction": 1.0,
                                        "reset_time": drift_t1,
                                    }
                                ],
                            }
                        ]
                    },
                },
            }
            unactivated_t2 = {
                "status": "SUCCESS",
                "command": {
                    "name": "usage",
                    "data": {
                        "groups": [
                            {
                                "name": "Gemini Models",
                                "buckets": [
                                    {
                                        "id": "gemini-weekly",
                                        "window": "weekly",
                                        "remaining_fraction": 1.0,
                                        "reset_time": drift_t2,
                                    }
                                ],
                            }
                        ]
                    },
                },
            }

            with mock.patch.dict(
                os.environ,
                {"AGY_QUOTA_WAKEUP_STATE_DIR": str(st_dir), "XDG_CONFIG_HOME": str(xdg_dir)},
            ):
                # 1) Pre-wakeup drift confirmed (t2 > t1), wakeup succeeds, post-wakeup recheck raises!
                with mock.patch.object(mod.time, "sleep"):
                    with mock.patch.object(
                        mod,
                        "fetch_usage_with_retries",
                        side_effect=[(unactivated_t1, 1), (unactivated_t2, 1), RuntimeError("recheck network blip")],
                    ):
                        with mock.patch.object(
                            mod, "fetch_available_models", return_value=[("gemini-3.8-flash-low", "Flash Low")]
                        ):
                            with mock.patch.object(
                                mod,
                                "send_wakeup_request",
                                return_value=({"status": "SUCCESS", "response": "OK", "usage": {"total_tokens": 370}}, "chat_only", None),
                            ):
                                with redirect_stderr(io.StringIO()):
                                    rc = mod.main(["run", "--pools", "gemini", "--agy-bin", str(fake_agy)])
                self.assertEqual(rc, 1)
                saved_state = mod.load_json_dict(st_dir / "state.json")
                self.assertIn("gemini", saved_state.get("pools", {}))
                self.assertIs(saved_state["pools"]["gemini"]["verified_active"], False)

                # 2) Uninstall refuses to delete units/proxy.json if systemd --user bus is unreachable while units exist.
                unit_dir = xdg_dir / "systemd" / "user"
                unit_dir.mkdir(parents=True, exist_ok=True)
                (unit_dir / "agy-quota-wakeup.timer").write_text("[Timer]\n", encoding="utf-8")
                mod.atomic_write_json(st_dir / "proxy.json", {"env": {"HTTPS_PROXY": "http://127.0.0.1:8080"}})

                with mock.patch.object(mod.platform, "system", return_value="Linux"):
                    with mock.patch.object(mod.shutil, "which", return_value="/usr/bin/systemctl"):
                        with mock.patch.object(mod, "systemd_user_available", return_value=(False, "bus down")):
                            with redirect_stderr(io.StringIO()):
                                rc_un = mod.main(["uninstall"])
                self.assertEqual(rc_un, 1)
                self.assertTrue((unit_dir / "agy-quota-wakeup.timer").exists())
                self.assertTrue((st_dir / "proxy.json").exists())

    def test_ensure_linux_linger_auto_enable_and_fallback_hint(self) -> None:
        class DummyCompleted:
            def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        # Case 1: Linger=no -> loginctl enable-linger succeeds
        with mock.patch.object(mod.shutil, "which", return_value="/usr/bin/loginctl"):
            with mock.patch.object(
                mod.subprocess,
                "run",
                side_effect=[
                    DummyCompleted(0, stdout="Linger=no\n"),
                    DummyCompleted(0, stdout=""),
                ],
            ) as mock_run:
                note = mod.ensure_linux_linger()
        self.assertIn("enabled via loginctl", note)
        self.assertEqual(mock_run.call_args_list[1][0][0], ["loginctl", "--no-ask-password", "enable-linger"])

        # Case 2: Linger=no and polkit denies -> graceful sudo hint without raising
        with mock.patch.object(mod.shutil, "which", return_value="/usr/bin/loginctl"):
            with mock.patch.object(
                mod.subprocess,
                "run",
                side_effect=[
                    DummyCompleted(0, stdout="Linger=no\n"),
                    DummyCompleted(1, stderr="Access denied"),
                    DummyCompleted(0, stdout="Linger=no\n"),
                ],
            ):
                note_denied = mod.ensure_linux_linger()
        self.assertIn("NOT enabled", note_denied)
        self.assertIn("sudo loginctl enable-linger", note_denied)

    def test_exhausted_omitempty_bucket_and_pre_wakeup_drift_guard(self) -> None:
        now_utc = dt.datetime(2026, 11, 19, 8, 0, 0, tzinfo=dt.timezone.utc)
        # Proto3 omits `remaining_fraction` when 0.0; must be treated as 0% active rather than crashing.
        exhausted = {
            "id": "gemini-weekly",
            "window": "weekly",
            "description": "You have exhausted your weekly limit.",
            "reset_time": "2026-11-22T08:00:00Z",
        }
        active0, reason0, meta0 = mod.evaluate_bucket_activity(exhausted, now_utc)
        self.assertTrue(active0)
        self.assertEqual(reason0, "exhausted_0pct")
        self.assertEqual(meta0["remaining_pct"], 0.0)

        # Missing reset_time at 100% must not blindly fire probes.
        no_reset = {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 1.0}
        active_nr, reason_nr, _ = mod.evaluate_bucket_activity(no_reset, now_utc)
        self.assertTrue(active_nr)
        self.assertEqual(reason_nr, "missing_reset_time")

        # User manually sent a tiny request 2m ago: fraction still 1.0, reset_time = now + 7d - 2m (fixed, not drifting).
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            st_dir = d / "state"
            fake_agy = d / "agy"
            fake_agy.write_text("#!/bin/sh\necho '1.5.11'\n", encoding="utf-8")
            fake_agy.chmod(0o755)

            real_now = dt.datetime.now(dt.timezone.utc)
            fixed_reset = (real_now + dt.timedelta(days=7) - dt.timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
            pinned_payload = {
                "status": "SUCCESS",
                "command": {
                    "name": "usage",
                    "data": {
                        "groups": [
                            {
                                "name": "Gemini Models",
                                "buckets": [
                                    {
                                        "id": "gemini-weekly",
                                        "window": "weekly",
                                        "remaining_fraction": 1.0,
                                        "reset_time": fixed_reset,
                                    }
                                ],
                            }
                        ]
                    },
                },
            }
            out_buf = io.StringIO()
            with mock.patch.dict(os.environ, {"AGY_QUOTA_WAKEUP_STATE_DIR": str(st_dir)}):
                with mock.patch.object(mod.time, "sleep"):
                    with mock.patch.object(
                        mod, "fetch_usage_with_retries", side_effect=[(pinned_payload, 1), (pinned_payload, 1)]
                    ):
                        with mock.patch.object(mod, "send_wakeup_request") as mock_send:
                            with redirect_stdout(out_buf):
                                rc = mod.main(["run", "--pools", "gemini", "--agy-bin", str(fake_agy)])
            self.assertEqual(rc, 0)
            mock_send.assert_not_called()
            rec = json.loads(out_buf.getvalue().strip())
            self.assertEqual(rec["status"], "ok")
            self.assertEqual(rec["pools"]["gemini"]["reason"], "reset_time_already_locked")

            # If the second pre-wakeup `/usage` fails due to a transient error, do NOT blindly send a probe.
            err_buf = io.StringIO()
            with mock.patch.dict(os.environ, {"AGY_QUOTA_WAKEUP_STATE_DIR": str(st_dir)}):
                with mock.patch.object(mod.time, "sleep"):
                    with mock.patch.object(
                        mod,
                        "fetch_usage_with_retries",
                        side_effect=[(pinned_payload, 1), RuntimeError("second check blip")],
                    ):
                        with mock.patch.object(mod, "send_wakeup_request") as mock_send2:
                            with redirect_stderr(err_buf):
                                rc_blip = mod.main(["run", "--pools", "gemini", "--agy-bin", str(fake_agy)])
            self.assertEqual(rc_blip, 1)
            mock_send2.assert_not_called()

            # Old agy < 1.3.4 must be rejected cleanly before sending `/usage`.
            old_agy = d / "old-agy"
            old_agy.write_text("#!/bin/sh\necho '1.3.0'\n", encoding="utf-8")
            old_agy.chmod(0o755)
            with self.assertRaises(RuntimeError):
                mod.ensure_min_agy_version(str(old_agy), {"PATH": "/usr/bin", "HOME": str(d)})

    def test_log_rotation_and_atomic_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            log_p = d / "wakeup.jsonl"
            for i in range(10):
                mod.append_jsonl_with_rotation(log_p, {"idx": i, "msg": "x" * 60}, max_bytes=250)
            self.assertTrue(log_p.exists())
            self.assertTrue((d / "wakeup.jsonl.1").exists())

            st_p = d / "state.json"
            mod.atomic_write_json(st_p, {"ok": True})
            self.assertEqual(mod.load_json_dict(st_p), {"ok": True})

    def test_linux_status_schedule_and_logs_flag_exclusivity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            st_dir = d / "state"
            xdg_dir = d / "xdg"
            svc_text, tmr_text = mod.build_systemd_units(1800, "all", offset_minute=18)
            unit_dir = xdg_dir / "systemd" / "user"
            unit_dir.mkdir(parents=True, exist_ok=True)
            (unit_dir / f"{mod.SYSTEMD_UNIT_BASE}.service").write_text(svc_text, encoding="utf-8")
            (unit_dir / f"{mod.SYSTEMD_UNIT_BASE}.timer").write_text(tmr_text, encoding="utf-8")

            class DummyProc:
                def __init__(self, out: str = "") -> None:
                    self.returncode = 0
                    self.stdout = out
                    self.stderr = ""

            out_st = io.StringIO()
            with mock.patch.dict(
                os.environ,
                {"AGY_QUOTA_WAKEUP_STATE_DIR": str(st_dir), "XDG_CONFIG_HOME": str(xdg_dir)},
            ):
                with mock.patch.object(mod.platform, "system", return_value="Linux"):
                    with mock.patch.object(mod.shutil, "which", return_value="/usr/bin/systemctl"):
                        with mock.patch.object(
                            mod.subprocess,
                            "run",
                            side_effect=lambda cmd, **kw: DummyProc("active\n" if "is-active" in cmd else "Linger=yes\n"),
                        ):
                            with redirect_stdout(out_st):
                                rc = mod.main(["status"])
            self.assertEqual(rc, 0)
            self.assertIn("schedule=hourly at :18,:48", out_st.getvalue())

            # Non-hour-dividing timer (`OnActiveSec=2700s` + `OnUnitActiveSec=2700s`) still parses cleanly in `status`
            _, tmr_odd = mod.build_systemd_units(2700, "all")
            (unit_dir / f"{mod.SYSTEMD_UNIT_BASE}.timer").write_text(tmr_odd, encoding="utf-8")
            out_st_odd = io.StringIO()
            with mock.patch.dict(
                os.environ,
                {"AGY_QUOTA_WAKEUP_STATE_DIR": str(st_dir), "XDG_CONFIG_HOME": str(xdg_dir)},
            ):
                with mock.patch.object(mod.platform, "system", return_value="Linux"):
                    with mock.patch.object(mod.shutil, "which", return_value="/usr/bin/systemctl"):
                        with mock.patch.object(
                            mod.subprocess,
                            "run",
                            side_effect=lambda cmd, **kw: DummyProc("active\n" if "is-active" in cmd else "Linger=yes\n"),
                        ):
                            with redirect_stdout(out_st_odd):
                                rc_odd = mod.main(["status"])
            self.assertEqual(rc_odd, 0)
            self.assertIn("schedule=every 2700s", out_st_odd.getvalue())

        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                mod.main(["logs", "--activations", "--errors"])
        self.assertEqual(ctx.exception.code, 2)

    def test_macos_install_kickstart_and_linux_install_output(self) -> None:
        class DummyProc:
            def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
                self.returncode = rc
                self.stdout = out
                self.stderr = err

        with tempfile.TemporaryDirectory() as td:
            d = Path(td).resolve()
            st_dir = d / "state"
            st_dir.mkdir(parents=True, exist_ok=True)
            xdg_dir = d / "xdg"
            plist_file = d / "LaunchAgents" / f"{mod.LAUNCHD_LABEL}.plist"
            fake_agy = d / "agy"
            fake_agy.write_text("#!/bin/sh\necho '1.5.11'\n", encoding="utf-8")
            fake_agy.chmod(0o755)

            # macOS install triggers kickstart and prints min-uptime=300s
            mac_calls = []
            mac_out = io.StringIO()

            def fake_mac_ok(cmd, **kwargs):
                mac_calls.append(list(cmd))
                return DummyProc(0)

            with mock.patch.object(mod, "launchd_plist_path", return_value=plist_file):
                with mock.patch.object(mod.subprocess, "run", side_effect=fake_mac_ok):
                    with redirect_stdout(mac_out):
                        rc_mac, _, _ = mod._install_macos(1800, "all", st_dir, agy_bin=None)
            self.assertEqual(rc_mac, 0)
            domain_target = f"gui/{os.getuid()}/{mod.LAUNCHD_LABEL}"
            self.assertIn(["launchctl", "kickstart", domain_target], mac_calls)
            self.assertIn("min-uptime=300s", mac_out.getvalue())
            self.assertNotIn("RunAtLoad=true", mac_out.getvalue())
            written_pl = mod.plistlib.loads(plist_file.read_bytes())
            self.assertIs(written_pl["RunAtLoad"], False)
            self.assertIn("--min-uptime", written_pl["ProgramArguments"])
            self.assertEqual(
                written_pl["ProgramArguments"][written_pl["ProgramArguments"].index("--min-uptime") + 1],
                "300",
            )

            # macOS `status` handles RunAtLoad=False, legacy RunAtLoad=True, and omitted RunAtLoad cleanly
            for pl_override, expected_flag in (
                (written_pl, "RunAtLoad=False"),
                ({**written_pl, "RunAtLoad": True}, "RunAtLoad=True"),
                ({k: v for k, v in written_pl.items() if k != "RunAtLoad"}, "RunAtLoad=False"),
            ):
                plist_file.write_bytes(mod.plistlib.dumps(pl_override, sort_keys=True))
                st_out = io.StringIO()
                with mock.patch.dict(os.environ, {"AGY_QUOTA_WAKEUP_STATE_DIR": str(st_dir)}):
                    with mock.patch.object(mod.platform, "system", return_value="Darwin"):
                        with mock.patch.object(mod, "launchd_plist_path", return_value=plist_file):
                            with mock.patch.object(mod.subprocess, "run", return_value=DummyProc(0)):
                                with redirect_stdout(st_out):
                                    rc_st_mac = mod.main(["status"])
                self.assertEqual(rc_st_mac, 0)
                self.assertIn(expected_flag, st_out.getvalue())

            # Kickstart failure reports clearly and returns 1
            err_kick = io.StringIO()

            def fake_mac_kick_fail(cmd, **kwargs):
                if cmd[:2] == ["launchctl", "kickstart"]:
                    return DummyProc(3, err="No such process")
                return DummyProc(0)

            with mock.patch.object(mod, "launchd_plist_path", return_value=plist_file):
                with mock.patch.object(mod.subprocess, "run", side_effect=fake_mac_kick_fail):
                    with redirect_stderr(err_kick):
                        rc_kf, _, _ = mod._install_macos(1800, "all", st_dir, agy_bin=None)
            self.assertEqual(rc_kf, 1)
            self.assertIn("launchctl kickstart failed", err_kick.getvalue())

            # Linux cron fallback includes --min-uptime 300; systemd install prints min-uptime=300s
            with mock.patch.dict(
                os.environ,
                {"AGY_QUOTA_WAKEUP_STATE_DIR": str(st_dir), "XDG_CONFIG_HOME": str(xdg_dir)},
            ):
                err_cron = io.StringIO()
                with mock.patch.object(mod.platform, "system", return_value="Linux"):
                    with mock.patch.object(mod, "systemd_user_available", return_value=(False, "no bus")):
                        with redirect_stdout(io.StringIO()), redirect_stderr(err_cron):
                            rc_cron = mod.main(["install", "--agy-bin", str(fake_agy)])
                self.assertEqual(rc_cron, 1)
                self.assertIn("crontab -e", err_cron.getvalue())
                self.assertIn("--min-uptime 300", err_cron.getvalue())

                out_linux = io.StringIO()
                with mock.patch.object(mod.platform, "system", return_value="Linux"):
                    with mock.patch.object(mod, "systemd_user_available", return_value=(True, "")):
                        with mock.patch.object(mod, "ensure_linux_linger", return_value="enabled"):
                            with mock.patch.object(
                                mod.subprocess,
                                "run",
                                side_effect=lambda cmd, **kw: DummyProc(
                                    0, out=("1.5.11\n" if len(cmd) >= 2 and cmd[1] == "--version" else "")
                                ),
                            ):
                                with redirect_stdout(out_linux):
                                    rc_lin = mod.main(["install", "--agy-bin", str(fake_agy)])
                self.assertEqual(rc_lin, 0)
                self.assertIn("min-uptime=300s", out_linux.getvalue())
                self.assertNotIn("Persistent=true", out_linux.getvalue())

    def test_system_uptime_parsing_linux_and_macos(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            uptime_file = Path(td) / "uptime"
            with mock.patch.object(mod.sys, "platform", "linux"):
                with mock.patch.object(mod, "_PROC_UPTIME_PATH", str(uptime_file)):
                    uptime_file.write_text("123.45 999.00\n", encoding="utf-8")
                    self.assertAlmostEqual(mod.get_system_uptime_seconds(), 123.45, places=2)

                    uptime_file.write_text("0.00 0.00\n", encoding="utf-8")
                    self.assertEqual(mod.get_system_uptime_seconds(), 0.0)

                    for bad in ("", "   \n", "-5.0 1.0\n", "nan 1.0\n", "inf 1.0\n", "notanumber 1.0\n"):
                        uptime_file.write_text(bad, encoding="utf-8")
                        self.assertIsNone(mod.get_system_uptime_seconds(), f"expected None for {bad!r}")

                    uptime_file.unlink()
                    self.assertIsNone(mod.get_system_uptime_seconds())

        class DummyProc:
            def __init__(self, rc: int, out: str = "") -> None:
                self.returncode = rc
                self.stdout = out
                self.stderr = ""

        with mock.patch.object(mod.sys, "platform", "darwin"):
            with mock.patch.object(mod.time, "time", return_value=1795444191.913346):
                sample = "{ sec = 1795444091, usec = 413346 } Mon Nov 23 22:28:11 2026\n"
                with mock.patch.object(mod.shutil, "which", return_value=None):
                    with mock.patch.object(mod.subprocess, "run", return_value=DummyProc(0, sample)) as mock_run:
                        self.assertAlmostEqual(mod.get_system_uptime_seconds(), 100.5, places=3)
                        self.assertEqual(mock_run.call_args[0][0], ["/usr/sbin/sysctl", "-n", "kern.boottime"])
                        self.assertEqual(mock_run.call_args[1]["timeout"], 5)

                with mock.patch.object(mod.shutil, "which", return_value="/custom/bin/sysctl"):
                    with mock.patch.object(mod.subprocess, "run", return_value=DummyProc(0, sample)) as mock_run2:
                        self.assertAlmostEqual(mod.get_system_uptime_seconds(), 100.5, places=3)
                        self.assertEqual(mock_run2.call_args[0][0], ["/custom/bin/sysctl", "-n", "kern.boottime"])

                # Without usec
                with mock.patch.object(mod.subprocess, "run", return_value=DummyProc(0, "{ sec = 1795444091 }")):
                    self.assertAlmostEqual(mod.get_system_uptime_seconds(), 100.913346, places=3)

                # Clock rollback (boot_ts in future -> negative uptime) returns None
                future = "{ sec = 1795445000, usec = 0 } Mon Nov 23 22:43:20 2026\n"
                with mock.patch.object(mod.subprocess, "run", return_value=DummyProc(0, future)):
                    self.assertIsNone(mod.get_system_uptime_seconds())

                for bad_proc in (
                    DummyProc(1, sample),
                    DummyProc(0, "unexpected format"),
                    DummyProc(0, "{ sec = 0, usec = 0 }"),
                    DummyProc(0, "{ sec = 1795444091, usec = 1500000 }"),
                ):
                    with mock.patch.object(mod.subprocess, "run", return_value=bad_proc):
                        self.assertIsNone(mod.get_system_uptime_seconds())

                with mock.patch.object(
                    mod.subprocess, "run", side_effect=subprocess.TimeoutExpired(["sysctl"], 5)
                ):
                    self.assertIsNone(mod.get_system_uptime_seconds())

        with mock.patch.object(mod.sys, "platform", "win32"):
            self.assertIsNone(mod.get_system_uptime_seconds())

    def test_wait_for_min_uptime_branches_and_run_ordering_and_version_timeout(self) -> None:
        # 1) min_uptime <= 0 skips reading OS uptime and sleeping
        with mock.patch.object(mod, "get_system_uptime_seconds") as mock_up:
            with mock.patch.object(mod.time, "sleep") as mock_sleep:
                self.assertEqual(mod.wait_for_min_uptime(0), 0.0)
                self.assertEqual(mod.wait_for_min_uptime(-5), 0.0)
        mock_up.assert_not_called()
        mock_sleep.assert_not_called()

        # 2) Uptime below threshold sleeps remaining seconds and logs to stderr unless quiet
        err_buf = io.StringIO()
        with mock.patch.object(mod, "get_system_uptime_seconds", return_value=42.5):
            with mock.patch.object(mod.time, "sleep") as mock_sleep:
                with redirect_stderr(err_buf):
                    slept = mod.wait_for_min_uptime(300, quiet=False)
        self.assertAlmostEqual(slept, 257.5, places=3)
        mock_sleep.assert_called_once_with(257.5)
        self.assertIn("waiting 257.5s", err_buf.getvalue())

        err_quiet = io.StringIO()
        with mock.patch.object(mod, "get_system_uptime_seconds", return_value=100.0):
            with mock.patch.object(mod.time, "sleep") as mock_sleep_q:
                with redirect_stderr(err_quiet):
                    slept_q = mod.wait_for_min_uptime(300, quiet=True)
        self.assertAlmostEqual(slept_q, 200.0, places=3)
        mock_sleep_q.assert_called_once_with(200.0)
        self.assertEqual(err_quiet.getvalue(), "")

        # 3) Uptime >= threshold or unreadable (None) does not sleep
        for val in (300.0, 999.0, None):
            with mock.patch.object(mod, "get_system_uptime_seconds", return_value=val):
                with mock.patch.object(mod.time, "sleep") as mock_sleep_no:
                    self.assertEqual(mod.wait_for_min_uptime(300), 0.0)
            mock_sleep_no.assert_not_called()

        # 4) --min-uptime bounds validation returns exit code 2
        with redirect_stderr(io.StringIO()):
            self.assertEqual(mod.main(["run", "--min-uptime", "-1"]), 2)
            self.assertEqual(mod.main(["run", "--min-uptime", "3601"]), 2)

        # 5) ensure_min_agy_version uses VERSION_CHECK_TIMEOUT_SECONDS (45s)
        self.assertEqual(mod.VERSION_CHECK_TIMEOUT_SECONDS, 45)
        captured_timeout = []

        def fake_ver_run(cmd, **kwargs):
            captured_timeout.append(kwargs.get("timeout"))
            return subprocess.CompletedProcess(cmd, 0, "1.5.11\n", "")

        with mock.patch.object(mod.subprocess, "run", side_effect=fake_ver_run):
            mod.ensure_min_agy_version("/usr/bin/agy", {"PATH": "/usr/bin"})
        self.assertEqual(captured_timeout, [45])

        # 6) execute_run invokes wait_for_min_uptime BEFORE flock, version check, and duration_ms timer
        with tempfile.TemporaryDirectory() as td:
            d = Path(td).resolve()
            st_dir = d / "state"
            fake_agy = d / "agy"
            fake_agy.write_text("#!/bin/sh\necho '1.5.11'\n", encoding="utf-8")
            fake_agy.chmod(0o755)

            events = []
            mono_clock = [1000.0]

            def fake_monotonic():
                return mono_clock[0]

            def fake_wait(min_s, quiet=False):
                events.append(("wait", min_s, quiet))
                mono_clock[0] += 120.0
                return 120.0

            orig_flock = mod.fcntl.flock

            def spy_flock(fd, op):
                events.append(("flock",))
                return orig_flock(fd, op)

            def spy_version(abin, env):
                events.append(("version",))
                mono_clock[0] += 0.25

            active_usage = {
                "status": "SUCCESS",
                "command": {
                    "name": "usage",
                    "data": {
                        "groups": [
                            {
                                "name": "Gemini Models",
                                "buckets": [
                                    {
                                        "id": "gemini-weekly",
                                        "window": "weekly",
                                        "remaining_fraction": 0.9,
                                        "reset_time": "2026-11-25T02:02:41Z",
                                    }
                                ],
                            }
                        ]
                    },
                },
            }
            out_buf = io.StringIO()
            with mock.patch.dict(os.environ, {"AGY_QUOTA_WAKEUP_STATE_DIR": str(st_dir)}):
                with mock.patch.object(mod.time, "monotonic", side_effect=fake_monotonic):
                    with mock.patch.object(mod, "wait_for_min_uptime", side_effect=fake_wait):
                        with mock.patch.object(mod.fcntl, "flock", side_effect=spy_flock):
                            with mock.patch.object(mod, "ensure_min_agy_version", side_effect=spy_version):
                                with mock.patch.object(
                                    mod, "fetch_usage_with_retries", return_value=(active_usage, 1)
                                ):
                                    with redirect_stdout(out_buf):
                                        rc = mod.main(
                                            [
                                                "run",
                                                "--pools",
                                                "gemini",
                                                "--min-uptime",
                                                "300",
                                                "--agy-bin",
                                                str(fake_agy),
                                            ]
                                        )
            self.assertEqual(rc, 0)
            self.assertEqual([e[0] for e in events], ["wait", "flock", "version"])
            self.assertEqual(events[0], ("wait", 300, False))
            rec = json.loads(out_buf.getvalue().strip())
            self.assertEqual(rec["status"], "ok")
            self.assertEqual(rec["duration_ms"], 250)

    def test_parse_iso_utc_and_zero_sentinel_full_branch_coverage(self) -> None:
        # 1) Valid RFC 3339 variants: separators, fractional lengths, timezone forms
        valid_cases = [
            ("2026-11-25T02:02:41Z", dt.datetime(2026, 11, 25, 2, 2, 41, tzinfo=dt.timezone.utc)),
            ("2026-11-25t02:02:41z", dt.datetime(2026, 11, 25, 2, 2, 41, tzinfo=dt.timezone.utc)),
            ("2026-11-25 02:02:41", dt.datetime(2026, 11, 25, 2, 2, 41, tzinfo=dt.timezone.utc)),
            ("  2026-11-25T02:02:41Z  ", dt.datetime(2026, 11, 25, 2, 2, 41, tzinfo=dt.timezone.utc)),
            ("2026-11-25T02:02:41.5Z", dt.datetime(2026, 11, 25, 2, 2, 41, 500000, tzinfo=dt.timezone.utc)),
            ("2026-11-25T02:02:41.12Z", dt.datetime(2026, 11, 25, 2, 2, 41, 120000, tzinfo=dt.timezone.utc)),
            ("2026-11-25T02:02:41.123Z", dt.datetime(2026, 11, 25, 2, 2, 41, 123000, tzinfo=dt.timezone.utc)),
            ("2026-11-25T02:02:41.123456Z", dt.datetime(2026, 11, 25, 2, 2, 41, 123456, tzinfo=dt.timezone.utc)),
            ("2026-11-25T02:02:41.123456789Z", dt.datetime(2026, 11, 25, 2, 2, 41, 123456, tzinfo=dt.timezone.utc)),
            ("2026-11-25T10:02:41+08:00", dt.datetime(2026, 11, 25, 2, 2, 41, tzinfo=dt.timezone.utc)),
            ("2026-11-25T10:02:41+0800", dt.datetime(2026, 11, 25, 2, 2, 41, tzinfo=dt.timezone.utc)),
            ("2026-11-24T18:32:41-07:30", dt.datetime(2026, 11, 25, 2, 2, 41, tzinfo=dt.timezone.utc)),
            ("2026-11-24T18:32:41.7-0730", dt.datetime(2026, 11, 25, 2, 2, 41, 700000, tzinfo=dt.timezone.utc)),
            ("2024-02-29T12:00:00-00:00", dt.datetime(2024, 2, 29, 12, 0, 0, tzinfo=dt.timezone.utc)),
            ("2026-11-25T00:00:00+23:59", dt.datetime(2026, 11, 24, 0, 1, 0, tzinfo=dt.timezone.utc)),
            ("2026-11-25T00:00:00-23:59", dt.datetime(2026, 11, 25, 23, 59, 0, tzinfo=dt.timezone.utc)),
        ]
        for raw, expected in valid_cases:
            parsed = mod.parse_iso_utc(raw)
            self.assertEqual(parsed, expected, f"raw={raw!r}")
            self.assertFalse(mod.is_zero_reset_sentinel(parsed), f"raw={raw!r}")

        # 2) Zero / epoch sentinels & year <= 2 underflow protection
        sentinel_cases = [
            "1970-01-01T00:00:00Z",
            "1970-01-01T08:00:00+08:00",
            "1969-12-31T19:00:00-05:00",
            "1970-01-02T00:00:00Z",  # inclusive upper bound
            "1960-05-01T00:00:00Z",
            "0001-01-01T00:00:00Z",
            "0001-01-01T00:00:00+08:00",  # would underflow without year <= 2 guard
            "0002-01-01T00:00:00+08:00",
        ]
        for raw in sentinel_cases:
            parsed = mod.parse_iso_utc(raw)
            self.assertIsNotNone(parsed, f"raw={raw!r}")
            self.assertTrue(mod.is_zero_reset_sentinel(parsed), f"raw={raw!r}")

        self.assertEqual(
            mod.parse_iso_utc("0001-01-01T00:00:00+08:00"),
            dt.datetime(1, 1, 1, tzinfo=dt.timezone.utc),
        )
        just_after_sentinel = mod.parse_iso_utc("1970-01-02T00:00:01Z")
        self.assertIsNotNone(just_after_sentinel)
        self.assertFalse(mod.is_zero_reset_sentinel(just_after_sentinel))
        self.assertFalse(mod.is_zero_reset_sentinel(None))

        # 3) Overflow on year 9999 + invalid/malformed inputs return None
        invalid_cases = [
            None,
            123,
            True,
            [],
            {},
            "",
            "   ",
            "2026-11-25",
            "2026-11-25T02:02Z",
            "2026-11-25  02:02:41Z",
            "2026-11-25\t02:02:41Z",
            "2026-11-25T02:02:41 Z",
            "2026-11-25T02:02:41.Z",
            "2026-11-25T02:02:41Z garbage",
            "2026-11-25T02:02:41Z\nmore",
            "２０２６-11-25T02:02:41Z",
            "2026-02-30T00:00:00Z",
            "2026-13-01T00:00:00Z",
            "2026-11-25T24:00:00Z",
            "2026-11-25T02:60:00Z",
            "0000-01-01T00:00:00Z",
            "2026-11-25T02:02:41+24:00",
            "2026-11-25T02:02:41-24:00",
            "2026-11-25T02:02:41+25:00",
            "2026-11-25T02:02:41+08:60",
            "2026-11-25T02:02:41-01:99",
            "2026-11-25T02:02:41+08",
            "9999-12-31T23:59:59-08:00",  # UTC conversion overflow
        ]
        for raw in invalid_cases:
            self.assertIsNone(mod.parse_iso_utc(raw), f"expected None for {raw!r}")

    def test_evaluate_bucket_activity_segments_and_validation(self) -> None:
        now_utc = dt.datetime(2026, 11, 25, 12, 0, 0, tzinfo=dt.timezone.utc)

        # Segment 1: zero/epoch sentinels -> unactivated_zero_reset with clean None metadata
        for zero_rt in (
            "1970-01-01T00:00:00Z",
            "1970-01-01T08:00:00+08:00",
            "1969-12-31T19:00:00-05:00",
            "0001-01-01T00:00:00Z",
            "0001-01-01T00:00:00+08:00",
        ):
            active, reason, meta = mod.evaluate_bucket_activity(
                {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 1.0, "reset_time": zero_rt},
                now_utc,
            )
            self.assertFalse(active, f"zero_rt={zero_rt!r}")
            self.assertEqual(reason, "unactivated_zero_reset", f"zero_rt={zero_rt!r}")
            self.assertIsNone(meta["reset_time"], f"zero_rt={zero_rt!r}")
            self.assertIsNone(meta["seconds_until_reset"], f"zero_rt={zero_rt!r}")

        # Segment 2: 1970-01-02 UTC < reset_time <= now_utc -> reset_time_expired_pending
        for expired_rt in (
            "1970-01-02T00:00:01Z",
            "2026-11-25T11:59:59Z",
            "2026-11-25T12:00:00Z",  # inclusive boundary == now_utc
        ):
            active, reason, _ = mod.evaluate_bucket_activity(
                {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 1.0, "reset_time": expired_rt},
                now_utc,
            )
            self.assertTrue(active, f"expired_rt={expired_rt!r}")
            self.assertEqual(reason, "reset_time_expired_pending", f"expired_rt={expired_rt!r}")

        # Segment 3: now_utc < reset_time < now_utc + 7d - grace -> reset_timer_counting_down
        for active_rt in (
            "2026-11-25T12:00:01Z",
            (now_utc + dt.timedelta(seconds=mod.WEEK_SECONDS - mod.COUNTDOWN_GRACE_SECONDS - 1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        ):
            active, reason, _ = mod.evaluate_bucket_activity(
                {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 1.0, "reset_time": active_rt},
                now_utc,
            )
            self.assertTrue(active, f"active_rt={active_rt!r}")
            self.assertEqual(reason, "reset_timer_counting_down", f"active_rt={active_rt!r}")

        # Segment 4: >= now_utc + 7d - grace -> unactivated_100pct
        boundary_rt = (now_utc + dt.timedelta(seconds=mod.WEEK_SECONDS - mod.COUNTDOWN_GRACE_SECONDS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        active4, reason4, _ = mod.evaluate_bucket_activity(
            {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 1.0, "reset_time": boundary_rt},
            now_utc,
        )
        self.assertFalse(active4)
        self.assertEqual(reason4, "unactivated_100pct")

        # Exhausted omitted remaining_fraction WITH zero sentinel must still be exhausted_0pct (never wake)
        active_ex, reason_ex, meta_ex = mod.evaluate_bucket_activity(
            {"id": "3p-weekly", "window": "weekly", "reset_time": "1970-01-01T00:00:00Z"},
            now_utc,
        )
        self.assertTrue(active_ex)
        self.assertEqual(reason_ex, "exhausted_0pct")
        self.assertIsNone(meta_ex["reset_time"])
        self.assertEqual(meta_ex["remaining_pct"], 0.0)

        # Explicit 0.0 remaining_fraction WITH zero sentinel -> remaining_below_100pct
        active_0, reason_0, _ = mod.evaluate_bucket_activity(
            {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 0.0, "reset_time": "1970-01-01T00:00:00Z"},
            now_utc,
        )
        self.assertTrue(active_0)
        self.assertEqual(reason_0, "remaining_below_100pct")

        # Missing remaining_fraction with neither reset_time nor description raises ValueError
        with self.assertRaises(ValueError):
            mod.evaluate_bucket_activity({"id": "3p-weekly", "window": "weekly"}, now_utc)

        # Invalid remaining_fraction values (bool, NaN, inf, out-of-range) raise ValueError
        for bad_rem in (True, False, float("nan"), float("inf"), float("-inf"), 1.5, -0.1, "not-a-num"):
            with self.assertRaises(ValueError, msg=f"bad_rem={bad_rem!r}"):
                mod.evaluate_bucket_activity(
                    {
                        "id": "3p-weekly",
                        "window": "weekly",
                        "remaining_fraction": bad_rem,
                        "reset_time": "1970-01-01T00:00:00Z",
                    },
                    now_utc,
                )

    def test_state_cooldown_and_pinned_guards_for_zero_and_legacy_dirty_state(self) -> None:
        t0 = dt.datetime(2026, 11, 25, 10, 0, 0, tzinfo=dt.timezone.utc)
        zero_bucket = {
            "id": "3p-weekly",
            "window": "weekly",
            "remaining_fraction": 1.0,
            "reset_time": "1970-01-01T00:00:00Z",
        }
        drift_rt = (t0 + dt.timedelta(days=7, minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        drift_bucket = {
            "id": "3p-weekly",
            "window": "weekly",
            "remaining_fraction": 1.0,
            "reset_time": drift_rt,
        }

        # 1) Unverified cooldown applies to zero-sentinel buckets (<2h skip, >=2h retry)
        unverified_state = {
            "last_probe_epoch": t0.timestamp(),
            "locked_reset_time": None,
            "verified_active": False,
        }
        act1, rsn1, _ = mod.evaluate_bucket_activity(zero_bucket, t0 + dt.timedelta(hours=1), unverified_state)
        self.assertTrue(act1)
        self.assertEqual(rsn1, "unverified_probe_cooldown")

        act2, rsn2, _ = mod.evaluate_bucket_activity(zero_bucket, t0 + dt.timedelta(hours=2, seconds=1), unverified_state)
        self.assertFalse(act2)
        self.assertEqual(rsn2, "unactivated_zero_reset")

        # 2) Legacy dirty state.json (verified_active=True + zero locked_reset_time) triggers 2h cooldown
        dirty_legacy_state = {
            "last_probe_epoch": t0.timestamp(),
            "locked_reset_time": "1970-01-01T00:00:00Z",
            "verified_active": True,
        }
        act_d1, rsn_d1, _ = mod.evaluate_bucket_activity(zero_bucket, t0 + dt.timedelta(minutes=3), dirty_legacy_state)
        self.assertTrue(act_d1)
        self.assertEqual(rsn_d1, "unverified_probe_cooldown")

        act_d2, rsn_d2, _ = mod.evaluate_bucket_activity(drift_bucket, t0 + dt.timedelta(minutes=3), dirty_legacy_state)
        self.assertTrue(act_d2)
        self.assertEqual(rsn_d2, "unverified_probe_cooldown")

        # 3) pinned_reset_time_just_activated rejects verified_active=False even if locked_reset_time matches
        unverified_matching_state = {
            "last_probe_epoch": t0.timestamp(),
            "locked_reset_time": drift_rt,
            "verified_active": False,
        }
        act_p, rsn_p, _ = mod.evaluate_bucket_activity(drift_bucket, t0 + dt.timedelta(minutes=2), unverified_matching_state)
        self.assertTrue(act_p)
        self.assertEqual(rsn_p, "unverified_probe_cooldown")

    def test_execute_run_zero_sentinel_pre_persist_recheck_and_cross_mode(self) -> None:
        def make_usage(reset_time: str, rem: float = 1.0, pool: str = "3p") -> dict:
            gid = "3p-weekly" if pool == "3p" else "gemini-weekly"
            gname = "Claude and GPT models" if pool == "3p" else "Gemini Models"
            return {
                "status": "SUCCESS",
                "command": {
                    "name": "usage",
                    "data": {
                        "groups": [
                            {
                                "name": gname,
                                "buckets": [
                                    {
                                        "id": gid,
                                        "window": "weekly",
                                        "remaining_fraction": rem,
                                        "reset_time": reset_time,
                                    }
                                ],
                            }
                        ]
                    },
                },
            }

        ok_probe = ({"status": "SUCCESS", "response": "OK", "usage": {"total_tokens": 500}}, "chat_only", None)

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            fake_agy = d / "agy"
            fake_agy.write_text("#!/bin/sh\necho '1.5.11'\n", encoding="utf-8")
            fake_agy.chmod(0o755)

            zero_u = make_usage("1970-01-01T00:00:00Z")

            # Case 1: Pre-send state persistence: even if send_wakeup_request raises/times out, state.json has verified_active=False
            st1 = d / "st1"
            with mock.patch.dict(os.environ, {"AGY_QUOTA_WAKEUP_STATE_DIR": str(st1)}):
                with mock.patch.object(mod.time, "sleep"):
                    with mock.patch.object(mod, "fetch_usage_with_retries", side_effect=[(zero_u, 1), (zero_u, 1)]):
                        with mock.patch.object(
                            mod, "fetch_available_models", return_value=[("claude-sonnet-5-5-low", "Sonnet Low")]
                        ):
                            with mock.patch.object(
                                mod, "send_wakeup_request", side_effect=subprocess.TimeoutExpired(["agy"], 90)
                            ):
                                with redirect_stderr(io.StringIO()):
                                    rc1 = mod.main(["run", "--pools", "3p", "--agy-bin", str(fake_agy)])
            self.assertEqual(rc1, 1)
            saved1 = mod.load_json_dict(st1 / "state.json")
            self.assertIn("3p", saved1.get("pools", {}))
            self.assertIs(saved1["pools"]["3p"]["verified_active"], False)
            self.assertIsNone(saved1["pools"]["3p"]["locked_reset_time"])

            # Case 2: zero -> zero confirmed -> wakeup -> +1s jumped to future & +4s locked (no 3rd recheck needed)
            st2 = d / "st2"
            now_utc = dt.datetime.now(dt.timezone.utc)
            locked_rt = (now_utc + dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
            locked_u = make_usage(locked_rt)
            out2 = io.StringIO()
            with mock.patch.dict(os.environ, {"AGY_QUOTA_WAKEUP_STATE_DIR": str(st2)}):
                with mock.patch.object(mod.time, "sleep"):
                    with mock.patch.object(
                        mod,
                        "fetch_usage_with_retries",
                        side_effect=[(zero_u, 1), (zero_u, 1), (locked_u, 1), (locked_u, 1)],
                    ) as mock_fetch2:
                        with mock.patch.object(
                            mod, "fetch_available_models", return_value=[("claude-sonnet-5-5-low", "Sonnet Low")]
                        ):
                            with mock.patch.object(mod, "send_wakeup_request", return_value=ok_probe):
                                with redirect_stdout(out2):
                                    rc2 = mod.main(["run", "--pools", "3p", "--agy-bin", str(fake_agy)])
            self.assertEqual(rc2, 0)
            self.assertEqual(mock_fetch2.call_count, 4)
            rec2 = json.loads(out2.getvalue().strip())
            self.assertEqual(rec2["status"], "activated")
            self.assertTrue(rec2["pools"]["3p"]["verified_active_after"])
            self.assertEqual(rec2["pools"]["3p"]["reason_after"], "reset_time_locked")
            saved2 = mod.load_json_dict(st2 / "state.json")
            self.assertIs(saved2["pools"]["3p"]["verified_active"], True)
            self.assertEqual(saved2["pools"]["3p"]["locked_reset_time"], locked_rt)

            # Case 3a: +1s still zero -> +4s jumps to future window -> 3rd conditional recheck locks -> verified_active=True
            st3a = d / "st3a"
            out3a = io.StringIO()
            with mock.patch.dict(os.environ, {"AGY_QUOTA_WAKEUP_STATE_DIR": str(st3a)}):
                with mock.patch.object(mod.time, "sleep"):
                    with mock.patch.object(
                        mod,
                        "fetch_usage_with_retries",
                        side_effect=[(zero_u, 1), (zero_u, 1), (zero_u, 1), (locked_u, 1), (locked_u, 1)],
                    ) as mock_fetch3a:
                        with mock.patch.object(
                            mod, "fetch_available_models", return_value=[("claude-sonnet-5-5-low", "Sonnet Low")]
                        ):
                            with mock.patch.object(mod, "send_wakeup_request", return_value=ok_probe):
                                with redirect_stdout(out3a):
                                    rc3a = mod.main(["run", "--pools", "3p", "--agy-bin", str(fake_agy)])
            self.assertEqual(rc3a, 0)
            self.assertEqual(mock_fetch3a.call_count, 5)
            rec3a = json.loads(out3a.getvalue().strip())
            self.assertEqual(rec3a["status"], "activated")
            self.assertEqual(rec3a["pools"]["3p"]["reason_after"], "reset_time_locked")
            saved3a = mod.load_json_dict(st3a / "state.json")
            self.assertIs(saved3a["pools"]["3p"]["verified_active"], True)
            self.assertEqual(saved3a["pools"]["3p"]["locked_reset_time"], locked_rt)

            # Case 3b: +1s still zero -> +4s jumps to future window -> 3rd recheck keeps drifting -> verified_active=False
            st3b = d / "st3b"
            drifted_rt = (now_utc + dt.timedelta(days=7, seconds=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
            drifted_u = make_usage(drifted_rt)
            err3b = io.StringIO()
            with mock.patch.dict(os.environ, {"AGY_QUOTA_WAKEUP_STATE_DIR": str(st3b)}):
                with mock.patch.object(mod.time, "sleep"):
                    with mock.patch.object(
                        mod,
                        "fetch_usage_with_retries",
                        side_effect=[(zero_u, 1), (zero_u, 1), (zero_u, 1), (locked_u, 1), (drifted_u, 1)],
                    ):
                        with mock.patch.object(
                            mod, "fetch_available_models", return_value=[("claude-sonnet-5-5-low", "Sonnet Low")]
                        ):
                            with mock.patch.object(mod, "send_wakeup_request", return_value=ok_probe):
                                with redirect_stderr(err3b):
                                    rc3b = mod.main(["run", "--pools", "3p", "--agy-bin", str(fake_agy)])
            self.assertEqual(rc3b, 1)
            rec3b = json.loads(err3b.getvalue().strip())
            self.assertEqual(rec3b["status"], "partial_error")
            self.assertFalse(rec3b["pools"]["3p"]["verified_active_after"])
            saved3b = mod.load_json_dict(st3b / "state.json")
            self.assertIs(saved3b["pools"]["3p"]["verified_active"], False)
            self.assertIsNone(saved3b["pools"]["3p"]["locked_reset_time"])

            # Case 4: +1s returns reset_time_expired_pending -> MUST NOT short-circuit -> runs +4s recheck -> still unverified
            st4 = d / "st4"
            expired_rt = (now_utc - dt.timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
            expired_u = make_usage(expired_rt)
            err4 = io.StringIO()
            with mock.patch.dict(os.environ, {"AGY_QUOTA_WAKEUP_STATE_DIR": str(st4)}):
                with mock.patch.object(mod.time, "sleep"):
                    with mock.patch.object(
                        mod,
                        "fetch_usage_with_retries",
                        side_effect=[(zero_u, 1), (zero_u, 1), (expired_u, 1), (zero_u, 1)],
                    ) as mock_fetch4:
                        with mock.patch.object(
                            mod, "fetch_available_models", return_value=[("claude-sonnet-5-5-low", "Sonnet Low")]
                        ):
                            with mock.patch.object(mod, "send_wakeup_request", return_value=ok_probe):
                                with redirect_stderr(err4):
                                    rc4 = mod.main(["run", "--pools", "3p", "--agy-bin", str(fake_agy)])
            self.assertEqual(rc4, 1)
            self.assertEqual(mock_fetch4.call_count, 4)
            saved4 = mod.load_json_dict(st4 / "state.json")
            self.assertIs(saved4["pools"]["3p"]["verified_active"], False)
            self.assertIsNone(saved4["pools"]["3p"]["locked_reset_time"])

            # Case 5: Cross-mode switches in 2.2s window (zero->drift & drift->zero) skip conservatively
            st5 = d / "st5"
            for first_u, second_u, expected_reason in (
                (zero_u, locked_u, "reset_time_already_locked"),
                (locked_u, zero_u, "reset_time_mode_changed"),
            ):
                out5 = io.StringIO()
                with mock.patch.dict(os.environ, {"AGY_QUOTA_WAKEUP_STATE_DIR": str(st5)}):
                    with mock.patch.object(mod.time, "sleep"):
                        with mock.patch.object(
                            mod, "fetch_usage_with_retries", side_effect=[(first_u, 1), (second_u, 1)]
                        ):
                            with mock.patch.object(mod, "send_wakeup_request") as mock_send5:
                                with redirect_stdout(out5):
                                    rc5 = mod.main(["run", "--pools", "3p", "--agy-bin", str(fake_agy)])
                self.assertEqual(rc5, 0)
                mock_send5.assert_not_called()
                rec5 = json.loads(out5.getvalue().strip())
                self.assertEqual(rec5["pools"]["3p"]["reason"], expected_reason)

            # Case 6: Far-future drifting timestamp (> now + 7d + 600s) is rejected as reset_time_out_of_range
            st6 = d / "st6"
            far1 = make_usage("2099-01-01T00:00:00Z")
            far2 = make_usage("2099-01-01T00:00:03Z")
            out6 = io.StringIO()
            with mock.patch.dict(os.environ, {"AGY_QUOTA_WAKEUP_STATE_DIR": str(st6)}):
                with mock.patch.object(mod.time, "sleep"):
                    with mock.patch.object(mod, "fetch_usage_with_retries", side_effect=[(far1, 1), (far2, 1)]):
                        with mock.patch.object(mod, "send_wakeup_request") as mock_send6:
                            with redirect_stdout(out6):
                                rc6 = mod.main(["run", "--pools", "3p", "--agy-bin", str(fake_agy)])
            self.assertEqual(rc6, 0)
            mock_send6.assert_not_called()
            rec6 = json.loads(out6.getvalue().strip())
            self.assertEqual(rec6["pools"]["3p"]["reason"], "reset_time_out_of_range")

    def test_format_record_line_and_status_probe_labels(self) -> None:
        # 1) unactivated_zero_reset displays `unactivated` (both None and legacy 1970 string)
        rec_new_zero = {
            "ts": "2026-11-25T12:00:00+08:00",
            "status": "dry_run",
            "duration_ms": 50,
            "pools": {
                "3p": {
                    "remaining_pct": 100.0,
                    "reset_time": None,
                    "reason": "unactivated_zero_reset",
                    "action": "dry_run_would_wake",
                }
            },
        }
        self.assertIn("3p:dry_run_would_wake(100.0%@unactivated)", mod.format_record_line(rec_new_zero))

        rec_legacy_zero = {
            "ts": "2026-11-25T12:00:00+08:00",
            "status": "ok",
            "duration_ms": 50,
            "pools": {
                "3p": {
                    "remaining_pct": 100.0,
                    "reset_time": "1970-01-01T08:00:00+08:00",
                    "reason": "unactivated_zero_reset",
                    "action": "none",
                }
            },
        }
        self.assertIn("3p:100.0%@unactivated", mod.format_record_line(rec_legacy_zero))

        # 2) exhausted_0pct / missing_reset_time / legacy non-zero reason with sentinel displays `-`
        rec_exhausted_and_missing = {
            "ts": "2026-11-25T12:00:00+08:00",
            "status": "ok",
            "duration_ms": 50,
            "pools": {
                "gemini": {
                    "remaining_pct": 0.0,
                    "reset_time": "1970-01-01T08:00:00+08:00",
                    "reason": "exhausted_0pct",
                    "action": "none",
                },
                "3p": {
                    "remaining_pct": 100.0,
                    "reset_time": "",
                    "reason": "missing_reset_time",
                    "action": "none",
                },
            },
        }
        line_em = mod.format_record_line(rec_exhausted_and_missing)
        self.assertIn("gemini:0.0%@-", line_em)
        self.assertIn("3p:100.0%@-", line_em)

        # 3) Paired reset_time_after + reason_after reading on probed records
        rec_probed_missing_after = {
            "ts": "2026-11-25T12:00:00+08:00",
            "status": "partial_error",
            "duration_ms": 1200,
            "pools": {
                "3p": {
                    "remaining_pct": 100.0,
                    "reset_time": None,
                    "reason": "unactivated_zero_reset",
                    "action": "probed",
                    "probe": {"model": "claude-sonnet-5-5-low", "mode": "chat_only"},
                    "verified_active_after": False,
                    "remaining_pct_after": 100.0,
                    "reset_time_after": None,
                    "reason_after": "missing_reset_time",
                }
            },
        }
        self.assertIn(
            "3p:WAKE_SENT(claude-sonnet-5-5-low,verified=False)->100.0%@-",
            mod.format_record_line(rec_probed_missing_after),
        )

        # 4) execute_status prints `Wakeup probes:` and `Last probe:`
        with tempfile.TemporaryDirectory() as td:
            st_dir = Path(td)
            mod.append_jsonl_with_rotation(st_dir / "wakeup.jsonl", rec_probed_missing_after)
            out_st = io.StringIO()
            with mock.patch.dict(os.environ, {"AGY_QUOTA_WAKEUP_STATE_DIR": str(st_dir)}):
                with mock.patch.object(mod.platform, "system", return_value="OtherOS"):
                    with redirect_stdout(out_st):
                        rc_st = mod.main(["status"])
            self.assertEqual(rc_st, 0)
            st_text = out_st.getvalue()
            self.assertIn("Wakeup probes: 1", st_text)
            self.assertIn("Last probe:", st_text)
            self.assertNotIn("Last activation:", st_text)

    def test_verified_true_with_none_locked_reset_triggers_unverified_cooldown(self) -> None:
        t0 = dt.datetime(2026, 11, 25, 10, 0, 0, tzinfo=dt.timezone.utc)
        dirty_none_state = {
            "last_probe_epoch": t0.timestamp(),
            "locked_reset_time": None,
            "verified_active": True,
        }
        zero_bucket = {
            "id": "3p-weekly",
            "window": "weekly",
            "remaining_fraction": 1.0,
            "reset_time": "1970-01-01T00:00:00Z",
        }
        check_in = t0 + dt.timedelta(minutes=30)
        drift_bucket = {
            "id": "3p-weekly",
            "window": "weekly",
            "remaining_fraction": 1.0,
            "reset_time": (check_in + dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        for bucket in (zero_bucket, drift_bucket):
            act_in, rsn_in, _ = mod.evaluate_bucket_activity(bucket, check_in, dirty_none_state)
            self.assertTrue(act_in)
            self.assertEqual(rsn_in, "unverified_probe_cooldown")

        act_after, rsn_after, _ = mod.evaluate_bucket_activity(
            zero_bucket, t0 + dt.timedelta(hours=2, seconds=1), dirty_none_state
        )
        self.assertFalse(act_after)
        self.assertEqual(rsn_after, "unactivated_zero_reset")

    def test_duplicate_weekly_buckets_for_same_pool_marked_error_and_skipped(self) -> None:
        dup_usage = {
            "status": "SUCCESS",
            "command": {
                "name": "usage",
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                {
                                    "id": "gemini-weekly",
                                    "window": "weekly",
                                    "remaining_fraction": 0.9,
                                    "reset_time": "2026-12-01T00:00:00Z",
                                }
                            ],
                        },
                        {
                            "name": "Claude and GPT models",
                            "buckets": [
                                {
                                    "id": "3p-weekly",
                                    "window": "weekly",
                                    "remaining_fraction": 1.0,
                                    "reset_time": "1970-01-01T00:00:00Z",
                                },
                                {
                                    "id": "3p-weekly-alt",
                                    "window": "weekly",
                                    "remaining_fraction": 1.0,
                                    "reset_time": "1970-01-01T00:00:00Z",
                                },
                            ],
                        },
                    ]
                },
            },
        }
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            st_dir = d / "state"
            fake_agy = d / "agy"
            fake_agy.write_text("#!/bin/sh\necho '1.5.11'\n", encoding="utf-8")
            fake_agy.chmod(0o755)
            err_buf = io.StringIO()
            with mock.patch.dict(os.environ, {"AGY_QUOTA_WAKEUP_STATE_DIR": str(st_dir)}):
                with mock.patch.object(mod, "fetch_usage_with_retries", return_value=(dup_usage, 1)):
                    with mock.patch.object(mod, "send_wakeup_request") as mock_send:
                        with redirect_stderr(err_buf):
                            rc = mod.main(["run", "--pools", "all", "--agy-bin", str(fake_agy)])
            self.assertEqual(rc, 1)
            mock_send.assert_not_called()
            rec = json.loads(err_buf.getvalue().strip())
            self.assertEqual(rec["status"], "error")
            self.assertEqual(rec["pools"]["gemini"]["action"], "none")
            self.assertEqual(rec["pools"]["3p"]["action"], "error")
            self.assertIn("duplicate weekly buckets", rec["pools"]["3p"]["error"])

    def test_pre_wakeup_bad_fraction_on_one_pool_does_not_block_healthy_pool(self) -> None:
        now_utc = dt.datetime.now(dt.timezone.utc)
        locked_rt = (now_utc + dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def make_both_usage(g_rem: object, g_rt: str, p3_rem: object, p3_rt: str) -> dict:
            return {
                "status": "SUCCESS",
                "command": {
                    "name": "usage",
                    "data": {
                        "groups": [
                            {
                                "name": "Gemini Models",
                                "buckets": [
                                    {
                                        "id": "gemini-weekly",
                                        "window": "weekly",
                                        "remaining_fraction": g_rem,
                                        "reset_time": g_rt,
                                    }
                                ],
                            },
                            {
                                "name": "Claude and GPT models",
                                "buckets": [
                                    {
                                        "id": "3p-weekly",
                                        "window": "weekly",
                                        "remaining_fraction": p3_rem,
                                        "reset_time": p3_rt,
                                    }
                                ],
                            },
                        ]
                    },
                },
            }

        first_u = make_both_usage(1.0, "1970-01-01T00:00:00Z", 1.0, "1970-01-01T00:00:00Z")
        second_u = make_both_usage(1.5, "1970-01-01T00:00:00Z", 1.0, "1970-01-01T00:00:00Z")
        after_u = make_both_usage(1.0, "1970-01-01T00:00:00Z", 1.0, locked_rt)
        ok_probe = ({"status": "SUCCESS", "response": "OK", "usage": {"total_tokens": 500}}, "chat_only", None)

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            st_dir = d / "state"
            fake_agy = d / "agy"
            fake_agy.write_text("#!/bin/sh\necho '1.5.11'\n", encoding="utf-8")
            fake_agy.chmod(0o755)
            err_buf = io.StringIO()
            with mock.patch.dict(os.environ, {"AGY_QUOTA_WAKEUP_STATE_DIR": str(st_dir)}):
                with mock.patch.object(mod.time, "sleep"):
                    with mock.patch.object(
                        mod,
                        "fetch_usage_with_retries",
                        side_effect=[(first_u, 1), (second_u, 1), (after_u, 1), (after_u, 1)],
                    ):
                        with mock.patch.object(
                            mod,
                            "fetch_available_models",
                            return_value=[
                                ("gemini-3.8-flash-low", "Flash Low"),
                                ("claude-sonnet-5-5-low", "Sonnet Low"),
                            ],
                        ):
                            with mock.patch.object(mod, "send_wakeup_request", return_value=ok_probe) as mock_send:
                                with redirect_stderr(err_buf):
                                    rc = mod.main(["run", "--pools", "all", "--agy-bin", str(fake_agy)])
            self.assertEqual(rc, 1)
            self.assertEqual(mock_send.call_count, 1)
            self.assertEqual(mock_send.call_args[0][1], "claude-sonnet-5-5-low")
            rec = json.loads(err_buf.getvalue().strip())
            self.assertEqual(rec["status"], "partial_error")
            self.assertEqual(rec["pools"]["gemini"]["action"], "error")
            self.assertIn("out-of-range", rec["pools"]["gemini"]["error"])
            self.assertEqual(rec["pools"]["3p"]["action"], "probed")
            self.assertTrue(rec["pools"]["3p"]["verified_active_after"])


if __name__ == "__main__":
    unittest.main()
