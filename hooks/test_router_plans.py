#!/usr/bin/env python3
"""Plan contract, plan store, stage ledger, and router-plan CLI."""

import copy
import json
import multiprocessing
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
POLICY = ROOT / "data" / "opus-arbitration-policy.json"
CLI = ROOT / "bin" / "router-plan"

import _opus_arbitration as arb
import _router_plans as plans


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


def step(step_id, role="main", model="main", deps=None, instructions="Write the plan file", checks=None):
    return {
        "id": step_id,
        "depends_on": [] if deps is None else deps,
        "executor_role": role,
        "allowed_model": model,
        "instructions": instructions,
        "acceptance_checks": ["The file parses"] if checks is None else checks,
        "requires_user_approval": False,
    }


def plan_for(task_id, base=0, steps=None, **overrides):
    body = {
        "schema_version": 1,
        "task_id": task_id,
        "base_plan_version": base,
        "objective": "Ship the plan store",
        "assumptions": ["The ledger is local"],
        "invariants": ["Plans are not permissions"],
        "steps": steps or [step("draft")],
        "risks": ["A stale write"],
        "handoff_notes": "Hand off to the next work package",
    }
    body.update(overrides)
    return body


def _mp_entry(home, kind, payload, queue):
    try:
        os.environ["HOME"] = home
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        if kind == "promote":
            import _router_plans as plans_mod

            try:
                plans_mod.promote(payload["task_id"], payload["plan"])
                queue.put("ok")
            except plans_mod.PlanError as exc:
                queue.put(exc.code)
            return
        import _opus_arbitration as arbitration

        ok, code, _attempt = arbitration.stage_start(
            "sess-race", payload["task_id"], payload["version"], payload["stage_id"]
        )
        queue.put("ok" if ok else code)
    except Exception as exc:
        queue.put("ERR:%s" % exc)


def _race(home, kind, payload):
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    procs = [
        ctx.Process(target=_mp_entry, args=(home, kind, payload, queue))
        for _ in range(2)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(20)
    results = []
    for proc in procs:
        if proc.exitcode != 0 and queue.empty():
            results.append("EXIT:%s" % proc.exitcode)
            continue
        results.append(queue.get(timeout=5))
    return results


def _run_cli(home, args):
    env = os.environ.copy()
    env["HOME"] = home
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("CLAUDE_SESSION_ID", None)
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=20,
    )


def _last_json(stdout):
    line = stdout.strip().splitlines()[-1]
    return json.loads(line)


def _install_policy(home):
    data = Path(home) / ".claude" / "hooks" / "data"
    data.mkdir(parents=True, exist_ok=True)
    shutil.copy(POLICY, data / "opus-arbitration-policy.json")


def test_contract(suite, home):
    os.environ["HOME"] = home
    valid = plan_for("task-1")
    suite.check("1", "valid plan passes", plans.validate_plan(valid) == [], str(plans.validate_plan(valid)))
    unknown = plan_for("task-1", extra="nope")
    missing = plan_for("task-1")
    del missing["objective"]
    bad_id = plan_for("task-1", steps=[step("Draft")])
    duplicate = plan_for("task-1", steps=[step("draft"), step("draft", instructions="Second copy of the step")])
    dangling = plan_for("task-1", steps=[step("draft", deps=["missing"])])
    self_dep = plan_for("task-1", steps=[step("draft", deps=["draft"])])
    cycle = plan_for("task-1", steps=[
        step("a", deps=["c"]),
        step("b", deps=["a"]),
        step("c", deps=["b"]),
    ])
    too_many = plan_for("task-1", steps=[step("s%02d" % index) for index in range(1, 14)])
    bad_model = plan_for("task-1", steps=[step("draft", role="file-reader", model="cursor")])
    names_opus = plan_for("task-1", steps=[step("draft", instructions="Call opus for the hard part")])
    bypass = plan_for("task-1", steps=[step("draft", instructions="Do not run cursor-agent -p here")])
    as_int = plan_for("task-1")
    as_int["base_plan_version"] = json.loads("true")
    cases = (
        ("unknown key", unknown, "PLAN_UNKNOWN_KEY"),
        ("missing key", missing, "PLAN_MISSING_KEY"),
        ("bad id", bad_id, "PLAN_BAD_STEP_ID"),
        ("duplicate id", duplicate, "PLAN_DUPLICATE_ID"),
        ("dangling dependency", dangling, "PLAN_DANGLING_DEPENDENCY"),
        ("self dependency", self_dep, "PLAN_SELF_DEPENDENCY"),
        ("3-cycle", cycle, "PLAN_CYCLE"),
        ("too many steps", too_many, "PLAN_TOO_MANY_STEPS"),
        ("wrong allowed_model", bad_model, "PLAN_BAD_MODEL"),
        ("opus in instructions", names_opus, "PLAN_NAMES_OPUS"),
        ("cursor-agent -p", bypass, "PLAN_BYPASSES_CURSOR_RUN"),
        ("true used as an int", as_int, "PLAN_BAD_TYPE"),
    )
    for name, body, code in cases:
        errors = plans.validate_plan(body)
        suite.check("1", "%s is rejected" % name, code in errors, str(errors))
    swapped_a = plan_for("task-1", steps=[step("b"), step("a")])
    swapped_b = plan_for("task-1", steps=[step("a"), step("b")])
    suite.check(
        "1",
        "scope digest sorts steps by id",
        plans.scope_digest(swapped_a) == plans.scope_digest(swapped_b)
        and plans.content_sha256(swapped_a) != plans.content_sha256(swapped_b),
        "",
    )


