"""Fixture tests for the measuring fixes G4-G7 (measure_skill_run.py) and G9-G12 (map_skill_deps.py)."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = load("measure_skill_run")
deps = load("map_skill_deps")


def asst(mid, ctx, content):
    return {"type": "assistant", "timestamp": "2026-10-26T10:00:00Z", "message": {
        "id": mid, "role": "assistant", "model": "claude-opus-5-5", "content": content,
        "usage": {"input_tokens": ctx, "output_tokens": 1}}}


def user(content, **extra):
    return {"type": "user", **extra, "message": {"role": "user", "content": content}}


checks = {}

# G4: a re-invoked skill's text arrives in two meta rows; both count, until the next request.
rows = [
    asst("a1", 1000, [{"type": "tool_use", "id": "s1", "name": "Skill", "input": {"skill": "kn-x"}}]),
    user([{"type": "tool_result", "tool_use_id": "s1", "content": "Launching skill: kn-x"}]),
    user([{"type": "text", "text": "Re-invocation of /kn-x" + "a" * 100}], isMeta=True, sourceToolUseID="s1"),
    user([{"type": "text", "text": "b" * 4000}], isMeta=True, sourceToolUseID="s1"),
    asst("a2", 2100, [{"type": "text", "text": "ok"}]),
    user([{"type": "text", "text": "c" * 4000}], isMeta=True, sourceToolUseID="s1"),
]
o = m.measure(rows)
checks["G4 every meta row of a load counts, none after the next request"] = \
    o["skills"][0]["est_tokens"] == round((len("Re-invocation of /kn-x") + 100 + 4000) / 4)

# G5: a <command-name> printed by a tool result is not a skill load.
rows = [
    asst("b1", 1000, [{"type": "tool_use", "id": "g1", "name": "Bash", "input": {"command": "grep -r command-name ."}}]),
    user([{"type": "tool_result", "tool_use_id": "g1", "content": "x.jsonl: <command-name>/wf-fake</command-name>"}]),
    user("<command-name>/wf-real</command-name>"),
    asst("b2", 1200, [{"type": "text", "text": "ok"}]),
]
o = m.measure(rows)
checks["G5 slash loads come from the user's own text only"] = [s["skill"] for s in o["skills"]] == ["wf-real"]

# G6: cursor-agent counts only in command position, never inside a heredoc or a grep.
cmds = {
    "cursor-agent -p 'fix it'": True,
    "cd x && cursor-agent --model m -p task": True,
    "grep -rn cursor-agent ~/.agents": False,
    "rg 'cursor-agent' file": False,
    "cat > spec.md <<'EOF'\ncursor-agent must not run git\nEOF": False,
    "cat > spec.md <<'EOF'\nnotes\nEOF\ncursor-agent -p go": True,
}
checks["G6 cursor-agent command position"] = all(m.runs_cursor_agent(c) == want for c, want in cmds.items())

# G7: an agent call without run_in_background is "not set", and true once a task-notification names it.
rows = [
    asst("c1", 1000, [{"type": "tool_use", "id": "ag1", "name": "Agent", "input": {"description": "one"}},
                      {"type": "tool_use", "id": "ag2", "name": "Agent", "input": {"description": "two"}}]),
    user("<task-notification><tool-use-id>ag1</tool-use-id><status>completed</status></task-notification>"),
    asst("c2", 1100, [{"type": "text", "text": "ok"}]),
]
d = {x["detail"]: x["background"] for x in m.measure(rows)["observed"]["delegations"]}
checks["G7 background from task-notification, else not set"] = d == {"one": True, "two": "not set"}
queued = rows[:1] + [{"type": "queue-operation", "operation": "enqueue", "content":
                      "<task-notification>\n<tool-use-id>ag2</tool-use-id>\n</task-notification>"}] + rows[1:]
d = {x["detail"]: x["background"] for x in m.measure(queued)["observed"]["delegations"]}
checks["G7 queued task-notification counts too"] = d == {"one": True, "two": True}

# G8: a request range limits the report.
o = m.measure(rows, from_request=1, to_request=2)
checks["G8 request range"] = o["observed"]["requests"] == 1 and o["observed"]["delegations"] == []

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    sk = tmp / "proj" / ".claude" / "skills" / "wf-demo"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: wf-demo\ndescription: x\n---\n"     # body starts on line 5
        "Ask the user to confirm.\n"                    # line 5
        "Use a `general-purpose` agent for each file.\n"
        "```bash\n"
        "npx -y @cc-wf-studio/cli validate x.json\n"    # line 8
        "T=$(mktemp -d)\n"
        "```\n"
        "Then run `python3 scripts/check.py`.\n",       # line 11
        encoding="utf-8")
    r = subprocess.run([sys.executable, "-I", str(SCRIPTS / "map_skill_deps.py"), str(sk)],
                       capture_output=True, text=True, cwd=tmp)
    out = json.loads(r.stdout)
    checks["G9 signals use SKILL.md line numbers"] = out["target"]["signals"]["asks user"] == [5]
    checks["G10 backticked built-in agent found"] = any(a.get("name") == "general-purpose" for a in out["agents_used"])
    kinds = {c["line"]: c["kind"] for c in out["commands"]}
    checks["G11 unclassified shell and inline commands listed"] = \
        kinds.get(8) == "unclassified" and kinds.get(9) == "unclassified" and kinds.get(11) == "unclassified"

    # G12: consumers in a hook script, a project workflow copy and settings.
    home = tmp / "home"
    (home / ".claude" / "hooks").mkdir(parents=True)
    (home / ".claude" / "hooks" / "route.py").write_text("NAME = 'wf-demo'\n", encoding="utf-8")
    (home / ".claude" / "settings.json").write_text('{"allow": ["Skill(wf-demo)"]}', encoding="utf-8")
    wf = home / "workplace" / "grp" / "app"
    (wf / ".claude").mkdir(parents=True)
    (wf / ".vscode" / "workflows").mkdir(parents=True)
    (wf / ".vscode" / "workflows" / "wf-demo.json").write_text('{"name": "wf-demo"}', encoding="utf-8")
    (wf / "node_modules" / ".claude").mkdir(parents=True)
    r = subprocess.run([sys.executable, "-I", str(SCRIPTS / "map_skill_deps.py"), "--consumers", "wf-demo"],
                       capture_output=True, text=True, cwd=tmp, env={**os.environ, "HOME": str(home)})
    files = {Path(c["file"]).name for c in json.loads(r.stdout)["consumers"]}
    checks["G12 consumers in hooks, settings and workflow copies"] = \
        files == {"route.py", "settings.json", "wf-demo.json"}

print("\n".join(f"{'PASS' if v else 'FAIL'} {k}" for k, v in checks.items()))
sys.exit(0 if all(checks.values()) else 1)
