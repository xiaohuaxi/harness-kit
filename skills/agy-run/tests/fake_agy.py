"""Fake ``agy`` executable speaking ACP v1 JSON-RPC over stdio for offline tests.

Lives at ``<home>/fake-agy-bin/agy``. ``agy_core`` passes a whitelisted environment
(``HOME`` is preserved), so the fake reads queued turn specs from ``$HOME/fake-agy/queue.json``
and logs invocations to ``$HOME/fake-agy/calls.jsonl``.
"""

import json
import os
import stat
import sys
import uuid
from pathlib import Path

DEFAULT_CONFIG_OPTIONS = [
    {
        "id": "model",
        "name": "Model",
        "category": "model",
        "type": "select",
        "currentValue": "gemini-4-carbon",
        "options": [
            {"value": "gemini-4-carbon", "name": "Gemini 4 Carbon"},
            {"value": "gemini-4-argon", "name": "Gemini 4 Argon"},
            {"value": "gemini-4-hydrogen", "name": "Gemini 4 Hydrogen"},
            {"value": "gemini-pro-orchestrator", "name": "Gemini Pro Orchestrator"},
            {"value": "gemini-pro-orchestrator-argon", "name": "Gemini Pro Orchestrator (Argon)"},
            {"value": "gemini-3.8-flash", "name": "Gemini 3.8 Flash"},
            {"value": "gpt-oss-120b-medium", "name": "GPT OSS 120B"},
        ],
    },
    {
        "id": "thought_level",
        "name": "Thinking Level",
        "category": "thought_level",
        "type": "select",
        "currentValue": "high",
        "options": [
            {"value": "low", "name": "Low"},
            {"value": "medium", "name": "Medium"},
            {"value": "high", "name": "High"},
        ],
    },
]

