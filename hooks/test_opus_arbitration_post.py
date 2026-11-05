#!/usr/bin/env python3
"""Subprocess tests for the Opus arbitration post hook and its judge protocol.

HOME and the ledger are temp paths. Prints a table and `Tally: N passed, M failed`.
"""

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import test_opus_arbitration as base
import test_session_cost as cost

POST = ROOT / "opus-arbitration-post.py"
REQUIRE = ROOT / "require-cursor-agent.py"
JUDGE = ROOT.parent / "agents" / "opus-judge.md"


def run_post(world, payload):
    return subprocess.run(
        [sys.executable, str(POST)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=world.env(),
        cwd=str(world.project),
        timeout=30,
        check=False,
    )


def context_of(proc):
    if proc.returncode != 0 or proc.stdout.strip() == "":
        return ""
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return ""
    hook = data.get("hookSpecificOutput") or {}
    return hook.get("additionalContext") or ""


def launch_judge(world, payload):
    denied = base.run_gate(world, payload)
    kind, reason = base.verdict(denied)
    request_id, packet = base.pending_from_deny(reason)
    if kind != "deny" or not request_id or not packet:
        raise AssertionError("deny %s %s" % (kind, reason[:240]))
    bad = base.judge_payload(world, "not the packet", payload["session_id"], "judge-bad-" + request_id)
    bad_kind, bad_reason = base.verdict(base.run_gate(world, bad))
    launched = base.run_gate(world, base.judge_payload(
        world, packet, payload["session_id"], "judge-" + request_id,
    ))
    launch_kind, launch_reason = base.verdict(launched)
    return {
        "packet": packet,
        "request_id": request_id,
        "bad_kind": bad_kind,
        "bad_reason": bad_reason,
        "launch_kind": launch_kind,
        "launch_reason": launch_reason,
        "tool_use_id": "judge-" + request_id,
    }


def test_decisions(suite):
    world = base.World()
    try:
        cases = (
            ("U11", "CONTINUE", "decision=CONTINUE", "Next step: follow the decision"),
            ("U12", "RETRY", "decision=RETRY", "using: follow the decision"),
            ("U13", "STOP", "decision=STOP", "move on: needs a closer look"),
        )
        for number, decision, needle, extra in cases:
            payload = base.agent_payload(
                world, model="opus", session_id="sess-" + decision, description=decision, prompt="work " + decision,
            )
            launched = launch_judge(world, payload)
            posted = run_post(world, base.judge_result(
                world, launched["request_id"], launched["tool_use_id"], decision, session_id=payload["session_id"],
            ))
            text = context_of(posted)
            grants = [row for row in base.rows(world, "grants") if row["session_id"] == payload["session_id"]]
            suite.check(
                number,
                "%s context and no grant" % decision,
                posted.returncode == 0
                and launched["bad_kind"] == "deny"
                and launched["launch_kind"] == "allow"
                and needle in text
                and extra in text
                and "Do not use Opus" in text
                and grants == [],
                "bad=%s launch=%s text=%r grants=%s" % (launched["bad_kind"], launched["launch_kind"], text[:240], grants),
            )
        payload = base.agent_payload(
            world, model="opus", session_id="sess-esc", description="approved", prompt="do the approved thing",
        )
        launched = launch_judge(world, payload)
        posted = run_post(world, base.judge_result(
            world, launched["request_id"], launched["tool_use_id"], "ESCALATE", session_id="sess-esc",
        ))
        text = context_of(posted)
        suite.check(
            "U14",
            "pending ESCALATE names the one-use grant",
            "decision=ESCALATE" in text and "expires in 300s" in text and "unchanged" in text,
            text[:300],
        )
        allowed = base.run_gate(world, {**payload, "tool_use_id": "reissue-1"})
        kind, reason = base.verdict(allowed)
        again = base.run_gate(world, {**payload, "tool_use_id": "reissue-2"})
        again_kind, again_reason = base.verdict(again)
        suite.check(
            "U14",
            "reissued call is allowed once",
            kind == "allow" and allowed.stdout == "" and again_kind == "deny",
            "first=%s second=%s %s" % (kind, again_kind, again_reason[:160]),
        )
        rejected = (
            ("U15", "malformed", {"text": "not json"}, "judge_invalid_json"),
            ("U15", "fenced", {"fence": True}, "judge_invalid_json"),
            ("U15", "extra key", {"extra_key": True}, "judge_invalid_json"),
            ("U16", "wrong request id", {"request_id_override": "esc-wrongwrongwrong"}, "judge_invalid_json"),
            ("U17", "async launched", {"async_launched": True}, "judge_incomplete"),
            ("U10", "sonnet resolved model", {"resolved_model": "claude-sonnet-5-5"}, "judge_wrong_model"),
        )
        for number, label, options, code in rejected:
            payload = base.agent_payload(
                world,
                model="opus",
                session_id="sess-bad-" + label.replace(" ", "-"),
                description=label,
                prompt="bad " + label,
            )
            launched = launch_judge(world, payload)
            before = len(base.rows(world, "grants"))
            posted = run_post(world, base.judge_result(
                world, launched["request_id"], launched["tool_use_id"], "ESCALATE",
                session_id=payload["session_id"], **options,
            ))
            text = context_of(posted)
            state = base.arb.show_request(launched["request_id"])["state"]
            suite.check(
                number,
                "post %s fail-closes" % label,
                posted.returncode == 0
                and "ARBITRATION_FAILED" in text
                and "reason=%s" % code in text
                and state == "FAILED_CLOSED"
                and len(base.rows(world, "grants")) == before
                and "Do not relaunch the judge" in text,
                "state=%s text=%r" % (state, text[:240]),
            )
    finally:
        world.close()


def test_composed(suite):
    world = base.World()
    try:
        footer = {
            "schema_version": 1,
            "task_id": "task-need",
            "status": "NEEDS_JUDGMENT",
            "reason_code": "CONFLICTING_REQUIREMENTS",
            "unresolved": ["A conflicts with B"],
            "evidence_refs": ["spec.md#1"],
            "attempts": [],
            "suggested_scope": "resolve the conflict",
        }
        text = "report\n```json\n%s\n```\n" % json.dumps(footer)
        posted = run_post(world, {
            "hook_event_name": "PostToolUse",
            "tool_name": "Agent",
            "session_id": "sess-compose",
            "cwd": str(world.project),
            "tool_use_id": "worker-1",
            "tool_input": {"subagent_type": "file-reader", "description": "read", "prompt": "look"},
            "tool_response": {
                "status": "completed",
                "agentId": "a" + "3" * 16,
                "agentType": "file-reader",
                "resolvedModel": "claude-haiku-4-5",
                "content": [{"type": "text", "text": text}],
            },
        })
        candidate = context_of(posted)
        request_id = base.request_id_from_text(candidate)
        packet = base.stored_packet(request_id)
        suite.check(
            "U14",
            "needs-judgment footer emits a candidate",
            posted.returncode == 0
            and packet is not None
            and "ARBITRATION_CANDIDATE" in candidate
            and "trigger=CONFLICTING_REQUIREMENTS" in candidate
            and 'prompt exactly "ARBITRATION-PACKET request_id=%s"' % request_id in candidate
            and "<<<PACKET" not in candidate,
            candidate[:300],
        )
        if packet is None:
            return
        launch_kind, launch_reason = base.verdict(base.run_gate(world, base.judge_payload(
            world, packet, "sess-compose", "judge-compose",
        )))
        judged = run_post(world, base.judge_result(
            world, request_id, "judge-compose", "ESCALATE", session_id="sess-compose",
        ))
        decision = context_of(judged)
        grants = [row for row in base.rows(world, "grants") if row.get("request_id") == request_id]
        raw = grants[0].get("invocation_json") if grants else None
        composed = json.loads(raw) if isinstance(raw, str) else None
        suite.check(
            "U14",
            "composed ESCALATE names the grant reference",
            launch_kind == "allow"
            and isinstance(composed, dict)
            and composed.get("model") == "opus"
            and "expires in 300s" in decision
            and 'prompt exactly "ARBITRATION-GRANT request_id=%s"' % request_id in decision
            and 'subagent_type "%s"' % composed.get("subagent_type") in decision
            and "<<<PACKET" not in decision,
            "launch=%s %s text=%r" % (launch_kind, launch_reason[:120], decision[:300]),
        )
        if not isinstance(composed, dict):
            return
        call = base.agent_payload(
            world, session_id="sess-compose", tool_use_id="composed-1", transcript=world.opus_transcript,
        )
        call["tool_input"] = composed
        kind, reason = base.verdict(base.run_gate(world, call))
        second = dict(call)
        second["tool_use_id"] = "composed-2"
        second_kind, second_reason = base.verdict(base.run_gate(world, second))
        modified = dict(call)
        modified["tool_use_id"] = "composed-3"
        modified["tool_input"] = dict(composed)
        modified["tool_input"]["prompt"] = composed["prompt"] + " more"
        modified_kind, modified_reason = base.verdict(base.run_gate(world, modified))
        suite.check(
            "U14",
            "composed call allowed once and a modified prompt denied",
            kind == "allow" and second_kind == "deny" and modified_kind == "deny",
            "first=%s second=%s %s modified=%s %s" % (kind, second_kind, second_reason[:80], modified_kind, modified_reason[:80]),
        )
    finally:
        world.close()


def _bash(world, spec, status, session, tool_use):
    binary = str(world.home / ".claude" / "hooks" / "bin" / "cursor-run")
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "session_id": session,
        "cwd": str(world.project),
        "tool_use_id": tool_use,
        "tool_input": {"command": "%s %s" % (binary, spec)},
        "tool_response": {"stdout": json.dumps({"status": status}) + "\n"},
    }