def test_store(suite, home):
    os.environ["HOME"] = home
    task_id = "versions"
    first = plan_for(task_id, 0)
    record = plans.promote(task_id, copy.deepcopy(first))
    stale_code = ""
    try:
        plans.promote(task_id, copy.deepcopy(first))
    except plans.PlanStoreError as exc:
        stale_code = exc.code
    second = plan_for(task_id, 1)
    second_record = plans.promote(task_id, copy.deepcopy(second))
    third = plans.promote(task_id, copy.deepcopy(plan_for(task_id, 2)))
    limit_code = ""
    try:
        plans.promote(task_id, copy.deepcopy(plan_for(task_id, 3)), max_versions=3)
    except plans.PlanStoreError as exc:
        limit_code = exc.code
    suite.check(
        "2",
        "promote advances versions and rejects stale or a fourth",
        record["version"] == 1
        and second_record["version"] == 2
        and third["version"] == 3
        and stale_code == "ROUTER_PLAN_STALE"
        and limit_code == "ROUTER_PLAN_VERSION_LIMIT"
        and plans.load_current(task_id)["version"] == 3,
        "stale=%s limit=%s current=%s" % (
            stale_code, limit_code, plans.load_current(task_id)["version"],
        ),
    )
    race_plan = plan_for("race-task", 0)
    results = _race(home, "promote", {"task_id": "race-task", "plan": race_plan})
    suite.check(
        "2",
        "one of two concurrent promotes wins",
        results.count("ok") == 1 and results.count("ROUTER_PLAN_STALE") == 1,
        str(results),
    )
    tamper = plans.promote("tamper-task", copy.deepcopy(plan_for("tamper-task")))
    path = Path(home) / ".claude" / "plans" / "router" / "tamper-task" / "v1.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["plan"]["objective"] = "tampered objective"
    path.write_text(json.dumps(stored), encoding="utf-8")
    corrupt = ""
    try:
        plans.load_current("tamper-task")
    except plans.PlanStoreError as exc:
        corrupt = exc.code
    suite.check("2", "tampered plan is corrupt", corrupt == "ROUTER_PLAN_CORRUPT", corrupt)
    snap = plans.promote("snap-task", copy.deepcopy(plan_for(
        "snap-task",
        steps=[step("draft", instructions="x" * 2000)],
        objective="y" * 400,
    )))
    text = plans.snapshot(snap)
    short = plans.snapshot(snap, max_chars=80)
    suite.check(
        "2",
        "snapshot has a header, no opus, and truncates",
        text.startswith("ROUTER-PLAN task=snap-task version=1 sha256=")
        and "opus" not in text.lower()
        and len(text) > 80
        and len(short) <= 80
        and short.endswith("[truncated]"),
        "short=%r len=%s" % (short, len(short)),
    )
    # tamper is unused except to show the record was written before the edit.
    suite.check("2", "tamper wrote a record before the edit", tamper["version"] == 1, str(tamper.get("version")))