FAKE_ACP_SOURCE = """#!{python}
import json, os, signal, subprocess, sys, threading, time, uuid
from pathlib import Path

base = Path(os.environ["HOME"]) / "fake-agy"
if sys.argv[1:2] == ["models"]:
    models_file = base / "models_spec.json"
    mspec = json.loads(models_file.read_text(encoding="utf-8")) if models_file.exists() else {{}}
    with open(base / "models_calls.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ)}}) + "\\n")
    if mspec.get("ignore_sigint"):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    if mspec.get("leave_child"):
        code = (
            "import signal,sys,time,pathlib,os\\n"
            "signal.signal(signal.SIGINT, signal.SIG_IGN)\\n"
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\\n"
            "time.sleep(60)\\n"
        )
        subprocess.Popen(
            [sys.executable, "-c", code, str(base / "models_child.pid")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(200):
            if (base / "models_child.pid").exists() and (base / "models_child.pid").read_text().strip():
                break
            time.sleep(0.02)
    if mspec.get("sleep"):
        time.sleep(mspec["sleep"])
    if mspec.get("stderr"):
        sys.stderr.write(mspec["stderr"])
        sys.stderr.flush()
    if "stdout" in mspec:
        if mspec["stdout"]:
            sys.stdout.write(mspec["stdout"])
            sys.stdout.flush()
    else:
        sys.stdout.write(
            "gemini-4-carbon-medium\\tGemini 4 Carbon (Medium)\\n"
            "gemini-3.8-flash-low\\tGemini 3.8 Flash (Low)\\n"
        )
        sys.stdout.flush()
    sys.exit(mspec.get("code", 0))

queue_data = json.loads((base / "queue.json").read_text(encoding="utf-8"))
log_path = base / "calls.jsonl"
existing = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
index = len(existing)
spec = queue_data[index] if index < len(queue_data) else None

if spec and spec.get("start_delay"):
    time.sleep(spec["start_delay"])
if spec and spec.get("ignore_sigint"):
    signal.signal(signal.SIGINT, signal.SIG_IGN)

call_id = index
record = {{
    "index": call_id,
    "pid": os.getpid(),
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "env_keys": sorted(os.environ),
    "env": dict(os.environ),
    "rpcs": [],
}}
with open(log_path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(record) + "\\n")

if spec is None:
    sys.stderr.write("fake agy: queue exhausted\\n")
    sys.exit(99)

if spec.get("leave_child"):
    code = (
        "import signal,sys,time,pathlib,os\\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\\n"
        "time.sleep(60)\\n"
    )
    subprocess.Popen(
        [sys.executable, "-c", code, str(base / "child.pid")],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(200):
        if (base / "child.pid").exists() and (base / "child.pid").read_text().strip():
            break
        time.sleep(0.02)

if spec.get("exit_immediately"):
    if spec.get("stderr"):
        sys.stderr.write(spec["stderr"])
        sys.stderr.flush()
    sys.exit(spec.get("code", 1))

out_lock = threading.Lock()
def send(obj):
    with out_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\\n")
        sys.stdout.flush()

def append_rpc_log(msg):
    rpc_log = base / f"rpcs-{{call_id}}.jsonl"
    with open(rpc_log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(msg, ensure_ascii=False) + "\\n")

config_options = spec.get("config_options") or {default_opts}
current_model = next((c["currentValue"] for c in config_options if c["id"] == "model"), "gemini-4-carbon")
current_effort = next((c["currentValue"] for c in config_options if c["id"] == "thought_level"), "high")
session_id = spec.get("session_id") or str(uuid.uuid4())

def options_for_model(m):
    opts = [dict(c) for c in config_options if c["id"] == "model"]
    opts[0]["currentValue"] = m
    if m == "gemini-pro-orchestrator":
        return opts
    eff_vals = ["low", "medium", "high", "xhigh"] if "hydrogen" in m else ["low", "medium", "high"]
    opts.append({{
        "id": "thought_level",
        "name": "Thinking Level",
        "category": "thought_level",
        "type": "select",
        "currentValue": current_effort if current_effort in eff_vals else "medium",
        "options": [{{"value": v, "name": v.title()}} for v in eff_vals],
    }})
    return opts

if spec.get("shell"):
    for cmd in spec["shell"]:
        done = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        with open(base / "shell.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({{
                "command": cmd,
                "code": done.returncode,
                "stdout": done.stdout,
                "stderr": done.stderr,
            }}) + "\\n")

prompt_count = 0
active_prompt_lock = threading.Lock()
active_prompt_id = None
steer_event = threading.Event()
latest_steer_prompt = None

def handle_prompt_worker(pid, text):
    global active_prompt_id
    if spec.get("sleep"):
        time.sleep(spec["sleep"])

    if spec.get("wait_for_steer"):
        if spec.get("pre_steer_chunk"):
            send({{
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {{
                    "sessionId": session_id,
                    "update": {{
                        "sessionUpdate": "agent_message_chunk",
                        "content": {{"type": "text", "text": spec["pre_steer_chunk"]}},
                    }},
                }},
            }})
        # Emit initial tool call in progress, wait until steered
        send({{
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {{
                "sessionId": session_id,
                "update": {{
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc_wait",
                    "name": "run_command",
                    "rawInput": {{"CommandLine": "sleep 5"}},
                    "status": "in_progress",
                }},
            }},
        }})
        steered = steer_event.wait(timeout=spec.get("steer_wait_timeout", 5.0))
        if not spec.get("keep_tool_in_progress"):
            send({{
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {{
                    "sessionId": session_id,
                    "update": {{
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tc_wait",
                        "status": "completed",
                    }},
                }},
            }})
        if steered:
            # First prompt already resolved when steer arrived; do not emit stale answer
            return

    if spec.get("request_permission"):
        rp = spec["request_permission"]
        send({{
            "jsonrpc": "2.0",
            "id": 900,
            "method": "session/request_permission",
            "params": {{
                "sessionId": session_id,
                "toolCall": {{"toolCallId": "tc_perm", "name": rp["name"], "rawInput": rp.get("params", {{}})}},
                "options": [
                    {{"optionId": "allow_once", "name": "Allow", "kind": "allow_once"}},
                    {{"optionId": "reject_once", "name": "Deny", "kind": "reject_once"}},
                ],
            }},
        }})
        time.sleep(0.15)
        send({{
            "jsonrpc": "2.0",
            "id": pid,
            "result": {{"stopReason": "end_turn", "usage": {{"inputTokens": 10, "outputTokens": 2, "totalTokens": 12}}}},
        }})
        return

    if spec.get("prompt_error"):
        pe = spec["prompt_error"]
        send({{
            "jsonrpc": "2.0",
            "id": pid,
            "error": {{"code": pe.get("code", -32000), "message": pe.get("message", "API failure")}},
        }})
        return

    for idx, tc in enumerate(spec.get("tools") or []):
        tc_id = f"tc_{{idx}}"
        end_status = tc.get("status", "completed")
        send({{
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {{
                "sessionId": session_id,
                "update": {{
                    "sessionUpdate": "tool_call",
                    "toolCallId": tc_id,
                    "name": tc["name"],
                    "rawInput": tc.get("params", {{}}),
                    "status": "in_progress",
                }},
            }},
        }})
        if end_status != "in_progress":
            send({{
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {{
                    "sessionId": session_id,
                    "update": {{
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": tc_id,
                        "status": end_status,
                    }},
                }},
            }})

    resp_text = spec.get("response", "OK")
    if resp_text:
        send({{
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {{
                "sessionId": session_id,
                "update": {{
                    "sessionUpdate": "agent_message_chunk",
                    "content": {{"type": "text", "text": resp_text}},
                }},
            }},
        }})

    use = spec.get("usage") or {{"inputTokens": 120, "outputTokens": 15, "totalTokens": 135}}
    send({{
        "jsonrpc": "2.0",
        "id": pid,
        "result": {{"stopReason": spec.get("stop_reason", "end_turn"), "usage": use}},
    }})

    bg = spec.get("bg_drain")
    if bg:
        brain_root = Path(os.environ.get("AGY_BRAIN_DIR") or (Path(os.environ["HOME"]) / ".gemini" / "antigravity-cli" / "brain"))
        logs = brain_root / session_id / ".system_generated" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        tf = logs / "transcript_full.jsonl"
        task_id = f"{{session_id}}/task-1"
        with open(tf, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({{
                "step_index": 1,
                "type": "GENERIC",
                "status": "DONE",
                "content": f"Tool is running as a background task with task id: {{task_id}}",
            }}) + "\\n")
            fh.write(json.dumps({{
                "step_index": 2,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": resp_text,
            }}) + "\\n")
        time.sleep(bg.get("delay", 0.25))
        with open(tf, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({{
                "step_index": 3,
                "type": "SYSTEM_MESSAGE",
                "status": "DONE",
                "content": f'Task id "{{task_id}}" finished with result: done',
            }}) + "\\n")
            if bg.get("wakeup_tool_calls"):
                fh.write(json.dumps({{
                    "step_index": 4,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "content": "",
                    "input_tokens": 50,
                    "output_tokens": 10,
                    "tool_calls": bg["wakeup_tool_calls"],
                }}) + "\\n")
            fh.write(json.dumps({{
                "step_index": 5,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": bg["final_text"],
                "input_tokens": 80,
                "output_tokens": 20,
            }}) + "\\n")

for raw_line in sys.stdin:
    raw_line = raw_line.strip()
    if not raw_line:
        continue
    msg = json.loads(raw_line)
    append_rpc_log(msg)
    rid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {{}}

    if method == "initialize":
        send({{"jsonrpc": "2.0", "id": rid, "result": {{"protocolVersion": 1, "agentCapabilities": {{}}}}}})
    elif method in ("session/new", "session/resume"):
        if method == "session/resume" and not spec.get("override_resume_id"):
            session_id = params["sessionId"]
        send({{
            "jsonrpc": "2.0",
            "id": rid,
            "result": {{"sessionId": session_id, "configOptions": options_for_model(current_model)}},
        }})
    elif method == "session/set_config_option":
        cid = params.get("configId")
        val = params.get("value")
        if cid == "model":
            current_model = val
        elif cid == "thought_level":
            current_effort = val
        send({{"jsonrpc": "2.0", "id": rid, "result": {{"configOptions": options_for_model(current_model)}}}})
    elif method == "session/prompt":
        prompt_count += 1
        blocks = params.get("prompt") or []
        p_text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        if prompt_count == 1:
            active_prompt_id = rid
            worker_thread = threading.Thread(target=handle_prompt_worker, args=(rid, p_text), daemon=True)
            worker_thread.start()
        else:
            # Steer / second prompt in same ACP session!
            if active_prompt_id is not None and spec.get("wait_for_steer"):
                old_pid = active_prompt_id
                active_prompt_id = None
                steer_event.set()
                send({{
                    "jsonrpc": "2.0",
                    "id": old_pid,
                    "result": {{"stopReason": "end_turn", "usage": {{"inputTokens": 40, "outputTokens": 5, "totalTokens": 45}}}},
                }})
                worker_thread.join(timeout=1.0)
            steered_reply = (
                spec["steered_response"]
                if "steered_response" in spec
                else ("STEERED: " + p_text)
            )
            if steered_reply:
                send({{
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {{
                        "sessionId": session_id,
                        "update": {{
                            "sessionUpdate": "agent_message_chunk",
                            "content": {{"type": "text", "text": steered_reply}},
                        }},
                    }},
                }})
            send({{
                "jsonrpc": "2.0",
                "id": rid,
                "result": {{"stopReason": "end_turn", "usage": {{"inputTokens": 60, "outputTokens": 12, "totalTokens": 72}}}},
            }})
"""


