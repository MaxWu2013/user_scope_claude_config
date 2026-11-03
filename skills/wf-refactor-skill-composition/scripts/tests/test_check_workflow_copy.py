"""Fixture test for check_workflow_copy.py: one workflow with every defect, one with them fixed."""
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("cwc", SCRIPTS / "check_workflow_copy.py")
cwc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cwc)


def node(i, t, **data):
    return {"id": i, "type": t, "name": i, "position": {"x": 0, "y": 0}, "data": data}


def conn(a, b, port="output"):
    return {"id": f"{a}-{b}-{port}", "from": a, "to": b, "fromPort": port, "toPort": "input"}


def opts(*labels):
    return [{"id": l, "label": l, "description": l} for l in labels]


FENCE = "~~~"
bad = {"nodes": [
    node("start", "start", label="Start"),
    node("p1", "prompt", label="Check", prompt="If values differ, stop and ask the user which applies."),
    node("sk", "skill", name="wf-x", executionMode="execute", executionPrompt="Export only; do not register."),
    node("ask", "askUserQuestion", questionText="Here is the spec. Approve it?", options=opts("Approve", "Needs changes", "Stop")),
    node("p2", "prompt", label="Build", prompt=f"{FENCE}bash\ndart format -l 120 a.dart\n{FENCE}"),
    node("p3", "prompt", label="Note", prompt="Stopping."),
    node("if1", "ifElse", branches=[{"label": "pass", "condition": "ok"}, {"label": "fail", "condition": "retry"}]),
    node("end", "end", label="End"),
], "connections": [
    conn("start", "p1"), conn("p1", "sk"), conn("sk", "ask"),
    conn("ask", "p2", "branch-0"), conn("ask", "p1", "branch-1"), conn("ask", "p3", "branch-2"),
    conn("p3", "end"), conn("p2", "if1"), conn("if1", "end", "branch-0"), conn("if1", "p2", "branch-1"),
]}

good = {"nodes": [
    node("start", "start", label="Start"),
    node("p1", "prompt", label="Check", prompt="Compare the values with the spec table."),
    node("v1", "ifElse", evaluationTarget="whether values match",
         branches=[{"label": "match", "condition": "match"}, {"label": "differ", "condition": "differ"}]),
    node("ask-m", "askUserQuestion", questionText="Values differ. Which applies?", options=opts("Fix the spec", "Stop")),
    node("sk", "skill", name="wf-x", executionMode="load"),
    node("ask", "askUserQuestion", questionText="Approve the spec at .figma_tmp/dialog/spec.md?",
         options=opts("Approve", "Needs changes", "Stop")),
    node("collect", "prompt", label="Collect", prompt="Ask the user what to change, then revise the spec."),
    node("p2", "prompt", label="Build", prompt=f"{FENCE}bash\ndart format -l 120 a.dart\n{FENCE}\nThen record the result with run_state.py."),
    node("if1", "ifElse", evaluationTarget="the route printed by run_state.py",
         branches=[{"label": "done", "condition": "route is done"}, {"label": "retry-execute", "condition": "route is retry-execute"}]),
    node("end", "end", label="End"),
    node("end-stop", "end", label="Stopped"),
], "connections": [
    conn("start", "p1"), conn("p1", "v1"), conn("v1", "sk", "branch-0"), conn("v1", "ask-m", "branch-1"),
    conn("ask-m", "p1", "branch-0"), conn("ask-m", "end-stop", "branch-1"),
    conn("sk", "ask"), conn("ask", "p2", "branch-0"), conn("ask", "collect", "branch-1"), conn("collect", "ask"),
    conn("ask", "end-stop", "branch-2"), conn("p2", "if1"), conn("if1", "end", "branch-0"), conn("if1", "p2", "branch-1"),
]}

# a question whose option *description* mentions approval is not an approval gate;
# a "Context & rules" node is description, not a step
bad["nodes"].insert(1, node("ctx", "prompt", label="Context & rules", prompt="Overview: ask the user before delegating."))
bad["nodes"].append(node("ask-x", "askUserQuestion", questionText="Values differ. Which applies?",
                         options=[{"id": "a", "label": "Exception", "description": "record the approved exception"},
                                  {"id": "b", "label": "Stop", "description": "stop"}]))