def test_cursor_run(suite):
    world = base.World()
    try:
        first = world.project / "spec-a.md"
        second = world.project / "spec-b.md"
        first.write_text("approach a\n", encoding="utf-8")
        second.write_text("approach b\n", encoding="utf-8")
        one = run_post(world, _bash(world, first, "failed", "sess-t04", "bash-a"))
        two = run_post(world, _bash(world, second, "failed", "sess-t04", "bash-b"))
        suite.check(
            "T04",
            "two different cursor-run specs become a candidate",
            context_of(one) == "" and "ARBITRATION_CANDIDATE" in context_of(two) and two.returncode == 0,
            "one=%r two=%r" % (context_of(one)[:120], context_of(two)[:200]),
        )
        same = world.project / "spec-same.md"
        same.write_text("same approach\n", encoding="utf-8")
        left = run_post(world, _bash(world, same, "failed", "sess-same", "bash-s1"))
        right = run_post(world, _bash(world, same, "failed", "sess-same", "bash-s2"))
        suite.check(
            "T04",
            "the same spec twice is not a candidate",
            context_of(left) == "" and context_of(right) == "",
            "left=%r right=%r" % (context_of(left)[:120], context_of(right)[:120]),
        )
        timed = world.project / "spec-time.md"
        timed.write_text("slow\n", encoding="utf-8")
        timeout = run_post(world, _bash(world, timed, "timeout", "sess-time", "bash-time"))
        suite.check("T04", "timeout status emits nothing", timeout.returncode == 0 and context_of(timeout) == "", context_of(timeout)[:160])
    finally:
        world.close()