def test_stages(suite, home):
    os.environ["HOME"] = home
    task_id = "stage-task"
    record = plans.promote(task_id, copy.deepcopy(plan_for(task_id)))
    arb.record_plan_version(
        "sess-stage", task_id, record["version"], record["content_sha256"], record["scope_digest"]
    )
    ok, code, attempt = arb.stage_start("sess-stage", task_id, 1, "draft")
    again_ok, again_code, _again = arb.stage_start("sess-stage", task_id, 1, "draft")
    finished = arb.stage_finish(task_id, 1, "draft", "completed")
    after_ok, after_code, _after = arb.stage_start("sess-stage", task_id, 1, "draft")
    failed_ok, _failed_code, failed_attempt = arb.stage_start("sess-stage", task_id, 1, "retry")
    failed_finish = arb.stage_finish(task_id, 1, "retry", "failed")
    retry_ok, _retry_code, retry_attempt = arb.stage_start("sess-stage", task_id, 1, "retry")
    stale_ok, stale_code, _stale = arb.stage_start("sess-stage", task_id, 9, "draft")
    unknown_ok, _unknown_code, _unknown_attempt = arb.stage_start("sess-stage", task_id, 1, "mystery")
    unknown_finish = arb.stage_finish(task_id, 1, "mystery", "unknown")
    blocked_ok, blocked_code, _blocked = arb.stage_start("sess-stage", task_id, 1, "mystery")
    suite.check(
        "3",
        "stage start, duplicate, failed retry, stale, and unknown",
        ok and code == "OK" and attempt == 1
        and again_ok is False and again_code == "ROUTER_STAGE_DUPLICATE"
        and finished is True
        and after_ok is False and after_code == "ROUTER_STAGE_DUPLICATE"
        and failed_ok and failed_attempt == 1 and failed_finish is True
        and retry_ok and retry_attempt == 2
        and stale_ok is False and stale_code == "ROUTER_PLAN_STALE"
        and unknown_ok and unknown_finish is True
        and blocked_ok is False and blocked_code == "ROUTER_STAGE_UNKNOWN",
        "again=%s after=%s stale=%s blocked=%s retry=%s" % (
            again_code, after_code, stale_code, blocked_code, retry_attempt,
        ),
    )
    rows_before = len(arb.task_stages(task_id))
    dup_check, dup_check_code = arb.stage_check(task_id, 1, "draft")
    stale_check, stale_check_code = arb.stage_check(task_id, 9, "draft")
    fresh_check, fresh_check_code = arb.stage_check(task_id, 1, "later")
    rows_after = len(arb.task_stages(task_id))
    suite.check(
        "3",
        "stage_check matches stage_start and writes nothing",
        dup_check is False and dup_check_code == "ROUTER_STAGE_DUPLICATE"
        and stale_check is False and stale_check_code == "ROUTER_PLAN_STALE"
        and fresh_check is True and fresh_check_code == "OK"
        and rows_after == rows_before,
        "dup=%s stale=%s fresh=%s %s rows=%s->%s" % (
            dup_check_code, stale_check_code, fresh_check, fresh_check_code, rows_before, rows_after,
        ),
    )
    replan_id = "replan-stage"
    replan_steps = [step("draft"), step("review", deps=["draft"])]
    first = plans.promote(replan_id, copy.deepcopy(plan_for(replan_id, steps=replan_steps)))
    arb.record_plan_version(
        "sess-stage", replan_id, first["version"], first["content_sha256"], first["scope_digest"],
    )
    started_ok, _started_code, started_attempt = arb.stage_start("sess-stage", replan_id, 1, "draft")
    review_ok, _review_code, _review_attempt = arb.stage_start("sess-stage", replan_id, 1, "review")
    judged = sqlite3.connect(arb.ledger_path())
    judged.execute(
        "UPDATE stages SET status='needs_judgment' WHERE task_id=? AND stage_id='review'",
        (replan_id,),
    )
    judged.commit()
    judged.close()
    second = plans.promote(replan_id, copy.deepcopy(plan_for(replan_id, base=1, steps=replan_steps)))
    arb.record_plan_version(
        "sess-stage", replan_id, second["version"], second["content_sha256"], second["scope_digest"],
    )
    same_ok, same_code, same_attempt = arb.stage_start("sess-stage", replan_id, 2, "draft")
    never_ok, never_code, _never = arb.stage_start("sess-stage", replan_id, 1, "review")
    suite.check(
        "3",
        "the same stage_id starts at attempt 1 on plan version 2",
        started_ok and started_attempt == 1
        and second["version"] == 2
        and same_ok and same_code == "OK" and same_attempt == 1,
        "started=%s v2=%s same=%s %s attempt=%s" % (
            started_attempt, second.get("version"), same_ok, same_code, same_attempt,
        ),
    )
    suite.check(
        "3",
        "a never-started v1 stage is stale after v2 is active",
        never_ok is False and never_code == "ROUTER_PLAN_STALE",
        never_code,
    )
    superseded = {
        row.get("stage_id"): row.get("status")
        for row in arb.task_stages(replan_id)
        if row.get("plan_version") == 1
    }
    events_path = Path(home) / ".claude" / "logs" / "opus-arbitration" / "events.jsonl"
    events_text = events_path.read_text(encoding="utf-8") if events_path.exists() else ""
    missing_plan, missing_code = arb.stage_check("no-plan-task", 1, "draft")
    shown = _run_cli(home, ["show", replan_id])
    suite.check(
        "3",
        "promote cancels older open stages and a missing plan is not stale",
        review_ok
        and superseded.get("draft") == "cancelled"
        and superseded.get("review") == "cancelled"
        and events_text.count("stage_superseded") >= 2
        and missing_plan is False and missing_code == "ROUTER_PLAN_MISSING"
        and "plan_version=1" in shown.stdout
        and "plan_version=2" in shown.stdout,
        "status=%s code=%s show=%s" % (superseded, missing_code, shown.stdout[-240:]),
    )
    results = _race(home, "stage", {"task_id": task_id, "version": 1, "stage_id": "parallel"})
    suite.check(
        "3",
        "one of two concurrent stage starts wins",
        results.count("ok") == 1 and results.count("ROUTER_STAGE_DUPLICATE") == 1,
        str(results),
    )


