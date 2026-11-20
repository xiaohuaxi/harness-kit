#!/usr/bin/env python3
"""Unit tests for codex-quota-wakeup."""

from __future__ import annotations

import datetime as dt
import fcntl
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

SCRIPT_PATH = Path(__file__).resolve().parent / "codex-quota-wakeup"
loader = importlib.machinery.SourceFileLoader("codex_quota_wakeup", str(SCRIPT_PATH))
spec = importlib.util.spec_from_loader("codex_quota_wakeup", loader)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


SAMPLE_DUAL_WINDOW_RL = {
    "accountId": "acct-alpha",
    "ordinaryUsageAllowed": True,
    "rateLimits": {
        "limitId": "codex",
        "planType": "pro",
        "primary": {
            "usedPercent": 15,
            "windowDurationMins": 300,
            "resetsAt": 1795010000,
        },
        "secondary": {
            "usedPercent": 0,
            "windowDurationMins": 10080,
            "resetsAt": 1795604800,
        },
        "spendControlReached": False,
        "rateLimitReachedType": None,
    },
    "rateLimitsByLimitId": {
        "codex": {
            "limitId": "codex",
            "planType": "pro",
            "primary": {
                "usedPercent": 15,
                "windowDurationMins": 300,
                "resetsAt": 1795010000,
            },
            "secondary": {
                "usedPercent": 0,
                "windowDurationMins": 10080,
                "resetsAt": 1795604800,
            },
            "spendControlReached": False,
            "rateLimitReachedType": None,
        }
    },
}

SAMPLE_SINGLE_WINDOW_RL = {
    "accountId": "acct-alpha",
    "ordinaryUsageAllowed": True,
    "rateLimits": {
        "limitId": "codex",
        "planType": "prolite",
        "primary": {
            "usedPercent": 6,
            "windowDurationMins": 10080,
            "resetsAt": 1795598255,
        },
        "secondary": None,
        "spendControlReached": False,
        "rateLimitReachedType": None,
    },
}

DEFAULT_CFG = {
    "cli_auth_credentials_store": "file",
    "forced_chatgpt_workspace_id": None,
    "forced_login_method": None,
    "features": {},
}


def _make_drifting_bundle(
    resets_at: int,
    used_pct: int = 0,
    account_id: str = "acct-alpha",
    email: str = "alice@example.com",
    config: dict = None,
    config_layers: list = None,
) -> dict:
    return {
        "account": {"account": {"type": "chatgpt", "email": email, "planType": "pro"}},
        "auth_status": {"authMethod": "chatgpt", "authToken": None, "requiresOpenaiAuth": True},
        "rate_limits": {
            "accountId": account_id,
            "ordinaryUsageAllowed": True,
            "rateLimits": {
                "limitId": "codex",
                "planType": "pro",
                "primary": {
                    "usedPercent": used_pct,
                    "windowDurationMins": 10080,
                    "resetsAt": resets_at,
                },
            },
        },
        "config": dict(DEFAULT_CFG) if config is None else config,
        "config_layers": (
            [{"name": {"type": "user"}, "config": dict(DEFAULT_CFG)}] if config_layers is None else config_layers
        ),
    }


