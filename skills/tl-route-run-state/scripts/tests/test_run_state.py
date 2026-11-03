"""Tests for run_state.py: routes, limits, legal transitions, idempotency, resume, hashes, CLI."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "run_state.py"
spec = importlib.util.spec_from_file_location("rs", SCRIPT)
rs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rs)


def run(*args):
    r = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def seq(*events, limits=None, s=None):
    s = s or rs.new_state("t", {**rs.DEFAULT_LIMITS, **(limits or {})})
    out = [rs.route(s)]
    for e in events:
        name, opts = (e, {}) if isinstance(e, str) else e
        s = rs.apply(s, name, **opts)
        out.append(rs.route(s))
    return out, s


def rejected(*events, last, **opts):
    _, s = seq(*events)
    try:
        rs.apply(s, last, **opts)
    except rs.Rejected:
        return True
    return False


checks = {}
# --- routes and limits
r, _ = seq("spec-written", "approved", "executor-success", "verify-pass")
checks["happy path"] = r == ["write-spec", "approve", "execute", "verify", "done"]
r, _ = seq("spec-written", "approved", "executor-partial", "executor-error", "executor-error")
checks["executor: 2 retries then escalate"] = r[-3:] == ["retry-execute", "retry-execute", "escalate"]
r, _ = seq("spec-written", "approved", "executor-success", "verify-fail-impl", "executor-success",
           "verify-fail-impl", "executor-success", "verify-fail-impl")
checks["verification: 2 retries then escalate"] = r[4] == "retry-execute" and r[6] == "retry-execute" and r[-1] == "escalate"
r, s = seq("spec-written", "approved", "executor-success", "verify-fail-spec", "spec-written")
checks["spec failure goes back through approval"] = r[-2:] == ["revise-spec", "approve"] and s["spec_version"] == 2
r, s = seq("spec-written", "revise", "spec-written")
checks["revise before approval: new version needs approval"] = r[-1] == "approve" and s["approved_spec_version"] is None
r, _ = seq("spec-written", "approval-reused")
checks["reused approval counts as approval"] = r[-1] == "execute"
r, _ = seq("spec-written", "approved", "executor-error", "executor-error", "executor-error", "more-attempts")
checks["more-attempts resets after escalation"] = r[-2:] == ["escalate", "retry-execute"]
r, _ = seq("spec-written", "approved", "stop")
checks["stop"] = r[-1] == "stopped"
r, _ = seq("spec-written", "approved", "executor-error", limits={"executor": 0})
checks["limit 0 escalates on first failure"] = r[-1] == "escalate"

# --- illegal transitions are rejected (the review's cases)
checks["reject verify-pass on a new run"] = rejected(last="verify-pass")
checks["reject approved with no spec"] = rejected(last="approved")
checks["reject any event after stop"] = rejected("spec-written", "approved", "stop", last="executor-success")
checks["reject any event after done"] = rejected("spec-written", "approved", "executor-success", "verify-pass", last="spec-written")
checks["reject executor events while escalated"] = rejected("spec-written", "approved", "executor-error",
                                                            "executor-error", "executor-error", last="executor-success")
checks["reject more-attempts when not escalated"] = rejected("spec-written", "approved", last="more-attempts")
checks["reject unknown event"] = rejected(last="no-such-event")
_, s0 = seq("spec-written")
try:
    rs.apply(s0, "verify-pass")
except rs.Rejected:
    pass
checks["rejection leaves state unchanged"] = rs.route(s0) == "approve" and s0["phase"] == "approval"

# --- idempotency
r, s = seq("spec-written", "approved", ("executor-error", {"event_id": "run-1"}), ("executor-error", {"event_id": "run-1"}))
checks["duplicate event id counted once"] = s["executor_retries"] == 1 and len(s["history"]) == 3

# --- exact failure checkpoint
r, s = seq("spec-written", ("step-failed", {"step": "open-figma"}))
checks["failed step recorded"] = r[-1] == "handle-failure" and s["failed_step"] == "open-figma"
checks["recover with the wrong step rejected"] = rejected("spec-written", ("step-failed", {"step": "open-figma"}),
                                                          last="step-recovered", step="export-images")
r, s = seq("spec-written", ("step-failed", {"step": "open-figma"}), ("step-recovered", {"step": "open-figma"}))
checks["recover returns to the failed phase"] = r[-1] == "approve" and s["failed_step"] is None and s["last_error"] is None
checks["recover without failure rejected"] = rejected("spec-written", last="step-recovered")

# --- approval by content hash
with tempfile.TemporaryDirectory() as d:
    f1 = Path(d, "spec.md"); f1.write_text("v1")
    f2 = Path(d, "other.md"); f2.write_text("v2")
    _, s = seq(("spec-written", {"file": str(f1)}))
    h1 = rs.sha256_file(f1)
    checks["spec hash stored"] = s["spec_hash"] == h1
    checks["approval-reused without hash rejected when hash stored"] = rejected(("spec-written", {"file": str(f1)}), last="approval-reused")
    checks["approval-reused with a different spec rejected"] = rejected(("spec-written", {"file": str(f1)}),
                                                                        last="approval-reused", file=str(f2))
    r, s = seq(("spec-written", {"file": str(f1)}), ("approval-reused", {"hash": h1}))
    checks["approval-reused with matching hash"] = r[-1] == "execute" and s["approved_spec_hash"] == h1
    r, s = seq(("spec-written", {"file": str(f1)}), "approved", "executor-success", "verify-fail-spec",
               ("spec-written", {"file": str(f2)}))
    checks["changed spec content needs approval"] = r[-1] == "approve"

# --- schema 1 files still load
old = {"schema": 1, "workflow": "t", "phase": "execute", "spec_version": 1, "approved_spec_version": 1,
       "executor_status": None, "executor_retries": 0, "verification_retries": 0, "limits": {"executor": 2, "verification": 2},
       "last_event": "approved", "last_error": None, "history": []}
checks["schema 1 upgraded"] = rs.route(rs.upgrade(old)) == "execute"

# --- CLI: init/resume/record/route/get, rejections, lock
with tempfile.TemporaryDirectory() as d:
    st = str(Path(d) / "x" / "run.json")
    c1 = run("init", st, "--workflow", "demo", "--limit", "executor=1")
    c2 = run("init", st)
    c3 = run("record", st, "spec-written")
    c4 = run("record", st, "approved")
    c5 = run("record", st, "executor-error", "--detail", "exit 1")
    c6 = run("route", st, "--json")
    c7 = run("record", st, "verify-pass")
    c8 = run("record", st, "executor-error")
    c9 = run("init", st, "--resume")
    hist = json.loads(run("get", st, "history")[1])
    checks["cli init prints first route"] = c1[:2] == (0, "write-spec")
    checks["cli init refuses an existing run"] = c2[0] != 0 and "--resume" in c2[2]
    checks["cli record prints next route"] = (c3[1], c4[1], c5[1]) == ("approve", "execute", "retry-execute")
    j = json.loads(c6[1])
    checks["cli route --json"] = j["route"] == "retry-execute" and j["executor_retries"] == 1 and j["last_error"] == "exit 1"
    checks["cli illegal event exits 2, says why"] = c7[0] == 2 and "not legal in phase execute" in c7[2]
    checks["cli limit from init respected"] = c8[1] == "escalate"
    checks["cli init --resume prints current route"] = c9[:2] == (0, "escalate")
    checks["history kept, rejected event not recorded"] = [h["event"] for h in hist] == [
        "spec-written", "approved", "executor-error", "executor-error"]
    checks["no temp file left"] = not any(p.suffix == ".tmp" for p in Path(d, "x").iterdir())

    st2 = str(Path(d) / "y" / "run.json")
    run("init", st2, "--limit", "executor=50"); run("record", st2, "spec-written"); run("record", st2, "approved")
    errs = []
    def worker(k):
        c = run("record", st2, "executor-error", "--event-id", f"w{k}")
        if c[0] != 0:
            errs.append(c)
    ts = [threading.Thread(target=worker, args=(k,)) for k in range(10)]
    [t.start() for t in ts]; [t.join() for t in ts]
    checks["concurrent writers lose no update"] = json.loads(run("get", st2, "executor_retries")[1]) == 10 and not errs

print("\n".join(f"{'PASS' if v else 'FAIL'} {k}" for k, v in checks.items()))
sys.exit(0 if all(checks.values()) else 1)