def test_cli(suite, home):
    os.environ["HOME"] = home
    _install_policy(home)
    good = Path(home) / "good.json"
    bad = Path(home) / "bad.json"
    good.write_text(json.dumps(plan_for("cli-valid")), encoding="utf-8")
    bad_body = plan_for("cli-valid")
    bad_body["extra"] = 1
    bad.write_text(json.dumps(bad_body), encoding="utf-8")
    valid = _run_cli(home, ["validate", str(good)])
    invalid = _run_cli(home, ["validate", str(bad)])
    suite.check(
        "4",
        "validate exit codes",
        valid.returncode == 0 and _last_json(valid.stdout)["ok"] is True
        and invalid.returncode == 1 and _last_json(invalid.stdout)["ok"] is False,
        "valid=%s invalid=%s %r" % (valid.returncode, invalid.returncode, invalid.stdout[-200:]),
    )
    outside = Path(home) / "outside.json"
    outside.write_text(json.dumps(plan_for("cli-task")), encoding="utf-8")
    refused = _run_cli(home, ["promote", "cli-task", str(outside)])
    drafts = Path(home) / ".claude" / "plans" / "router" / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    inside = drafts / "cli-task.json"
    inside.write_text(json.dumps(plan_for("cli-task")), encoding="utf-8")
    promoted = _run_cli(home, ["promote", "cli-task", str(inside)])
    promoted_body = _last_json(promoted.stdout) if promoted.returncode == 0 else {}
    missing_draft = _run_cli(home, ["promote", "cli-task", str(drafts / "missing.json")])
    unread_dir = drafts / "unread.json"
    unread_dir.mkdir()
    unread = _run_cli(home, ["promote", "cli-task", str(unread_dir)])
    usage = _run_cli(home, [])
    suite.check(
        "4",
        "a missing draft is not a corrupt plan, and help names the headless allow rule",
        missing_draft.returncode == 1
        and _last_json(missing_draft.stdout).get("code") == "ROUTER_DRAFT_MISSING"
        and "ROUTER_PLAN_CORRUPT" not in missing_draft.stdout
        and unread.returncode == 1
        and _last_json(unread.stdout).get("code") == "ROUTER_DRAFT_MISSING"
        and usage.returncode == 2
        and 'Write(<HOME>/.claude/plans/router/drafts/*)' in usage.stdout,
        "missing=%s unread=%s usage=%r" % (
            missing_draft.returncode, unread.returncode, usage.stdout[-240:],
        ),
    )
    suite.check(
        "4",
        "promote outside drafts is usage and inside drafts is ok",
        refused.returncode == 2 and "ROUTER_DRAFT_LOCATION" in refused.stdout
        and promoted.returncode == 0 and promoted_body.get("ok") is True
        and arb.active_plan_version("cli-task") == 1,
        "refused=%s %r promoted=%s %r" % (
            refused.returncode, refused.stdout[-240:], promoted.returncode, promoted.stdout[-240:],
        ),
    )
    show_started, show_code, _show_attempt = arb.stage_start("sess-show", "cli-task", 1, "draft")
    shown_cli = _run_cli(home, ["show", "cli-task"])
    suite.check(
        "4",
        "show prints plan_version on every stage row",
        show_started and show_code == "OK" and "stage=draft plan_version=1" in shown_cli.stdout,
        shown_cli.stdout[-240:],
    )
    registered = arb.register_candidate(
        session_id="sess-cli",
        task_id="close-task",
        trigger="T02",
        reason_code="UNAPPROVED_OPUS",
        scope_digest="close-task",
    )
    request_id = registered["request"]["request_id"] if registered.get("ok") else ""
    closed = _run_cli(home, ["close", request_id, "--reason", "not a replan"])
    closed_body = _last_json(closed.stdout) if closed.stdout.strip() else {}
    suite.check(
        "4",
        "close on a non-replan request is refused",
        registered.get("ok") and closed.returncode == 1 and closed_body.get("ok") is False,
        "registered=%s exit=%s stdout=%r" % (registered.get("reason"), closed.returncode, closed.stdout[-240:]),
    )