def test_retry_wording(suite):
    world = base.World()
    try:
        cases = (
            ("haiku", "on haiku (or answer directly)", False),
            ("sonnet", "on sonnet (or answer directly)", False),
            ("opus", "on the current non-Opus model", True),
            ("", "on the current non-Opus model", True),
        )
        for model, needle, generic in cases:
            payload = base.agent_payload(
                world, model="opus", session_id="sess-retry-" + (model or "missing"),
                description="retry " + (model or "missing"), prompt="retry body " + (model or "missing"),
            )
            launched = launch_judge(world, payload)
            posted = run_post(world, base.judge_result(
                world, launched["request_id"], launched["tool_use_id"], "RETRY",
                session_id=payload["session_id"], recommended_model=model,
            ))
            text = context_of(posted)
            suite.check(
                "U12",
                "RETRY on %s names a non-Opus model" % (model or "missing"),
                posted.returncode == 0
                and "decision=RETRY" in text
                and needle in text
                and "using: follow the decision" in text
                and "on the same model" not in text
                and "on opus" not in text.lower()
                and ("Do not use Opus" in text)
                and (generic or model in text),
                text[:300],
            )
    finally:
        world.close()


def test_general_purpose_footer(suite):
    world = base.World()
    try:
        def posted(agent_type, footer, tool_use):
            text = "report\n```json\n%s\n```\n" % json.dumps(footer)
            return run_post(world, {
                "hook_event_name": "PostToolUse",
                "tool_name": "Agent",
                "session_id": "sess-" + tool_use,
                "cwd": str(world.project),
                "tool_use_id": tool_use,
                "tool_input": {"subagent_type": agent_type, "description": "work", "prompt": "look"},
                "tool_response": {
                    "status": "completed",
                    "agentId": "a" + "4" * 16,
                    "agentType": agent_type,
                    "resolvedModel": "claude-haiku-4-5",
                    "content": [{"type": "text", "text": text}],
                },
            })

        empty = {
            "schema_version": 1,
            "task_id": "task-empty",
            "status": "NEEDS_JUDGMENT",
            "reason_code": "UNRESOLVED",
            "unresolved": [],
            "attempts": [],
        }
        filled = dict(empty)
        filled["task_id"] = "task-filled"
        filled["unresolved"] = ["which index"]
        owned = posted("file-reader", empty, "owned-empty")
        generic = posted("general-purpose", empty, "generic-empty")
        claimed = posted("general-purpose", filled, "generic-filled")
        claimed_text = context_of(claimed)
        body = base.packet_object(base.stored_packet(base.request_id_from_text(claimed_text))) or {}
        suite.check(
            "T03",
            "general-purpose footer needs attempts or unresolved",
            owned.returncode == 0
            and "ARBITRATION_CANDIDATE" in context_of(owned)
            and generic.returncode == 0
            and context_of(generic) == ""
            and "ARBITRATION_CANDIDATE" in claimed_text
            and body.get("current_model_observed") == "claude-haiku-4-5"
            and body.get("objective_failure_evidence") == [{"unresolved": "which index"}],
            "owned=%r generic=%r claimed=%r" % (context_of(owned)[:120], context_of(generic)[:80], claimed_text[:220]),
        )
    finally:
        world.close()


