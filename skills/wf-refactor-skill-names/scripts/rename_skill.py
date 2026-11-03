#!/usr/bin/env python3
"""Rename the user's own skills: folder, ownership.md, `name:` and references.

Dry run by default: prints the plan as JSON and changes nothing. --apply carries it out
and prints the same JSON with "applied": true per row.

Usage:
  rename_skill.py --skills-dir DIR --old OLD --new NEW [--project DIR] [--apply]
  rename_skill.py --plan PLAN.json [--apply] [--out RESULT.json]

PLAN.json is a list of {"skills_dir", "old", "new", "project"?, "skip_files"?: [paths]}.
"project" adds that project's CLAUDE.md, .claude and .vscode/workflows to the search
(default: the project that holds skills_dir). "skip_files" are reference hits to leave
alone, e.g. where the old name is an unrelated common word.

Every row is checked before anything moves; one refused row stops the whole run:
the source has a SKILL.md, the target does not exist, the new name passes the naming
check, the skill is `mine`, and the folder is not a symlink (its target may be shared,
so ask the user).

References to the old name (whole word) are sorted into:
  edit      text files this script rewrites with --apply
  workflow  .vscode/workflows copies: regenerate with wf-convert-skills-to-workflows
  external  files inside someone else's skill: list for the user, never edit
  job       LaunchAgents / crontab lines that run the skill by path: update and reload by hand
  skipped   files named in skip_files
Session transcripts, .trash and projects/*/memory are never searched.

Exit codes: 0 ok, 1 a row was refused, 2 usage error.
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_audit():
    spec = importlib.util.spec_from_file_location("audit", HERE / "audit_skill_names.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


audit = load_audit()


def tilde(p):
    p = Path(p)
    try:
        return "~/" + str(p.relative_to(Path.home()))
    except ValueError:
        return str(p)


def word_re(name):
    return re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])")


def skill_of(f):
    """The skill folder that holds file f, or None."""
    for d in f.parents:
        if d.parent.name == "skills" and (d / "SKILL.md").is_file():
            return d
    return None


HOME_CLAUDE_KEEP = {"skills", "commands", "agents", "hooks", "CLAUDE.md", "settings.json", "settings.local.json"}


def never_search(f):
    parts = f.parts
    if ".trash" in parts or "memory" in parts or f.suffix == ".jsonl":
        return True
    home_claude = (Path.home() / ".claude").resolve()
    if home_claude in f.parents:
        return f.relative_to(home_claude).parts[0] not in HOME_CLAUDE_KEEP
    return False


def job_hits(old, project):
    pat = f"/skills/{old}/"
    hits = []
    dirs = [Path.home() / "Library" / "LaunchAgents"]
    if project:
        dirs.append(Path(project) / "scripts")
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*")):
            if f.is_file() and f.suffix in (".plist", ".sh", ".py", ".zsh", ""):
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for n, line in enumerate(text.splitlines(), 1):
                    if pat in line:
                        hits.append({"file": str(f), "line": n, "text": line.strip()[:160]})
    try:
        cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        cron = ""
    for n, line in enumerate(cron.splitlines(), 1):
        if pat in line:
            hits.append({"file": "crontab", "line": n, "text": line.strip()[:160]})
    return hits


def plan_row(row, me, mine, ext, codes):
    sdir = Path(row["skills_dir"]).expanduser()
    old, new = row["old"], row["new"]
    src, dst = sdir / old, sdir / new
    if row.get("project"):
        project = Path(row["project"]).expanduser()
    elif sdir.resolve().parent == (Path.home() / ".claude").resolve():
        project = None  # global skills: pass --project to search a project that uses them
    else:
        project = sdir.parent.parent
    out = {"skills_dir": str(sdir), "old": old, "new": new, "project": str(project) if project else None,
           "src": str(src), "dst": str(dst), "refused": [], "applied": False}

    if not (src / "SKILL.md").is_file():
        out["refused"].append(f"{src} has no SKILL.md")
    if src.is_symlink():
        out["refused"].append(f"{src} is a symlink to {os.readlink(src)}; ask whether to rename the target too")
    if dst.exists() or dst.is_symlink():
        out["refused"].append(f"{dst} already exists")
    status = audit.check(new, codes)
    if status != "ok":
        out["refused"].append(f"new name `{new}` fails the naming check ({status})")
    if out["refused"]:
        return out
    who = audit.owner(src, me, mine, ext, audit.lock_entries(sdir))
    out["owner"] = who
    if who != "mine":
        out["refused"].append(f"owner is `{who}`, not mine; never rename it"
                              + (" (ask the user and record the answer in ownership.md)" if who.startswith("unknown") else ""))
        return out

    out["move"] = "git mv" if audit.git(sdir, "rev-parse", "--show-toplevel") else "mv"
    skip = {str(Path(p).expanduser().resolve()) for p in row.get("skip_files") or []}
    pat = word_re(old)
    roots = audit.search_roots([sdir])
    if project and project != sdir.parent.parent:
        roots += [r for r in (project / "CLAUDE.md", project / ".claude", project / ".vscode" / "workflows")
                  if r.exists()]
    refs = {k: [] for k in ("edit", "workflow", "external", "skipped")}
    seen = set()
    for f in audit.files_under(roots):
        rf = f.resolve()
        if rf in seen or never_search(rf):
            continue
        seen.add(rf)
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = [{"line": n, "text": ln.strip()[:160]} for n, ln in enumerate(text.splitlines(), 1) if pat.search(ln)]
        if not lines:
            continue
        entry = {"file": str(rf), "hits": lines}
        owning = skill_of(rf)
        if str(rf) in skip:
            refs["skipped"].append(entry)
        elif ".vscode" in rf.parts and "workflows" in rf.parts:
            refs["workflow"].append(entry)
        elif owning and owning.resolve() != src.resolve() and \
                audit.owner(owning, me, mine, ext, audit.lock_entries(owning.parent)) != "mine":
            entry["owner"] = audit.owner(owning, me, mine, ext, audit.lock_entries(owning.parent))
            refs["external"].append(entry)
        else:
            refs["edit"].append(entry)
    out["references"] = refs
    out["jobs"] = job_hits(old, project)
    return out


def update_ownership(src, dst):
    md = audit.HERE / "ownership.md"
    lines = md.read_text(encoding="utf-8").splitlines(keepends=True) if md.is_file() else ["## Mine\n"]
    section, mine_end, replaced = None, None, False
    for i, line in enumerate(lines):
        if line.startswith("## "):
            if section == "mine" and mine_end is None:
                mine_end = i
            section = line[3:].strip().lower()
        elif section == "mine" and line.startswith("- "):
            val = line[2:].strip().strip("`")
            if not any(ch in val for ch in "*?[") and Path(val).expanduser().resolve() == src.resolve():
                lines[i] = f"- {tilde(dst)}\n"
                replaced = True
    if not replaced:
        at = mine_end if mine_end is not None else len(lines)
        while at > 0 and lines[at - 1].strip() == "":
            at -= 1
        lines.insert(at, f"- {tilde(dst)}\n")
    md.write_text("".join(lines), encoding="utf-8")


def set_name(skill_md, new):
    text = skill_md.read_text(encoding="utf-8")
    m = re.match(r"(---\s*\n)(.*?)(\n---)", text, re.S)
    if not m:
        return
    fm = re.sub(r"^name:.*$", f"name: {new}", m.group(2), count=1, flags=re.M)
    skill_md.write_text(m.group(1) + fm + m.group(3) + text[m.end():], encoding="utf-8")


def apply_row(p):
    src, dst = Path(p["src"]), Path(p["dst"])
    old, new = p["old"], p["new"]
    rsrc = src.resolve()
    rdst = rsrc.parent / dst.name
    if p["move"] == "git mv":
        r = subprocess.run(["git", "-C", str(src.parent), "mv", src.name, dst.name], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"git mv failed: {r.stderr.strip()}")
    else:
        os.rename(src, dst)
    update_ownership(src, dst)
    pat = word_re(old)
    for e in p["references"]["edit"]:
        f = Path(e["file"])
        if rsrc in f.parents:
            f = rdst / f.relative_to(rsrc)
        text = f.read_text(encoding="utf-8")
        f.write_text(pat.sub(new, text), encoding="utf-8")
    set_name(dst / "SKILL.md", new)
    p["applied"] = True


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", type=Path)
    ap.add_argument("--skills-dir")
    ap.add_argument("--old")
    ap.add_argument("--new")
    ap.add_argument("--project")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    if a.plan:
        rows = json.loads(a.plan.read_text(encoding="utf-8"))
    elif a.skills_dir and a.old and a.new:
        rows = [{"skills_dir": a.skills_dir, "old": a.old, "new": a.new, "project": a.project}]
    else:
        ap.print_usage(sys.stderr)
        return 2

    me, mine, ext = audit.read_ownership()
    codes = audit.project_codes()
    plans = [plan_row(r, me, mine, ext, codes) for r in rows]
    targets = [p["dst"] for p in plans]
    for p in plans:
        if targets.count(p["dst"]) > 1:
            p["refused"].append("another row renames to the same target")
    refused = any(p["refused"] for p in plans)
    if a.apply and not refused:
        for p in plans:
            apply_row(p)
    text = json.dumps(plans, ensure_ascii=False, indent=1)
    if a.out:
        a.out.write_text(text, encoding="utf-8")
    print(text)
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
