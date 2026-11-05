#!/usr/bin/env python3
"""Rollback checks for planning-off, arbitration disable, and a v2 revert.

Prints a table and `Tally: N passed, M failed`. Exits non-zero on failure.
Each case builds its own temp HOME and drives the hooks through subprocess.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))

import test_opus_arbitration as gate

V2 = "fd4cce0"
V2_SUITES = (
    "test_opus_arbitration.py",
    "test_opus_arbitration_post.py",
    "test_opus_workflow_guard.py",
    "test_route_tools.py",
)


class Suite:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []

    def check(self, number, name, ok, detail=""):
        status = "PASS" if ok else "FAIL"
        print("%4s  %-78s %s" % (number, name, status))
        if ok:
            self.passed += 1
        else:
            self.failed += 1
            self.failures.append("%s %s: %s" % (number, name, detail))
            if detail:
                print("      %s" % detail)


def _plan_files(world, task_id):
    directory = world.home / ".claude" / "plans" / "router" / task_id
    found = {}
    if not directory.exists():
        return found
    for path in sorted(directory.glob("v*.json")):
        if path.is_file():
            found[path.name] = path.read_bytes()
    return found


def _stable(items, key):
    ordered = sorted(items, key=lambda row: str(row.get(key)))
    return json.dumps(ordered, sort_keys=True, default=str)


def _ledger_snapshot(world):
    return {
        "requests": _stable(gate.rows(world, "requests"), "request_id"),
        "grants": _stable(gate.rows(world, "grants"), "grant_id"),
        "plans": _stable(gate.rows(world, "plans"), "task_id"),
        "stages": _stable(gate.rows(world, "stages"), "stage_id"),
    }


def _ids(world):
    return {row["request_id"] for row in gate.rows(world, "requests")}


def test_rb1(suite):
    world = gate.World()
    try:
        gate._set_planning(world, True)
        gate._planner_file(world)
        task_id = "rb1-task"
        record = gate._publish_plan("sess-rb1-plan", task_id)
        granted_payload = gate._planner_call(
            world,
            session_id="sess-rb1-grant",
            tool_use_id="rb1-grant-open",
            prompt="ROUTER-TASK rb1-grant\nPlan the granted work",
            description="Plan the granted work",
        )
        granted, grant_id, grant_result = gate.escalate(world, granted_payload)
        grant_rows = [row for row in gate.rows(world, "grants") if row.get("request_id") == grant_id]
        files_before = _plan_files(world, task_id)
        ledger_before = _ledger_snapshot(world)
        gate._set_planning(world, False)
        files_after = _plan_files(world, task_id)
        ledger_after = _ledger_snapshot(world)
        suite.check(
            "RB1e",
            "turning planning off leaves plan files and ledger rows",
            record.get("version") == 1
            and files_before
            and files_before == files_after
            and ledger_before == ledger_after,
            "files=%s" % sorted(files_before),
        )
        before_ids = _ids(world)
        kind, reason = gate.verdict(gate.run_gate(world, gate._planner_call(
            world, session_id="sess-rb1-a", tool_use_id="rb1-a",
            prompt="ROUTER-TASK rb1-a\nPlan a new task", description="Plan a new task",
        )))
        suite.check(
            "RB1a",
            "a planner call from Sonnet is disabled and creates no request",
            kind == "deny" and reason == "OPUS_PLANNER_DISABLED" and _ids(world) == before_ids,
            "kind=%s reason=%s ids=%s" % (kind, reason[:160], len(_ids(world)) - len(before_ids)),
        )
        use_kind, use_reason = gate.verdict(gate.run_gate(
            world, {**granted, "tool_use_id": "rb1-use"},
        ))
        grant_state = next(
            (row.get("state") for row in gate.rows(world, "requests") if row.get("request_id") == grant_id),
            None,
        )
        suite.check(
            "RB1b",
            "a minted PLAN_ONLY grant cannot be consumed after planning is off",
            grant_result.get("decision") == "ESCALATE"
            and grant_rows and grant_rows[0].get("purpose") == "PLAN_ONLY"
            and use_kind == "deny" and use_reason == "OPUS_PLANNER_DISABLED"
            and gate._grant_uses(world, grant_id) == 1
            and grant_state == "GRANT_READY",
            "use=%s %s uses=%s state=%s" % (
                use_kind, use_reason[:120], gate._grant_uses(world, grant_id), grant_state,
            ),
        )
        version = record["version"]
        agent_id = "a" + "c" * 16
        ready = gate._reserve_worker(
            world, "sess-rb1-replan", task_id, version, "draft", "rb1-wf", "rb1-prompt", agent_id,
        )
        stopped = gate._worker_stop(
            world, "sess-rb1-replan", agent_id,
            gate._replan_footer(task_id, "draft", version),
            world.opus_transcript,
        )
        surfaced = gate._main_pretool(world, "sess-rb1-replan", "rb1-main")
        text = (gate.hook_body(surfaced) or {}).get("additionalContext") or ""
        replans = [
            row for row in gate.rows(world, "requests")
            if row.get("candidate_type") == "REPLAN_REQUEST"
        ]
        suite.check(
            "RB1c",
            "a revision footer is REPLAN_BLOCKED reason=planning_disabled",
            ready and stopped.returncode == 0 and not replans
            and "REPLAN_BLOCKED task=%s stage=draft reason=planning_disabled" % task_id in text,
            "ready=%s exit=%s text=%s" % (ready, stopped.returncode, text[:240]),
        )
        opus_kind, opus_reason = gate.verdict(gate.run_gate(world, gate.agent_payload(
            world, model="opus", session_id="sess-rb1-opus", tool_use_id="rb1-opus",
            description="Do the work", prompt="do the work",
        )))
        denied = gate._call(
            world, "Workflow", {"script": "echo hi\n"}, world.sonnet_transcript,
            "sess-rb1-wf-deny", "rb1-wf-deny",
        )
        deny_kind, deny_reason = gate.verdict(gate.run_gate(world, denied))
        allowed = gate._call(
            world, "Workflow", {"script": "echo hi\n"}, world.sonnet_transcript,
            "sess-rb1-wf-ok", "rb1-wf-ok",
        )
        gate._plant_guard_attest(world, "sess-rb1-wf-ok", "rb1-wf-ok")
        allow_kind, allow_reason = gate.verdict(gate.run_gate(world, allowed))
        suite.check(
            "RB1d",
            "opus still needs arbitration and a workflow still passes or denies",
            opus_kind == "deny" and "ARBITRATION_REQUIRED" in opus_reason
            and deny_kind == "deny" and deny_reason == "WORKFLOW_GUARD_NOT_ACTIVE"
            and allow_kind == "allow",
            "opus=%s %s deny=%s %s allow=%s %s" % (
                opus_kind, opus_reason[:80], deny_kind, deny_reason[:80], allow_kind, allow_reason[:80],
            ),
        )
        suite.check(
            "RB1e",
            "plan history files stay after the disabled calls",
            _plan_files(world, task_id) == files_before,
            "files=%s" % sorted(_plan_files(world, task_id)),
        )
    finally:
        world.close()


def _tally_line(text):
    for line in reversed(text.splitlines()):
        if line.startswith("Tally:"):
            return line
    return ""


def _tally_ok(line):
    parts = line.replace(",", "").split()
    if len(parts) != 5 or parts[0] != "Tally:" or parts[2] != "passed" or parts[4] != "failed":
        return False
    return parts[1].isdigit() and parts[3] == "0" and int(parts[1]) > 0


def _physical(path):
    """macOS /tmp and /var are symlinks. Path checks need the real directory."""
    return Path(os.path.realpath(path))


def _sandbox_home(checkout):
    """Sandbox HOME: v2 tree at .claude, cursor-agent.md, and the vault dir."""
    sandbox = _physical(tempfile.mkdtemp(prefix="rb3-home-"))
    claude = sandbox / ".claude"
    claude.mkdir()
    rsync_cmd = ["rsync", "-a", "--exclude", ".git", str(checkout) + "/", str(claude) + "/"]
    print("RB3 command: %s" % " ".join(rsync_cmd))
    synced = subprocess.run(
        rsync_cmd,
        capture_output=True, text=True, timeout=180, check=False,
    )
    if synced.returncode != 0:
        raise RuntimeError(synced.stderr[-300:])
    init_cmd = ["git", "-C", str(claude), "init", "-q"]
    print("RB3 command: %s" % " ".join(init_cmd))
    init = subprocess.run(
        init_cmd,
        capture_output=True, text=True, timeout=30, check=False,
    )
    if init.returncode != 0:
        raise RuntimeError(init.stderr[-300:])
    agent = Path("/Users/wujincheng/.agents/cursor-agent.md")
    if agent.is_file():
        dest = sandbox / ".agents"
        dest.mkdir()
        shutil.copy(agent, dest / "cursor-agent.md")
    (sandbox / "workplace" / "project" / "Obsidian_doc").mkdir(parents=True)
    return sandbox


def test_rb3(suite):
    clone = _physical(tempfile.mkdtemp(prefix="rb3-clone-"))
    sandbox = None
    ledger_home = _physical(tempfile.mkdtemp(prefix="rb3-ledger-"))
    try:
        clone_cmd = ["git", "clone", "--quiet", str(REPO), str(clone)]
        print("RB3 command: %s" % " ".join(clone_cmd))
        cloned = subprocess.run(clone_cmd, capture_output=True, text=True, timeout=180, check=False)
        checkout_cmd = ["git", "-C", str(clone), "checkout", "--quiet", V2]
        print("RB3 command: %s" % " ".join(checkout_cmd))
        checked = subprocess.run(checkout_cmd, capture_output=True, text=True, timeout=60, check=False)
        head = ""
        if checked.returncode == 0:
            shown = subprocess.run(
                ["git", "-C", str(clone), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            head = shown.stdout.strip()
        suite.check(
            "RB3",
            "temp clone checks out %s" % V2,
            cloned.returncode == 0 and checked.returncode == 0 and head.startswith(V2),
            "clone=%s checkout=%s head=%s err=%s" % (
                cloned.returncode, checked.returncode, head[:12],
                (cloned.stderr or checked.stderr)[:200],
            ),
        )
        if cloned.returncode != 0 or checked.returncode != 0:
            return
        sandbox = _sandbox_home(clone)
        env = os.environ.copy()
        env["HOME"] = str(sandbox)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("PYTHONPATH", None)
        env.pop("CLAUDE_PROJECT_DIR", None)
        for key in (
            "CLAUDE_ROUTING",
            "CLAUDE_ROUTING_MODE",
            "CLAUDE_ROUTING_SCOPE",
            "CLAUDE_ROUTING_OTHER",
            "CLAUDE_ROUTING_ALLOW_OPUS_SUBAGENT",
        ):
            env.pop(key, None)
        for name in V2_SUITES:
            script = clone / "hooks" / name
            command = [sys.executable, str(script)]
            print("RB3 command: HOME=%s %s" % (sandbox, " ".join(command)))
            proc = subprocess.run(
                command, capture_output=True, text=True, env=env, timeout=900, check=False,
            )
            tally = _tally_line(proc.stdout)
            print(tally or "RB3 %s produced no tally (exit %s)" % (name, proc.returncode))
            suite.check(
                "RB3",
                "%s at %s passes" % (name, V2),
                proc.returncode == 0 and _tally_ok(tally),
                "exit=%s tally=%s tail=%s" % (
                    proc.returncode, tally, (proc.stdout + proc.stderr)[-400:],
                ),
            )
        saved_home = os.environ.get("HOME")
        os.environ["HOME"] = str(ledger_home)
        made = {"ok": False}
        request_id = ""
        try:
            data = ledger_home / ".claude" / "hooks" / "data"
            data.mkdir(parents=True)
            shutil.copy(ROOT / "data" / "opus-arbitration-policy.json", data / "opus-arbitration-policy.json")
            import _opus_arbitration as arb

            made = arb.register_candidate(
                session_id="sess-rb3",
                task_id="rb3-task",
                trigger="T02",
                reason_code="UNAPPROVED_OPUS",
                scope_digest="rb3",
                summary="migrated",
            )
            request = made.get("request") if isinstance(made.get("request"), dict) else {}
            request_id = request.get("request_id") or ""
            arb.record_plan_version("sess-rb3", "rb3-plan", 1, "a" * 64, "b" * 64)
        finally:
            if saved_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved_home
        db = ledger_home / ".claude" / "logs" / "opus-arbitration" / "ledger.db"
        conn = sqlite3.connect(db)
        req_cols = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        status_cmd = [sys.executable, str(clone / "hooks" / "bin" / "opus-arbitration"), "status"]
        status_env = env.copy()
        status_env["HOME"] = str(ledger_home)
        print("RB3 command: HOME=%s %s" % (ledger_home, " ".join(status_cmd)))
        status = subprocess.run(
            status_cmd, capture_output=True, text=True, env=status_env, timeout=30, check=False,
        )
        print("RB3 v2 status exit=%s" % status.returncode)
        if status.stdout.strip():
            print(status.stdout.rstrip())
        suite.check(
            "RB3",
            "v2 status reads a v2.1 ledger",
            "candidate_type" in req_cols and "plan_task_id" in req_cols
            and "plans" in tables and "stages" in tables
            and made.get("ok") is True and request_id
            and status.returncode == 0 and request_id in status.stdout,
            "exit=%s cols=%s stdout=%s stderr=%s" % (
                status.returncode, sorted(req_cols), status.stdout[:300], status.stderr[:200],
            ),
        )
        project = ledger_home / "project"
        project.mkdir()
        transcript = ledger_home / ".claude" / "projects" / "demo" / "sonnet.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "model": "claude-sonnet-5-5", "content": [{"type": "text", "text": "ok"}]},
            }) + "\n",
            encoding="utf-8",
        )
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent",
            "session_id": "sess-rb3-gate",
            "cwd": str(project),
            "transcript_path": str(transcript),
            "tool_use_id": "rb3-opus",
            "tool_input": {
                "prompt": "do the work",
                "description": "Do the work",
                "subagent_type": "general-purpose",
                "model": "opus",
            },
        }
        gate_cmd = [sys.executable, str(clone / "hooks" / "opus-arbitration-gate.py")]
        print("RB3 command: HOME=%s %s" % (ledger_home, " ".join(gate_cmd)))
        gated = subprocess.run(
            gate_cmd, input=json.dumps(payload), capture_output=True, text=True,
            env=status_env, timeout=30, check=False,
        )
        try:
            body = json.loads(gated.stdout)
        except json.JSONDecodeError:
            body = {}
        decision = ((body.get("hookSpecificOutput") or {}).get("permissionDecision"))
        gate_reason = (body.get("hookSpecificOutput") or {}).get("permissionDecisionReason") or ""
        print("RB3 v2 gate decision=%s reason=%s" % (decision, gate_reason[:160]))
        suite.check(
            "RB3",
            "v2 gate still denies Opus on a v2.1 ledger",
            gated.returncode == 0 and decision == "deny",
            "exit=%s decision=%s reason=%s stderr=%s" % (
                gated.returncode, decision, gate_reason[:160], gated.stderr[:200],
            ),
        )
    finally:
        shutil.rmtree(clone, ignore_errors=True)
        if sandbox is not None:
            shutil.rmtree(sandbox, ignore_errors=True)
        shutil.rmtree(ledger_home, ignore_errors=True)


def test_rb2(suite):
    """disable cancels pending plan work and then denies every planner call."""
    world = gate.World()
    try:
        gate._set_planning(world, True)
        gate._planner_file(world)
        pending_payload = gate._planner_call(
            world,
            session_id="sess-rb2-pending",
            tool_use_id="rb2-pending",
            prompt="ROUTER-TASK rb2-pending\nPlan the pending work",
            description="Plan the pending work",
        )
        pending_kind, pending_reason = gate.verdict(gate.run_gate(world, pending_payload))
        pending_id = gate.request_id_from_text(pending_reason)
        task_id = "rb2-task"
        version = gate._publish_plan("sess-rb2-replan", task_id)["version"]
        agent_id = "a" + "d" * 16
        ready = gate._reserve_worker(
            world, "sess-rb2-replan", task_id, version, "draft", "rb2-wf", "rb2-prompt", agent_id,
        )
        stopped = gate._worker_stop(
            world, "sess-rb2-replan", agent_id,
            gate._replan_footer(task_id, "draft", version),
            world.opus_transcript,
        )
        replans = [
            row for row in gate.rows(world, "requests")
            if row.get("candidate_type") == "REPLAN_REQUEST" and row.get("plan_task_id") == task_id
        ]
        replan_id = replans[0]["request_id"] if replans else ""
        grant_payload = gate._planner_call(
            world,
            session_id="sess-rb2-grant",
            tool_use_id="rb2-grant-open",
            prompt="ROUTER-TASK rb2-grant\nPlan the granted work",
            description="Plan the granted work",
        )
        _granted, grant_id, grant_result = gate.escalate(world, grant_payload)
        grant_row = next(
            (row for row in gate.rows(world, "grants") if row.get("request_id") == grant_id),
            {},
        )
        disabled = subprocess.run(
            [sys.executable, str(gate.CLI), "disable"],
            capture_output=True, text=True, env=world.env(), timeout=30, check=False,
        )
        states = {row["request_id"]: row for row in gate.rows(world, "requests")}
        pending_row = states.get(pending_id) or {}
        replan_row = states.get(replan_id) or {}
        grant_request = states.get(grant_id) or {}
        uses = gate._grant_uses(world, grant_id)
        off_kind, off_reason = gate.verdict(gate.run_gate(world, gate._planner_call(
            world, session_id="sess-rb2-off", tool_use_id="rb2-off",
            prompt="ROUTER-TASK rb2-off\nPlan after disable", description="Plan after disable",
        )))
        again_kind, again_reason = gate.verdict(gate.run_gate(world, {
            **pending_payload, "tool_use_id": "rb2-off-2",
        }))
        suite.check(
            "RB2",
            "disable cancels pending PLAN_REQUEST, REPLAN_PENDING, and PLAN_ONLY grants",
            disabled.returncode == 0
            and pending_kind == "deny" and "ARBITRATION_REQUIRED" in pending_reason
            and ready and stopped.returncode == 0
            and grant_result.get("decision") == "ESCALATE"
            and grant_row.get("purpose") == "PLAN_ONLY"
            and pending_row.get("candidate_type") == "PLAN_REQUEST"
            and pending_row.get("state") == "CANCELLED"
            and replan_row.get("state") == "CANCELLED"
            and grant_request.get("state") == "CANCELLED"
            and uses == 0,
            "pending=%s replan=%s grant=%s uses=%s disable=%s" % (
                pending_row.get("state"), replan_row.get("state"), grant_request.get("state"),
                uses, disabled.returncode,
            ),
        )
        suite.check(
            "RB2",
            "every planner call is denied after disable",
            off_kind == "deny" and off_reason == "OPUS_DENIED mode=disabled"
            and again_kind == "deny" and again_reason == "OPUS_DENIED mode=disabled",
            "off=%s again=%s" % (off_reason[:120], again_reason[:120]),
        )
    finally:
        world.close()


def main():
    suite = Suite()
    test_rb1(suite)
    test_rb2(suite)
    test_rb3(suite)
    print("Tally: %s passed, %s failed" % (suite.passed, suite.failed))
    return 0 if suite.failed == 0 and suite.passed > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