def test_import_failure(suite):
    tmp = Path(tempfile.mkdtemp(prefix="opus-post-import-"))
    try:
        dest = tmp / "opus-arbitration-post.py"
        dest.write_text(POST.read_text(encoding="utf-8"), encoding="utf-8")
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run(
            [sys.executable, str(dest)],
            input="{}",
            text=True,
            capture_output=True,
            env=env,
            cwd=str(tmp),
            timeout=15,
            check=False,
        )
        suite.check(
            "POST",
            "import failure exits 0 with no output",
            proc.returncode == 0 and proc.stdout == "" and proc.stderr == "",
            "exit=%s stdout=%r stderr=%r" % (proc.returncode, proc.stdout[:200], proc.stderr[:300]),
        )
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_judge_file(suite):
    text = JUDGE.read_text(encoding="utf-8")
    front = text.split("---", 2)[1]
    suite.check("U33", "opus-judge pins opus and no tools", "\nmodel: opus\n" in front and "\ntools: []\n" in front, front)


def test_worker_writes(suite):
    world = base.World()
    try:
        payload = base.agent_payload(
            world, model="opus", session_id="sess-worker", description="worker", prompt="analyze",
        )
        _call, _request, result = base.escalate(world, payload)
        suite.check("U34", "worker setup escalates", result.get("decision") == "ESCALATE", str(result)[:200])
        agent_id = "a" + "7" * 16
        base.arb.record_agent_result({
            "session_id": "sess-worker",
            "cwd": str(world.project),
            "tool_use_id": "consume-worker",
            "tool_response": {
                "status": "completed",
                "agentId": agent_id,
                "agentType": "general-purpose",
                "resolvedModel": "claude-opus-5-5",
            },
        })
        # The grant is not consumed yet, so this run is routine. Consume, then record again.
        kind, _reason = base.verdict(base.run_gate(world, {**payload, "tool_use_id": "consume-worker"}))
        base.arb.record_agent_result({
            "session_id": "sess-worker",
            "cwd": str(world.project),
            "tool_use_id": "consume-worker",
            "tool_response": {
                "status": "completed",
                "agentId": agent_id,
                "agentType": "general-purpose",
                "resolvedModel": "claude-opus-5-5",
            },
        })
        runs = [row for row in base.rows(world, "agent_runs") if row["agent_id"] == agent_id]
        suite.check("U34", "escalated worker is recorded", kind == "allow" and runs and runs[0]["kind"] == "escalated", str(runs))

        def write(path):
            body = {
                "tool_name": "Write",
                "tool_input": {"file_path": path, "content": "x = 1\n"},
                "agent_id": agent_id,
                "session_id": "sess-worker",
                "cwd": str(world.project),
            }
            env = world.env()
            env["CLAUDE_PROJECT_DIR"] = str(world.project)
            return subprocess.run(
                [sys.executable, str(REQUIRE)],
                input=json.dumps(body),
                text=True,
                capture_output=True,
                env=env,
                cwd=str(world.project),
                timeout=20,
                check=False,
            )

        code = write(str(world.project / "lib" / "foo.py"))
        suite.check(
            "U34",
            "escalated worker still cannot write a py file",
            code.returncode == 2 and code.stdout == "",
            "exit=%s stdout=%r stderr=%r" % (code.returncode, code.stdout[:120], code.stderr[:120]),
        )
        hooks = write(str(world.home / ".claude" / "hooks" / "x.py"))
        asked = ""
        try:
            asked = json.loads(hooks.stdout)["hookSpecificOutput"]["permissionDecision"]
        except (json.JSONDecodeError, KeyError, TypeError):
            asked = ""
        suite.check(
            "U35",
            "escalated worker still cannot write a hook",
            hooks.returncode == 0 and asked == "ask",
            "exit=%s stdout=%r" % (hooks.returncode, hooks.stdout[:200]),
        )
    finally:
        world.close()


def test_failure_and_bash(suite):
    world = base.World()
    try:
        payload = base.agent_payload(
            world, model="opus", session_id="sess-fail", description="fail", prompt="judge will fail",
        )
        launched = launch_judge(world, payload)
        failed = run_post(world, {
            "hook_event_name": "PostToolUseFailure",
            "tool_name": "Agent",
            "session_id": "sess-fail",
            "cwd": str(world.project),
            "tool_use_id": launched["tool_use_id"],
            "tool_input": payload["tool_input"],
            "error": "agent crashed",
        })
        state = base.arb.show_request(launched["request_id"])["state"]
        suite.check(
            "FAIL",
            "judge tool failure is FAILED_CLOSED and prints nothing",
            failed.returncode == 0 and failed.stdout == "" and state == "FAILED_CLOSED",
            "exit=%s state=%s stdout=%r" % (failed.returncode, state, failed.stdout[:120]),
        )
    finally:
        world.close()
    quiet = base.World()
    try:
        proc = run_post(quiet, {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "session_id": "sess-echo",
            "cwd": str(quiet.project),
            "tool_input": {"command": "echo hi"},
            "tool_response": {"stdout": "hi\n"},
        })
        ledger = quiet.home / ".claude" / "logs" / "opus-arbitration" / "ledger.db"
        suite.check(
            "BASH",
            "non-cursor-run bash prints nothing and creates no ledger",
            proc.returncode == 0 and proc.stdout == "" and not ledger.exists(),
            "exit=%s stdout=%r ledger=%s" % (proc.returncode, proc.stdout[:80], ledger.exists()),
        )
    finally:
        quiet.close()
    crash = base.World()
    try:
        ledger = crash.home / ".claude" / "logs" / "opus-arbitration"
        ledger.mkdir(parents=True)
        (ledger / "ledger.db").write_text("this is not a database", encoding="utf-8")
        proc = run_post(crash, {
            "hook_event_name": "PostToolUse",
            "tool_name": "Agent",
            "session_id": "sess-crash",
            "cwd": str(crash.project),
            "tool_use_id": "tool-crash",
            "tool_response": {
                "status": "completed",
                "agentId": "a" + "8" * 16,
                "agentType": "file-reader",
                "content": [{"type": "text", "text": "hi"}],
            },
        })
        suite.check(
            "CRASH",
            "corrupt ledger exits 0 with no output",
            proc.returncode == 0 and proc.stdout == "",
            "exit=%s stdout=%r stderr=%r" % (proc.returncode, proc.stdout[:80], proc.stderr[:200]),
        )
    finally:
        crash.close()


