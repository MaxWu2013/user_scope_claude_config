#!/usr/bin/env python3
"""Map how one skill uses other skills and sub-agents.

Read-only. Usage: map_skill_deps.py <skill name | skill folder | SKILL.md> [extra dir ...]
                  map_skill_deps.py --consumers <skill name>
Known skills come from ~/.claude/skills, ./.claude/skills and any extra dirs
(a skills folder, a .claude folder or a project root). Agents come from
~/.claude/agents, ./.claude/agents and <extra>/.claude/agents.

Prints JSON: the target's frontmatter and files, every skill and agent its text
mentions, each dependency's frontmatter, and who else mentions each dependency
(so single-use helpers stand out); skills that mention the target although the
target never mentions them (`mentioned_by_only`: candidate missing helpers); and
every `mcp__<server>__<tool>` it names with whether that server is configured;
per helper, `effects` (write hints, delegation mentions, file-changing commands);
and `commands`: shell commands in A's own text, `mutating`, `read-only` or
`unclassified` (a shell-block line or inline command it can't judge: read it).
Line numbers in `signals` and `commands` are SKILL.md line numbers.
--consumers lists every file that names the skill: ~/.claude (skills, agents,
commands, hooks, settings), each project's .claude, .vscode/workflows and CLAUDE.md.
The project A lives in is always searched, whatever the current directory.
It finds mentions and hints; it does not judge them.
"""
import json
import os
import re
import sys
from pathlib import Path

HOME = Path.home()
SKIP_DIRS = {"node_modules", ".git", ".trash", ".Trash", "synced", "__pycache__"}
BUILTIN_AGENTS = ("Explore", "Plan", "general-purpose")
FLAGS = ("context", "agent", "background", "disable-model-invocation",
         "user-invocable", "allowed-tools", "tools", "skills", "isolation", "model")
SIGNALS = {
    "spawns sub-agent": r"\b(sub-?agents?|Agent tool|spawn)\b",
    "fork": r"\b(context:\s*fork|fork(ed)?)\b|/subtask",
    "background/wait": r"\b(background|foreground|wait(s|ing)? for)\b",
    "parallel": r"\bparallel\b",
    "worktree": r"\bworktree\b",
    "asks user": r"AskUserQuestion|\b(approv\w*|confirm\w*|ask the user)\b",
    "runs script": r"scripts/[\w./-]+|\b(python3?|bash|node)\s+\S+\.(py|sh|js)",
}


def parse_frontmatter(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if not m:
        return {}, text
    fm, key = {}, None
    for line in m.group(1).splitlines():
        item = re.match(r"\s+-\s*(.+)", line)
        if item and key:
            fm[key] = (fm[key] if isinstance(fm[key], list) else []) + [item.group(1).strip()]
            continue
        kv = re.match(r"([\w-]+):\s*(.*)", line)
        if kv:
            key, val = kv.group(1), kv.group(2).strip().strip("'\"")
            if val.startswith("[") and val.endswith("]"):
                val = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
            fm[key] = val if val != "" else []
    return fm, text[m.end():]


def dirs_from(arg, sub):
    p = Path(arg).expanduser()
    for cand in (p, p / sub, p / ".claude" / sub):
        if cand.is_dir() and cand.name == sub:
            return cand
    return None


def collect(extra):
    skills, agents = {}, {}
    roots = [HOME / ".claude", Path.cwd() / ".claude", *[Path(e).expanduser() for e in extra]]
    for r in roots:
        sd = dirs_from(r, "skills")
        if sd:
            for d in sorted(sd.iterdir()):
                if d.is_dir() and d.name not in SKIP_DIRS and (d / "SKILL.md").is_file():
                    fm, _ = parse_frontmatter(d / "SKILL.md")
                    skills.setdefault(fm.get("name") or d.name, d.resolve())
        ad = dirs_from(r, "agents")
        if ad:
            for f in sorted(ad.glob("*.md")):
                fm, _ = parse_frontmatter(f)
                agents.setdefault(fm.get("name") or f.stem, f.resolve())
    return skills, agents


def resolve_target(arg, skills):
    p = Path(arg).expanduser()
    if p.is_file() and p.name == "SKILL.md":
        return p.parent.resolve()
    if (p / "SKILL.md").is_file():
        return p.resolve()
    return skills.get(arg.lstrip("/"))


def mentions(text, name):
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text) is not None