bad["connections"] = [c for c in bad["connections"] if not (c["from"] == "start" and c["to"] == "p1")]
bad["connections"] += [conn("start", "ctx"), conn("ctx", "ask-x"), conn("ask-x", "p1", "branch-0"), conn("ask-x", "end", "branch-1")]
checks0 = {"description mentioning approval is not a gate": not cwc.is_approval(bad["nodes"][-1])}
staged = {"nodes": [node("start", "start", label="Start"),
                    node("dl", "skill", name="wf-dl", executionMode="execute", executionPrompt="Use its stage mode: --stage .figma_tmp/x/ (its 'Stage-only mode' section)"),
                    node("ok", "askUserQuestion", questionText="Approve the spec at .figma_tmp/x/spec.md?", options=opts("Approve", "Stop")),
                    node("end", "end", label="End")],
          "connections": [conn("start", "dl"), conn("dl", "ok"), conn("ok", "end", "branch-0"), conn("ok", "end", "branch-1")]}
checks0["stage mode is not a change before approval"] = cwc.check(staged) == []
viadec = {"nodes": [node("start", "start", label="Start"),
                    node("chk", "prompt", label="Check", prompt="If values differ, stop and ask the user."),
                    node("d", "ifElse", evaluationTarget="whether values match",
                         branches=[{"label": "match", "condition": "m"}, {"label": "differ", "condition": "d"}]),
                    node("q", "askUserQuestion", questionText="Values differ. Which applies?", options=opts("Fix", "Stop")),
                    node("end", "end", label="End")],
          "connections": [conn("start", "chk"), conn("chk", "d"), conn("d", "end", "branch-0"), conn("d", "q", "branch-1"),
                          conn("q", "chk", "branch-0"), conn("q", "end", "branch-1")]}
checks0["prose stop before a decision is reported for confirmation"] = \
    [(f["check"], f["detail"]["decision"]) for f in cwc.check(viadec)] == [("prose_stop_via_decision", "d")]
fb = cwc.check(bad)
kinds = {}
for f in fb:
    kinds.setdefault(f["check"], []).append(f["node"])
fg = cwc.check(good)

checks = {
    **checks0,
    "context node not a prose stop": "ctx" not in kinds.get("prose_stop_no_branch", []),
    "prose stop without branch": kinds.get("prose_stop_no_branch") == ["p1"],
    "execute before approval": kinds.get("execute_before_approval") == ["sk"],
    "mutating prompt after approval not flagged": "p2" not in kinds.get("execute_before_approval", []),
    "execute override": kinds.get("execute_override") == ["sk"],
    "approval without artifact": kinds.get("approval_without_artifact") == ["ask"],
    "needs changes without collecting": kinds.get("needs_changes_no_collect") == ["ask"],
    "stop option merges back": kinds.get("stop_option_merges_back") == ["ask"],
    "decision without target": kinds.get("decision_no_target") == ["if1"],
    "automatic loop unbounded, user-paced loop not": kinds.get("unbounded_loop") == ["if1,p2"],
    "retry decided in prose": kinds.get("control_in_prose") == ["if1"],
    "fixed workflow is clean": fg == [],
}
if not checks["fixed workflow is clean"]:
    print("unexpected on fixed workflow:", fg)
def build(nodes_, conns_):
    return {"nodes": nodes_, "connections": conns_}


