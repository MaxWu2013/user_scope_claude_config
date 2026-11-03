#!/usr/bin/env python3
"""Find real runs of a skill in Claude Code transcripts, under its current and former names.

Read-only. Usage: find_skill_runs.py <skill folder | SKILL.md> [--projects DIR]

Former names come from git: every path SKILL.md had (`git log --follow`) and every
`name:` it had in those commits. Without git history, only the current name is searched
and the output says so.

A run is a Skill tool call whose `skill` is one of the names, or a typed
`/<name>` command. Any other mention (e.g. the available-skills listing) is counted as
`listing_only`, never as a run. Sub-agent transcripts (<session>/subagents/*.jsonl) are
searched too.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
CMD = re.compile(r"<command-name>/?([^<\s]+)</command-name>")
NAME = re.compile(r"^name:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", re.M)


def git(cwd, *args):
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def frontmatter_name(text):
    m = re.match(r"---\s*\n(.*?)\n---", text, re.S)
    n = NAME.search(m.group(1)) if m else None
    return n.group(1).strip() if n else None


def names_of(folder):
    """Return (current name, {former names}, history_available)."""
    skill_md = folder / "SKILL.md"
    current = frontmatter_name(skill_md.read_text(encoding="utf-8", errors="replace")) or folder.name
    names = {current, folder.name}
    root = git(folder, "rev-parse", "--show-toplevel")
    if not root:
        return current, names - {current}, False
    rel = skill_md.resolve().relative_to(Path(root.strip()).resolve())
    log = git(root.strip(), "log", "--follow", "--name-only", "--format=@@%H", "--", str(rel)) or ""
    commit = None
    for line in log.splitlines():
        if line.startswith("@@"):
            commit = line[2:]
        elif line.strip() and commit:
            p = Path(line.strip())
            names.add(p.parent.name)
            old = git(root.strip(), "show", f"{commit}:{p}")
            if old:
                n = frontmatter_name(old)
                if n:
                    names.add(n)
    names.discard("")
    return current, names - {current}, True


def text_of(content):
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text")


def invoked(r, names):
    """Name invoked by this transcript row, or None."""
    if r.get("type") not in ("user", "assistant"):   # e.g. api-request-blob rows replay old messages
        return None
    m = r.get("message")
    if not isinstance(m, dict):
        return None
    if m.get("role") == "assistant":
        for c in m.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Skill":
                s = str((c.get("input") or {}).get("skill", "")).lstrip("/")
                if s in names or s.split(":")[-1] in names:
                    return s
    elif m.get("role") == "user" and not r.get("isMeta"):
        for n in CMD.findall(text_of(m.get("content"))):
            if n in names or n.split(":")[-1] in names:
                return n
    return None


def scan(names, projects):
    runs, listing = {}, 0
    files = sorted(projects.glob("*/*.jsonl")) + sorted(projects.glob("*/*/subagents/*.jsonl"))
    for f in files:
        hit_any = False
        for line in f.open(encoding="utf-8", errors="replace"):
            if not any(n in line for n in names):
                continue
            hit_any = True
            try:
                r = json.loads(line)
            except ValueError:
                continue
            n = invoked(r, names)
            if n:
                e = runs.setdefault(str(f), {"session": f.stem if f.parent.name != "subagents" else f.parent.parent.name,
                                             "subagent": f.parent.name == "subagents",
                                             "project": (f.parent if f.parent.name != "subagents" else f.parent.parent.parent).name,
                                             "names": set(), "invocations": 0, "first": r.get("timestamp")})
                e["names"].add(n)
                e["invocations"] += 1
        if hit_any and str(f) not in runs:
            listing += 1
    return [dict(v, names=sorted(v["names"])) for v in runs.values()], listing


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("skill")
    ap.add_argument("--projects", default=str(PROJECTS))
    a = ap.parse_args(argv)
    p = Path(a.skill).expanduser()
    folder = p.parent if p.name == "SKILL.md" else p
    if not (folder / "SKILL.md").is_file():
        print(f"not a skill folder: {a.skill}", file=sys.stderr)
        return 1
    current, former, history = names_of(folder)
    runs, listing = scan({current, *former}, Path(a.projects).expanduser())
    print(json.dumps({"current_name": current, "former_names": sorted(former),
                      "git_history": history, "runs": runs,
                      "transcripts_listing_only": listing}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
