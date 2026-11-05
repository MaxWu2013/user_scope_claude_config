#!/usr/bin/env python3
"""Subprocess tests for the workflow worker guard."""

import json
import stat
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import test_opus_arbitration as base

GUARD = ROOT / "opus-workflow-guard.py"
WF = base.FIXTURES / "workflow"
DELEGATION = (
    "WORKFLOW_WORKER_NO_DELEGATION; report NEEDS_JUDGMENT in your status footer instead, "
    "and the main session will arbitrate. When a checkpoint shows the plan is wrong, "
    "report NEEDS_JUDGMENT with reason_code PLAN_REVISION_REQUIRED, the stage_id, a narrow "
    "unresolved_question and evidence_refs, then stop. Do not delegate."
)


def run_guard(world, payload, extra=None):
    return subprocess.run(
        [sys.executable, str(GUARD)],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        text=True,
        capture_output=True,
        env=world.env(extra),
        cwd=str(world.project),
        timeout=30,
        check=False,
    )


def load_workflow(name):
    return json.loads((WF / name).read_text(encoding="utf-8"))


def write_transcript(world, session_id, agent_id, model_id=None, fallback=None):
    folder = world.home / ".claude" / "projects" / "demo" / session_id / "subagents" / "workflows" / "wf_1"
    folder.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "system", "content": "boot"})]
    if model_id:
        lines.append(json.dumps({
            "type": "attachment",
            "attachment": {
                "type": "model",
                "identity": {"modelId": model_id, "marketingName": model_id},
            },
        }))
    if fallback:
        lines.append(json.dumps({
            "type": "system",
            "content": "The exact model ID is %s" % fallback,
        }))
    lines.append("{" + ("x" * 4000))
    path = folder / ("agent-%s.jsonl" % agent_id)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    transcript = world.home / ".claude" / "projects" / "demo" / "main.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    if not transcript.exists():
        transcript.write_text("{}\n", encoding="utf-8")
    return transcript, path


def worker_read(world, session_id, agent_id, transcript, prompt_id):
    payload = load_workflow("20261105T043809.971209Z-PreToolUse-Read.json")
    payload["session_id"] = session_id
    payload["cwd"] = str(world.project)
    payload["transcript_path"] = str(transcript)
    payload["agent_id"] = agent_id
    payload["prompt_id"] = prompt_id
    payload["tool_use_id"] = "read-" + agent_id
    return payload


def write_spawn(world, session_id, agent_id):
    folder = world.home / ".claude" / "logs" / "opus-arbitration" / "mod"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("spawns-%s.jsonl" % session_id)
    line = json.dumps({
        "session_id": session_id,
        "runId": "wf_1",
        "agentIndex": 1,
        "agentId": agent_id,
        "model_requested": None,
        "model_forced": "sonnet",
        "ts": "2026-11-05T00:00:00Z",
    })
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def launch_workflow(world, session_id, prompt_id):
    payload = load_workflow("20261105T043808.278093Z-PreToolUse-Workflow.json")
    payload["cwd"] = str(world.project)
    payload["transcript_path"] = str(world.sonnet_transcript)
    payload["session_id"] = session_id
    payload["prompt_id"] = prompt_id
    payload["tool_use_id"] = "launch-" + session_id
    base._plant_guard_attest(world, session_id, payload["tool_use_id"])
    proc = base.run_gate(world, payload)
    kind, reason = base.verdict(proc)
    if kind != "allow":
        raise AssertionError("workflow launch %s %s" % (kind, reason[:200]))


def start_worker(world, session_id, agent_id, prompt_id, transcript, extra=None, log=True):
    if log:
        write_spawn(world, session_id, agent_id)
    payload = load_workflow("20261105T043808.357244Z-SubagentStart-none.json")
    payload["session_id"] = session_id
    payload["cwd"] = str(world.project)
    payload["transcript_path"] = str(transcript)
    payload["agent_id"] = agent_id
    payload["prompt_id"] = prompt_id
    return run_guard(world, payload, extra)


