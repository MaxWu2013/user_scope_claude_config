#!/usr/bin/env python3
# Require cursor-agent delegation Hook
#
# Deny-by-default: blocks direct implementation (Edit/Write/MultiEdit and
# common Bash write bypasses) in every Claude Code session. When cursor-run
# is executable, coding work goes through cursor-run and a headless
# cursor-agent (-p / --print) is denied. Otherwise the fallback message still
# names a direct cursor-agent command:
#   /runner:sub-agents Run cursor-agent on <task description>
#
# User scope: lives in ~/.claude/hooks and is registered in
# ~/.claude/settings.json, so it applies to all projects.
#
# Allowed without delegation: documents (*.md / *.mdx, non-code files under
# docs/, vault notes) and specs/memory/plans.
#
# Scope: Claude Code must delegate implementation; Cursor is the implementation
# runtime. Cursor also runs Claude Code hooks (third-party hooks), so when the
# payload says Cursor is running this hook, it allows the call.
#
# Reads the hook payload from stdin. On exit 2, the reason MUST go to stderr —
# Claude Code ignores stdout JSON when exit code is 2.

import datetime
import json
import os
import re
import sys

sys.dont_write_bytecode = True
from _agent_def import read_agent_definition
from _runtime import is_cursor_runtime, CURSOR_FORMAT_CHANGED

try:
    from _paths import *
except ImportError:
    pass


WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
OBSIDIAN_WRITE_TOOLS = {
    "mcp__mcp-obsidian__obsidian_put_content",
    "mcp__mcp-obsidian__obsidian_append_content",
    "mcp__mcp-obsidian__obsidian_patch_content",
}
ASK_CLASSES = {"control", "outside", "vault_asset"}
ASK_REASON = "needs your approval (routing control plane / outside workspace / vault asset)."
SILENT_ALLOW = {"state", "vault_structured", "document", "data"}
_DATA_WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}

# Kept in sync with the phase 6c deny text. Built per call, not at import.
_CURSOR_RUN_MESSAGE = """\
Direct implementation edits are blocked in this session; coding work goes to cursor-agent through cursor-run.

Fast path:
1. Run ~/.claude/hooks/bin/cursor-run new <task> to get the spec path.
   Specs must not go in .claude/specs anymore.
2. Write the spec there (Write tool; add the line
   `verification_required: false` only for trivial tasks).
3. Run (Bash, foreground; use run_in_background for long tasks and wait for the notification):
   ~/.claude/hooks/bin/cursor-run <printed path>
The last stdout line is JSON: status, exit_code, control_changed, changed_paths,
verification_required; the log and result sit beside the spec.
Exit 0 = done (verify the diff with file-reader if verification_required).
Exit 3 = cursor failed, timed out, or the routing control plane changed; 4 = a completion
check failed; 2 = bad spec path. For 3 and 4: stop and tell the user.

Batch several small edits into one spec: each run has about 20 seconds of fixed startup.

Carve-outs: documents (*.md / *.mdx, non-code files under docs/, vault notes) and
specs/memory/plans may be written directly. Do not bypass via Bash (sed -i, tee, redirects, heredocs)."""

_HEADLESS_PREFIX = (
    "Start cursor-agent only through ~/.claude/hooks/bin/cursor-run "
    "<printed path> (run cursor-run new <task> and write the spec first)."
)

# Best-effort, not a shell parser. The token scan runs on the quote-stripped
# command; bash/sh/zsh -c and eval are scanned on the raw command because
# quote-stripping hides the payload they wrap.
_CURSOR_AGENT_TOKEN = re.compile(r"(?:^|[\s;&|/(])cursor-agent\b")
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n]")
_WRAPPER = re.compile(r"\b(?:bash|sh|zsh)\s+-c\b|\beval\b")
_PRINT_ANYWHERE = re.compile(r"(?:^|[^\w-])(?:-p|--print)(?=$|[^\w-])")
_PRINT_TOKENS = {"-p", "--print"}