def _catch(call):
    try:
        call()
    except plans.PlanError as exc:
        return exc.code
    return ""


def test_hardening(suite, home):
    os.environ["HOME"] = home
    _install_policy(home)
    reserved = [plans.validate_plan(plan_for(name)) for name in ("drafts", ".", "..")]
    suite.check(
        "5",
        "reserved task ids are rejected",
        all("PLAN_BAD_TASK_ID" in errors for errors in reserved)
        and _catch(lambda: plans.promote("drafts", plan_for("drafts"))) == "PLAN_BAD_TASK_ID",
        str(reserved),
    )
    root = Path(home) / ".claude" / "plans" / "router"
    root.parent.mkdir(parents=True, exist_ok=True)
    planted = Path(home) / "plan-link-target"
    planted.mkdir()
    if root.exists() or root.is_symlink():
        if root.is_symlink() or root.is_file():
            root.unlink()
        else:
            shutil.rmtree(root)
    root.symlink_to(planted)
    suite.check(
        "5",
        "a symlinked plans root is refused",
        _catch(lambda: plans.promote("linked", plan_for("linked"))) == "ROUTER_PLAN_ERROR",
        "",
    )
    root.unlink()
    root.mkdir()
    task_link = root / "linked-task"
    task_link.symlink_to(planted)
    suite.check(
        "5",
        "a symlinked task dir is refused",
        _catch(lambda: plans.promote("linked-task", plan_for("linked-task"))) == "ROUTER_PLAN_ERROR",
        "",
    )
    task_link.unlink()
    task_id = "recheck"
    plans.promote(task_id, plan_for(task_id))
    directory = root / task_id
    stored = json.loads((directory / "v1.json").read_text(encoding="utf-8"))
    stored["plan"]["objective"] = "A rewritten objective that stays valid"
    stored["content_sha256"] = plans.content_sha256(stored["plan"])
    (directory / "v1.json").write_text(json.dumps(stored) + "\n", encoding="utf-8")
    pointer = {"version": 1, "content_sha256": stored["content_sha256"]}
    (directory / "current").write_text(json.dumps(pointer) + "\n", encoding="utf-8")
    suite.check(
        "5",
        "load_current rejects a scope digest mismatch",
        _catch(lambda: plans.load_current(task_id)) == "ROUTER_PLAN_CORRUPT",
        "",
    )
    old_id = "old-orphan"
    plans.promote(old_id, plan_for(old_id))
    old_dir = root / old_id
    leftover = old_dir / "v2.json"
    leftover.write_text("{}\n", encoding="utf-8")
    stale = time.time() - 120
    os.utime(leftover, (stale, stale))
    drafts = root / "drafts"
    drafts.mkdir(exist_ok=True)
    draft = drafts / (old_id + ".json")
    draft.write_text(json.dumps(plan_for(old_id, 1, steps=[step("next")])), encoding="utf-8")
    promoted = _run_cli(home, ["promote", old_id, str(draft)])
    renamed = [name for name in os.listdir(old_dir) if name.startswith("v2.orphan-")]
    suite.check(
        "5",
        "an old orphan is renamed and logged",
        promoted.returncode == 0 and "orphan=v2.orphan-" in promoted.stdout and len(renamed) == 1,
        "renamed=%s stdout=%s" % (renamed, promoted.stdout[:240]),
    )
    young_id = "young-orphan"
    plans.promote(young_id, plan_for(young_id))
    young_file = root / young_id / "v2.json"
    young_file.write_text("{}\n", encoding="utf-8")
    suite.check(
        "5",
        "a young orphan is stale",
        _catch(lambda: plans.promote(young_id, plan_for(young_id, 1, steps=[step("next")]))) == "ROUTER_PLAN_STALE"
        and young_file.exists(),
        "",
    )
    link_id = "link-orphan"
    plans.promote(link_id, plan_for(link_id))
    link_file = root / link_id / "v2.json"
    link_file.symlink_to(leftover if leftover.exists() else draft)
    suite.check(
        "5",
        "a non-regular orphan is stale",
        _catch(lambda: plans.promote(link_id, plan_for(link_id, 1, steps=[step("next")]))) == "ROUTER_PLAN_STALE"
        and link_file.is_symlink(),
        "",
    )
    shown = _run_cli(home, ["show", old_id])
    shown_body = _last_json(shown.stdout) if shown.returncode == 0 else {}
    suite.check(
        "5",
        "show stays quiet when the ledger matches",
        shown.returncode == 0 and "ROUTER_PLAN_LEDGER_MISSING" not in shown.stdout
        and "ledger_missing" not in shown_body,
        shown.stdout[-240:],
    )
    bare_id = "bare-plan"
    plans.promote(bare_id, plan_for(bare_id))
    bare = _run_cli(home, ["show", bare_id])
    bare_body = _last_json(bare.stdout) if bare.stdout.strip() else {}
    suite.check(
        "5",
        "show flags a plan with no ledger row",
        bare.returncode == 0 and "ROUTER_PLAN_LEDGER_MISSING version=1" in bare.stdout
        and bare_body.get("ledger_missing") is True,
        bare.stdout[-240:],
    )
    logs = Path(home) / ".claude" / "logs"
    if logs.is_symlink() or logs.is_file():
        logs.unlink()
    elif logs.exists():
        shutil.rmtree(logs)
    logs.write_text("not a directory\n", encoding="utf-8")
    failed_id = "ledger-fail"
    failed_draft = drafts / (failed_id + ".json")
    failed_draft.write_text(json.dumps(plan_for(failed_id)), encoding="utf-8")
    failed = _run_cli(home, ["promote", failed_id, str(failed_draft)])
    stored_version = root / failed_id / "v1.json"
    suite.check(
        "5",
        "promote reports a ledger failure after the file is stored",
        failed.returncode == 1 and "ROUTER_PLAN_LEDGER_FAILED version=1" in failed.stdout
        and stored_version.is_file(),
        "exit=%s stdout=%r" % (failed.returncode, failed.stdout[-300:]),
    )


def main():
    home = tempfile.mkdtemp(prefix="router-plans-")
    suite = Suite()
    try:
        test_contract(suite, home)
        test_store(suite, home)
        test_stages(suite, home)
        test_cli(suite, home)
        test_hardening(suite, home)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    print("Tally: %s passed, %s failed" % (suite.passed, suite.failed))
    return 0 if suite.failed == 0 and suite.passed > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
