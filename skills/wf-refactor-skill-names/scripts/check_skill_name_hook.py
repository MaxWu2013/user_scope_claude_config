#!/usr/bin/env python3
"""PostToolUse hook: check a SKILL.md the user owns after it is written.

Reads the hook JSON on stdin. Silent (exit 0, no output) unless the written file is
<skills folder>/<skill>/SKILL.md and the skill is not someone else's. Then:
- the folder name or `name:` breaks [<project>-]<type>-<skill-name> → warn;
- the Write created the file (a new skill) → tell Claude to run
  wf-refactor-skill-names on it, then wf-refactor-skill-composition if it is a
  workflow (`wf`) or orchestrator (`or`) skill.
"""
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKFLOW_TYPES = ("wf", "or")  # skills that get wf-refactor-skill-composition


def load_audit():
    spec = importlib.util.spec_from_file_location("audit", HERE / "audit_skill_names.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def skill_type(folder, audit, projects):
    """The <type> part of a conforming name, or None if the name does not conform."""
    if audit.check(folder, projects) != "ok":
        return None
    parts = folder.split("-")
    return parts[1] if parts[0] in projects and parts[1] in audit.TYPES else parts[0]


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    inp = data.get("tool_input") or {}
    resp = data.get("tool_response") or {}
    path = inp.get("file_path") or (resp.get("filePath") if isinstance(resp, dict) else None)
    if not path:
        return 0
    f = Path(path).expanduser()
    if f.name != "SKILL.md" or f.parent.parent.name != "skills":
        return 0
    skill = f.parent
    if any(p in (".trash", "synced", "plugins") for p in skill.parts):
        return 0

    audit = load_audit()
    me, mine, ext = audit.read_ownership()
    who = audit.owner(skill, me, mine, ext, audit.lock_entries(skill.parent))
    if who.startswith(("ext:", "unknown:")):  # someone else's, or probably imported
        return 0

    name = audit.frontmatter_name(f) or ""
    projects = audit.project_codes()
    status = audit.check(skill.name, projects)
    problems = []
    if status != "ok":
        problems.append(f"folder name `{skill.name}` breaks the naming convention ({status})")
    if name and name != skill.name:
        problems.append(f"`name: {name}` does not match the folder name `{skill.name}`")
    if who == "unknown" and not problems:
        problems.append("it is not listed under Mine in ownership.md")
    # New: just created by Write, or not yet recorded anywhere (e.g. skill-creator wrote
    # it from a shell script and Claude is now editing it). Recording it under Mine ends this.
    created = (isinstance(resp, dict) and resp.get("type") == "create") or who == "unknown"
    if not problems and not created:
        return 0

    rules = HERE.parent / "SKILL.md"
    naming = (f"Name skills `[<project>-]<type>-<skill-name>` (types wf/kn/tl/tp/st/or) as defined in "
              f"{rules} (section \"The naming convention\"), keep `name:` equal to the folder name, "
              f"and add the skill's path under Mine in {HERE.parent / 'ownership.md'}.")
    if not created:
        msg = f"Skill {skill}: " + "; ".join(problems) + ". " + naming + " Propose the fix to the user before renaming."
        short = "Skill name check: " + skill.name + " — " + "; ".join(problems)
    else:
        kind = skill_type(skill.name, audit, projects)
        step1 = ("1. Run the `wf-refactor-skill-names` skill on it"
                 + (" (" + "; ".join(problems) + "). " + naming if problems else " (the name already conforms; confirm the ownership entry).")
                 + " Propose any rename to the user before doing it.")
        if kind in WORKFLOW_TYPES:
            step2 = (f"2. Its type is `{kind}`, so then run the `wf-refactor-skill-composition` skill on "
                     f"`{skill.name}` (it proposes changes and asks before applying any).")
        elif kind is None:
            step2 = ("2. After naming, if its type is `wf` (workflow) or `or` (orchestrator), run the "
                     "`wf-refactor-skill-composition` skill on it with its final name.")
        else:
            step2 = f"2. Its type is `{kind}`, not a workflow or orchestrator, so skip wf-refactor-skill-composition."
        msg = f"New skill created: {skill}. Before calling the work finished:\n{step1}\n{step2}"
        short = f"New skill {skill.name}: run wf-refactor-skill-names" + (
            ", then wf-refactor-skill-composition" if kind in WORKFLOW_TYPES
            else ", then wf-refactor-skill-composition if it is a workflow" if kind is None else "")
    print(json.dumps({
        "systemMessage": short,
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg},
    }))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 - a hook must never fail silently or block the edit
        print(json.dumps({"systemMessage": f"Skill name check crashed: {type(e).__name__}: {e} "
                                           f"({Path(__file__).name}); the edit itself went through."}))
        sys.exit(0)