def test_guard(suite):
    world = base.World()
    try:
        ordinary = run_guard(world, {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "session_id": "sess-ordinary",
            "tool_use_id": "ordinary",
        })
        suite.check(
            "WG",
            "non-worker payload prints nothing",
            ordinary.returncode == 0 and ordinary.stdout == "",
            "code=%s stdout=%r" % (ordinary.returncode, ordinary.stdout[:120]),
        )
        started = time.perf_counter()
        runs = 200
        for index in range(runs):
            proc = run_guard(world, {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "session_id": "sess-fast",
                "tool_use_id": "fast-%s" % index,
            })
            if proc.returncode != 0 or proc.stdout != "":
                suite.check("WG", "fast path stays quiet", False, proc.stdout[:80])
                break
        else:
            mean_ms = (time.perf_counter() - started) * 1000.0 / runs
            suite.check(
                "WG",
                "fast path mean stays under 30 ms",
                mean_ms < 30,
                "mean_ms=%.2f" % mean_ms,
            )
        attest_call = load_workflow("20261105T043808.278093Z-PreToolUse-Workflow.json")
        attest_call["cwd"] = str(world.project)
        attest_call["transcript_path"] = str(world.sonnet_transcript)
        attest_call["session_id"] = "sess-attest"
        attest_call["tool_use_id"] = "tu-attest"
        attest_written = run_guard(world, attest_call)
        attest_path = world.home / ".claude" / "logs" / "opus-arbitration" / "guard" / "attest-sess-attest-tu-attest"
        attest_mode = stat.S_IMODE(attest_path.stat().st_mode) if attest_path.exists() else None
        suite.check(
            "WG",
            "main-thread Workflow writes the guard attestation",
            attest_written.returncode == 0
            and attest_path.is_file()
            and not attest_path.is_symlink()
            and attest_mode == 0o600,
            "code=%s mode=%s" % (attest_written.returncode, attest_mode),
        )
        broken = run_guard(world, "{not-json workflow-subagent")
        suite.check(
            "WG",
            "malformed workflow-subagent json exits 2",
            broken.returncode == 2,
            "code=%s" % broken.returncode,
        )

        session_id = "sess-guard"
        prompt_id = "prompt-guard"
        launch_workflow(world, session_id, prompt_id)
        sonnet_id = "a6e7c6dc16ef9330e"
        transcript, sonnet_path = write_transcript(world, session_id, sonnet_id, "claude-sonnet-5-5")
        started_worker = start_worker(world, session_id, sonnet_id, prompt_id, transcript)
        read = run_guard(world, worker_read(world, session_id, sonnet_id, transcript, prompt_id))
        suite.check(
            "WG",
            "sonnet worker read allows",
            started_worker.returncode == 0 and started_worker.stdout == ""
            and read.returncode == 0 and read.stdout == "",
            "start=%s read=%s %s" % (started_worker.returncode, read.returncode, read.stdout[:160]),
        )
        sonnet_path.write_text(
            json.dumps({
                "type": "attachment",
                "attachment": {"type": "model", "identity": {"modelId": "claude-opus-5-5"}},
            }) + "\n",
            encoding="utf-8",
        )
        cached = run_guard(world, worker_read(world, session_id, sonnet_id, transcript, prompt_id))
        suite.check(
            "WG",
            "cached sonnet family skips a later opus transcript",
            cached.returncode == 0 and cached.stdout == "",
            cached.stdout[:160],
        )

        opus_id = "a2cde9c0a464df578"
        _opus_transcript, _opus_path = write_transcript(world, session_id, opus_id, "claude-opus-5-5")
        start_worker(world, session_id, opus_id, prompt_id, transcript)
        denied = run_guard(world, worker_read(world, session_id, opus_id, transcript, prompt_id))
        events = (world.home / ".claude" / "logs" / "opus-arbitration" / "events.jsonl").read_text(encoding="utf-8")
        kind, reason = base.verdict(denied)
        suite.check(
            "WG",
            "opus worker read denies and records the event",
            kind == "deny" and reason == "WORKFLOW_WORKER_MODEL_DENIED model=claude-opus-5-5"
            and "unarbitrated_opus_run" in events,
            reason[:200],
        )
        missing_id = "a1234567890abcde1"
        start_worker(world, session_id, missing_id, prompt_id, transcript)
        missing = run_guard(world, worker_read(world, session_id, missing_id, transcript, prompt_id))
        kind, reason = base.verdict(missing)
        suite.check(
            "WG",
            "missing worker transcript denies",
            kind == "deny" and reason == "WORKFLOW_WORKER_MODEL_DENIED model=unknown",
            reason[:200],
        )
        haiku_id = "a1234567890abcde2"
        write_transcript(world, session_id, haiku_id, fallback="claude-haiku-5-5")
        start_worker(world, session_id, haiku_id, prompt_id, transcript)
        haiku = run_guard(world, worker_read(world, session_id, haiku_id, transcript, prompt_id))
        suite.check(
            "WG",
            "system model-id fallback allows haiku",
            haiku.returncode == 0 and haiku.stdout == "",
            haiku.stdout[:160],
        )

        agent_call = worker_read(world, session_id, sonnet_id, transcript, prompt_id)
        agent_call["tool_name"] = "Agent"
        agent_call["tool_input"] = {"prompt": "go", "description": "go", "subagent_type": "general-purpose"}
        kind, reason = base.verdict(run_guard(world, agent_call))
        suite.check(
            "WG",
            "worker agent call denies",
            kind == "deny" and reason == DELEGATION,
            reason[:200],
        )

        tight = {"CLAUDE_ROUTING_WORKFLOW_MAX_WORKERS": "1"}
        cap_session = "sess-cap-workers"
        launch_workflow(world, cap_session, prompt_id)
        first_id = "a1234567890abcde3"
        second_id = "a1234567890abcde4"
        write_transcript(world, cap_session, first_id, "claude-sonnet-5-5")
        write_transcript(world, cap_session, second_id, "claude-sonnet-5-5")
        start_worker(world, cap_session, first_id, prompt_id, transcript, tight)
        start_worker(world, cap_session, second_id, prompt_id, transcript, tight)
        over = run_guard(
            world,
            worker_read(world, cap_session, second_id, transcript, prompt_id),
            tight,
        )
        kind, reason = base.verdict(over)
        suite.check(
            "WG",
            "over-cap worker denies",
            kind == "deny" and reason == "WORKFLOW_FANOUT_CAP",
            reason[:200],
        )

        stray_id = "a1234567890abcde5"
        stray_session = "sess-untracked"
        write_transcript(world, stray_session, stray_id, "claude-sonnet-5-5")
        start_worker(world, stray_session, stray_id, "no-such-prompt", transcript)
        stray = run_guard(world, worker_read(world, stray_session, stray_id, transcript, "no-such-prompt"))
        kind, reason = base.verdict(stray)
        suite.check(
            "WG",
            "untracked worker denies",
            kind == "deny" and reason == "WORKFLOW_UNTRACKED",
            reason[:200],
        )

        bypass_session = "sess-bypass"
        bypass_id = "a1234567890abcde7"
        launch_workflow(world, bypass_session, prompt_id)
        write_transcript(world, bypass_session, bypass_id, "claude-sonnet-5-5")
        start_worker(world, bypass_session, bypass_id, prompt_id, transcript, log=False)
        bypass = run_guard(world, worker_read(world, bypass_session, bypass_id, transcript, prompt_id))
        kind, reason = base.verdict(bypass)
        bypass_events = (world.home / ".claude" / "logs" / "opus-arbitration" / "events.jsonl").read_text(encoding="utf-8")
        suite.check(
            "WG",
            "worker missing from the spawn log denies and records the event",
            kind == "deny" and reason == "WORKFLOW_WORKER_NOT_MOD_SPAWNED"
            and "unarbitrated_workflow_spawn" in bypass_events,
            reason[:200],
        )

        footer = {
            "schema_version": 1,
            "task_id": "wf-task",
            "status": "NEEDS_JUDGMENT",
            "reason_code": "NEEDS_OPUS",
            "unresolved": ["the hard part"],
            "attempts": [{"strategy_digest": "abc", "outcome": "failed"}],
        }
        stop = load_workflow("20261105T043812.562702Z-SubagentStop-none.json")
        stop["session_id"] = session_id
        stop["cwd"] = str(world.project)
        stop["transcript_path"] = str(transcript)
        stop["agent_id"] = sonnet_id
        stop["prompt_id"] = prompt_id
        stop["agent_transcript_path"] = str(sonnet_path)
        stop["last_assistant_message"] = json.dumps(footer)
        stopped = run_guard(world, stop)
        pending = [
            row for row in base.rows(world, "requests")
            if row.get("session_id") == session_id and row.get("trigger") == "WORKFLOW_WORKER_REQUEST"
        ]
        flag = world.home / ".claude" / "logs" / "opus-arbitration" / "surface" / session_id
        mode = stat.S_IMODE(flag.stat().st_mode) if flag.exists() else None
        suite.check(
            "WG",
            "needs-judgment stop records a pending candidate and the flag",
            stopped.returncode == 0 and stopped.stdout == ""
            and len(pending) == 1 and pending[0]["state"] == "JUDGE_PENDING"
            and flag.is_file() and mode == 0o600,
            "code=%s rows=%s mode=%s" % (stopped.returncode, pending, mode),
        )
        main_call = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "session_id": session_id,
            "cwd": str(world.project),
            "permission_mode": "default",
            "tool_use_id": "main-1",
            "tool_input": {"file_path": "/tmp/readme"},
        }
        first = run_guard(world, main_call)
        body = base.hook_body(first) or {}
        text = body.get("additionalContext") or ""
        second = run_guard(world, dict(main_call, tool_use_id="main-2"))
        suite.check(
            "WG",
            "main pretool emits the candidate once",
            first.returncode == 0
            and "ARBITRATION_CANDIDATE" in text
            and "trigger=WORKFLOW_WORKER_REQUEST" in text
            and "permissionDecision" not in body
            and "<<<PACKET" not in text
            and second.returncode == 0 and second.stdout == ""
            and not flag.exists(),
            "text=%s second=%r" % (text[:240], second.stdout[:80]),
        )

        auto_session = "sess-auto-surface"
        launch_workflow(world, auto_session, prompt_id)
        auto_id = "a1234567890abcde6"
        _auto_transcript, auto_path = write_transcript(world, auto_session, auto_id, "claude-sonnet-5-5")
        start_worker(world, auto_session, auto_id, prompt_id, transcript)
        auto_stop = dict(stop)
        auto_stop["session_id"] = auto_session
        auto_stop["agent_id"] = auto_id
        auto_stop["agent_transcript_path"] = str(auto_path)
        run_guard(world, auto_stop)
        auto_main = dict(main_call)
        auto_main["session_id"] = auto_session
        auto_main["permission_mode"] = "auto"
        auto_main["tool_use_id"] = "main-auto"
        emitted = run_guard(world, auto_main)
        auto_text = (base.hook_body(emitted) or {}).get("additionalContext") or ""
        suite.check(
            "WG",
            "auto mode surface candidate contains the packet",
            "<<<PACKET" in auto_text and "PACKET>>>" in auto_text and "copy this prompt exactly" in auto_text,
            auto_text[:240],
        )
        fragment = json.loads((ROOT / "data" / "opus-arbitration-settings-fragment.json").read_text(encoding="utf-8"))
        pre = fragment["PreToolUse"]
        guard_pre = [item for item in pre if "matcher" not in item]
        suite.check(
            "WG",
            "fragment registers the guard and workflow post",
            guard_pre and guard_pre[0]["hooks"][0]["onFailure"] == "block"
            and guard_pre[0]["hooks"][0]["timeout"] == 5
            and "opus-workflow-guard.py" in guard_pre[0]["hooks"][0]["command"]
            and fragment["PostToolUse"][0]["matcher"] == "Agent|SendMessage|Bash|Workflow"
            and fragment["SubagentStart"][0]["hooks"][0]["onFailure"] == "continue"
            and fragment["SubagentStop"][0]["hooks"][0]["onFailure"] == "continue",
            json.dumps(fragment)[:300],
        )
    finally:
        world.close()