def test_session_cost(suite):
    root = Path(tempfile.mkdtemp(prefix="arb-cost-"))
    home = root / "home"
    project = root / "leaf-project"
    stub = root / "ccusage"
    payload = root / "payload.json"
    prices = root / "prices.json"
    try:
        cost.stub_script(stub)
        cost.pricing_file(prices)
        cost.build_core(root / "projects", home, project)
        cost.ccusage_payload(
            payload,
            [
                cost.entry("2026-01-01T00:00:00.000Z", "claude-opus-5-5", 10, 1, 0.5),
                cost.entry("2026-01-01T00:01:00.000Z", "claude-haiku-5-5", 5, 6, 1.25),
                cost.entry("2026-01-01T00:02:00.000Z", "claude-sonnet-5-5", 3, 3, 0.25),
                cost.entry("2026-01-01T00:03:00.000Z", "claude-opus-5-5", 4, 4, 0.1),
            ],
            2.1,
            36,
        )
        env = cost.stub_env(payload)
        missing = cost.base_config(root / "projects", home, stub, prices)
        missing["ledger_path"] = str(root / "missing.db")
        bare = json.loads(cost.run([cost.SID, "--json"], missing, env).stdout)
        human_bare = cost.run([cost.SID], missing, env)
        ledger = root / "ledger.db"
        conn = sqlite3.connect(ledger)
        conn.executescript(
            """
            CREATE TABLE requests (request_id TEXT, session_id TEXT, decision TEXT, state TEXT);
            CREATE TABLE grants (grant_id TEXT, session_id TEXT, consumed_tool_use_id TEXT, expires REAL);
            CREATE TABLE agent_runs (agent_id TEXT, session_id TEXT, kind TEXT);
            """
        )
        conn.execute(
            "INSERT INTO requests VALUES (?,?,?,?)",
            ("esc-1", cost.SID, "CONTINUE", "JUDGED_CONTINUE"),
        )
        conn.execute(
            "INSERT INTO requests VALUES (?,?,?,?)",
            ("esc-2", cost.SID, "ESCALATE", "GRANT_CONSUMED"),
        )
        conn.execute(
            "INSERT INTO requests VALUES (?,?,?,?)",
            ("esc-3", cost.SID, "STOP", "FAILED_CLOSED"),
        )
        conn.execute(
            "INSERT INTO grants VALUES (?,?,?,?)",
            ("grn-1", cost.SID, "toolu-used", 9_999_999_999),
        )
        conn.execute(
            "INSERT INTO grants VALUES (?,?,?,?)",
            ("grn-2", cost.SID, None, 1),
        )
        conn.execute("INSERT INTO agent_runs VALUES (?,?,?)", ("child", cost.SID, "judge"))
        conn.execute("INSERT INTO agent_runs VALUES (?,?,?)", ("leaf", cost.SID, "escalated"))
        conn.commit()
        conn.close()
        present = dict(missing)
        present["ledger_path"] = str(ledger)
        report = json.loads(cost.run([cost.SID, "--json"], present, env).stdout)
        human = cost.run([cost.SID], present, env)
        arb = report.get("arbitration") or {}
        child = cost.agent_by_id(report, "child")
        leaf = cost.agent_by_id(report, "leaf")
        main = cost.agent_by_id(report, "main")
        builtin = cost.agent_by_id(report, "builtin")
        suite.check(
            "U40",
            "fixture totals are unchanged when arbitration is added",
            bare["totals"] == report["totals"]
            and bare.get("arbitration") == {"available": False}
            and "available: false" in human_bare.stdout
            and "unarbitrated_opus_agent" not in human_bare.stdout,
            "bare=%s present=%s" % (bare.get("totals"), report.get("totals")),
        )
        costs = arb.get("costs") or {}
        suite.check(
            "U40",
            "arbitration categories match per-agent costs",
            arb.get("available") is True
            and costs.get("judge") == child["executor"]["cost"]
            and costs.get("escalated_opus") == leaf["executor"]["cost"]
            and costs.get("main") == main["executor"]["cost"]
            and arb.get("candidates") == 3
            and arb.get("judge_runs") == 1
            and arb.get("decisions") == {"CONTINUE": 1, "RETRY": 0, "ESCALATE": 1, "STOP": 1}
            and arb.get("grants") == {"issued": 2, "consumed": 1, "expired": 1}
            and arb.get("failed_closed") == 1
            and "coverage_warning: unarbitrated_opus_agent builtin" in arb.get("coverage_warnings", [])
            and "Arbitration" in human.stdout
            and "FAILED_CLOSED: 1" in human.stdout,
            "arb=%s" % arb,
        )
        unclassified = costs.get("unclassified")
        others = 0.0
        for agent in report["agents"]:
            if agent["id"] not in ("main", "child", "leaf"):
                value = agent["executor"]["cost"]
                if isinstance(value, (int, float)):
                    others += value
        suite.check(
            "U40",
            "unclassified cost is the remaining agents and is not double counted",
            unclassified == others and costs.get("judge") != costs.get("main"),
            "unclassified=%s others=%s" % (unclassified, others),
        )
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def _observed(world, request_id):
    path = world.home / ".claude" / "logs" / "opus-arbitration" / "events.jsonl"
    if not path.exists():
        return []
    found = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if "updated_input_observed" not in line or request_id not in line:
            continue
        try:
            found.append(json.loads(line).get("value"))
        except json.JSONDecodeError:
            continue
    return found