class FakeAgy:
    def __init__(self, home):
        self.home = Path(home)
        self.base = self.home / "fake-agy"
        self.bin_dir = self.home / "fake-agy-bin"
        self.base.mkdir(parents=True, exist_ok=True)
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        exe = self.bin_dir / "agy"
        exe.write_text(
            FAKE_ACP_SOURCE.format(
                python=sys.executable,
                default_opts=repr(DEFAULT_CONFIG_OPTIONS),
            ),
            encoding="utf-8",
        )
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        self.replies = []
        self._save()

    def _save(self):
        (self.base / "queue.json").write_text(json.dumps(self.replies), encoding="utf-8")

    def queue(self, response="OK", session_id=None, **extra):
        entry = {"response": response, **extra}
        if session_id is not None:
            entry["session_id"] = session_id
        self.replies.append(entry)
        self._save()
        return entry.get("session_id")

    def set_models_response(self, **spec):
        (self.base / "models_spec.json").write_text(json.dumps(spec), encoding="utf-8")

    def models_calls(self):
        log = self.base / "models_calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def calls(self):
        log = self.base / "calls.jsonl"
        if not log.exists():
            return []
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        for call in calls:
            rpc_file = self.base / f"rpcs-{call['index']}.jsonl"
            rpcs = (
                [json.loads(l) for l in rpc_file.read_text(encoding="utf-8").splitlines()]
                if rpc_file.exists()
                else []
            )
            call["rpcs"] = rpcs
            prompts = [
                "".join(b.get("text", "") for b in (r.get("params", {}).get("prompt") or []))
                for r in rpcs
                if r.get("method") == "session/prompt"
            ]
            call["prompts"] = prompts
            call["prompt"] = prompts[0] if prompts else ""
        return calls

    def shell_results(self):
        log = self.base / "shell.jsonl"
        return (
            [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            if log.exists()
            else []
        )

    def path_env(self):
        return str(self.bin_dir) + os.pathsep + os.environ.get("PATH", "")


def new_id():
    return str(uuid.uuid4())


FILTER_TEXT = "Partial answer. This request was blocked by Gemini's filters (reason: SAFETY)."
