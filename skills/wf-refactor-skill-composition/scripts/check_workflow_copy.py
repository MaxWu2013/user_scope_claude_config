#!/usr/bin/env python3
"""Check a CC Workflow Studio workflow (a derived copy of a skill) for control-flow defects.

Read-only. Usage: check_workflow_copy.py <workflow.json> [--root <project root>]

Every finding is a heuristic pointing at a node to read; none is a verdict. Checks:
  execute_before_approval   skill nodes in `execute` mode, or prompts with file-changing
                            commands, reachable from start before any approval question (D26)
  unbounded_loop            an automatic cycle (no question node in it) whose nodes never
                            state a limit (max N, at most N, …) (D27)
  prose_stop_no_branch      a prompt that says stop / ask / wait for approval but has a
                            single way out that is not a question, an end, or a decision
                            with a branch to one (D24)
  prose_stop_via_decision   the same, but a decision with a branch to a question/end follows:
                            confirm by reading that it branches on what the prompt stops for (D24)
  stop_option_merges_back   a question option meaning stop / cancel that does not lead to an end (D24)
  approval_without_artifact an approval question that names no file path (D9)
  needs_changes_no_collect  an approval "changes" option that loops back without a node
                            that asks what to change (D9)
  decision_no_target        an ifElse / switch without `evaluationTarget`
  execute_override          an executed skill whose `executionPrompt` restricts it ("only",
                            "do not") — compare with the helper's own steps (D22)
  control_in_prose          a decision that branches on a count, limit or version, or a prompt
                            that counts attempts, with no run-state file/script behind it (D28)
  executed_no_failure_path  a node that executes something (skill in execute mode, mcp,
                            subAgent, subAgentFlow, a delegation prompt) whose next node is
                            not a decision or question, so a failure has nowhere to go (D29)
  approval_skip_in_prose    a prompt says an approval may be skipped/reused, but no decision
                            checks approval validity (approved version vs current) (D9)
  content_chain             3+ prompt nodes in a straight line (one way in, one way out)
                            carrying content but no control (D30)
  default_as_route          a switch on a run-state route whose default branch stands for a
                            real route (e.g. "route is escalate"): an unexpected route or a
                            rejected event would be treated as that route (D28)
A loop whose decision reads the route from a run-state script (`run_state.py`) is bounded
by that state (its `escalate` exit), so it is not reported as unbounded.
An executed skill whose `executionPrompt` selects the helper's own `--stage` mode is not
counted as changing the repo before approval (it writes to a scratch folder). An
`executionPrompt` that only selects flags the helper documents in its `argument-hint` is the
helper's own mode, not an override.
`--root` (default: the folder holding .vscode/) lets it read helpers' SKILL.md to
report what an executed helper writes.
"""
import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("map_skill_deps", HERE / "map_skill_deps.py")
deps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deps)

APPROVAL = re.compile(r"\bapprov\w*", re.I)
STOP_PROSE = re.compile(r"stop and ask|ask the user|wait for (the user'?s? )?approval|do not continue until|"
                        r"flag it to the user|get the user'?s approval|wait for (their|an) answer", re.I)
STOP_OPTION = re.compile(r"^\s*(stop|cancel|abort|quit|do not|don't)\b", re.I)
CHANGES_OPTION = re.compile(r"change|revise|edit|fix", re.I)
COLLECT = re.compile(r"\bask (the user )?what to change|which changes|what should change|collect the (requested )?changes", re.I)
LIMIT = re.compile(r"\b(max(imum)?|at most|up to|no more than|limit(ed)? to)\s*(of\s*)?\d+|\b\d+\s*(automatic\s+)?(retries|attempts|tries)\b|"
                   r"attempt (count|number)", re.I)
