#!/usr/bin/env python3
"""SessionStart hook: tell a Claude orchestrator the routing rules up front.

Not registered. Fail open: bad input or any error exits 0 with no output.
Emits additionalContext only when every guard holds:
- stdin is a JSON object;
- hook_event_name is exactly "SessionStart" (any source: startup, resume, clear, compact, fork);
- cursor_version is absent or empty/whitespace (a non-empty value is the Cursor runtime);
- agent_type is absent or empty (a named agent such as file-reader must not be instructed);
- CLAUDE_ROUTING is not "off".
Anything else, including Cursor's "sessionStart" spelling, exits 0 with no output.
Routing state is loaded only after those guards. If that fails, print nothing and exit 0.
"""

import json
import os
import sys

sys.dont_write_bytecode = True

_CODE = (
    "Code goes only through ~/.claude/hooks/bin/cursor-run new <task>, "
    "then write the spec at the printed path, "
    "then ~/.claude/hooks/bin/cursor-run <path>."
)
_STATE = "Specs, memory and plans belong to the main session."
_DOC_STRICT = "Document edits go to the file-writer subagent."
_DOC_TASK = (
    "One small edit of up to 20 lines in one file per session is allowed on the main thread, "
    "and other document work goes to file-writer."
)
_READ_STRICT = (
    "Direct Read, Grep, Glob and Bash reads on the main thread are denied; use file-reader."
)
_READ_TASK = (
    "Read is allowed for 10 files and 2000 lines per session, "
    "and larger reads, Grep, Glob, Bash reads and search go to file-reader."
)
_SAFE_READ = "The file-reader searches with safe-read rg -e A -e B (no | or parentheses)."
_ARBITRATION_TASK = (
    "Opus subagents need arbitration: if a hook replies ARBITRATION_REQUIRED or "
    "ARBITRATION_CANDIDATE, launch the opus-judge agent in the foreground with the short "
    "prompt the hook gives (ARBITRATION-PACKET request_id=…), then follow its "
    "ARBITRATION_DECISION (CONTINUE/RETRY at most once/STOP without Opus; ESCALATE: issue "
    "exactly the granted call). Never retry an Opus call directly, never launch opus-judge "
    "without a packet, and never use background or nested Opus agents."
)
_ARBITRATION_STRICT = "Opus subagents are denied in strict mode; use sonnet or haiku."
_PLANNING_TASK = (
    "Complex multi-stage work: if you are Opus, write the plan yourself and run router-plan promote; "
    "use a Workflow only for parallel fan-out, passing router-plan snapshot text in args. "
    "opus-planner is for Sonnet main sessions and needs arbitration."
)


def _blank(value):
    """True when a field was omitted, null, or only whitespace."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def _orchestrator_session(payload):
    if not isinstance(payload, dict):
        return False
    if payload.get("hook_event_name") != "SessionStart":
        return False
    if not _blank(payload.get("cursor_version")):
        return False
    if not _blank(payload.get("agent_type")):
        return False
    return True


def _context_text(mode, reads_routed):
    """One sentence per rule that is in force for this session."""
    sentences = [_CODE, _STATE]
    if mode == "task-aware":
        sentences.append(_DOC_TASK)
        sentences.append(_ARBITRATION_TASK)
        sentences.append(_PLANNING_TASK)
        if reads_routed:
            sentences.append(_READ_TASK)
            sentences.append(_SAFE_READ)
    else:
        sentences.append(_DOC_STRICT)
        sentences.append(_ARBITRATION_STRICT)
        if reads_routed:
            sentences.append(_READ_STRICT)
            sentences.append(_SAFE_READ)
    return " ".join(sentences)


def _current_context():
    import importlib.util

    root = os.path.dirname(os.path.realpath(__file__))
    if root not in sys.path:
        sys.path.insert(0, root)
    paths = importlib.import_module("_paths")
    mode, _mode_source = paths.resolve_routing_mode()
    reads, _reads_source = paths.resolve_routing_reads()
    location = importlib.util.spec_from_file_location(
        "route_tools_context", os.path.join(root, "route-tools.py")
    )
    if location is None or location.loader is None:
        raise ImportError("route tools")
    route = importlib.util.module_from_spec(location)
    location.loader.exec_module(route)
    pilot = route.session_is_pilot(paths)
    return _context_text(mode, pilot or reads == "on")


def main():
    try:
        payload = json.loads(sys.stdin.read())
        if os.environ.get("CLAUDE_ROUTING") == "off":
            return 0
        if not _orchestrator_session(payload):
            return 0
        text = _current_context()
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": text,
            }
        }, ensure_ascii=False))
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