def cursor_run_path():
    # CURSOR_RUN = $CURSOR_RUN_PATH or ~/.claude/hooks/bin/cursor-run.
    # CURSOR_RUN_PATH is a test seam; production leaves it unset.
    return os.environ.get("CURSOR_RUN_PATH") or os.path.join(
        os.path.expanduser("~"), ".claude", "hooks", "bin", "cursor-run"
    )


def cursor_run_ready():
    path = cursor_run_path()
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _segment_containing(text, index):
    start = 0
    for match in _SEGMENT_SPLIT.finditer(text):
        if index < match.start():
            return text[start:match.start()]
        start = match.end()
    return text[start:]


def is_headless_cursor_agent(raw, scrubbed):
    """Best-effort: does this command start a headless cursor-agent run?

    On scrub_command(remove_quoted_spans(cmd)): a cursor-agent token whose
    same segment (up to ';', '&&', '||', '|', or newline) contains -p or
    --print as its own token. On the raw command: bash/sh/zsh -c or eval
    appearing together with cursor-agent and -p or --print anywhere.
    """
    if isinstance(scrubbed, str):
        for match in _CURSOR_AGENT_TOKEN.finditer(scrubbed):
            word_at = match.end() - len("cursor-agent")
            segment = _segment_containing(scrubbed, word_at)
            if any(token in _PRINT_TOKENS for token in segment.split()):
                return True
    if not isinstance(raw, str):
        return False
    if _WRAPPER.search(raw) and re.search(r"\bcursor-agent\b", raw) and _PRINT_ANYWHERE.search(raw):
        return True
    return False


def build_delegate_message():
    if cursor_run_ready():
        return _CURSOR_RUN_MESSAGE

    try:
        model, permission = read_agent_definition()
    except Exception:
        model = None
        permission = None
        definition_read = False
    else:
        definition_read = True

    command = ["cursor-agent", "-p"]
    if permission == "yolo":
        command.append("--force")
    if model:
        command.extend(["--model", model])
    command.extend(['--workspace "$CLAUDE_PROJECT_DIR"', '"<task description>"'])

    message = [
        "Direct implementation edits are blocked in this session; coding work goes to cursor-agent.\n\n",
        "Fast path: run this literal command:\n",
        f'{" ".join(command)}\n\n',
    ]
    if definition_read:
        message.append("The model and permission values above were read from the agent definition.\n\n")
        if permission != "yolo":
            permission_level = permission or "unspecified"
            message.append(
                f"The permission level is {permission_level}; check `.agents/cursor-agent.md` "
                "for the matching flag.\n\n"
            )
    else:
        message.append(
            "The agent definition could not be read; consult `.agents/cursor-agent.md` "
            "for the matching model and permission settings.\n\n"
        )
    message.extend([
        "Alternative: /runner:sub-agents Run cursor-agent on <task description>; "
        "this adds an agent-discovery step.\n\n",
        "Each delegation costs roughly 20 seconds of fixed startup regardless of task size, "
        "so batch several small edits into a single call.\n\n",
        "For a large task, write the spec to a .md file first and point cursor-agent at that file.\n\n",
        "Carve-outs: documents (*.md / *.mdx, non-code files under docs/, vault notes) "
        "and specs/memory/plans may be written directly. "
        "Do not bypass via Bash (sed -i, tee, redirects, heredocs).",
    ])
    return "".join(message)


def deny():
    # Per call, not at import, so the CURSOR_RUN_PATH seam is visible.
    delegate = build_delegate_message()
    print(delegate, file=sys.stderr)
    return 2


def deny_headless():
    print(_HEADLESS_PREFIX, file=sys.stderr)
    print(build_delegate_message(), file=sys.stderr)
    return 2


def append_routing_log(tool, path, session_id, project):
    """Append one skill_config record. Never raises."""
    try:
        log_path = os.environ.get("ROUTING_LOG") or os.path.join(
            os.path.expanduser("~"), ".claude", "logs", "routing.jsonl"
        )
        directory = os.path.dirname(log_path) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        record = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "class": "skill_config",
            "tool": tool,
            "path": path,
            "session_id": session_id if isinstance(session_id, str) else None,
            "project": project,
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        fd = os.open(log_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(log_path, 0o600)
    except Exception:
        return


def ask(cls, path):
    reason = f"{cls}: {path} {ASK_REASON}"
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }))
    return 0


