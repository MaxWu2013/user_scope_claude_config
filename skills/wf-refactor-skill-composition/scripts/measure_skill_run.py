#!/usr/bin/env python3
"""Measure one Claude Code session: context per request, skill loads, delegations, compactions.

Read-only. Usage: measure_skill_run.py <transcript.jsonl | session id> [--stage-regex REGEX]
                  [--from-request N] [--to-request M]
--from-request/--to-request limit the report to requests N..M-1 (0-based, as in
`at_request`), so one run inside a longer session can be measured on its own.

Every number is labelled by how it was obtained:
  observed  - copied from the transcript (API usage, compaction records, timestamps)
  estimated - derived (skill text tokens = characters / 4)
  heuristic - stage boundaries matched by --stage-regex in assistant text
Context of a request = input_tokens + cache_read_input_tokens + cache_creation_input_tokens,
as reported by the API for that request. Only Claude models report it reliably; requests
from other backends are counted but flagged.
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
CMD = re.compile(r"<command-name>/?([^<]+)</command-name>")
# cursor-agent in command position: line start or after ; & | (not inside an argument)
CURSOR_AGENT = re.compile(r"(^|[;&|]\s*|\n\s*)cursor-agent\s")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(\n|$)", re.S)
TASK_ID = re.compile(r"<tool-use-id>\s*([^<\s]+)\s*</tool-use-id>")


def resolve(arg):
    p = Path(arg).expanduser()
    if p.is_file():
        return p
    hits = list(PROJECTS.glob(f"*/{arg}.jsonl"))
    return hits[0] if len(hits) == 1 else None


def rows_of(path):
    out = []
    for line in path.open(encoding="utf-8", errors="replace"):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def ctx_of(usage):
    return sum(usage.get(k, 0) or 0 for k in
               ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def text_of(content):
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if isinstance(c, dict):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif c.get("type") == "tool_result":
                parts.append(text_of(c.get("content")))
    return "\n".join(parts)


def own_text(content):
    """The user's own words: text parts only, never tool results."""
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text")


def runs_cursor_agent(cmd):
    """True when the command runs cursor-agent, not when it only greps or quotes the word."""
    return bool(CURSOR_AGENT.search(HEREDOC.sub("\n", cmd)))


def ts(r):
    try:
        return datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
    except (KeyError, ValueError, AttributeError):
        return None