def test_updated_input_observed(suite):
    world = base.World()
    try:
        expanded_call = base.agent_payload(
            world, model="opus", session_id="sess-obs-exp", description="expanded", prompt="expanded body",
        )
        launched = launch_judge(world, expanded_call)
        posted = run_post(world, {
            **base.judge_result(
                world, launched["request_id"], launched["tool_use_id"], "CONTINUE",
                session_id="sess-obs-exp",
            ),
            "tool_input": {
                "subagent_type": "opus-judge",
                "model": "opus",
                "description": "Judge",
                "prompt": launched["packet"],
            },
        })
        text = context_of(posted)
        suite.check(
            "REF",
            "expanded judge prompt is observed and still correlates",
            posted.returncode == 0
            and "decision=CONTINUE" in text
            and _observed(world, launched["request_id"]) == ["expanded"],
            "text=%r events=%s" % (text[:180], _observed(world, launched["request_id"])),
        )
        reference_call = base.agent_payload(
            world, model="opus", session_id="sess-obs-ref", description="reference", prompt="reference body",
        )
        denied = base.run_gate(world, reference_call)
        kind, reason = base.verdict(denied)
        request_id, packet = base.pending_from_deny(reason)
        tool_use = "judge-" + request_id
        opened = base.run_gate(world, base.agent_payload(
            world, subagent_type="opus-judge", model="opus", session_id="sess-obs-ref",
            description="Judge %s" % request_id,
            prompt="ARBITRATION-PACKET request_id=%s" % request_id,
            tool_use_id=tool_use, transcript=world.opus_transcript,
        ))
        hook = base.hook_body(opened)
        updated = hook.get("updatedInput") if isinstance(hook, dict) else None
        posted = run_post(world, {
            **base.judge_result(
                world, request_id, tool_use, "STOP", session_id="sess-obs-ref",
            ),
            "tool_input": {
                "subagent_type": "opus-judge",
                "model": "opus",
                "description": "Judge %s" % request_id,
                "prompt": "ARBITRATION-PACKET request_id=%s" % request_id,
            },
        })
        text = context_of(posted)
        suite.check(
            "REF",
            "reference judge prompt is observed and still correlates",
            kind == "deny"
            and isinstance(updated, dict)
            and updated.get("prompt") == packet
            and posted.returncode == 0
            and "decision=STOP" in text
            and _observed(world, request_id) == ["reference"],
            "text=%r events=%s stdout=%s" % (text[:180], _observed(world, request_id), opened.stdout[:180]),
        )
    finally:
        world.close()


