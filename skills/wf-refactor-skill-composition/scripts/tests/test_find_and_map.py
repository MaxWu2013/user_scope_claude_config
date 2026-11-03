"""Fixture tests for find_skill_runs.py and the missing-helper fields of map_skill_deps.py."""
import importlib.util
import json
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


find = load("find_skill_runs")
deps = load("map_skill_deps")


def skill(folder, name, body=""):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\n{body}\n")


def git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)


checks = {}
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    # --- former names: folder renamed, and name: changed once without a folder rename
    repo = tmp / "repo"
    skill(repo / "skills" / "old-dialog", "old-dialog")
    git(repo.parent, "init", "-q", str(repo))
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "1")
    skill(repo / "skills" / "old-dialog", "mid-dialog")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "2")
    git(repo, "mv", "skills/old-dialog", "skills/pz-wf-dialog")
    skill(repo / "skills" / "pz-wf-dialog", "pz-wf-dialog")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "3")
    cur, former, hist = find.names_of(repo / "skills" / "pz-wf-dialog")
    checks["current name"] = cur == "pz-wf-dialog"
    checks["former names from folder and name:"] = former == {"old-dialog", "mid-dialog"} and hist

    # --- transcripts: real runs vs listing vs replayed rows
    proj = tmp / "projects" / "p1"
    proj.mkdir(parents=True)
    rows = [
        {"type": "user", "message": {"role": "user", "content": "available skills: old-dialog, pz-wf-dialog"}, "isMeta": True},
        {"type": "user", "message": {"role": "user", "content": "<command-name>/old-dialog</command-name>"}, "timestamp": "t1"},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": "pz-wf-dialog"}}]}},
        {"type": "api-request-blob", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": "pz-wf-dialog"}}]}},
    ]
    (proj / "s-run.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    (proj / "s-list.jsonl").write_text(json.dumps(rows[0]))
    sub = proj / "s-parent" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-1.jsonl").write_text(json.dumps(rows[2]))
    runs, listing = find.scan({"pz-wf-dialog", "old-dialog", "mid-dialog"}, tmp / "projects")
    by = {r["session"]: r for r in runs}
    checks["run under old and new name, blob ignored"] = by.get("s-run", {}).get("invocations") == 2 \
        and by["s-run"]["names"] == ["old-dialog", "pz-wf-dialog"]
    checks["sub-agent run found"] = by.get("s-parent", {}).get("subagent") is True
    checks["listing-only not a run"] = "s-list" not in by and listing == 1

    # --- map: run the real script on a fixture project
    proj_root = tmp / "proj"
    sk = proj_root / ".claude" / "skills"
    skill(sk / "zq-orchestrator", "zq-orchestrator",
          "uses `zq-helper`. Call mcp__Nonexistent_Server__get_x and mcp__claude_ai_Figma__get_y.")
    skill(sk / "zq-helper", "zq-helper",
          "Append the key to `string_en.json` and `k.dart`.\n> quoted line > not a redirect\n"
          "~~~bash\nkdart_cli -p .\n~~~\n")
    skill(sk / "zq-consumer", "zq-consumer", "a consumer of this skill is `zq-orchestrator`")
    r = subprocess.run([sys.executable, str(SCRIPTS / "map_skill_deps.py"), "zq-orchestrator"],
                       cwd=proj_root, capture_output=True, text=True)
    out = json.loads(r.stdout)
    mcp = out["mcp_tools"]
    checks["unconfigured MCP server flagged"] = mcp.get("mcp__Nonexistent_Server__get_x") == "NOT CONFIGURED"
    checks["managed connector not judged"] = mcp.get("mcp__claude_ai_Figma__get_y", "").startswith("managed")
    checks["project_of"] = deps.project_of(sk / "zq-orchestrator") == proj_root
    checks["used = zq-helper"] = [s["name"] for s in out["skills_used"]] == ["zq-helper"]
    checks["mentioned_by_only = zq-consumer"] = [s["name"] for s in out["mentioned_by_only"]] == ["zq-consumer"]

    # project found from A's path even when run from elsewhere
    r2 = subprocess.run([sys.executable, str(SCRIPTS / "map_skill_deps.py"), str(sk / "zq-orchestrator")],
                        cwd=tmp, capture_output=True, text=True)
    out2 = json.loads(r2.stdout)
    checks["project searched from any cwd"] = [s["name"] for s in out2["skills_used"]] == ["zq-helper"]

    # helper effects: writes + mutating command in code; quote line ignored
    eff = out["skills_used"][0]["effects"]
    checks["helper write hints"] = eff["write_hint_count"] >= 1 and not eff["mentions_delegation"]
    checks["helper mutating only from code"] = eff["mutating_commands"] == ["regenerates k.dart"]

    # A's own commands
    body = "\n".join([
        "~~~bash", "dart format -l 120 lib/a.dart", "dart format --output=none --set-exit-if-changed lib",
        "flutter analyze banban_base/<module>/lib", "echo hi > out.txt", "~~~",
        "~~~dart", "final x = a > b;", "~~~",
        "Never run `kdart_cli -p .`; read `Read the spec <path> now`.",
        "> a quote > here",
    ])
    cmds = {c["command"]: c for c in deps.commands_in(body, offset=10)}
    checks["dart format mutating"] = cmds.get("dart format -l 120 lib/a.dart", {}).get("kind") == "mutating"
    checks["dart format check read-only"] = \
        cmds.get("dart format --output=none --set-exit-if-changed lib", {}).get("kind") == "read-only"
    checks["placeholder not a redirect"] = cmds.get("flutter analyze banban_base/<module>/lib", {}).get("kind") == "read-only"
    checks["real redirect mutating"] = cmds.get("echo hi > out.txt", {}).get("kind") == "mutating"
    checks["dart code not scanned"] = not any("a > b" in c for c in cmds)
    checks["negated flagged"] = cmds.get("kdart_cli -p .", {}).get("negated") is True
    checks["prose/quote not commands"] = not any("spec" in c or "quote" in c for c in cmds)
    checks["absolute line numbers"] = cmds["dart format -l 120 lib/a.dart"]["line"] == 12

print("\n".join(f"{'PASS' if v else 'FAIL'} {k}" for k, v in checks.items()))
sys.exit(0 if all(checks.values()) else 1)
