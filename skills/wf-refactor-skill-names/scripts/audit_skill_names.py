#!/usr/bin/env python3
"""List skills, who made them, and check their names against [<project>-]<type>-<skill-name>.

Read-only. Usage: audit_skill_names.py [dir ...]
Each dir may be a skills folder, a .claude folder or a project root.
Default: ~/.claude/skills and ./.claude/skills.

Owner column: mine (may be renamed), ext:<why> (never rename), unknown (ask the user).
"""
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Folder holding ownership.md and projects.md; SKILL_NAMES_CONFIG_DIR overrides it for tests.
HERE = Path(os.environ.get("SKILL_NAMES_CONFIG_DIR") or Path(__file__).resolve().parent.parent)
HOME = Path.home()
BULK_IMPORT = 10  # a commit adding this many skills at once is treated as an import

TYPES = ("wf", "kn", "tl", "tp", "st", "or")
LEGACY = {"kw": "kn", "sty": "st", "orch": "or"}
SKIP_DIRS = {"node_modules", ".git", ".trash", ".Trash"}
SEARCH_EXT = {".md", ".json", ".yaml", ".yml", ".py", ".sh", ".txt", ".toml"}


def skills_dir(arg):
    p = Path(arg).expanduser()
    for cand in (p, p / "skills", p / ".claude" / "skills"):
        if cand.is_dir() and any((c / "SKILL.md").is_file() for c in cand.iterdir() if c.is_dir()):
            return cand
    return None


def frontmatter_name(skill_md):
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return None
    nm = re.search(r"^name:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", m.group(1), re.M)
    return nm.group(1).strip() if nm else None


def git(cwd, *args):
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def read_ownership():
    """Return (my identities, mine paths, external paths) from ownership.md."""
    me, mine, ext = set(), set(), set()
    md = HERE / "ownership.md"
    section = None
    if md.is_file():
        for line in md.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                section = line[3:].strip().lower()
            elif line.startswith("- ") and section:
                val = line[2:].strip().strip("`")
                if section == "me":
                    me.add(val.lower())
                elif section in ("mine", "external"):
                    p = Path(val).expanduser()
                    if not any(ch in val for ch in "*?["):
                        p = p.resolve()
                    (mine if section == "mine" else ext).add(p)
    for key in ("user.name", "user.email"):
        v = git(HOME, "config", "--global", key)
        if v:
            me.add(v.lower())
    return me, mine, ext


def norm(s):
    return re.sub(r"[\s._-]+", "", s.lower())


def keys(s):
    """Forms of a name or email to compare: whole, and the email's local part without a noreply id."""
    s = s.strip().lower()
    out = {norm(s)}
    if "@" in s:
        local = s.split("@", 1)[0]
        out |= {norm(local), norm(local.split("+", 1)[-1])}
    return out - {""}


def is_me(name, email, me):
    mine = set().union(*(keys(m) for m in me)) if me else set()
    return bool((keys(name) | keys(email)) & mine)


def lock_entries(skills_folder):
    """Skill names recorded by an installer lock file near this skills folder."""
    found = {}
    project = skills_folder.parent.parent
    for lock in (HOME / ".agents" / ".skill-lock.json", project / "skills-lock.json"):
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for name, info in (data.get("skills") or {}).items():
            found.setdefault(name, (lock, info))
    return found


def listed(real, entries):
    return any(fnmatch.fnmatch(str(real), str(e)) for e in entries)