nb = build([
    node("start", "start", label="Start"),
    node("ctx", "prompt", label="Context & rules", prompt="Overview: delegate via `/runner:sub-agents Run cursor-agent on <task>`."),
    node("dl", "skill", name="wf-dl", executionMode="execute"),
    node("s4", "prompt", label="S4", prompt="tokens"), node("s5", "prompt", label="S5", prompt="widgets"),
    node("s6", "prompt", label="S6", prompt="text"),
    node("spec", "prompt", label="Spec", prompt="If a parent approved it, don't ask a second time. Add 1 to `Attempts`."),
    node("ok", "askUserQuestion", questionText="Approve the spec at .figma_tmp/x/spec.md?", options=opts("Approve", "Stop")),
    node("del", "prompt", label="Delegate", prompt="Delegate with `/runner:sub-agents Run cursor-agent on x`."),
    node("chk", "prompt", label="Check", prompt="run checks"),
    node("d", "switch", evaluationTarget="check result",
         branches=[{"label": "pass", "condition": "ok"}, {"label": "fail", "condition": "attempts below 2"},
                   {"label": "default", "condition": "Other cases", "isDefault": True}]),
    node("end", "end", label="End"), node("end2", "end", label="Stopped"),
], [conn("start", "ctx"), conn("ctx", "dl"), conn("dl", "s4"), conn("s4", "s5"), conn("s5", "s6"), conn("s6", "spec"),
    conn("spec", "ok"), conn("ok", "del", "branch-0"), conn("ok", "end2", "branch-1"), conn("del", "chk"), conn("chk", "d"),
    conn("d", "end", "branch-0"), conn("d", "del", "branch-1"), conn("d", "end2", "branch-2")])
kb = {}
for f in cwc.check(nb):
    kb.setdefault(f["check"], []).append(f["node"])
checks["D29 executed skill and delegation without failure path"] = sorted(kb.get("executed_no_failure_path", [])) == ["del", "dl"]
checks["D28 counting prompt and counting decision"] = sorted(kb.get("control_in_prose", [])) == ["d", "spec"]
checks["D9 approval skip in prose"] = kb.get("approval_skip_in_prose") == ["spec"]
checks["D30 content chain"] = kb.get("content_chain") == ["s4"]

ng = build([
    node("start", "start", label="Start"),
    node("ctx", "prompt", label="Context & rules", prompt="Overview."),
    node("dl", "skill", name="wf-dl", executionMode="execute", executionPrompt="Use its stage mode: --stage .figma_tmp/x/"),
    node("dok", "ifElse", evaluationTarget="whether the export succeeded", branches=[
        {"label": "ok", "condition": "exported"}, {"label": "failed", "condition": "failed"}]),
    node("design", "prompt", label="Design", prompt="tokens, widgets, text"),
    node("spec", "prompt", label="Spec", prompt="Write the spec; record spec-written with run_state.py."),
    node("valid", "ifElse", evaluationTarget="approved_spec_version equals spec_version (run_state.py get)", branches=[
        {"label": "already approved", "condition": "approved version is current"},
        {"label": "needs approval", "condition": "approved version is not current"}]),
    node("ok", "askUserQuestion", questionText="Approve the spec at .figma_tmp/x/spec.md?", options=opts("Approve", "Stop")),
    node("del", "prompt", label="Delegate", prompt="Delegate with `/runner:sub-agents Run cursor-agent on x`; record its status with run_state.py."),
    node("d", "switch", evaluationTarget="the route printed by run_state.py", branches=[
        {"label": "verify", "condition": "route is verify"}, {"label": "retry-execute", "condition": "route is retry-execute"},
        {"label": "escalate", "condition": "route is escalate"},
        {"label": "default", "condition": "Other cases: unexpected route (control error)", "isDefault": True}]),
    node("chk", "prompt", label="Check", prompt="run checks; record the result with run_state.py"),
    node("d2", "switch", evaluationTarget="the route printed by run_state.py", branches=[
        {"label": "done", "condition": "route is done"}, {"label": "retry-execute", "condition": "route is retry-execute"},
        {"label": "escalate", "condition": "route is escalate"},
        {"label": "default", "condition": "Other cases: unexpected route (control error)", "isDefault": True}]),
    node("end", "end", label="End"), node("end2", "end", label="Stopped"),
], [conn("start", "ctx"), conn("ctx", "dl"), conn("dl", "dok"), conn("dok", "design", "branch-0"), conn("dok", "end2", "branch-1"),
    conn("design", "spec"), conn("spec", "valid"), conn("valid", "del", "branch-0"), conn("valid", "ok", "branch-1"),
    conn("ok", "del", "branch-0"), conn("ok", "end2", "branch-1"), conn("del", "d"),
    conn("d", "chk", "branch-0"), conn("d", "del", "branch-1"), conn("d", "end2", "branch-2"), conn("d", "end2", "branch-3"),
    conn("chk", "d2"), conn("d2", "end", "branch-0"), conn("d2", "del", "branch-1"), conn("d2", "end2", "branch-2"),
    conn("d2", "end2", "branch-3")])