STAGED = re.compile(r"--stage\b")
APPROVED_PATH = re.compile(r"approv\w*[^.;]*\b(is current|equals|matches|reused)\b", re.I)
STATE_REF = re.compile(r"run_state\.py|run-state|state file|run\.json|route printed|printed route", re.I)
COUNTING = re.compile(r"\b(attempts?|retr(y|ies)|re-?runs?|times|limit|version|used up|fewer than|below \d|at most \d|max(imum)? \d|add 1|count(er)?)\b", re.I)
SKIP_APPROVAL = re.compile(r"(don'?t|do not) ask (a second time|again)|skip (the )?(approval|question)|already (shown|approved)[^.]*approv|reuse (the |an )?approval", re.I)
DELEGATION_RUN = re.compile(r"/runner:sub-agents\s+Run|^\s*Delegate with", re.I | re.M)
EXECUTING = ("mcp", "subAgent", "subAgentFlow", "codex")
ROUTE_TOKEN = re.compile(r"\b(write-spec|approve|execute|retry-execute|verify|revise-spec|handle-failure|escalate|done|stopped)\b")
PATH = re.compile(r"[\w.-]*/[\w./<>-]+|\b[\w<>-]+\.(md|json|ya?ml|dart)\b")


def text_of(node):
    d = node.get("data", {})
    parts = [d.get(k, "") for k in ("prompt", "label", "questionText", "evaluationTarget", "executionPrompt", "description")]
    parts += [b.get("condition", "") + " " + b.get("label", "") for b in d.get("branches", [])]
    parts += [o.get("label", "") + " " + o.get("description", "") for o in d.get("options", [])]
    return "\n".join(p for p in parts if isinstance(p, str))


def graph(wf):
    nodes = {n["id"]: n for n in wf["nodes"]}
    out = {i: [] for i in nodes}
    for c in wf["connections"]:
        out.setdefault(c["from"], []).append((c["fromPort"], c["to"]))
    return nodes, out


def helper_flags(root, skill_path):
    """Flags a helper documents in its own frontmatter argument-hint (its own modes)."""
    if not skill_path:
        return set()
    p = Path(skill_path).expanduser() if skill_path.startswith("~") else (Path(root) / skill_path if root else None)
    if not p or not p.is_file():
        return set()
    t = p.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^argument-hint:\s*(.+)$", t[: t.find("\n---", 4)], re.M)
    return set(re.findall(r"--[a-z][\w-]*", m.group(1))) if m else set()


def is_approval(node):
    """An approval gate: the question or an option label asks to approve (descriptions don't count)."""
    if node["type"] != "askUserQuestion":
        return False
    d = node.get("data", {})
    return bool(APPROVAL.search(d.get("questionText", "")) or
                any(APPROVAL.search(o.get("label", "")) for o in d.get("options", [])))


def is_approval_check(node):
    """A decision that checks approval validity (approved version = current, from run state):
    its approved branch is an approval gate too, e.g. a parent's approval being reused."""
    return node["type"] in ("ifElse", "switch") and bool(APPROVAL.search(text_of(node))) \
        and bool(re.search(r"version|approved_spec", text_of(node), re.I)) and bool(STATE_REF.search(text_of(node)))


def is_context(node):
    """The converter's "Context & rules" node describes the whole skill; it does not act."""
    return node["type"] == "prompt" and str(node.get("data", {}).get("label", "")).lower().startswith("context")