class CodexQuotaWakeupTests(unittest.TestCase):
    def test_extract_weekly_window_single_and_dual_plans(self) -> None:
        w_dual = mod.extract_weekly_window(SAMPLE_DUAL_WINDOW_RL)
        self.assertEqual(w_dual["slot"], "secondary")
        now_utc = dt.datetime.fromtimestamp(1795604800 - mod.WEEK_SECONDS, tz=dt.timezone.utc)
        active_d, reason_d, meta_d = mod.evaluate_weekly_activity(w_dual, now_utc)
        self.assertFalse(active_d)
        self.assertEqual(reason_d, "unactivated_100pct")
        self.assertEqual(meta_d["remaining_pct"], 100.0)

        w_single = mod.extract_weekly_window(SAMPLE_SINGLE_WINDOW_RL)
        self.assertEqual(w_single["slot"], "primary")
        active_s, reason_s, meta_s = mod.evaluate_weekly_activity(w_single, now_utc)
        self.assertTrue(active_s)
        self.assertEqual(reason_s, "used_above_0pct")
        self.assertEqual(meta_s["remaining_pct"], 94.0)

        near_weekly = mod.extract_weekly_window(
            {
                "ordinaryUsageAllowed": True,
                "rateLimits": {
                    "limitId": "codex",
                    "planType": "pro",
                    "primary": {
                        "usedPercent": 0,
                        "windowDurationMins": 10070,
                        "resetsAt": int(now_utc.timestamp()) + 10070 * 60,
                    },
                },
            }
        )
        act_nw, rsn_nw, _ = mod.evaluate_weekly_activity(near_weekly, now_utc)
        self.assertFalse(act_nw)
        self.assertEqual(rsn_nw, "unactivated_100pct")

        five_hour_exhausted = mod.extract_weekly_window(
            {
                "rateLimits": {
                    "limitId": "codex",
                    "planType": "plus",
                    "primary": {
                        "usedPercent": 100,
                        "windowDurationMins": 300,
                        "resetsAt": int(now_utc.timestamp()) + 3600,
                    },
                    "secondary": {
                        "usedPercent": 0,
                        "windowDurationMins": 10080,
                        "resetsAt": int(now_utc.timestamp()) + mod.WEEK_SECONDS,
                    },
                },
            }
        )
        act_5h, rsn_5h, _ = mod.evaluate_weekly_activity(five_hour_exhausted, now_utc)
        self.assertTrue(act_5h)
        self.assertEqual(rsn_5h, "quota_exhausted_or_blocked")

    def test_config_read_auth_overrides_system_layer_guard_and_service_tier(self) -> None:
        parsed_cfg = {
            "cli_auth_credentials_store": "keyring",
            "forced_login_method": "chatgpt",
            "forced_chatgpt_workspace_id": ["ws-team-1", "ws-team-2"],
            "allow_symlinked_codex_home": True,
            "chatgpt_base_url": "https://evil-untrusted.example/backend-api/",
            "features": {"secret_auth_storage": True, "hooks": True},
        }
        overrides = mod.build_auth_overrides_from_config(parsed_cfg)
        self.assertIn('cli_auth_credentials_store="keyring"', overrides)
        self.assertIn("features.secret_auth_storage=true", overrides)
        self.assertFalse(any("allow_symlinked_codex_home" in ov for ov in overrides))
        self.assertFalse(any("forced_chatgpt_workspace_id" in ov for ov in overrides))
        self.assertFalse(any("forced_login_method" in ov for ov in overrides))
        self.assertFalse(any("evil-untrusted" in ov for ov in overrides))

        # User-layer `forced_*`, disabled layers, and compliant Team/Enterprise policies are NOT conflicts!
        self.assertFalse(
            mod.has_non_user_login_restrictions(
                [{"name": {"type": "user"}, "config": {"forced_login_method": "chatgpt"}}],
                active_account_id="ws-team-1",
            )
        )
        self.assertFalse(
            mod.has_non_user_login_restrictions(
                [
                    {
                        "name": {"type": "enterpriseManaged"},
                        "config": {
                            "forced_login_method": "chatgpt",
                            "forced_chatgpt_workspace_id": ["ws-team-1", "ws-team-2"],
                        },
                    },
                    {
                        "name": {"type": "system"},
                        "disabledReason": "stale",
                        "config": {"forced_login_method": "api"},
                    },
                ],
                active_account_id="ws-team-1",
            )
        )
        # Conflicting system `forced_login_method="api"` or mismatched workspace ID IS flagged.
        self.assertTrue(
            mod.has_non_user_login_restrictions(
                [
                    {"name": {"type": "user"}, "config": {"forced_login_method": "chatgpt"}},
                    {"name": {"type": "system"}, "config": {"forced_login_method": "api"}},
                ],
                active_account_id="ws-team-1",
            )
        )
        self.assertTrue(
            mod.has_non_user_login_restrictions(
                [
                    {
                        "name": {"type": "system"},
                        "config": {"forced_chatgpt_workspace_id": "ws-other"},
                    }
                ],
                active_account_id="ws-team-1",
            )
        )

        cmd = mod.build_wakeup_command("/usr/local/bin/codex", None, "low", auth_overrides=overrides)
        self.assertIn(f'chatgpt_base_url="{mod.TRUSTED_CHATGPT_BASE_URL}"', cmd)
        self.assertIn(f'openai_base_url="{mod.TRUSTED_CHATGPT_CODEX_BASE_URL}"', cmd)
        self.assertIn("features.secret_auth_storage=true", cmd)
        self.assertIn('service_tier="default"', cmd)
        self.assertIn("features.fast_mode=false", cmd)
        self.assertIn("features.plugins=false", cmd)
        self.assertNotIn("--model", cmd)
        self.assertNotIn('service_tier="fast"', cmd)

        probe_cmd = mod.build_app_server_probe_cmd("/usr/local/bin/codex")
        self.assertIn(f'chatgpt_base_url="{mod.TRUSTED_CHATGPT_BASE_URL}"', probe_cmd)
        self.assertIn("features.plugins=false", probe_cmd)
        self.assertNotIn("--stdio", probe_cmd)

    def test_live_stdio_rpc_mock_server_handles_minus_32600_and_endpoint_validation(self) -> None:
        mock_server_code = """#!/usr/bin/env python3
import json, os, sys
mode = os.environ.get("MOCK_CODEX_MODE", "ok")
for line in sys.stdin:
    msg = json.loads(line)
    rid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": 999, "method": "mcp/elicitation", "params": {}}) + "\\n")
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"userAgent": "mock"}}) + "\\n")
        sys.stdout.flush()
    elif method == "account/read":
        marker = os.environ.get("MOCK_ACCOUNT_MARKER")
        if marker:
            with open(marker, "a") as mf:
                mf.write("account\\n")
        acct = {"type": "apiKey"} if mode == "apikey" else {"type": "chatgpt", "email": "u@example.com", "planType": "prolite"}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"account": acct, "requiresOpenaiAuth": True}}) + "\\n")
        sys.stdout.flush()
    elif method == "getAuthStatus":
        am = "agentIdentity" if mode == "agent_identity" else ("apikey" if mode == "apikey" else "chatgpt")
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"authMethod": am, "authToken": None, "requiresOpenaiAuth": True}}) + "\\n")
        sys.stdout.flush()
    elif method == "account/rateLimits/read":
        marker = os.environ.get("MOCK_ACCOUNT_MARKER")
        if marker:
            with open(marker, "a") as mf:
                mf.write("ratelimits\\n")
        if msg.get("params") is not None and mode == "invalid_req_32600":
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32600, "message": "Invalid request"}}) + "\\n")
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
                "accountId": "acct-mock",
                "ordinaryUsageAllowed": True,
                "rateLimits": {"limitId": "codex", "planType": "prolite", "primary": {"usedPercent": 5, "windowDurationMins": 10080, "resetsAt": 1800000000}}
            }}) + "\\n")
        sys.stdout.flush()
    elif method == "config/read":
        cfg_ret = {"cli_auth_credentials_store": "file"}
        if mode == "managed_gateway":
            cfg_ret["chatgpt_base_url"] = "https://corp-gateway.example/backend-api/"
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"config": cfg_ret, "origins": {}, "layers": [{"name": {"type": "user"}, "config": {"cli_auth_credentials_store": "file"}}]}}) + "\\n")
        sys.stdout.flush()
"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td).resolve()
            fake_codex = d / "codex"
            fake_codex.write_text(mock_server_code, encoding="utf-8")
            fake_codex.chmod(0o755)

            env = mod.build_clean_env()
            env["MOCK_CODEX_MODE"] = "invalid_req_32600"
            snap = mod.fetch_quota_snapshot(str(fake_codex), env=env)
            self.assertEqual(snap["rate_limits"]["rateLimits"]["primary"]["usedPercent"], 5)
            self.assertEqual(snap["config"]["cli_auth_credentials_store"], "file")
            self.assertFalse(mod.has_non_user_login_restrictions(snap["config_layers"], "acct-mock"))

            env["MOCK_CODEX_MODE"] = "apikey"
            with self.assertRaises(RuntimeError) as ctx:
                mod.fetch_quota_snapshot(str(fake_codex), env=env)
            self.assertIn("apiKey", str(ctx.exception))

            env["MOCK_CODEX_MODE"] = "agent_identity"
            with self.assertRaises(RuntimeError) as ctx_ai:
                mod.fetch_quota_snapshot(str(fake_codex), env=env)
            self.assertIn("agentIdentity", str(ctx_ai.exception))

            marker_file = d / "rpc_calls.log"
            env["MOCK_CODEX_MODE"] = "managed_gateway"
            env["MOCK_ACCOUNT_MARKER"] = str(marker_file)
            with self.assertRaises(RuntimeError) as ctx_gw:
                mod.fetch_quota_snapshot(str(fake_codex), env=env)
            self.assertIn("chatgpt_base_url", str(ctx_gw.exception))
            self.assertFalse(marker_file.exists())

    def test_end_to_end_activation_pre_send_rollback_and_clock_rollback_tolerance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td).resolve()
            st_dir = d / "state"

            fake_codex = d / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "if len(sys.argv) > 1 and sys.argv[1] == '--version':\n"
                "    print('codex-cli 0.170.0')\n"
                "    sys.exit(0)\n"
                "if os.environ.get('FAKE_EXEC_REJECT_ARGS') == '1':\n"
                "    sys.stderr.write('error: bad CLI argument\\n')\n"
                "    sys.exit(2)\n"
                "print(json.dumps({'type': 'thread.started', 'thread_id': 't-live-1'}))\n"
                "print(json.dumps({'type': 'item.completed', 'item': {'id': 'i1', 'type': 'agent_message', 'text': 'OK'}}))\n"
                "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 120, 'cached_input_tokens': 0, 'output_tokens': 4, 'reasoning_output_tokens': 2}}))\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)

            base_ep = int(dt.datetime.now(dt.timezone.utc).timestamp())
            b1 = _make_drifting_bundle(base_ep + mod.WEEK_SECONDS)
            b2 = _make_drifting_bundle(base_ep + mod.WEEK_SECONDS + 3)
            b3 = _make_drifting_bundle(base_ep + mod.WEEK_SECONDS + 6)
            locked_ts = base_ep + mod.WEEK_SECONDS + 7
            b_locked = _make_drifting_bundle(locked_ts)
            b_locked_jitter = _make_drifting_bundle(locked_ts - 1)

            with mock.patch.dict(os.environ, {"CODEX_QUOTA_WAKEUP_STATE_DIR": str(st_dir)}):
                with mock.patch.object(mod.time, "sleep"):
                    # 1) Pre-send exit code 2 rolls back `state.json` cleanly
                    with mock.patch.dict(os.environ, {"FAKE_EXEC_REJECT_ARGS": "1"}):
                        with mock.patch.object(
                            mod, "fetch_quota_with_retries", side_effect=[(b1, 1), (b2, 1), (b3, 1)]
                        ):
                            with redirect_stderr(io.StringIO()):
                                rc_pre = mod.main(["run", "--codex-bin", str(fake_codex)])
                    self.assertEqual(rc_pre, 1)
                    self.assertEqual(mod.load_json_dict(st_dir / "state.json").get("pools"), {})

                    # 2) Dry-run selects `(codex-default)` + `low` without `model/list`
                    dry_out = io.StringIO()
                    with mock.patch.object(
                        mod, "fetch_quota_with_retries", side_effect=[(b1, 1), (b2, 1), (b3, 1)]
                    ):
                        with redirect_stdout(dry_out):
                            rc_dry = mod.main(["run", "--dry-run", "--codex-bin", str(fake_codex)])
                    self.assertEqual(rc_dry, 0)
                    dry_rec = json.loads(dry_out.getvalue())
                    self.assertEqual(dry_rec["pools"]["codex"]["selected_model"], "(codex-default)")
                    self.assertEqual(dry_rec["pools"]["codex"]["selected_effort"], "low")
                    self.assertEqual(dry_rec["pools"]["codex"]["model_source"], "cli_default")

                    # 3) Real activation locks with 1s rounding jitter (`t2 = t1 - 1`)
                    act_out = io.StringIO()
                    with mock.patch.object(
                        mod,
                        "fetch_quota_with_retries",
                        side_effect=[(b1, 1), (b2, 1), (b3, 1), (b_locked, 1), (b_locked_jitter, 1)],
                    ):
                        with redirect_stdout(act_out):
                            rc_ok = mod.main(["run", "--codex-bin", str(fake_codex)])
                    self.assertEqual(rc_ok, 0)
                    rec_ok = json.loads(act_out.getvalue().strip())
                    self.assertEqual(rec_ok["status"], "activated")
                    self.assertEqual(rec_ok["pools"]["codex"]["probe"]["model"], "(codex-default)")
                    self.assertEqual(rec_ok["pools"]["codex"]["probe"]["total_tokens"], 124)
                    self.assertEqual(rec_ok["pools"]["codex"]["reason_after"], "reset_time_locked")

                    # 4) Small backward NTP/WSL2 clock rollback (-30s) still keeps probe history & cooldown
                    st_saved = mod.load_json_dict(st_dir / "state.json")["pools"]["codex"]
                    t_back = float(st_saved["last_probe_epoch"]) - 30.0
                    self.assertEqual(len(mod._recent_probe_epochs(st_saved, t_back)), 1)
                    st_unver = dict(st_saved, verified_active=False, unverified_streak=1)
                    self.assertEqual(
                        mod._check_probe_budget_block(st_unver, t_back, has_explicit_model=False),
                        "unverified_probe_cooldown",
                    )

    def test_fixed_separate_pool_cap_and_account_switch_reset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td).resolve()
            st_dir = d / "state"
            fake_codex = d / "codex"
            fake_codex.write_text("#!/bin/sh\necho 'codex-cli 0.170.0'\n", encoding="utf-8")
            fake_codex.chmod(0o755)

            base_ep = 1800000000
            sent_count = [0]

            def ok_send(*args, **kwargs):
                sent_count[0] += 1
                return ({"thread_id": "t-spark", "response": "OK", "duration_seconds": 1.0, "total_tokens": 80}, "ephemeral_exec")

            with mock.patch.dict(os.environ, {"CODEX_QUOTA_WAKEUP_STATE_DIR": str(st_dir)}):
                with mock.patch.object(mod.time, "sleep"):
                    for hour in (0, 3, 6):
                        cur_ep = base_ep + hour * 3600
                        cur_dt = dt.datetime.fromtimestamp(cur_ep, tz=dt.timezone.utc)
                        b1 = _make_drifting_bundle(cur_ep + mod.WEEK_SECONDS, account_id="acct-A")
                        b2 = _make_drifting_bundle(cur_ep + mod.WEEK_SECONDS + 3, account_id="acct-A")
                        b3 = _make_drifting_bundle(cur_ep + mod.WEEK_SECONDS + 6, account_id="acct-A")
                        b4 = _make_drifting_bundle(cur_ep + mod.WEEK_SECONDS + 8, account_id="acct-A")
                        b5 = _make_drifting_bundle(cur_ep + mod.WEEK_SECONDS + 11, account_id="acct-A")
                        with mock.patch.object(mod.time, "time", return_value=float(cur_ep)):
                            with mock.patch.object(mod.dt, "datetime", wraps=dt.datetime) as mock_dt:
                                mock_dt.now.return_value = cur_dt
                                with mock.patch.object(
                                    mod, "fetch_quota_with_retries", side_effect=[(b1, 1), (b2, 1), (b3, 1), (b4, 1), (b5, 1)]
                                ):
                                    with mock.patch.object(mod, "send_wakeup_request", side_effect=ok_send):
                                        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                                            mod.main(["run", "--model", "codex-spark", "--codex-bin", str(fake_codex)])
                self.assertEqual(sent_count[0], 1)

                # Switching to a DIFFERENT user email inside the SAME workspace (`acct-A`) gets an independent key!
                cur_ep = base_ep + 4 * 3600
                cur_dt = dt.datetime.fromtimestamp(cur_ep, tz=dt.timezone.utc)
                bb1 = _make_drifting_bundle(cur_ep + mod.WEEK_SECONDS, account_id="acct-A", email="bob@example.com")
                bb2 = _make_drifting_bundle(cur_ep + mod.WEEK_SECONDS + 3, account_id="acct-A", email="bob@example.com")
                bb3 = _make_drifting_bundle(cur_ep + mod.WEEK_SECONDS + 6, account_id="acct-A", email="bob@example.com")
                with mock.patch.object(mod.time, "sleep"):
                    with mock.patch.object(mod.time, "time", return_value=float(cur_ep)):
                        with mock.patch.object(mod.dt, "datetime", wraps=dt.datetime) as mock_dt:
                            mock_dt.now.return_value = cur_dt
                            with mock.patch.object(
                                mod, "fetch_quota_with_retries", side_effect=[(bb1, 1), (bb2, 1), (bb3, 1)]
                            ):
                                out_b = io.StringIO()
                                with redirect_stdout(out_b):
                                    rc_b = mod.main(["run", "--dry-run", "--model", "codex-spark", "--codex-bin", str(fake_codex)])
                self.assertEqual(rc_b, 0)
                self.assertEqual(json.loads(out_b.getvalue())["pools"]["codex"]["action"], "dry_run_would_wake")
                # Plaintext email is NEVER stored in state.json
                raw_state_text = (st_dir / "state.json").read_text(encoding="utf-8")
                self.assertNotIn("alice@example.com", raw_state_text)
                self.assertNotIn("bob@example.com", raw_state_text)

                # Mid-run account switch during 3-point drift (`A, A, B`) aborts cleanly without waking!
                cur_ep2 = base_ep + 5 * 3600
                cur_dt2 = dt.datetime.fromtimestamp(cur_ep2, tz=dt.timezone.utc)
                mc1 = _make_drifting_bundle(cur_ep2 + mod.WEEK_SECONDS, account_id="acct-C", email="carol@example.com")
                mc2 = _make_drifting_bundle(cur_ep2 + mod.WEEK_SECONDS + 3, account_id="acct-C", email="carol@example.com")
                md3 = _make_drifting_bundle(cur_ep2 + mod.WEEK_SECONDS + 6, account_id="acct-D", email="dave@example.com")
                with mock.patch.object(mod.time, "sleep"):
                    with mock.patch.object(mod.time, "time", return_value=float(cur_ep2)):
                        with mock.patch.object(mod.dt, "datetime", wraps=dt.datetime) as mock_dt:
                            mock_dt.now.return_value = cur_dt2
                            with mock.patch.object(
                                mod, "fetch_quota_with_retries", side_effect=[(mc1, 1), (mc2, 1), (md3, 1)]
                            ):
                                with mock.patch.object(mod, "send_wakeup_request", side_effect=ok_send):
                                    out_mix = io.StringIO()
                                    with redirect_stdout(out_mix):
                                        rc_mix = mod.main(["run", "--codex-bin", str(fake_codex)])
                self.assertEqual(rc_mix, 0)
                self.assertEqual(sent_count[0], 1)
                self.assertEqual(json.loads(out_mix.getvalue())["pools"]["codex"]["reason"], "account_changed_during_check")

    def test_false_positive_reset_time_locked_remains_strictly_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td).resolve()
            st_dir = d / "state"
            fake_codex = d / "codex"
            fake_codex.write_text("#!/bin/sh\necho 'codex-cli 0.170.0'\n", encoding="utf-8")
            fake_codex.chmod(0o755)

            base_ep = 1810000000
            sent_total = [0]

            def ok_send(*args, **kwargs):
                sent_total[0] += 1
                return ({"thread_id": "t-fp", "response": "OK", "duration_seconds": 1.0, "total_tokens": 90}, "ephemeral_exec")

            with mock.patch.dict(os.environ, {"CODEX_QUOTA_WAKEUP_STATE_DIR": str(st_dir)}):
                with mock.patch.object(mod.time, "sleep"):
                    # Simulate 10 consecutive 30-minute ticks where post-wakeup 2-sample recheck
                    # returns identical `resetsAt` (`reset_time_locked`), yet later ticks keep drifting.
                    for step in range(10):
                        cur_ep = base_ep + step * 1800
                        cur_dt = dt.datetime.fromtimestamp(cur_ep, tz=dt.timezone.utc)
                        b1 = _make_drifting_bundle(cur_ep + mod.WEEK_SECONDS)
                        b2 = _make_drifting_bundle(cur_ep + mod.WEEK_SECONDS + 3)
                        b3 = _make_drifting_bundle(cur_ep + mod.WEEK_SECONDS + 6)
                        fluke_locked = _make_drifting_bundle(cur_ep + mod.WEEK_SECONDS + 6)
                        with mock.patch.object(mod.time, "time", return_value=float(cur_ep)):
                            with mock.patch.object(mod.dt, "datetime", wraps=dt.datetime) as mock_dt:
                                mock_dt.now.return_value = cur_dt
                                with mock.patch.object(
                                    mod,
                                    "fetch_quota_with_retries",
                                    side_effect=[(b1, 1), (b2, 1), (b3, 1), (fluke_locked, 1), (fluke_locked, 1)],
                                ):
                                    with mock.patch.object(mod, "send_wakeup_request", side_effect=ok_send):
                                        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                                            mod.main(["run", "--codex-bin", str(fake_codex)])
            # Preserved `unverified_streak` + rolling 7d cap strictly bound total sends (<= 2)!
            self.assertLessEqual(sent_total[0], 2)

    def test_clean_env_relative_ca_path_and_systemd_uninstall_guard(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td).resolve()
            st_dir = d / "state"
            xdg_dir = d / "xdg"
            fake_codex = d / "codex"
            fake_codex.write_text("#!/bin/sh\necho 'codex-cli 0.170.0'\n", encoding="utf-8")
            fake_codex.chmod(0o755)
            cert_file = d / "corp-ca.pem"
            cert_file.write_text("CERT", encoding="utf-8")

            old_cwd = os.getcwd()
            try:
                os.chdir(d)
                env_install = {
                    "CODEX_QUOTA_WAKEUP_STATE_DIR": str(st_dir),
                    "CODEX_REFRESH_TOKEN_URL_OVERRIDE": "https://evil-refresh.example",
                    "CODEX_AGENT_IDENTITY_AUTHAPI_BASE_URL": "https://evil-agent-auth.example",
                    "CODEX_AGENT_IDENTITY_JWKS_BASE_URL": "https://evil-jwks.example",
                    "OPENAI_FEDERATION_RULE_ID": "fed-rule",
                    "OPENAI_ORGANIZATION": "org-custom",
                    "all_proxy": "http://bob:topsecret@127.0.0.1:8080",
                    "SSL_CERT_FILE": "./corp-ca.pem",
                }
                with mock.patch.dict(os.environ, env_install, clear=False):
                    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "CODEX_CA_CERTIFICATE"):
                        os.environ.pop(k, None)
                    captured = mod.capture_proxy_env(os.environ)
                    self.assertEqual(captured["SSL_CERT_FILE"], str(cert_file))
                    mod.atomic_write_json(st_dir / "proxy.json", {"env": captured})
                    built_live = mod.build_clean_env(state_dir=st_dir)
                    self.assertNotIn("CODEX_REFRESH_TOKEN_URL_OVERRIDE", built_live)
                    self.assertNotIn("CODEX_AGENT_IDENTITY_AUTHAPI_BASE_URL", built_live)
                    self.assertNotIn("CODEX_AGENT_IDENTITY_JWKS_BASE_URL", built_live)
                    self.assertNotIn("OPENAI_FEDERATION_RULE_ID", built_live)
                    self.assertNotIn("OPENAI_ORGANIZATION", built_live)
                    self.assertEqual(built_live["SSL_CERT_FILE"], str(cert_file))

                    # `install --no-proxy` drops upstream proxy vars while preserving custom CA!
                    saved_np, cleared_np = mod.persist_install_proxy_env(st_dir, disable_proxy=True)
                    self.assertTrue(cleared_np)
                    self.assertEqual(saved_np, {"SSL_CERT_FILE": str(cert_file)})
                    # Restore full proxy.json for background env assertion below
                    mod.atomic_write_json(st_dir / "proxy.json", {"env": captured})
            finally:
                os.chdir(old_cwd)

            clean_bg = {k: v for k, v in os.environ.items() if k not in mod.PROXY_ENV_KEYS}
            with mock.patch.dict(os.environ, clean_bg, clear=True):
                built_bg = mod.build_clean_env(state_dir=st_dir)
            self.assertEqual(built_bg["SSL_CERT_FILE"], str(cert_file))
            self.assertEqual(built_bg["https_proxy"], "http://bob:topsecret@127.0.0.1:8080")

            # Live `SSL_CERT_FILE` overrides saved `CODEX_CA_CERTIFICATE` cleanly (`astra` R5 P2-1)
            mod.atomic_write_json(st_dir / "proxy.json", {"env": {"CODEX_CA_CERTIFICATE": "/old/ca.pem"}})
            new_cert = d / "new-ca.pem"
            new_cert.write_text("NEW", encoding="utf-8")
            with mock.patch.dict(os.environ, {**clean_bg, "SSL_CERT_FILE": str(new_cert)}, clear=True):
                built_rot = mod.build_clean_env(state_dir=st_dir)
            self.assertEqual(built_rot.get("SSL_CERT_FILE"), str(new_cert))
            self.assertNotIn("CODEX_CA_CERTIFICATE", built_rot)

            # Linux uninstall refuses to delete unit files if UnitFileState is still `enabled`
            unit_dir = xdg_dir / "systemd" / "user"
            unit_dir.mkdir(parents=True, exist_ok=True)
            (unit_dir / "codex-quota-wakeup.timer").write_text("[Timer]\n", encoding="utf-8")

            class DummyProc:
                def __init__(self, rc: int, out: str = "", err: str = "") -> None:
                    self.returncode = rc
                    self.stdout = out
                    self.stderr = err

            with mock.patch.dict(os.environ, {"CODEX_QUOTA_WAKEUP_STATE_DIR": str(st_dir), "XDG_CONFIG_HOME": str(xdg_dir)}):
                with mock.patch.object(mod.platform, "system", return_value="Linux"):
                    with mock.patch.object(mod.shutil, "which", return_value="/usr/bin/systemctl"):
                        with mock.patch.object(mod, "systemd_user_available", return_value=(True, "")):
                            with mock.patch.object(
                                mod.subprocess,
                                "run",
                                side_effect=[
                                    DummyProc(1, err="disable denied"),
                                    DummyProc(0),
                                    DummyProc(0),
                                    DummyProc(0, out="LoadState=loaded\nActiveState=inactive\nUnitFileState=enabled\n"),
                                    DummyProc(0, out="LoadState=loaded\nActiveState=inactive\n"),
                                ],
                            ):
                                with redirect_stderr(io.StringIO()):
                                    rc_un_fail = mod.main(["uninstall"])
            self.assertEqual(rc_un_fail, 1)
            self.assertTrue((unit_dir / "codex-quota-wakeup.timer").exists())

    def test_scheduler_units_and_macos_bootstrap_rollback(self) -> None:
        svc, tmr = mod.build_systemd_units(
            1800, codex_bin="/opt/my dir/codex%1", model_id="gpt-6-luna", custom_state_dir=Path("/tmp/st$1")
        )
        self.assertIn('"/opt/my dir/codex%%1"', svc)
        self.assertIn("CODEX_QUOTA_WAKEUP_STATE_DIR=/tmp/st$1", svc)
        self.assertIn("OnCalendar=*-*-* *:03,33:00", tmr)

        with tempfile.TemporaryDirectory() as td:
            d = Path(td).resolve()
            st_dir = d / "state"
            st_dir.mkdir(parents=True, exist_ok=True)
            plist_file = d / "LaunchAgents" / f"{mod.LAUNCHD_LABEL}.plist"
            plist_file.parent.mkdir(parents=True, exist_ok=True)
            plist_file.write_bytes(b"OLD_VALID_PLIST")

            class DummyProc:
                def __init__(self, rc: int, err: str = "") -> None:
                    self.returncode = rc
                    self.stdout = ""
                    self.stderr = err

            calls = []

            def fake_run(cmd, **kwargs):
                calls.append(list(cmd))
                if cmd[:2] == ["launchctl", "print"]:
                    return DummyProc(0)
                if cmd[:2] == ["launchctl", "bootout"]:
                    return DummyProc(0)
                if cmd[:2] == ["launchctl", "bootstrap"]:
                    # Fail the 4 attempts for the new plist, succeed on rollback!
                    bootstrap_count = sum(1 for c in calls if c[:2] == ["launchctl", "bootstrap"])
                    return DummyProc(134, "Input/output error") if bootstrap_count <= 4 else DummyProc(0)
                return DummyProc(0)

            with mock.patch.object(mod, "launchd_plist_path", return_value=plist_file):
                with mock.patch.object(mod.time, "sleep"):
                    with mock.patch.object(mod.subprocess, "run", side_effect=fake_run):
                        with redirect_stderr(io.StringIO()):
                            rc, _, _ = mod._install_macos(1800, st_dir, codex_bin=None)
            self.assertEqual(rc, 1)
            self.assertEqual(plist_file.read_bytes(), b"OLD_VALID_PLIST")
            self.assertEqual(sum(1 for c in calls if c[:2] == ["launchctl", "bootstrap"]), 5)
            self.assertTrue(any(c[:2] == ["launchctl", "enable"] for c in calls))

    def test_ops_commands_install_uninstall_status_logs_rotation_lock_and_nvm_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td).resolve()
            st_dir = d / "state"
            xdg_dir = d / "xdg"
            nvm_bin = d / "nvm" / "v22" / "bin"
            nvm_bin.mkdir(parents=True, exist_ok=True)
            fake_node = nvm_bin / "node"
            fake_node.write_text("#!/bin/sh\necho 'codex-cli 0.170.0'\n", encoding="utf-8")
            fake_node.chmod(0o755)
            # Simulate an npm-installed `codex` wrapper whose `#!/usr/bin/env node` needs sibling `nvm_bin` on PATH
            fake_codex = nvm_bin / "codex"
            fake_codex.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            fake_codex.chmod(0o755)

            env_min = {"PATH": "/usr/bin:/bin", "HOME": str(d)}
            mod.ensure_bin_dir_in_path(env_min, str(fake_codex))
            self.assertTrue(env_min["PATH"].startswith(str(nvm_bin) + ":"))
            mod.ensure_min_codex_version(str(fake_codex), env_min)

            plist = mod.build_launchd_plist(1800, st_dir, codex_bin=str(fake_codex), offset_minute=18)
            self.assertIn(str(nvm_bin), plist["EnvironmentVariables"]["PATH"].split(":"))
            self.assertEqual(plist["StartCalendarInterval"], [{"Minute": 18}, {"Minute": 48}])
            svc_txt, tmr_txt = mod.build_systemd_units(1800, codex_bin=str(fake_codex), offset_minute=18)
            self.assertIn(str(nvm_bin), svc_txt)
            self.assertIn("OnCalendar=*-*-* *:18,48:00", tmr_txt)
            with redirect_stderr(io.StringIO()):
                self.assertEqual(mod.main(["install", "--minute-offset", "60"]), 2)

            # Log rotation produces 0600 backup `.1` and both files are read back in order
            log_file = st_dir / "wakeup.jsonl"
            mod.append_jsonl_with_rotation(
                log_file,
                {"ts": "2026-11-20T01:00:00+08:00", "status": "ok", "duration_ms": 100, "pools": {}},
                max_bytes=120,
            )
            mod.append_jsonl_with_rotation(
                log_file,
                {
                    "ts": "2026-11-20T01:30:00+08:00",
                    "status": "activated",
                    "duration_ms": 200,
                    "pools": {"codex": {"action": "probed", "probe": {"model": "(codex-default)"}, "verified_active_after": True}},
                },
                max_bytes=120,
            )
            mod.append_jsonl_with_rotation(
                log_file,
                {"ts": "2026-11-20T02:00:00+08:00", "status": "error", "duration_ms": 50, "error": "boom"},
                max_bytes=1024 * 1024,
            )
            rotated = st_dir / "wakeup.jsonl.1"
            self.assertTrue(rotated.is_file())
            self.assertEqual(rotated.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(mod.read_all_log_records(st_dir)), 3)

            # `status`, `logs --activations`, `logs --errors`, and mutually exclusive flag guard
            with mock.patch.dict(os.environ, {"CODEX_QUOTA_WAKEUP_STATE_DIR": str(st_dir), "XDG_CONFIG_HOME": str(xdg_dir)}):
                out_st = io.StringIO()
                with mock.patch.object(mod.platform, "system", return_value="Linux"):
                    with mock.patch.object(mod.shutil, "which", return_value=None):
                        with redirect_stdout(out_st):
                            rc_st = mod.main(["status", "-n", "5"])
                self.assertEqual(rc_st, 0)
                self.assertIn("Total recorded runs: 3 | Activations: 1 | Errors: 1", out_st.getvalue())

                out_act = io.StringIO()
                with redirect_stdout(out_act):
                    rc_act = mod.main(["logs", "--activations", "--json"])
                self.assertEqual(rc_act, 0)
                self.assertEqual(json.loads(out_act.getvalue().strip())["status"], "activated")

                out_err = io.StringIO()
                with redirect_stdout(out_err):
                    rc_err = mod.main(["logs", "--errors", "--json"])
                self.assertEqual(rc_err, 0)
                self.assertEqual(json.loads(out_err.getvalue().strip())["status"], "error")

                with redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as se:
                        mod.main(["logs", "--activations", "--errors"])
                self.assertEqual(se.exception.code, 2)

                # Concurrent `wakeup.lock` contention returns 0 with `skipped_locked`
                lock_fh = (st_dir / "wakeup.lock").open("a+", encoding="utf-8")
                try:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    out_lk = io.StringIO()
                    with redirect_stdout(out_lk):
                        rc_lk = mod.main(["run"])
                    self.assertEqual(rc_lk, 0)
                    self.assertEqual(json.loads(out_lk.getvalue())["status"], "skipped_locked")
                finally:
                    lock_fh.close()

                # Linux `install`: cron fallback omits `sys.executable`; systemd install uses `loginctl --no-ask-password`
                err_cron = io.StringIO()
                with mock.patch.object(mod.platform, "system", return_value="Linux"):
                    with mock.patch.object(mod, "systemd_user_available", return_value=(False, "no bus")):
                        with redirect_stdout(io.StringIO()), redirect_stderr(err_cron):
                            rc_cron = mod.main(["install", "--codex-bin", str(fake_codex)])
                self.assertEqual(rc_cron, 1)
                self.assertIn("crontab -e", err_cron.getvalue())
                self.assertNotIn(sys.executable, err_cron.getvalue())

                recorded_cmds = []

                class DummyProc:
                    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
                        self.returncode = rc
                        self.stdout = out
                        self.stderr = err

                def fake_linux_run(cmd, **kwargs):
                    recorded_cmds.append(list(cmd))
                    if len(cmd) >= 2 and cmd[1] == "--version":
                        return DummyProc(0, out="codex-cli 0.170.0\n")
                    if cmd[:2] == ["loginctl", "show-user"]:
                        return DummyProc(0, out="Linger=no\n")
                    if cmd[:2] == ["loginctl", "--no-ask-password"]:
                        return DummyProc(1, err="Interactive authentication required")
                    return DummyProc(0)

                out_inst = io.StringIO()
                with mock.patch.object(mod.platform, "system", return_value="Linux"):
                    with mock.patch.object(mod.shutil, "which", return_value="/usr/bin/loginctl"):
                        with mock.patch.object(mod, "systemd_user_available", return_value=(True, "")):
                            with mock.patch.object(mod.subprocess, "run", side_effect=fake_linux_run):
                                with redirect_stdout(out_inst):
                                    rc_inst = mod.main(
                                        ["install", "--minute-offset", "18", "--codex-bin", str(fake_codex)]
                                    )
                self.assertEqual(rc_inst, 0)
                self.assertIn(["loginctl", "--no-ask-password", "enable-linger"], recorded_cmds)
                self.assertTrue((xdg_dir / "systemd" / "user" / "codex-quota-wakeup.timer").is_file())

                out_st_linux = io.StringIO()
                with mock.patch.object(mod.platform, "system", return_value="Linux"):
                    with mock.patch.object(mod.shutil, "which", return_value="/usr/bin/systemctl"):
                        with mock.patch.object(
                            mod.subprocess,
                            "run",
                            side_effect=lambda cmd, **kw: DummyProc(
                                0, out=("active\n" if "is-active" in cmd else "Linger=yes\n")
                            ),
                        ):
                            with redirect_stdout(out_st_linux):
                                rc_st_linux = mod.main(["status"])
                self.assertEqual(rc_st_linux, 0)
                self.assertIn("schedule=hourly at :18,:48", out_st_linux.getvalue())

                # macOS `uninstall` clean unload removes plist
                plist_path = d / "LaunchAgents" / f"{mod.LAUNCHD_LABEL}.plist"
                plist_path.parent.mkdir(parents=True, exist_ok=True)
                plist_path.write_text("<plist/>", encoding="utf-8")

                def fake_mac_un(cmd, **kwargs):
                    if cmd[:2] == ["launchctl", "print"]:
                        return DummyProc(113, err="Could not find service")
                    return DummyProc(0)

                with mock.patch.object(mod.platform, "system", return_value="Darwin"):
                    with mock.patch.object(mod, "launchd_plist_path", return_value=plist_path):
                        with mock.patch.object(mod.subprocess, "run", side_effect=fake_mac_un):
                            with redirect_stdout(io.StringIO()):
                                rc_un_mac = mod.main(["uninstall"])
                self.assertEqual(rc_un_mac, 0)
                self.assertFalse(plist_path.exists())

    def test_system_layer_login_restriction_missing_account_id_and_force_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td).resolve()
            st_dir = d / "state"
            codex_home = d / "codex_home"
            codex_home.mkdir(parents=True, exist_ok=True)
            auth_file = codex_home / "auth.json"
            auth_file.write_text('{"tokens": {"access_token": "synthetic"}}', encoding="utf-8")

            fake_codex = d / "codex"
            fake_codex.write_text("#!/bin/sh\necho 'codex-cli 0.170.0'\n", encoding="utf-8")
            fake_codex.chmod(0o755)

            base_ep = 1820000000
            cur_dt = dt.datetime.fromtimestamp(base_ep, tz=dt.timezone.utc)
            conflict_layers = [
                {"name": {"type": "user"}, "config": {"forced_login_method": "chatgpt"}},
                {"name": {"type": "system"}, "config": {"forced_login_method": "api"}},
            ]
            s1 = _make_drifting_bundle(base_ep + mod.WEEK_SECONDS, config_layers=conflict_layers)
            s2 = _make_drifting_bundle(base_ep + mod.WEEK_SECONDS + 3, config_layers=conflict_layers)
            s3 = _make_drifting_bundle(base_ep + mod.WEEK_SECONDS + 6, config_layers=conflict_layers)

            sent = [0]

            def fail_if_sent(*args, **kwargs):
                sent[0] += 1
                raise AssertionError("exec must not be spawned")

            with mock.patch.dict(
                os.environ,
                {"CODEX_QUOTA_WAKEUP_STATE_DIR": str(st_dir), "CODEX_HOME": str(codex_home)},
            ):
                with mock.patch.object(mod.time, "sleep"), mock.patch.object(mod.time, "time", return_value=float(base_ep)):
                    with mock.patch.object(mod.dt, "datetime", wraps=dt.datetime) as mock_dt:
                        mock_dt.now.return_value = cur_dt
                        # 1) System-layer `forced_login_method="api"` overridden by user `"chatgpt"` skips exec cleanly
                        out1 = io.StringIO()
                        with mock.patch.object(mod, "fetch_quota_with_retries", side_effect=[(s1, 1), (s2, 1), (s3, 1)]):
                            with mock.patch.object(mod, "send_wakeup_request", side_effect=fail_if_sent):
                                with redirect_stdout(out1):
                                    rc1 = mod.main(["run", "--codex-bin", str(fake_codex)])
                        self.assertEqual(rc1, 0)
                        self.assertEqual(sent[0], 0)
                        self.assertTrue(auth_file.is_file())
                        self.assertEqual(
                            json.loads(out1.getvalue())["pools"]["codex"]["reason"],
                            "system_login_restriction_present",
                        )

                        # 2) Missing `accountId` skips automatic send with `missing_account_identity`,
                        # while `email: None` with a valid `accountId` still produces a valid identity key.
                        no_aid = _make_drifting_bundle(base_ep + mod.WEEK_SECONDS, account_id="")
                        null_email = _make_drifting_bundle(base_ep + mod.WEEK_SECONDS, account_id="acct-alpha", email=None)
                        with_email = _make_drifting_bundle(base_ep + mod.WEEK_SECONDS, account_id="acct-alpha", email="a@example.com")
                        self.assertIsNotNone(mod.extract_account_identity_key(null_email))
                        self.assertNotEqual(
                            mod.extract_account_identity_key(null_email),
                            mod.extract_account_identity_key(with_email),
                        )
                        out2 = io.StringIO()
                        with mock.patch.object(mod, "fetch_quota_with_retries", side_effect=[(no_aid, 1)]):
                            with mock.patch.object(mod, "send_wakeup_request", side_effect=fail_if_sent):
                                with redirect_stdout(out2):
                                    rc2 = mod.main(["run", "--codex-bin", str(fake_codex)])
                        self.assertEqual(rc2, 0)
                        self.assertEqual(
                            json.loads(out2.getvalue())["pools"]["codex"]["reason"],
                            "missing_account_identity",
                        )

                        # Version gate accepts >= 0.158.0 and rejects 0.157.9
                        fake_158 = d / "codex-158"
                        fake_158.write_text("#!/bin/sh\necho 'codex-cli 0.158.0'\n", encoding="utf-8")
                        fake_158.chmod(0o755)
                        mod.ensure_min_codex_version(str(fake_158), mod.build_clean_env())
                        fake_157 = d / "codex-157"
                        fake_157.write_text("#!/bin/sh\necho 'codex-cli 0.157.9'\n", encoding="utf-8")
                        fake_157.chmod(0o755)
                        with self.assertRaises(RuntimeError):
                            mod.ensure_min_codex_version(str(fake_157), mod.build_clean_env())

                        # 3) `--force` still respects `quota_exhausted_or_blocked` (`usedPercent=100`) AND rolling 3/week cap!
                        exhausted = _make_drifting_bundle(base_ep + mod.WEEK_SECONDS, used_pct=100)
                        out3 = io.StringIO()
                        with mock.patch.object(mod, "fetch_quota_with_retries", side_effect=[(exhausted, 1)]):
                            with mock.patch.object(mod, "send_wakeup_request", side_effect=fail_if_sent):
                                with redirect_stdout(out3):
                                    rc3 = mod.main(["run", "--force", "--codex-bin", str(fake_codex)])
                        self.assertEqual(rc3, 0)
                        self.assertEqual(sent[0], 0)
                        self.assertEqual(
                            json.loads(out3.getvalue())["pools"]["codex"]["reason"],
                            "quota_exhausted_or_blocked",
                        )
                        self.assertEqual((st_dir / "wakeup.jsonl").stat().st_mode & 0o777, 0o600)

                        active_5pct = _make_drifting_bundle(base_ep + mod.WEEK_SECONDS, used_pct=5)
                        acct_k = mod.extract_account_identity_key(active_5pct)
                        assert acct_k is not None
                        capped_entry = {
                            "account_key": acct_k,
                            "last_probe_epoch": float(base_ep - 10000),
                            "probe_epochs": [float(base_ep - 30000), float(base_ep - 20000), float(base_ep - 10000)],
                            "verified_active": True,
                            "unverified_streak": 0,
                        }
                        mod.atomic_write_json(
                            st_dir / "state.json",
                            {"pools": {"codex": capped_entry}, "accounts": {acct_k: capped_entry}},
                        )
                        out4 = io.StringIO()
                        with mock.patch.object(mod, "fetch_quota_with_retries", side_effect=[(active_5pct, 1)]):
                            with mock.patch.object(mod, "send_wakeup_request", side_effect=fail_if_sent):
                                with redirect_stdout(out4):
                                    rc4 = mod.main(["run", "--force", "--codex-bin", str(fake_codex)])
                        self.assertEqual(rc4, 0)
                        self.assertEqual(sent[0], 0)
                        self.assertEqual(
                            json.loads(out4.getvalue())["pools"]["codex"]["reason"],
                            "weekly_wakeup_cap_reached",
                        )


if __name__ == "__main__":
    unittest.main()