def measure(rows, stage_regex=None, from_request=None, to_request=None):
    requests, seen = [], {}
    background_ids = set()
    skills, delegations, compactions, errors = [], [], [], 0
    pending_skill = {}          # tool_use id -> skill entry waiting for its loaded text
    stage_re = re.compile(stage_regex) if stage_regex else None
    stages = {}
    for r in rows:
        m = r.get("message")
        if r.get("type") == "system" and r.get("subtype") == "compact_boundary":
            md = r.get("compactMetadata") or {}
            compactions.append({"at_request": len(requests), "trigger": md.get("trigger"),
                                "preTokens": md.get("preTokens"), "postTokens": md.get("postTokens")})
            continue
        # A notification that arrives while the session is busy is queued: it is stored in
        # queue-operation / attachment rows, not in a user row.
        if r.get("type") in ("queue-operation", "attachment"):
            raw = json.dumps(r, ensure_ascii=False)
            if "<task-notification>" in raw or "task-notification" in raw:
                background_ids.update(TASK_ID.findall(raw.replace("\\n", "\n")))
            continue
        # Only real conversation rows; api-request-blob rows replay earlier messages.
        if not isinstance(m, dict) or r.get("type") not in ("user", "assistant"):
            continue
        if m.get("role") == "assistant":
            mid = m.get("id") or id(r)
            if mid not in seen:            # rows of one message repeat its usage; count it once
                pending_skill.clear()      # a skill's text arrives before the next request
                seen[mid] = len(requests)
                u = m.get("usage") or {}
                requests.append({"model": m.get("model"), "ctx": ctx_of(u),
                                 "output": u.get("output_tokens", 0) or 0, "ts": ts(r)})
            idx = seen[mid]
            for c in m.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and stage_re:
                    for s in stage_re.findall(c.get("text", "")):
                        s = s if isinstance(s, str) else "".join(s)
                        stages.setdefault(s, idx)
                if c.get("type") != "tool_use":
                    continue
                name, inp = c.get("name"), c.get("input") or {}
                if name == "Skill":
                    e = {"skill": inp.get("skill"), "via": "Skill tool", "at_request": idx, "chars": 0}
                    skills.append(e)
                    pending_skill[c.get("id")] = e
                    if str(inp.get("skill", "")).startswith("runner:"):
                        delegations.append({"kind": "runner", "at_request": idx,
                                            "detail": str(inp.get("args", ""))[:80]})
                elif name == "Bash" and runs_cursor_agent(str(inp.get("command", ""))):
                    delegations.append({"kind": "cursor-agent CLI", "at_request": idx, "id": c.get("id"),
                                        "background": inp["run_in_background"] if "run_in_background" in inp
                                        else "not set",
                                        "detail": str(inp.get("command"))[:80]})
                elif name in ("Agent", "Task"):
                    delegations.append({"kind": f"agent:{inp.get('subagent_type') or 'default'}",
                                        "at_request": idx, "id": c.get("id"),
                                        "background": inp["run_in_background"] if "run_in_background" in inp
                                        else "not set",
                                        "detail": str(inp.get("description", ""))[:80]})
        elif m.get("role") == "user":
            txt = text_of(m.get("content"))
            if "<task-notification>" in txt:
                background_ids.update(TASK_ID.findall(txt))
            for c in m.get("content") or [] if isinstance(m.get("content"), list) else []:
                if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error"):
                    errors += 1
            src = r.get("sourceToolUseID")
            if r.get("isMeta") and src in pending_skill:
                pending_skill[src]["chars"] += len(txt)
            elif not r.get("isMeta"):
                cm = CMD.search(own_text(m.get("content")))
                if cm:
                    skills.append({"skill": cm.group(1).strip(), "via": "slash command",
                                   "at_request": len(requests), "chars": len(txt)})
            elif r.get("isMeta") and txt.startswith("Base directory for this skill") and skills \
                    and skills[-1]["via"] == "slash command" and skills[-1]["at_request"] == len(requests):
                skills[-1]["chars"] += len(txt)

    for d in delegations:
        # a <task-notification> for the call proves it ran in the background
        if d.get("id") in background_ids:
            d["background"] = True

    def ctx_at(i):
        return requests[i]["ctx"] if 0 <= i < len(requests) else None

    for e in skills:
        i = e["at_request"]
        before = ctx_at(i if e["via"] == "Skill tool" else i - 1)
        after = ctx_at(i + 1 if e["via"] == "Skill tool" else i)
        e["est_tokens"] = round(e.pop("chars") / 4)
        e["observed_ctx_delta"] = (after - before) if before is not None and after is not None else None

    lo = from_request or 0
    hi = to_request if to_request is not None else len(requests)
    if lo or hi != len(requests):
        inside = lambda e: lo <= e["at_request"] < hi  # noqa: E731
        skills = [e for e in skills if inside(e)]
        delegations = [d for d in delegations if inside(d)]
        compactions = [c for c in compactions if inside(c)]
        stages = {k: v for k, v in stages.items() if lo <= v < hi}
        requests = requests[lo:hi]
        stages = {k: v - lo for k, v in stages.items()}
        for e in skills + delegations + compactions:
            e["at_request"] -= lo
    claude = [q for q in requests if str(q["model"] or "").startswith("claude")]
    other = sorted({q["model"] for q in requests if q not in claude and q["model"]})
    t = [q["ts"] for q in requests if q["ts"]]
    stage_list = sorted(stages.items(), key=lambda kv: kv[1])
    stage_out = []
    for n, (name, start) in enumerate(stage_list):
        end = stage_list[n + 1][1] if n + 1 < len(stage_list) else len(requests)
        seg = requests[start:end]
        stage_out.append({"stage": name, "requests": len(seg),
                          "ctx_start": seg[0]["ctx"] if seg else None,
                          "ctx_peak": max((q["ctx"] for q in seg), default=None)})
    return {
        "observed": {
            "requests": len(requests),
            "assistant_rows": sum(1 for r in rows if r.get("type") == "assistant"),
            "peak_ctx": max((q["ctx"] for q in claude), default=0),
            "final_ctx": claude[-1]["ctx"] if claude else 0,
            "output_tokens": sum(q["output"] for q in requests),
            "compactions": compactions,
            "delegations": delegations,
            "tool_errors": errors,
            "duration_min": round((max(t) - min(t)).total_seconds() / 60, 1) if len(t) > 1 else 0,
            "non_claude_models": other,
        },
        "skills": skills,
        "stages_heuristic": stage_out,
    }


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("transcript")
    ap.add_argument("--stage-regex", help=r'e.g. "Gate (\d)" or "STAGE (\d) START"')
    ap.add_argument("--from-request", type=int)
    ap.add_argument("--to-request", type=int)
    a = ap.parse_args(argv)
    path = resolve(a.transcript)
    if not path:
        print(f"transcript not found (or ambiguous): {a.transcript}", file=sys.stderr)
        return 1
    out = {"transcript": str(path), **measure(rows_of(path), a.stage_regex, a.from_request, a.to_request)}
    if a.from_request is not None or a.to_request is not None:
        out["range"] = {"from_request": a.from_request or 0, "to_request": a.to_request,
                        "note": "at_request values are relative to from_request"}
    subdir = path.with_suffix("") / "subagents"
    out["subagents"] = []
    for f in sorted(subdir.glob("*.jsonl")) if subdir.is_dir() else []:
        s = measure(rows_of(f))["observed"]
        out["subagents"].append({"file": f.name, "requests": s["requests"],
                                 "peak_ctx": s["peak_ctx"], "output_tokens": s["output_tokens"]})
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