def _role_payload(world, agent_type, tool, tool_use_id, agent_id=None):
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "agent_type": agent_type,
        "session_id": "sess-role",
        "cwd": str(world.project),
        "tool_use_id": tool_use_id,
        "tool_input": {},
    }
    if agent_id:
        payload["agent_id"] = agent_id
    return payload


def test_role_tools(suite):
    denied_tools = (
        "Bash",
        "Write",
        "Edit",
        "Agent",
        "mcp__mcp-obsidian__obsidian_put_content",
        "Workflow",
    )
    world = base.World()
    try:
        for agent_type in ("opus-judge", "opus-planner"):
            for tool in denied_tools:
                kind, reason = base.verdict(run_guard(
                    world, _role_payload(world, agent_type, tool, "%s-%s" % (agent_type, tool)),
                ))
                suite.check(
                    "RT",
                    "%s %s is denied" % (agent_type, tool),
                    kind == "deny" and reason == "OPUS_ROLE_TOOL_DENIED %s" % tool,
                    reason[:200],
                )
            allowed = run_guard(world, _role_payload(world, agent_type, "Read", agent_type + "-read"))
            suite.check(
                "RT",
                "%s Read is allowed" % agent_type,
                allowed.returncode == 0 and allowed.stdout == "",
                "code=%s stdout=%r" % (allowed.returncode, allowed.stdout[:120]),
            )
        inside = _role_payload(
            world, "opus-judge", "Bash", "judge-inside", agent_id="a" + "1" * 16,
        )
        kind, reason = base.verdict(run_guard(world, inside))
        suite.check(
            "RT",
            "a judge tool call inside a workflow is still denied",
            kind == "deny" and reason == "OPUS_ROLE_TOOL_DENIED Bash",
            reason[:200],
        )
        other = run_guard(world, _role_payload(world, "file-reader", "Bash", "other-bash"))
        suite.check(
            "RT",
            "another agent type is unaffected",
            other.returncode == 0 and other.stdout == "",
            "code=%s stdout=%r" % (other.returncode, other.stdout[:120]),
        )
        policy = world.home / ".claude" / "hooks" / "data" / "opus-arbitration-policy.json"
        saved = policy.read_text(encoding="utf-8")
        renamed = json.loads(saved)
        renamed["judge_agent_type"] = "custom-judge"
        renamed["planning"]["planner_agent_type"] = "custom-planner"
        policy.write_text(json.dumps(renamed), encoding="utf-8")
        custom_kind, custom_reason = base.verdict(run_guard(
            world, _role_payload(world, "custom-judge", "Write", "custom-judge-write"),
        ))
        old_kind, old_reason = base.verdict(run_guard(
            world, _role_payload(world, "opus-judge", "Bash", "renamed-old-judge"),
        ))
        suite.check(
            "RT",
            "policy names replace the defaults",
            custom_kind == "deny" and custom_reason == "OPUS_ROLE_TOOL_DENIED Write"
            and old_kind == "allow",
            "custom=%s %s old=%s %s" % (custom_kind, custom_reason[:80], old_kind, old_reason[:80]),
        )
        policy.write_text("{", encoding="utf-8")
        for agent_type, tool in (("opus-judge", "Bash"), ("opus-planner", "Edit")):
            kind, reason = base.verdict(run_guard(
                world, _role_payload(world, agent_type, tool, "bad-policy-" + agent_type),
            ))
            suite.check(
                "RT",
                "a malformed policy still denies %s" % agent_type,
                kind == "deny" and reason == "OPUS_ROLE_TOOL_DENIED %s" % tool,
                reason[:200],
            )
    finally:
        world.close()