def test_unarbitrated_alert(suite):
    world = base.World()
    try:
        agent_id = "a" + "3" * 16
        payload = base.judge_result(
            world, "req-alert", "tu-alert", "CONTINUE",
            session_id="sess-alert", agent_id=agent_id,
        )
        payload["tool_response"]["agentType"] = "general-purpose"
        payload["tool_response"]["status"] = "completed"
        payload["tool_response"]["resolvedModel"] = "claude-opus-5-5"
        posted = run_post(world, payload)
        text = context_of(posted)
        events = (world.home / ".claude" / "logs" / "opus-arbitration" / "events.jsonl").read_text(encoding="utf-8")
        expected = "ARBITRATION_ALERT unarbitrated Opus run agent=%s; report this to the user." % agent_id
        ledger = world.home / ".claude" / "logs" / "opus-arbitration" / "ledger.db"
        summary = cost.MOD.load_arbitration("sess-alert", {}, {"ledger_path": str(ledger)})
        suite.check(
            "H8",
            "completed unarbitrated opus alerts and is counted",
            posted.returncode == 0
            and text == expected
            and "unarbitrated_opus_run" in events
            and agent_id in events
            and summary.get("unarbitrated_opus_runs") == 1,
            "text=%r count=%s" % (text[:240], summary.get("unarbitrated_opus_runs")),
        )
        async_id = "a" + "4" * 16
        async_payload = base.judge_result(
            world, "req-async", "tu-async", "CONTINUE",
            session_id="sess-alert", agent_id=async_id, async_launched=True,
        )
        async_payload["tool_response"]["agentType"] = "general-purpose"
        async_payload["tool_response"]["resolvedModel"] = "claude-opus-4"
        async_text = context_of(run_post(world, async_payload))
        suite.check(
            "H8",
            "async launched opus alerts",
            async_text == "ARBITRATION_ALERT unarbitrated Opus run agent=%s; report this to the user." % async_id,
            async_text[:240],
        )
        sonnet_id = "a" + "5" * 16
        sonnet_payload = base.judge_result(
            world, "req-sonnet", "tu-sonnet", "CONTINUE",
            session_id="sess-alert", agent_id=sonnet_id, resolved_model="claude-sonnet-5-5",
        )
        sonnet_payload["tool_response"]["agentType"] = "file-reader"
        sonnet_text = context_of(run_post(world, sonnet_payload))
        suite.check("H8", "sonnet result does not alert", sonnet_text == "", sonnet_text[:160])
    finally:
        world.close()


def test_failed_names_and_deadline(suite):
    world = base.World()
    try:
        base.arb.record_event("seed-fail-name", session_id="sess-fail-name")
        db = world.home / ".claude" / "logs" / "opus-arbitration" / "ledger.db"
        now = time.time()
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO requests (request_id, session_id, state, created, updated) VALUES (?,?,?,?,?)",
            ("req-fail-name", "sess-fail-name", "GRANT_CONSUMED", now, now),
        )
        conn.execute(
            "INSERT INTO grants (grant_id, request_id, session_id, consumed_tool_use_id, uses_left) VALUES (?,?,?,?,?)",
            ("grant-fail-name", "req-fail-name", "sess-fail-name", "tu-fail-name", 0),
        )
        conn.commit()
        conn.close()
        failed = run_post(world, {
            "hook_event_name": "PostToolUseFailure",
            "tool_name": "Agent",
            "session_id": "sess-fail-name",
            "cwd": str(world.project),
            "tool_use_id": "tu-fail-name",
            "tool_input": {"name": "esc-fail", "subagent_type": "general-purpose", "prompt": "x"},
            "error": "worker crashed",
        })
        runs = [row for row in base.rows(world, "agent_runs") if row.get("name") == "esc-fail"]
        state = {row["request_id"]: row["state"] for row in base.rows(world, "requests")}
        suite.check(
            "K3",
            "failed escalated worker is stored by name",
            failed.returncode == 0 and failed.stdout == ""
            and len(runs) == 1
            and runs[0]["kind"] == "escalated"
            and runs[0]["status"] == "failed"
            and runs[0]["agent_id"] == "failure:tu-fail-name"
            and state.get("req-fail-name") == "WORKER_FAILED",
            "runs=%s state=%s out=%r" % (runs, state.get("req-fail-name"), failed.stdout[:80]),
        )
        safe_id = "a" + "6" * 16
        base.arb.record_agent_result({
            "session_id": "sess-fail-name",
            "cwd": str(world.project),
            "tool_use_id": "tu-safe-name",
            "tool_input": {"name": "esc-fail"},
            "tool_response": {
                "status": "completed",
                "agentId": safe_id,
                "agentType": "file-reader",
                "resolvedModel": "claude-sonnet-5-5",
            },
        })
        kind, reason = base.verdict(base.run_gate(
            world, base.send_payload(world, "esc-fail", session_id="sess-fail-name"),
        ))
        suite.check(
            "K3",
            "one blocked name row denies resume",
            kind == "deny" and "OPUS_RESUME_DENIED" in reason,
            "kind=%s reason=%s" % (kind, reason[:160]),
        )
        judge_id = "a" + "2" * 16
        judged = run_post(world, {
            "hook_event_name": "PostToolUseFailure",
            "tool_name": "Agent",
            "session_id": "sess-fail-name",
            "cwd": str(world.project),
            "tool_use_id": "tu-judge-fail-name",
            "tool_input": {"name": "judge-fail", "subagent_type": "opus-judge"},
            "tool_response": {"agentId": judge_id},
            "error": "judge crashed",
        })
        judge_runs = [row for row in base.rows(world, "agent_runs") if row.get("name") == "judge-fail"]
        judge_kind, judge_reason = base.verdict(base.run_gate(
            world, base.send_payload(world, "judge-fail", session_id="sess-fail-name"),
        ))
        suite.check(
            "K3",
            "failed judge keeps agentId and blocks its name",
            judged.returncode == 0 and judged.stdout == ""
            and len(judge_runs) == 1
            and judge_runs[0]["agent_id"] == judge_id
            and judge_runs[0]["kind"] == "judge"
            and judge_runs[0]["status"] == "failed"
            and judge_kind == "deny" and "OPUS_RESUME_DENIED" in judge_reason,
            "runs=%s reason=%s" % (judge_runs, judge_reason[:160]),
        )

        source = POST.read_text(encoding="utf-8")
        before = len(base.rows(world, "events"))
        import importlib.util
        spec = importlib.util.spec_from_file_location("opus_arbitration_post_deadline", POST)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        origin = time.monotonic()
        calls = {"n": 0}

        def fake_monotonic():
            calls["n"] += 1
            if calls["n"] == 1:
                return origin
            return origin + 7

        saved_clock = base.arb.time.monotonic
        base.arb.time.monotonic = fake_monotonic
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Agent",
            "session_id": "sess-deadline",
            "cwd": str(world.project),
            "tool_use_id": "tu-deadline",
            "tool_input": {"subagent_type": "file-reader", "prompt": "x", "description": "d"},
            "tool_response": {
                "status": "completed",
                "agentId": "a" + "7" * 16,
                "agentType": "file-reader",
                "resolvedModel": "claude-haiku-4-5",
                "content": [{"type": "text", "text": "done"}],
            },
        }
        saved_in, saved_out = sys.stdin, sys.stdout
        sys.stdin = io.StringIO(json.dumps(payload))
        sys.stdout = io.StringIO()
        try:
            code = mod.main()
            output = sys.stdout.getvalue()
        finally:
            sys.stdin = saved_in
            sys.stdout = saved_out
            base.arb.time.monotonic = saved_clock
        after = base.rows(world, "events")
        names = " ".join(row.get("event") or "" for row in after)
        suite.check(
            "K6",
            "post deadline is 6s and records nothing once exceeded",
            "invocation(deadline=6.0)" in source
            and code == 0 and output == ""
            and len(after) == before and "post_hook_error" not in names,
            "code=%s out=%r before=%s after=%s calls=%s" % (code, output[:80], before, len(after), calls["n"]),
        )
    finally:
        world.close()