fng = cwc.check(ng)
checks["state-routed workflow is clean"] = fng == []
# a delegation after a state-checked approval decision is after approval, even without a question on that path
ng2 = json.loads(json.dumps(ng))
for n in ng2["nodes"]:
    if n["id"] == "del":
        n["data"]["prompt"] += "\n~~~bash\ndart format -l 120 a.dart\n~~~"
# a resume switch: its approved branch is gated, its first-run branch is not
res = build([
    node("start", "start", label="Start"),
    node("sw", "switch", evaluationTarget="the route printed by run_state.py after init --resume", branches=[
        {"label": "write-spec", "condition": "route is write-spec: a new run"},
        {"label": "execute", "condition": "route is execute: the approved spec version is current"},
        {"label": "default", "condition": "Other cases: unexpected route (control error)", "isDefault": True}]),
    node("dl", "skill", name="wf-dl", executionMode="execute"),
    node("dlok", "ifElse", evaluationTarget="whether it worked", branches=[{"label": "ok", "condition": "ok"}, {"label": "no", "condition": "no"}]),
    node("del", "prompt", label="Delegate", prompt="~~~bash\ndart format -l 120 a.dart\n~~~\nrecord with run_state.py"),
    node("dd", "ifElse", evaluationTarget="the route printed by run_state.py", branches=[{"label": "done", "condition": "route is done"}, {"label": "other", "condition": "route is not done"}]),
    node("end", "end", label="End")],
    [conn("start", "sw"), conn("sw", "dl", "branch-0"), conn("sw", "del", "branch-1"), conn("sw", "end", "branch-2"),
     conn("dl", "dlok"), conn("dlok", "end", "branch-0"), conn("dlok", "end", "branch-1"), conn("del", "dd"),
     conn("dd", "end", "branch-0"), conn("dd", "end", "branch-1")])
eb = sorted(f["node"] for f in cwc.check(res) if f["check"] == "execute_before_approval")
checks["approved branch gated, first-run branch still scanned"] = eb == ["dl"]
bad_default = json.loads(json.dumps(ng))
for n in bad_default["nodes"]:
    if n["id"] == "d":
        n["data"]["branches"][-1]["condition"] = "Other cases: route is escalate"  # the anti-pattern
checks["default standing for a route flagged"] = [f["node"] for f in cwc.check(bad_default) if f["check"] == "default_as_route"] == ["d"]
checks["approval-validity decision counts as a gate"] = not any(f["check"] == "execute_before_approval" for f in cwc.check(ng2))
if fng:
    print("unexpected on state-routed workflow:", fng)

import tempfile as _tf
with _tf.TemporaryDirectory() as _d:
    _root = Path(_d)
    (_root / ".claude/skills/zq-sync").mkdir(parents=True)
    (_root / ".claude/skills/zq-sync/SKILL.md").write_text('---\nname: zq-sync\ndescription: x\nargument-hint: "[--resume-only]"\n---\nbody\n')
    def _wf(ep):
        return build([node("start", "start", label="Start"),
                      node("s", "skill", name="zq-sync", executionMode="execute", skillPath=".claude/skills/zq-sync/SKILL.md", executionPrompt=ep),
                      node("q", "ifElse", evaluationTarget="whether it worked", branches=[{"label": "ok", "condition": "ok"}, {"label": "no", "condition": "no"}]),
                      node("end", "end", label="End")],
                     [conn("start", "s"), conn("s", "q"), conn("q", "end", "branch-0"), conn("q", "end", "branch-1")])
    own = [f["check"] for f in cwc.check(_wf("Run it with --resume-only (its own mode)."), str(_root))]
    other = [f["check"] for f in cwc.check(_wf("Sync only resume rows; do not ask."), str(_root))]
    checks["own documented mode is not an override"] = "execute_override" not in own
    checks["per-caller restriction is still an override"] = "execute_override" in other

print("\n".join(f"{'PASS' if v else 'FAIL'} {k}" for k, v in checks.items()))
if not all(checks.values()):
    print(kinds)
sys.exit(0 if all(checks.values()) else 1)