def _exact_router_plan(cmd):
    """The same plain hooks-path form route-tools allows for the main session.

    A line break is rejected before the match. Horizontal space only.
    """
    if not isinstance(cmd, str) or cmd == "" or "\n" in cmd or "\r" in cmd:
        return False
    home = re.escape(os.path.expanduser("~"))
    pattern = (
        r"^[ \t]*(?:~|" + home + r")/\.claude/hooks/bin/router-plan[ \t]+"
        r"[^\"$`\\;&|<>()]+$"
    )
    return re.match(pattern, cmd) is not None


def _router_plan_mentioned(cmd):
    if not isinstance(cmd, str) or cmd == "":
        return False
    home = re.escape(os.path.expanduser("~"))
    pattern = (
        r"(?:~|" + home + r")/\.claude/hooks/bin/router-plan(?![A-Za-z0-9._-])"
    )
    return re.search(pattern, cmd) is not None


def decide(hook_input):
    try:
        d = json.loads(hook_input or "")
    except Exception:
        d = None

    # Cursor is the implementation runtime; answer in its documented format.
    # This stays before importing _paths so a missing module still allows Cursor.
    if is_cursor_runtime(d):
        print(json.dumps({"permission": "allow"}))
        return 0

    try:
        from _paths import (
            classify_write_target,
            classify_path,
            WRITE_HINT,
            GENERATOR_WRITE,
            remove_quoted_spans,
            scrub_command,
            strip_heredoc_bodies,
            extract_paths_from_command,
            resolve_target,
            project_dir,
        )
    except ImportError:
        return deny()

    if not isinstance(d, dict):
        return deny()

    if d.get("hook_event_name") == "preToolUse":
        print(CURSOR_FORMAT_CHANGED, file=sys.stderr)
        return 2

    tool = (d.get("tool_name") or "").strip()
    ti = d.get("tool_input") or {}

    if tool in WRITE_TOOLS or tool in OBSIDIAN_WRITE_TOOLS:
        cls = classify_write_target(tool, ti)
        resolved = resolve_target(tool, ti)
        path = resolved[0] if resolved else ""
        if cls == "code":
            return deny()
        if cls in ASK_CLASSES:
            return ask(cls, path)
        if cls == "skill_config":
            raw_session = d.get("session_id")
            session_id = raw_session if isinstance(raw_session, str) else None
            try:
                project = project_dir()
            except Exception:
                project = None
            append_routing_log(tool, path, session_id, project)
            return 0
        if cls == "data":
            if tool in _DATA_WRITE_TOOLS:
                return 0
            return deny()
        if cls in SILENT_ALLOW:
            return 0
        return deny()

    if tool == "Bash":
        cmd = ti.get("command") or ""
        processed_cmd = scrub_command(remove_quoted_spans(cmd))
        # Every Claude caller, main session and subagents. CLAUDE_ROUTING is
        # ignored. When cursor-run is not installed, this check is skipped.
        # The headless scan runs before an exact router-plan allow.
        if cursor_run_ready() and is_headless_cursor_agent(cmd, processed_cmd):
            return deny_headless()
        if _router_plan_mentioned(cmd):
            if _exact_router_plan(cmd):
                return 0
            return deny()
        processed_cmd = scrub_command(remove_quoted_spans(strip_heredoc_bodies(cmd)))
        if GENERATOR_WRITE.search(processed_cmd):
            return deny()
        if not WRITE_HINT.search(processed_cmd):
            return 0
        paths = extract_paths_from_command(processed_cmd)
        if paths:
            if all(classify_path(p) in ("document", "state") for p in paths):
                return 0
            return deny()
        # Write-like command but no clear path → fail closed (heredocs, vars).
        return deny()

    return 0


def main():
    try:
        hook_input = sys.stdin.read()
    except Exception:
        return deny()
    try:
        return decide(hook_input)
    except Exception:
        return deny()


if __name__ == "__main__":
    sys.exit(main())