def test_auto_escalate(suite):
    world = base.World()
    try:
        registered = base.arb.register_candidate(
            session_id="sess-auto-esc",
            task_id="task-auto-esc",
            trigger="T03",
            reason_code="CONFLICTING_REQUIREMENTS",
            requester_agent_type="file-reader",
            summary="resolve",
            task_summary="resolve the conflict",
            project=str(world.project),
        )
        request = base.arb.promote_pending(registered["request"]["request_id"]) if registered.get("ok") else None
        request_id = request["request_id"] if isinstance(request, dict) else ""
        launch_kind, launch_reason = base.verdict(base.run_gate(world, base.judge_payload(
            world, request.get("packet_text") if isinstance(request, dict) else "",
            "sess-auto-esc", "judge-auto-esc", wrapped=False,
        )))
        posted = run_post(world, {
            **base.judge_result(world, request_id, "judge-auto-esc", "ESCALATE", session_id="sess-auto-esc"),
            "permission_mode": "auto",
        })
        decision = context_of(posted)
        grants = [row for row in base.rows(world, "grants") if row.get("request_id") == request_id]
        raw = grants[0].get("invocation_json") if grants else None
        suite.check(
            "AUTO",
            "auto mode escalate gives the invocation verbatim",
            launch_kind == "allow"
            and isinstance(raw, str)
            and raw in decision
            and "copy this invocation exactly" in decision
            and "ARBITRATION-GRANT request_id=" not in decision,
            "launch=%s %s text=%r" % (launch_kind, launch_reason[:80], decision[:300]),
        )
    finally:
        world.close()


def main():
    suite = base.Suite()
    test_decisions(suite)
    test_retry_wording(suite)
    test_general_purpose_footer(suite)
    test_import_failure(suite)
    test_composed(suite)
    test_cursor_run(suite)
    test_judge_file(suite)
    test_worker_writes(suite)
    test_failure_and_bash(suite)
    test_session_cost(suite)
    test_updated_input_observed(suite)
    test_unarbitrated_alert(suite)
    test_failed_names_and_deadline(suite)
    test_auto_escalate(suite)
    suite.check(
        "REF",
        "gate output never contains permissionDecision allow",
        not any(base._records_allow(text) for text in base.GATE_STDOUTS),
        "outputs=%s" % len(base.GATE_STDOUTS),
    )
    print("Tally: %s passed, %s failed" % (suite.passed, suite.failed))
    return 0 if suite.failed == 0 and suite.passed > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