def skill_text(folder):
    """SKILL.md plus text files the skill ships (references, agents, templates)."""
    parts = []
    for f in sorted(folder.rglob("*")):
        if f.is_file() and f.suffix in {".md", ".yaml", ".yml", ".json", ".txt"} \
                and not any(s in f.parts for s in SKIP_DIRS):
            parts.append(f.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def users_of(name, skills, agents, exclude):
    found = []
    for other, folder in skills.items():
        if folder != exclude and mentions(skill_text(folder), name):
            found.append(f"skill:{other}")
    for other, f in agents.items():
        if mentions(f.read_text(encoding="utf-8", errors="replace"), name):
            found.append(f"agent:{other}")
    for md in (HOME / ".claude" / "CLAUDE.md", Path.cwd() / "CLAUDE.md", Path.cwd() / ".claude" / "CLAUDE.md"):
        if md.is_file() and mentions(md.read_text(encoding="utf-8", errors="replace"), name):
            found.append(f"file:{md}")
    return sorted(set(found))


def flags(fm):
    return {k: fm[k] for k in FLAGS if k in fm}


MCP_TOOL = re.compile(r"mcp__([A-Za-z0-9_-]+?)__[A-Za-z0-9_]+")
MANAGED_MCP = ("claude_ai_", "plugin_")   # connectors and plugin servers: not listed in ~/.claude.json


def norm(server):
    return re.sub(r"[^A-Za-z0-9]", "_", server).lower()


def configured_mcp(project):
    """Normalized names of MCP servers configured for the user and for `project`."""
    names = set()
    try:
        d = json.loads((HOME / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    names |= set(d.get("mcpServers") or {})
    if project:
        names |= set(((d.get("projects") or {}).get(str(project)) or {}).get("mcpServers") or {})
        try:
            names |= set(json.loads((project / ".mcp.json").read_text(encoding="utf-8")).get("mcpServers") or {})
        except (OSError, ValueError):
            pass
    return {norm(n) for n in names}


def mcp_tools(text, project):
    """Every mcp__<server>__<tool> named in text, with whether its server is configured."""
    have = configured_mcp(project)
    out = {}
    for m in MCP_TOOL.finditer(text):
        server = m.group(1)
        status = ("managed (not checkable here)" if server.startswith(MANAGED_MCP)
                  else "configured" if norm(server) in have else "NOT CONFIGURED")
        out[m.group(0)] = status
    return out


# Heuristics for what a helper does and what A runs. They point at lines to read; they
# are not verdicts.
WRITE_HINT = re.compile(
    r"\b(append|appends|write|writes|register|registers|save|saves|create|creates|edit|edits|"
    r"modify|modifies|delete|deletes|rename|renames|update|updates|add|adds)\b[^\n]{0,80}?"
    r"(\.(dart|json|ya?ml|arb|gradle|kts?|swift|xml|plist|proto|png|svg|webp|jpe?g)\b|pubspec|k\.dart|\bfiles?\b)",
    re.I)
DELEGATION = re.compile(r"cursor-agent|runner:sub-agents|\bdelegat\w*|sub-?agent", re.I)
MUTATING = [
    (r"\bdart\s+format\b(?![^\n]*(--output[= ]none|--set-exit-if-changed))", "dart format rewrites files"),
    (r"\bbuild_runner\s+(build|watch)\b", "build_runner writes generated files"),
    (r"\b(flutter|dart)\s+pub\s+(get|add|upgrade|remove)\b", "pub changes lock/config"),
    (r"\bdart\s+fix\s+--apply\b", "dart fix --apply rewrites files"),
    (r"\bgit\s+(add|commit|push|checkout|reset|rebase|merge|stash|mv|rm|restore|switch)\b", "git changes the tree or history"),
    (r"(^|[\s;&|(])(rm|mv|cp|mkdir|touch|tee|truncate|rsync)\s", "file-system write"),
    (r"\bsed\s+[^\n]*-i\b", "in-place edit"),
    (r"\b(npm|yarn|pnpm)\s+(install|i|add)\b", "package install"),
    (r"\bprotoc\b[^\n]*--\w+_out", "protoc writes generated code"),
    (r"kdart_cli", "regenerates k.dart"),
    # a real redirect has a space (or line start) before '>' and isn't to /dev/null;
    # this skips placeholders such as <module>/lib and arrows such as ->
    (r"(^|\s)>{1,2}\s*(?!/dev/null)[\w./~$\"']", "shell redirect writes a file"),
]
READ_ONLY = re.compile(r"\b(dart\s+format\b[^\n]*(--output[= ]none|--set-exit-if-changed)|flutter\s+(analyze|test)|dart\s+analyze|git\s+(status|diff|log|show)|grep|cat|ls|head|tail|wc|find)\b")
NEGATED = re.compile(r"\b(do not|don't|never|DO NOT)\b", re.I)
FENCE = re.compile(r"^(`{3,}|~{3,})\s*([\w+-]*)")
SHELL_LANGS = {"", "bash", "sh", "zsh", "shell", "console"}
EXECUTABLE = re.compile(r"^(python3?|npx|node|bash|sh|zsh|perl|ruby|uv|pip3?|deno|bun|go|cargo|mktemp|"
                        r"claude|gh|cursor-agent|codex|osascript|launchctl|crontab|curl|jq|env)\b")


def helper_effects(text):
    """What a helper's text says it changes, and whether it hands edits to a delegate."""
    writes = [m.group(0).strip()[:100] for m in WRITE_HINT.finditer(text)]
    return {"write_hints": writes[:5], "write_hint_count": len(writes),
            "mentions_delegation": bool(DELEGATION.search(text)),
            # only shell code blocks and inline code count, as for A (prose and quotes don't)
            "mutating_commands": sorted({w for c in commands_in(text) if not c["negated"] for w in c["why"]})}


def commands_in(body, offset=0):
    """Shell commands in A's own SKILL.md (shell code blocks and inline code), classified."""
    out, fence, shell = [], None, False
    for n, line in enumerate(body.splitlines(), 1 + offset):
        s = line.strip()
        m = FENCE.match(s)
        if m and fence is None:
            fence, shell = m.group(1)[0] * 3, m.group(2).lower() in SHELL_LANGS
            continue
        if fence and s.startswith(fence):
            fence = None
            continue
        if fence:
            candidates = [s] if shell and s and not s.startswith("#") else []
        else:
            candidates = re.findall(r"`([^`\n]{4,})`", line)
        for c in candidates:
            kinds = [why for pat, why in MUTATING if re.search(pat, c)]
            kind = "mutating" if kinds else "read-only" if READ_ONLY.search(c) else \
                "unclassified" if fence or EXECUTABLE.match(c) else None
            if kind:
                out.append({"line": n, "command": c[:120], "kind": kind, "why": kinds,
                            "negated": bool(NEGATED.search(line))})
    return out


def project_of(folder):
    """<project> for <project>/.claude/skills/<skill>, else None."""
    parts = folder.parts
    if len(parts) >= 3 and parts[-2] == "skills" and parts[-3] == ".claude" and folder.parent.parent.parent != HOME:
        return folder.parent.parent.parent
    return None


CONSUMER_EXT = {".md", ".json", ".yaml", ".yml", ".py", ".sh", ".zsh", ".js", ".ts", ".txt", ".toml"}


def consumer_roots():
    hc = HOME / ".claude"
    roots = [hc / s for s in ("skills", "agents", "commands", "hooks")]
    roots += [hc / "CLAUDE.md", hc / "settings.json", hc / "settings.local.json"]
    projects = {Path.cwd()}
    work = HOME / "workplace"
    for dp, dns, _ in os.walk(work):
        depth = len(Path(dp).relative_to(work).parts)
        if ".claude" in dns:
            projects.add(Path(dp))
        dns[:] = [d for d in dns if depth < 4 and d not in SKIP_DIRS and not d.startswith(".")]
    for p in sorted(projects):
        roots += [p / ".claude", p / ".vscode" / "workflows", p / "CLAUDE.md"]
    return [r for r in roots if r.exists()]


def consumers(name):
    """Every file under the consumer roots that names `name`, with the matching lines."""
    found, seen = [], set()
    pat = re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])")
    for root in consumer_roots():
        files = [root] if root.is_file() else (
            f for f in root.rglob("*") if f.is_file() and not any(s in f.parts for s in SKIP_DIRS))
        for f in files:
            rf = f.resolve()
            if rf in seen or (f.suffix not in CONSUMER_EXT and f.suffix):
                continue
            seen.add(rf)
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = [n for n, ln in enumerate(text.splitlines(), 1) if pat.search(ln)]
            if lines:
                found.append({"file": str(rf), "lines": lines[:20]})
    return found


def main(argv):
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    if argv[0] == "--consumers":
        if len(argv) != 2:
            print(__doc__, file=sys.stderr)
            return 2
        print(json.dumps({"name": argv[1], "consumers": consumers(argv[1].lstrip("/"))},
                         indent=2, ensure_ascii=False))
        return 0
    extra = list(argv[1:])
    first = Path(argv[0]).expanduser()
    folder = first.parent if first.name == "SKILL.md" else first
    if (folder / "SKILL.md").is_file():
        # Always search the project A lives in, whatever the current directory is.
        proj = project_of(folder.resolve())
        if proj:
            extra.insert(0, str(proj))
    skills, agents = collect(extra)
    target = resolve_target(argv[0], skills)
    if not target:
        print(f"skill not found: {argv[0]}", file=sys.stderr)
        return 1
    fm, body = parse_frontmatter(target / "SKILL.md")
    full = (target / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    tname = fm.get("name") or target.name
    text = skill_text(target)
    lines = body.splitlines()
    offset = full.count("\n") - body.count("\n")

    out = {
        "target": {
            "name": tname, "path": str(target), "lines": len(lines),
            "frontmatter": flags(fm),
            "files": sorted(str(f.relative_to(target)) for f in target.rglob("*")
                            if f.is_file() and not any(s in f.parts for s in SKIP_DIRS)),
            "signals": {k: [i + 1 + offset for i, l in enumerate(lines) if re.search(v, l, re.I)]
                        for k, v in SIGNALS.items()},
        },
        "skills_used": [],
        "agents_used": [],
        # Possible missing helpers: they name A, A never names them (read each one).
        "mentioned_by_only": [],
        "mcp_tools": mcp_tools(text, project_of(target)),
        "commands": commands_in(body, offset),
        "searched": sorted({str(p.parent) for p in skills.values()}),
    }
    for name, folder in skills.items():
        if folder == target or name == tname:
            continue
        if not mentions(text, name):
            if mentions(skill_text(folder), tname):
                out["mentioned_by_only"].append({"name": name, "path": str(folder)})
            continue
        dfm, dbody = parse_frontmatter(folder / "SKILL.md")
        users = users_of(name, skills, agents, folder)
        out["skills_used"].append({
            "name": name, "path": str(folder), "lines": len(dbody.splitlines()),
            "frontmatter": flags(dfm),
            "has_scripts": (folder / "scripts").is_dir(),
            "effects": helper_effects((folder / "SKILL.md").read_text(encoding="utf-8", errors="replace")),
            "used_by": users,
            "single_use": users == [f"skill:{tname}"],
        })
    for name, f in agents.items():
        if not mentions(text, name):
            continue
        afm, _ = parse_frontmatter(f)
        pre = afm.get("skills") or []
        pre = pre if isinstance(pre, list) else [pre]
        out["agents_used"].append({
            "name": name, "path": str(f), "frontmatter": flags(afm),
            "preloads_unknown": [s for s in pre if s not in skills],
            "preloads_blocked": [s for s in pre if s in skills and str(parse_frontmatter(
                skills[s] / "SKILL.md")[0].get("disable-model-invocation", "")).lower() == "true"],
        })
    for name in BUILTIN_AGENTS:
        if re.search(rf"[`*]?\b{re.escape(name)}\b[`*]?(\s+(agent|sub-?agent))", text):
            out["agents_used"].append({"name": name, "builtin": True})
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