def test_replan_surface(suite):
    world = base.World()
    try:
        base._enable_planning(world)
        task_id = "guard-replan"
        session_id = "sess-guard-replan"
        version = base._publish_plan(session_id, task_id)["version"]
        agent_id = "a" + "8" * 16
        ready = base._reserve_worker(
            world, session_id, task_id, version, "draft", "wf-guard", "prompt-guard", agent_id,
        )
        footer = base._replan_footer(task_id, "draft", version)
        stop = json.loads(
            (WF / "20261105T043812.562702Z-SubagentStop-none.json").read_text(encoding="utf-8")
        )
        stop["session_id"] = session_id
        stop["cwd"] = str(world.project)
        stop["transcript_path"] = str(world.opus_transcript)
        stop["agent_id"] = agent_id
        stop["last_assistant_message"] = json.dumps(footer)
        stop.pop("agent_transcript_path", None)
        stopped = run_guard(world, stop)
        surfaced = run_guard(world, {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "session_id": session_id,
            "cwd": str(world.project),
            "tool_use_id": "guard-main",
            "tool_input": {"file_path": "/tmp/readme"},
        })
        text = ""
        try:
            text = json.loads(surfaced.stdout)["hookSpecificOutput"]["additionalContext"]
        except (json.JSONDecodeError, KeyError, TypeError):
            text = surfaced.stdout
        suite.check(
            "R2",
            "the guard surfaces one REPLAN_REQUIRED line",
            ready and stopped.returncode == 0
            and text.startswith("REPLAN_REQUIRED ")
            and "drafts/%s.json" % task_id in text
            and "router-plan promote %s" % task_id in text,
            text[:300],
        )
    finally:
        world.close()


def main():
    suite = base.Suite()
    test_guard(suite)
    test_role_tools(suite)
    test_replan_surface(suite)
    print("Tally: %s passed, %s failed" % (suite.passed, suite.failed))
    if suite.failures:
        print("\n".join(suite.failures))
    return 0 if suite.failed == 0 and suite.passed > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