def owner(d, me, mine, ext, locks):
    real = d.resolve()
    if listed(real, ext):
        return "ext:listed"
    if listed(real, mine):
        return "mine"
    if str(real).startswith(str(HOME / ".claude" / "plugins")):
        return "ext:plugin"
    if str(real).startswith(str(HOME / ".claude" / "skills" / "synced")):
        return "ext:claude.ai"
    if d.name in locks:
        lock, info = locks[d.name]
        src = str(info.get("source", ""))
        if info.get("sourceType") != "local" or not src.startswith(str(HOME)):
            return "ext:installed"
    repo = git(real, "rev-parse", "--show-toplevel")
    if repo:
        target = real / "SKILL.md"
        # A staged, uncommitted rename (git mv) has no history at the new path yet:
        # judge it by the path it was moved from.
        rel = os.path.relpath(target, repo)
        for line in git(repo, "status", "--porcelain").splitlines():
            if line[:1] == "R" and " -> " in line:
                frm, to = (s.strip('"') for s in line[3:].split(" -> ", 1))
                if to == rel:
                    target = Path(repo) / frm
        # --follow finds the commit that first added the file under its old name.
        added = git(repo, "log", "--follow", "--diff-filter=A", "--format=%h\t%an\t%ae\t%s", "--", str(target))
        if added:
            commit, name, email, subject = added.splitlines()[-1].split("\t", 3)
            local = {git(repo, "config", k) for k in ("user.name", "user.email")} - {""}
            if not is_me(name, email, me | local):
                return f"ext:{name}"
            # Committed by the user, but maybe copied in from elsewhere.
            if re.search(r"https?://|github\.com|gitlab\.com", subject):
                return "ext:imported"
            files = git(repo, "show", "--diff-filter=A", "--name-only", "--format=", commit)
            if sum(1 for f in files.splitlines() if f.endswith("/SKILL.md")) >= BULK_IMPORT:
                return f"unknown:bulk-{commit}"
            return "mine"
    text = (real / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    au = re.search(r"^\s*author:\s*['\"]?([^'\"\n]+)", text, re.M)
    if au and not is_me(au.group(1), "", me):
        return f"ext:{au.group(1).strip()}"
    return "unknown"


def project_codes():
    md = HERE / "projects.md"
    if not md.is_file():
        return set()
    return set(re.findall(r"^\|\s*`([a-z0-9]+)`\s*\|", md.read_text(encoding="utf-8"), re.M))


def check(name, projects):
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name) or len(name) > 64:
        return "bad-chars"
    parts = name.split("-")
    if len(parts) >= 3 and parts[1] in TYPES + tuple(LEGACY):
        if parts[0] not in projects:
            return f"unknown-proj-{parts[0]}"
        parts = parts[1:]
    if len(parts) < 2:
        return "no-type"
    if parts[0] in LEGACY:
        return f"legacy-{parts[0]}->{LEGACY[parts[0]]}"
    if parts[0] in TYPES:
        return "ok"
    return "no-type"


def search_roots(sdirs):
    home = Path.home() / ".claude"
    roots = {home / "skills", home / "commands", home / "agents",
             home / "CLAUDE.md", home / "settings.json", home / "settings.local.json"}
    for s in sdirs:
        claude = s.parent
        project = claude.parent
        roots |= {claude, project / "CLAUDE.md", project / ".vscode" / "workflows"}
    return [r for r in roots if r.exists()]


def files_under(roots):
    for r in roots:
        if r.is_file():
            yield r
            continue
        for dp, dns, fns in os.walk(r):
            dns[:] = [d for d in dns if d not in SKIP_DIRS]
            for f in fns:
                if Path(f).suffix in SEARCH_EXT:
                    yield Path(dp) / f


def main(argv):
    args = argv or ["~/.claude/skills", "./.claude/skills"]
    sdirs = []
    for a in args:
        s = skills_dir(a)
        if s and s.resolve() not in [x.resolve() for x in sdirs]:
            sdirs.append(s)
    if not sdirs:
        print("no skills folders found in:", " ".join(args))
        return 1

    skills = []
    for s in sdirs:
        for d in sorted(s.iterdir()):
            md = d / "SKILL.md"
            if d.name in SKIP_DIRS or not md.is_file():
                if d.is_dir() and d.name not in SKIP_DIRS:
                    print(f"skip (no SKILL.md): {d}")
                continue
            skills.append((s, d, frontmatter_name(md) or "?"))

    codes = project_codes()
    me, mine, ext = read_ownership()
    corpus = {}
    for f in files_under(search_roots(sdirs)):
        try:
            corpus[f] = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass

    print(f"\n{'owner':<22} {'status':<16} {'folder':<34} {'name:':<34} refs  path")
    locks_by_dir = {}
    for s, d, name in skills:
        if s not in locks_by_dir:
            locks_by_dir[s] = lock_entries(s)
        who = owner(d, me, mine, ext, locks_by_dir[s])
        status = check(d.name, codes)
        if name != d.name:
            status += " name!=dir"
        pat = re.compile(r"(?<![\w-])" + re.escape(d.name) + r"(?![\w-])")
        own = d.resolve()
        refs = sum(1 for f, t in corpus.items()
                   if own not in f.resolve().parents and pat.search(t))
        link = " -> " + os.readlink(d) if d.is_symlink() else ""
        print(f"{who[:22]:<22} {status:<16} {d.name:<34} {name:<34} {refs:<5} {d}{link}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