def sccs(nodes, out):
    """Tarjan's strongly connected components; returns those that form a cycle."""
    index, low, stack, on, res, counter = {}, {}, [], set(), [], [0]

    def visit(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for _, w in out.get(v, []):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1 or any(t == v for _, t in out.get(v, [])):
                res.append(sorted(comp))

    sys.setrecursionlimit(10000)
    for v in nodes:
        if v not in index:
            visit(v)
    return res


def check(wf, root=None):
    nodes, out = graph(wf)
    findings = []

    def add(kind, node, detail):
        findings.append({"check": kind, "node": node, "detail": detail})

    # before approval: reachable from start without passing an approval question, or the
    # "approval is current" branch of a decision that checks approval validity
    pre, todo = set(), [n for n in nodes if nodes[n]["type"] == "start"]
    while todo:
        v = todo.pop()
        if v in pre:
            continue
        pre.add(v)
        if is_approval(nodes[v]):
            continue
        branches = nodes[v].get("data", {}).get("branches", [])
        for port, t in out.get(v, []):
            if is_approval_check(nodes[v]) and port.startswith("branch-"):
                i = int(port.split("-")[1])
                if i < len(branches) and APPROVED_PATH.search(branches[i].get("condition", "")):
                    continue  # this branch is taken only with a current approval
            todo.append(t)
    has_approval = any(is_approval(n) for n in nodes.values())
    for v in sorted(pre):
        n, d = nodes[v], nodes[v].get("data", {})
        if n["type"] == "skill" and d.get("executionMode", "execute") == "execute":
            if STAGED.search(d.get("executionPrompt", "")):
                continue  # told to use the helper's own stage mode: writes go to scratch, not the repo
            writes = None
            sp = d.get("skillPath")
            if root and sp and (Path(root) / sp).is_file():
                eff = deps.helper_effects((Path(root) / sp).read_text(encoding="utf-8", errors="replace"))
                writes = {"write_hint_count": eff["write_hint_count"], "mutating_commands": eff["mutating_commands"]}
            add("execute_before_approval", v,
                {"skill": d.get("name"), "approval_exists": bool(has_approval), "helper_effects": writes})
        if n["type"] == "prompt":
            mut = [c for c in deps.commands_in(d.get("prompt", "")) if c["kind"] == "mutating" and not c["negated"]]
            if mut:
                add("execute_before_approval", v, {"commands": [c["command"][:80] for c in mut]})

    for comp in sccs(nodes, out):
        # a loop through a question is paced by the user; only automatic loops need a limit
        if any(nodes[v]["type"] == "askUserQuestion" for v in comp):
            continue
        # a loop routed by a run-state script is bounded by that state (its escalate exit)
        if any(nodes[v]["type"] in ("ifElse", "switch") and STATE_REF.search(text_of(nodes[v]))
               and any(t not in comp for _, t in out.get(v, [])) for v in comp):
            continue
        if not any(LIMIT.search(text_of(nodes[v])) for v in comp):
            add("unbounded_loop", ",".join(comp), "automatic cycle (no question in it) and no node states a limit")

    for v, n in nodes.items():
        d = n.get("data", {})
        exits = out.get(v, [])
        if n["type"] == "prompt" and not is_context(n) and STOP_PROSE.search(d.get("prompt", "")):
            said = STOP_PROSE.search(d["prompt"]).group(0)
            nxt = [t for _, t in exits]
            direct = any(nodes[t]["type"] in ("askUserQuestion", "end") for t in nxt)
            via = [t for t in nxt if nodes[t]["type"] in ("ifElse", "switch")
                   and any(nodes[x]["type"] in ("askUserQuestion", "end") for _, x in out.get(t, []))]
            if len(exits) <= 1 and not direct:
                if via:
                    # a decision with a branch to a question/end follows: it may realise this stop, or
                    # only classify something else — a script can't tell, so ask the reader to confirm
                    add("prose_stop_via_decision", v, {"said": said, "decision": via[0],
                                                       "confirm": "does this decision branch on the condition the prompt stops for?"})
                else:
                    add("prose_stop_no_branch", v, said)
        if n["type"] == "askUserQuestion":
            for i, o in enumerate(d.get("options", [])):
                targets = [t for p, t in exits if p == f"branch-{i}"]
                if STOP_OPTION.search(o.get("label", "")) and not any(nodes[t]["type"] == "end" for t in targets):
                    add("stop_option_merges_back", v, {"option": o.get("label"), "to": targets})
            if is_approval(n):
                if not PATH.search(d.get("questionText", "")):
                    add("approval_without_artifact", v, d.get("questionText", "")[:120])
                for i, o in enumerate(d.get("options", [])):
                    if CHANGES_OPTION.search(o.get("label", "")):
                        targets = [t for p, t in exits if p == f"branch-{i}"]
                        if not any(COLLECT.search(text_of(nodes[t])) or nodes[t]["type"] == "askUserQuestion" for t in targets):
                            add("needs_changes_no_collect", v, {"option": o.get("label"), "to": targets})
        if n["type"] in ("ifElse", "switch") and not d.get("evaluationTarget"):
            add("decision_no_target", v, [b.get("label") for b in d.get("branches", [])])
        ep = d.get("executionPrompt", "")
        own_mode = bool(re.findall(r"--[a-z][\w-]*", ep)) and \
            set(re.findall(r"--[a-z][\w-]*", ep)) <= helper_flags(root, d.get("skillPath"))
        if n["type"] == "skill" and d.get("executionMode", "execute") == "execute" \
                and not STAGED.search(ep) and not own_mode \
                and re.search(r"\b(only|do not|don't|never)\b", ep, re.I):
            add("execute_override", v, {"skill": d.get("name"), "executionPrompt": d["executionPrompt"][:160]})
    # D28: control logic held in prose
    for v, n in nodes.items():
        d = n.get("data", {})
        if is_context(n):
            continue
        if n["type"] in ("ifElse", "switch"):
            cond = " ".join([d.get("evaluationTarget", "")] + [b.get("condition", "") for b in d.get("branches", [])])
            if COUNTING.search(cond) and not STATE_REF.search(cond):
                add("control_in_prose", v, {"decision_on": COUNTING.search(cond).group(0)})
        if n["type"] == "prompt":
            m = re.search(r"\b(add 1 to|reset `?\w+`? to 0|count (the )?(attempts|retries))\b", d.get("prompt", ""), re.I)
            if m and not STATE_REF.search(d.get("prompt", "")):
                add("control_in_prose", v, {"prompt_counts": m.group(0)})

    # D29: executed steps without a failure path
    def branches_after(v):
        return any(nodes[t]["type"] in ("ifElse", "switch", "askUserQuestion") for _, t in out.get(v, []))
    for v, n in nodes.items():
        d = n.get("data", {})
        executes = (n["type"] == "skill" and d.get("executionMode", "execute") == "execute") or n["type"] in EXECUTING \
            or (n["type"] == "prompt" and not is_context(n) and DELEGATION_RUN.search(d.get("prompt", "")))
        if executes and not branches_after(v):
            add("executed_no_failure_path", v, {"type": n["type"], "next": [t for _, t in out.get(v, [])]})

    # D9: approval reuse only in prose
    checks_validity = any(nodes[v]["type"] in ("ifElse", "switch") and APPROVAL.search(text_of(nodes[v]))
                          and re.search(r"version|approved_spec", text_of(nodes[v]), re.I) for v in nodes)
    if not checks_validity:
        for v, n in nodes.items():
            if n["type"] == "prompt" and not is_context(n) and SKIP_APPROVAL.search(n.get("data", {}).get("prompt", "")):
                add("approval_skip_in_prose", v, SKIP_APPROVAL.search(n["data"]["prompt"]).group(0))

    # D28: a route switch whose default stands for a real route
    for v, n in nodes.items():
        d = n.get("data", {})
        if n["type"] == "switch" and STATE_REF.search(d.get("evaluationTarget", "")):
            for b in d.get("branches", []):
                if b.get("isDefault") and ROUTE_TOKEN.search(b.get("condition", "")):
                    add("default_as_route", v, {"default": b.get("condition")})

    # D30: straight chains of content-only prompt nodes
    indeg = {v: 0 for v in nodes}
    for c in wf["connections"]:
        indeg[c["to"]] = indeg.get(c["to"], 0) + 1
    def plain(v):
        return nodes[v]["type"] == "prompt" and not is_context(nodes[v]) and len(out.get(v, [])) == 1
    seen = set()
    for v in nodes:
        if v in seen or not plain(v):
            continue
        prev = [c["from"] for c in wf["connections"] if c["to"] == v]
        if len(prev) == 1 and plain(prev[0]) and indeg[v] == 1:
            continue  # not the head of a chain
        chain, cur = [v], v
        while True:
            nxt = out[cur][0][1]
            if nxt in chain or not plain(nxt) or indeg[nxt] != 1:
                break
            chain.append(nxt)
            cur = nxt
        seen.update(chain)
        if len(chain) >= 3:
            add("content_chain", chain[0], {"chain": chain})
    return findings


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("workflow")
    ap.add_argument("--root")
    a = ap.parse_args(argv)
    p = Path(a.workflow).expanduser()
    wf = json.loads(p.read_text(encoding="utf-8"))
    root = a.root or next((str(q.parent) for q in p.resolve().parents if q.name == ".vscode"), None)
    f = check(wf, root)
    print(json.dumps({"workflow": str(p), "root": root, "kind": "heuristic", "findings": f,
                      "counts": {k: sum(1 for x in f if x["check"] == k) for k in sorted({x["check"] for x in f})}},
                     indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
