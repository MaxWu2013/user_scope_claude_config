#!/usr/bin/env python3
"""PreToolUse role router (the "who" hook).

Fail open: bad input, a missing _paths, or any decision error exits 0 with
no output. Cursor is allowed before any local import. CLAUDE_ROUTING=off
exits 0 before any local import. SystemExit is not swallowed.
CLAUDE_ROUTING_SCOPE=pilots enforces routing for the vault and pt-zh pilots
only when the session started outside them. File-reader, file-writer, and
Explore (same rules as file-reader) still go through decide() in a non-pilot
project. Agent spawns do too, under pilots and all-writes, even when
routing-reads is off. all-writes also routes document writes in every other
project. Reads outside the pilots stay quiet unless ~/.claude/routing-reads
is on, which runs decide() for that project. Any other scope value leaves
behavior unchanged.

Routing mode is task-aware unless CLAUDE_ROUTING_MODE or ~/.claude/routing-mode
selects strict. An unknown mode value is strict. Read routing follows
~/.claude/routing-reads: on or off, and a missing or unknown value is off.
"""

import sys

sys.dont_write_bytecode = True

import datetime
import fcntl
import json
import os
import re
import shlex
import stat
import time
from types import SimpleNamespace

# Inline copy of _runtime.is_cursor_runtime. Do not import _runtime or _paths
# on this path: Cursor must be allowed even when those modules are missing.
def is_cursor_runtime(d) -> bool:
    if not isinstance(d, dict):
        return False
    version = d.get("cursor_version")
    return (
        d.get("hook_event_name") == "preToolUse"
        and isinstance(version, str)
        and bool(version.strip())
    )


READ_TOOLS = {"Read", "Grep", "Glob"}
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
OBSIDIAN_PREFIX = "mcp__mcp-obsidian__obsidian_"
OBSIDIAN_READ_OPS = {
    "get_file_contents",
    "batch_get_file_contents",
    "simple_search",
    "complex_search",
    "search_by_tag",
    "list_files_in_dir",
    "list_files_in_vault",
    "get_frontmatter",
    "get_periodic_note",
    "get_recent_periodic_notes",
    "get_recent_changes",
}
OBSIDIAN_WRITE_OPS = {"put_content", "append_content", "patch_content"}
ROUTED_WRITE_CLASSES = {"document", "vault_structured", "state"}
READ_ALLOW_CLASSES = {"state", "control", "skill_config", "outside"}

READER_BASH_DENY = "file-reader may only run ~/.claude/hooks/bin/safe-read."
READER_WRITE_DENY = "file-reader is read-only."
WRITER_BASH_DENY = "file-writer has no shell. Report what you could not do."
STATE_DENY = "Specs, memory and plans belong to the orchestrator. Report back instead."
OTHER_DOC_DENY = (
    "Not allowed here: document edits from this subagent. "
    "Give the file-writer subagent (Agent tool) the exact files and changes."
)
ORCH_DOC_DENY = (
    "Not allowed here: editing documents on the main thread. "
    "Give the file-writer subagent (Agent tool) the exact files and changes."
)
ORCH_BASH_WRITE_DENY = (
    "Not allowed here: writing documents through Bash. "
    "Give the file-writer subagent (Agent tool) the exact files and changes."
)
CURSOR_AGENT_DENY = "Start cursor-agent only through ~/.claude/hooks/bin/cursor-run <spec>."
SESSION_COST_DENY = "session-cost must be invoked alone with plain arguments."
ROUTER_PLAN_MAIN_ONLY = "router-plan is main-session only"
ROUTER_PLAN_DENY = "router-plan must be invoked alone with plain arguments."
WRITER_SEARCH_DENY = "file-writer may not search. Ask the file-reader subagent (Agent tool)."
WRITER_READ_DENY = "file-writer may only Read documents."
WRITER_OBSIDIAN_READ_DENY = "file-writer may not read the vault through Obsidian."
WRITER_READ_CLASSES = {"document", "vault_structured"}
EXPLORE_AGENT_DENY = "Explore is off in strict mode; use file-reader."

_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_READ_FILE_BUDGET = 10
_READ_LINE_BUDGET = 2000
# Read returns at most this many lines, so a call with no limit charges that window.
_READ_WINDOW = 2000
_READ_LINE_HINT = " Read a smaller range with offset and limit, or use file-reader for large files."
_EDIT_LINE_BUDGET = 20
_STATE_MAX = 1024 * 1024
_STATE_TTL_SECONDS = 7 * 24 * 60 * 60
_ROUTING_MODE_USAGE = "usage: routing-mode [status|strict|task-aware]"
_ROUTING_READS_USAGE = "usage: routing-reads [status|on|off]"

# Shell-metacharacter class from the spec. In a raw pattern, \\ is one
# backslash and \n / \r are newline / carriage return.
_SAFE_ARG = r'[^"$`\\;&|<>()\n\r]'
READ_CMD = re.compile(
    r"(?:^|&&|\|\||[;\n|])\s*(?:cat|head|tail|less|more|grep|egrep|rg|ag|find|fd|tree|awk|sed\s+-n)\b"
    r"|\bgit\s+(?:diff|show)\b(?![^\n;&|]*--(?:stat|name-only|name-status|shortstat)\b)"
    r"|\bgit\s+log\b[^\n;&|]*\s(?:-p|--patch)\b"
)
CURSOR_AGENT_RE = re.compile(r"(?:^|[\s;&|/])cursor-agent\b")


def _horizontal_home_command(binary):
    """Hooks path with horizontal space only. A line break is not a separator."""
    home = re.escape(os.path.expanduser("~"))
    return r"^[ \t]*(?:~|" + home + r")/\.claude/hooks/bin/" + re.escape(binary)


def safe_read_re():
    return re.compile(
        _horizontal_home_command("safe-read") + r"(?:[ \t]+" + _SAFE_ARG + r"*)?[ \t]*$"
    )


def cursor_run_re():
    return re.compile(_horizontal_home_command("cursor-run") + r"[ \t]+" + _SAFE_ARG + r"+$")


def safe_read_exact(raw):
    """One single-line safe-read command. A line break never matches."""
    if _command_has_break(raw):
        return False
    return safe_read_re().match(raw) is not None


def cursor_run_exact(raw):
    """One single-line cursor-run command. A line break never matches."""
    if _command_has_break(raw):
        return False
    return cursor_run_re().match(raw) is not None


def _line_break_hooks_deny(raw):
    """Deny reason when a line break sits in a safe-read or cursor-run command.

    Those allows are single-line. A break is not an exact command, and it must
    not fall through to a silent allow.
    """
    if not _command_has_break(raw):
        return None
    if _hooks_binary_mentioned(raw, "cursor-run"):
        return CURSOR_AGENT_DENY
    if _hooks_binary_mentioned(raw, "safe-read"):
        return (
            "Not allowed here: reading files or full diffs through Bash. "
            "Ask the file-reader subagent (Agent tool); batch your questions."
        )
    return None


def _plain_hooks_re(binary):
    """One hooks binary, optional leading tabs or spaces, then plain arguments.

    Newlines are not whitespace here. Callers also reject a raw command that
    contains a line break before matching, because $ accepts a final newline.
    """
    home = re.escape(os.path.expanduser("~"))
    return re.compile(
        r"^[ \t]*(?:~|" + home + r")/\.claude/hooks/bin/"
        + re.escape(binary) + r"[ \t]+" + _SAFE_ARG + r"+$"
    )


def _command_has_break(raw):
    return isinstance(raw, str) and ("\n" in raw or "\r" in raw)


def session_cost_re():
    # One plain invocation: path, then arguments with no shell metacharacters.
    return _plain_hooks_re("session-cost")


def session_cost_exact(raw):
    """True for one single-line session-cost command. A line break never matches."""
    if _command_has_break(raw):
        return False
    return session_cost_re().match(raw) is not None


def router_plan_re():
    # Same shape as session-cost: the hooks path, then plain arguments only.
    return _plain_hooks_re("router-plan")


def router_plan_verdict(role, raw):
    """'allow', 'main', 'form', or None when the command is not router-plan."""
    if not isinstance(raw, str) or raw == "":
        return None
    exact = (not _command_has_break(raw)) and router_plan_re().match(raw) is not None
    if not exact and not _hooks_binary_mentioned(raw, "router-plan"):
        return None
    if role != "orchestrator":
        return "main"
    if exact:
        return "allow"
    return "form"


def _hooks_binary_mentioned(raw, binary):
    """True when raw names ~/.claude/hooks/bin/<binary> anywhere, including quotes."""
    if not isinstance(raw, str) or raw == "":
        return False
    home = re.escape(os.path.expanduser("~"))
    pattern = (
        r"(?:~|" + home + r")/\.claude/hooks/bin/"
        + re.escape(binary)
        + r"(?![A-Za-z0-9._-])"
    )
    return re.search(pattern, raw) is not None


def cursor_run_path():
    # CURSOR_RUN_PATH is a test seam for "cursor-run is installed". Production
    # leaves it unset and checks ~/.claude/hooks/bin/cursor-run. The allowed
    # command shape stays the spec regex either way.
    override = os.environ.get("CURSOR_RUN_PATH")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".claude", "hooks", "bin", "cursor-run")


def cursor_run_ready():
    path = cursor_run_path()
    return os.path.isfile(path) and os.access(path, os.X_OK)


def emit(decision, reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))
    return 0


# Filled in by decide() before a tool handler runs. deny/ask read it.
_LOG = {
    "role": "",
    "agent_type": "",
    "tool": "",
    "path": None,
    "command": None,
}

# Session fields for hook records. project is captured before pilot scope
# retargets CLAUDE_PROJECT_DIR. mode and reads switch records do not use this.
_CALL = {
    "session_id": None,
    "project": None,
    "mode": None,
}


def _escape_controls(text):
    parts = []
    for ch in text:
        code = ord(ch)
        if code < 32 or code == 127:
            parts.append(json.dumps(ch)[1:-1])
        else:
            parts.append(ch)
    return "".join(parts)


def deny(reason):
    try:
        _log_decision("deny", reason)
    except Exception:
        pass
    return emit("deny", reason)


def ask(reason):
    try:
        _log_decision("ask", reason)
    except Exception:
        pass
    return emit("ask", reason)


def read_deny(subject, extra=""):
    return deny(
        f"Not allowed here: {subject}. "
        "Ask the file-reader subagent (Agent tool); batch your questions."
        + extra
    )


def _routing_log_path():
    return os.environ.get("ROUTING_LOG") or os.path.join(
        os.path.expanduser("~"), ".claude", "logs", "routing.jsonl"
    )


def _append_record(record):
    log_path = _routing_log_path()
    directory = os.path.dirname(log_path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    fd = os.open(log_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(log_path, 0o600)


def _capture_call_context(data, paths):
    """Remember session_id, project and mode for later hook records.

    project is the session project as this call first sees it. Pilot scope may
    retarget CLAUDE_PROJECT_DIR before the decision is logged.
    """
    _CALL["session_id"] = None
    _CALL["project"] = None
    _CALL["mode"] = None
    try:
        raw = data.get("session_id") if isinstance(data, dict) else None
        if isinstance(raw, str):
            _CALL["session_id"] = raw
        _CALL["project"] = paths.project_dir()
        mode, _source = paths.resolve_routing_mode()
        _CALL["mode"] = mode
    except Exception:
        return


def append_routing_log(role, agent_type, tool, path, *, decision="allow-log", reason=None, command=None, extra=None):
    """One JSON line. Never raises.

    Hook records carry session_id, project and mode. deny and ask also carry
    reason, and Bash adds command. extra supplies allowance totals.
    """
    try:
        record = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "role": role,
            "agent_type": agent_type,
            "tool": tool,
            "path": path,
            "decision": decision,
            "session_id": _CALL.get("session_id"),
            "project": _CALL.get("project"),
            "mode": _CALL.get("mode"),
        }
        if isinstance(extra, dict):
            record.update(extra)
        if decision in ("deny", "ask"):
            text = reason if isinstance(reason, str) else ""
            record["reason"] = text[:120]
            if tool == "Bash":
                raw = command if isinstance(command, str) else ""
                record["command"] = _escape_controls(raw[:200])
        _append_record(record)
    except Exception:
        return


def _log_allowance(decision, fields):
    append_routing_log(
        _LOG.get("role", ""),
        _LOG.get("agent_type", ""),
        _LOG.get("tool", ""),
        _LOG.get("path"),
        decision=decision,
        extra=fields if isinstance(fields, dict) else None,
    )


def append_mode_log(mode):
    """{ts, class, mode}. Never raises. Honours ROUTING_LOG."""
    try:
        _append_record({
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "class": "mode",
            "mode": mode,
        })
    except Exception:
        return


def append_reads_log(value):
    """{ts, class, value}. Never raises. Honours ROUTING_LOG."""
    try:
        _append_record({
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "class": "reads",
            "value": value,
        })
    except Exception:
        return


def _log_decision(decision, reason):
    append_routing_log(
        _LOG.get("role", ""),
        _LOG.get("agent_type", ""),
        _LOG.get("tool", ""),
        _LOG.get("path"),
        decision=decision,
        reason=reason,
        command=_LOG.get("command"),
        extra=_LOG.get("extra"),
    )


def _arm_log(role, d, tool, ti, paths):
    command = None
    path = None
    if tool == "Bash":
        raw = ti.get("command")
        command = raw if isinstance(raw, str) else ""
    elif tool in READ_TOOLS:
        literal, _real = resolve_read_target(tool, ti, paths.project_dir)
        path = literal or None
    elif tool in WRITE_TOOLS or tool.startswith(OBSIDIAN_PREFIX):
        found = logged_path(tool, ti, paths.resolve_target)
        path = found or None
    _LOG["role"] = role
    _LOG["agent_type"] = _agent_type(d)
    _LOG["tool"] = tool
    _LOG["path"] = path
    _LOG["command"] = command
    _LOG["extra"] = None


def role_of(d):
    # No agent_id means the main session, including `claude --agent X`.
    # Explore is the routed stand-in for the built-in: same rules as file-reader.
    agent_id = d.get("agent_id")
    if agent_id is None or agent_id == "":
        return "orchestrator"
    agent_type = d.get("agent_type")
    if agent_type == "file-reader" or agent_type == "Explore":
        return "file-reader"
    if agent_type == "file-writer":
        return "file-writer"
    return "other"


def _agent_type(d):
    agent_type = d.get("agent_type")
    return agent_type if isinstance(agent_type, str) else ""


def resolve_like(raw, project_dir):
    text = os.path.expanduser(str(raw).replace("\\", "/"))
    lex = os.path.normpath(text if os.path.isabs(text) else os.path.join(project_dir(), text))
    return lex, os.path.realpath(lex)


def resolve_read_target(tool, ti, project_dir):
    raw = None
    if tool == "Read":
        raw = ti.get("file_path")
    elif tool in ("Grep", "Glob"):
        raw = ti.get("path")
    if not isinstance(raw, str) or not raw.strip():
        raw = project_dir()
    return resolve_like(raw, project_dir)


def stricter_class(literal, real, classify, strictness):
    return min((classify(literal), classify(real)), key=strictness.index)


def in_project_or_vault(path, project_dir, under, vault_roots):
    proj = os.path.normpath(project_dir())
    np = os.path.normpath(path)
    if np == proj or under(np, proj):
        return True
    return any(under(np, root) for root in vault_roots)


def claude_md_paths(project_dir):
    home = os.path.expanduser("~")
    found = set()
    for raw in (
        os.path.join(project_dir(), "CLAUDE.md"),
        os.path.join(home, ".claude", "CLAUDE.md"),
    ):
        norm = os.path.normpath(raw)
        found.add(norm)
        found.add(os.path.realpath(norm))
    return found


def read_allowed(literal, real, paths):
    # Explicit CLAUDE.md paths, then the allow classes. Scratch files outside
    # the workspace classify as code (not outside) and stay readable. Deny
    # covers the project dir itself and document / vault_structured /
    # vault_asset / code inside the project or a vault.
    names = claude_md_paths(paths.project_dir)
    if os.path.normpath(literal) in names or os.path.normpath(real) in names:
        return True
    cls = stricter_class(literal, real, paths.classify, paths.STRICTNESS)
    if cls in READ_ALLOW_CLASSES:
        return True
    if in_project_or_vault(literal, paths.project_dir, paths.under, paths.VAULT_ROOTS):
        return False
    if in_project_or_vault(real, paths.project_dir, paths.under, paths.VAULT_ROOTS):
        return False
    return True


def logged_path(tool, ti, resolve_target):
    resolved = resolve_target(tool, ti)
    if resolved:
        return resolved[0]
    raw = ti.get("file_path") or ti.get("notebook_path") or ti.get("filepath") or ""
    return raw if isinstance(raw, str) else ""


def _state_dir():
    return os.path.join(os.path.expanduser("~"), ".claude", "logs", "routing-state")


def _gc_state_files(directory, keep):
    try:
        names = os.listdir(directory)
    except OSError:
        return
    cutoff = time.time() - _STATE_TTL_SECONDS
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        if path == keep:
            continue
        try:
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                continue
            if st.st_mtime < cutoff:
                os.unlink(path)
        except OSError:
            continue


def _mutate_state(session_id, mutator):
    """Run mutator(state) under an exclusive lock.

    Returns the mutator's result, or None when the session id is unusable or
    state cannot be read. Writes only when the mutator returns True. A state
    error means the caller grants no budget and no small-edit allowance.
    """
    if not isinstance(session_id, str) or _SESSION_ID.fullmatch(session_id) is None:
        return None
    fd = None
    try:
        directory = _state_dir()
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        path = os.path.join(directory, session_id + ".json")
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.chmod(path, 0o600)
        raw = os.read(fd, _STATE_MAX + 1)
        if len(raw) > _STATE_MAX:
            return None
        if raw.strip():
            state = json.loads(raw.decode("utf-8"))
            if not isinstance(state, dict):
                return None
        else:
            state = {}
        outcome = mutator(state)
        if outcome is True:
            data = (json.dumps(state, separators=(",", ":")) + "\n").encode("utf-8")
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, data)
            os.fsync(fd)
            _gc_state_files(directory, path)
        return outcome
    except Exception:
        return None
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)


def _text_lines(text):
    if text == "":
        return 0
    count = text.count("\n")
    if not text.endswith("\n"):
        count += 1
    return count


def _file_line_count(real, stop_at):
    """Line count of real, or None when it cannot be read.

    Stops once stop_at lines have been seen and returns stop_at. Callers only
    clip a window to the file length, so a saturated count is enough.
    """
    try:
        count = 0
        saw = False
        ends_nl = False
        with open(real, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                saw = True
                count += chunk.count(b"\n")
                ends_nl = chunk.endswith(b"\n")
                if count >= stop_at:
                    return stop_at
    except OSError:
        return None
    if not saw:
        return 0
    if not ends_nl:
        count += 1
    if count > stop_at:
        return stop_at
    return count


def _read_span(ti, real):
    """Inclusive 1-based line span this Read would charge.

    Returns None when the file cannot be measured and no limit is given, which
    grants no budget. A span with start > end is empty: it charges nothing.
    """
    offset = ti.get("offset")
    if isinstance(offset, int) and not isinstance(offset, bool) and offset > 0:
        start = offset
    else:
        start = 1
    limit = ti.get("limit")
    limited = isinstance(limit, int) and not isinstance(limit, bool) and limit >= 0
    if limited:
        end = start + limit - 1
    else:
        end = start + _READ_WINDOW - 1
    if limited and start > end:
        return (start, end)
    count = _file_line_count(real, end)
    if count is None:
        if not limited:
            return None
        return (start, end)
    if end > count:
        end = count
    return (start, end)


def _merge_ranges(ranges):
    """Sort and merge inclusive ranges into the stored form."""
    ordered = sorted((start, end) for start, end in ranges)
    if not ordered:
        return []
    merged = [[ordered[0][0], ordered[0][1]]]
    for start, end in ordered[1:]:
        last = merged[-1]
        if start <= last[1] + 1:
            if end > last[1]:
                last[1] = end
        else:
            merged.append([start, end])
    return merged


def _covered_lines(ranges):
    return sum(end - start + 1 for start, end in ranges)


def _uncovered(start, end, existing):
    """Sub-ranges of start..end that existing (merged) ranges do not cover."""
    if start > end:
        return []
    fresh = []
    cursor = start
    for estart, eend in existing:
        if eend < cursor:
            continue
        if estart > end:
            break
        if estart > cursor:
            fresh.append([cursor, estart - 1])
        if eend + 1 > cursor:
            cursor = eend + 1
        if cursor > end:
            return fresh
    if cursor <= end:
        fresh.append([cursor, end])
    return fresh


def _stored_ranges(stored):
    """Charged ranges for one file, or None when the value is malformed.

    A non-negative integer N is the old format and means lines 1..N.
    """
    if isinstance(stored, bool):
        return None
    if isinstance(stored, int):
        if stored < 0:
            return None
        if stored == 0:
            return []
        return [[1, stored]]
    if not isinstance(stored, list):
        return None
    ranges = []
    for item in stored:
        if not isinstance(item, list) or len(item) != 2:
            return None
        start, end = item
        if (
            isinstance(start, bool) or not isinstance(start, int)
            or isinstance(end, bool) or not isinstance(end, int)
            or start < 1
            or end < start
        ):
            return None
        ranges.append((start, end))
    return _merge_ranges(ranges)


def _charge_lines(stored):
    if not isinstance(stored, int) or isinstance(stored, bool) or stored < 0:
        return None
    return stored


def _charge_read(session_id, real, span, bucket=None):
    """Charge span, a (start, end) inclusive range. Empty when start > end.

    Returns (outcome, charged). outcome is True when the read is allowed,
    "lines" when it would pass the line budget, False when it would pass the
    file budget, or None on a state error. charged is the allowance totals
    when outcome is True.

    bucket None charges the orchestrator counters on the session. A bucket
    (one general-purpose agent_id) keeps its own counters under agent_reads
    and does not share the main session's file or line totals.
    """
    charged = {}

    def mutator(state):
        target = state
        if bucket is not None:
            agents = state.get("agent_reads")
            if agents is None:
                agents = {}
                state["agent_reads"] = agents
            if not isinstance(agents, dict):
                return None
            inner = agents.get(bucket)
            if inner is None:
                inner = {}
                agents[bucket] = inner
            elif not isinstance(inner, dict):
                return None
            target = inner
        reads = target.get("reads")
        if reads is None:
            reads = {}
        if not isinstance(reads, dict):
            return None
        parsed = {}
        total = 0
        for key, value in reads.items():
            ranges = _stored_ranges(value)
            if ranges is None:
                return None
            parsed[key] = ranges
            total += _covered_lines(ranges)
        start, end = span
        if real in parsed:
            previous = parsed[real]
        else:
            if len(reads) >= _READ_FILE_BUDGET:
                return False
            previous = []
        fresh = _uncovered(start, end, previous)
        extra = _covered_lines(fresh)
        if extra and total + extra > _READ_LINE_BUDGET:
            return "lines"
        reads[real] = _merge_ranges(previous + fresh)
        target["reads"] = reads
        charged["lines"] = extra
        charged["budget_files"] = len(reads)
        charged["budget_lines"] = total + extra
        return True

    outcome = _mutate_state(session_id, mutator)
    if outcome is True:
        return True, charged
    return outcome, None


def _allow_read(session_id, real, ti, bucket=None):
    span = _read_span(ti, real)
    if span is None:
        return False, None
    return _charge_read(session_id, real, span, bucket=bucket)


def _changed_lines(tool, ti):
    if tool == "Edit":
        old = ti.get("old_string")
        new = ti.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return None
        return max(_text_lines(old), _text_lines(new))
    if tool == "MultiEdit":
        edits = ti.get("edits")
        if not isinstance(edits, list) or not edits:
            return None
        total = 0
        for edit in edits:
            if not isinstance(edit, dict):
                return None
            old = edit.get("old_string")
            new = edit.get("new_string")
            if not isinstance(old, str) or not isinstance(new, str):
                return None
            total += max(_text_lines(old), _text_lines(new))
        return total
    if tool.startswith(OBSIDIAN_PREFIX):
        op = tool[len(OBSIDIAN_PREFIX):]
        if op in ("append_content", "patch_content"):
            content = ti.get("content")
            if not isinstance(content, str):
                return None
            return _text_lines(content)
    return None


def _small_edit_eligible(tool):
    if tool in ("Edit", "MultiEdit"):
        return True
    if tool.startswith(OBSIDIAN_PREFIX):
        return tool[len(OBSIDIAN_PREFIX):] in ("append_content", "patch_content")
    return False


def _charge_edit(session_id, real, changed):
    """Return (outcome, charged). charged is set only when the edit is allowed."""
    charged = {}

    def mutator(state):
        edit = state.get("edit")
        if edit is None:
            if changed > _EDIT_LINE_BUDGET:
                return False
            state["edit"] = {"path": real, "lines": changed}
            charged["lines"] = changed
            charged["edit_lines"] = changed
            return True
        if not isinstance(edit, dict):
            return None
        path = edit.get("path")
        lines = _charge_lines(edit.get("lines"))
        if not isinstance(path, str) or lines is None:
            return None
        if path != real or lines + changed > _EDIT_LINE_BUDGET:
            return False
        edit["lines"] = lines + changed
        state["edit"] = edit
        charged["lines"] = changed
        charged["edit_lines"] = lines + changed
        return True

    outcome = _mutate_state(session_id, mutator)
    if outcome is True:
        return True, charged
    return outcome, None


def _allow_small_edit(session_id, tool, ti, paths):
    changed = _changed_lines(tool, ti)
    if changed is None:
        return False, None
    found = paths.resolve_target(tool, ti)
    if not found:
        return False, None
    outcome, charged = _charge_edit(session_id, found[1], changed)
    return outcome is True, charged


def handle_write(role, d, tool, ti, paths, mode, session_id):
    if role == "file-reader":
        return deny(READER_WRITE_DENY)
    cls = paths.classify_write_target(tool, ti)
    if cls not in ROUTED_WRITE_CLASSES:
        return 0
    if role == "orchestrator":
        if cls == "state":
            return 0
        if cls == "document" and mode == "task-aware" and _small_edit_eligible(tool):
            allowed, charged = _allow_small_edit(session_id, tool, ti, paths)
            if allowed:
                _log_allowance("allow-edit", charged)
                return 0
        return deny(ORCH_DOC_DENY)
    if cls == "state":
        return deny(STATE_DENY)
    if role == "file-writer":
        return 0
    # other subagent, document or vault_structured
    other_mode = os.environ.get("CLAUDE_ROUTING_OTHER") or "log"
    if other_mode == "strict":
        return deny(OTHER_DOC_DENY)
    append_routing_log(role, _agent_type(d), tool, logged_path(tool, ti, paths.resolve_target))
    return 0


def _general_purpose_read(d, tool, ti, paths, mode, session_id):
    """Orchestrator read budget, counted per agent_id, with the same logging.

    Task-aware Read of a routed path charges 10 files / 2000 distinct lines.
    Over budget denies and points at file-reader. Strict denies Read, Grep
    and Glob the same way the orchestrator does. Other sub-agents do not
    come through here.
    """
    literal, real = resolve_read_target(tool, ti, paths.project_dir)
    if read_allowed(literal, real, paths):
        return 0
    subject = "%s from general-purpose" % tool
    if mode == "task-aware" and tool == "Read":
        bucket = d.get("agent_id")
        if not isinstance(bucket, str) or bucket == "":
            bucket = "general-purpose"
        outcome, charged = _allow_read(session_id, real, ti, bucket=bucket)
        if outcome is True:
            _log_allowance("allow-budget", charged)
            return 0
        if outcome == "lines":
            return read_deny(subject, _READ_LINE_HINT)
    return read_deny(subject)


def handle_read(role, d, tool, ti, paths, mode, session_id):
    if role == "file-writer":
        if tool in ("Grep", "Glob"):
            return deny(WRITER_SEARCH_DENY)
        if tool == "Read":
            literal, real = resolve_read_target(tool, ti, paths.project_dir)
            cls = stricter_class(literal, real, paths.classify, paths.STRICTNESS)
            if cls not in WRITER_READ_CLASSES:
                return deny(WRITER_READ_DENY)
        return 0
    if role == "other" and _agent_type(d) == "general-purpose":
        return _general_purpose_read(d, tool, ti, paths, mode, session_id)
    if role != "orchestrator":
        return 0
    literal, real = resolve_read_target(tool, ti, paths.project_dir)
    if read_allowed(literal, real, paths):
        return 0
    if mode == "task-aware" and tool == "Read":
        outcome, charged = _allow_read(session_id, real, ti)
        if outcome is True:
            _log_allowance("allow-budget", charged)
            return 0
        if outcome == "lines":
            return read_deny(f"{tool} on the main thread", _READ_LINE_HINT)
    return read_deny(f"{tool} on the main thread")


def _other_bash(role, d, raw, paths):
    # State wins over document: log mode must not allow a spec write that also names a doc.
    cmd = paths.scrub_command(paths.remove_quoted_spans(paths.strip_heredoc_bodies(raw)))
    if not paths.WRITE_HINT.search(cmd):
        return 0
    classified = [
        (item, paths.classify_path(item))
        for item in paths.extract_paths_from_command(cmd)
    ]
    state_paths = [item for item, cls in classified if cls == "state"]
    if state_paths:
        _LOG["path"] = state_paths[0]
        return deny(STATE_DENY)
    doc_paths = [item for item, cls in classified if cls in ("document", "vault_structured")]
    if not doc_paths:
        return 0
    if (os.environ.get("CLAUDE_ROUTING_OTHER") or "log") == "strict":
        _LOG["path"] = doc_paths[0]
        return deny(OTHER_DOC_DENY)
    append_routing_log(role, _agent_type(d), "Bash", doc_paths[0])
    return 0


def _orch_bash_document_writes(cmd, paths):
    """Orchestrator document-write check: WRITE_HINT through ORCH_BASH_WRITE_DENY."""
    if paths.WRITE_HINT.search(cmd):
        for extracted in paths.extract_paths_from_command(cmd):
            if paths.classify_path(extracted) in ("document", "vault_structured"):
                _LOG["path"] = extracted
                return deny(ORCH_BASH_WRITE_DENY)
    return 0


# Same quoted-span rule as _paths.remove_quoted_spans. Pipes inside quotes are data.
_QUOTED_SPAN = re.compile(r"""'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*["]""")
# Short options that take a value. `F` is a flag (fixed-string); `f` is the pattern file.
_GREP_VALUE_SHORT = {"e", "f", "A", "B", "C", "m", "d", "D", "g", "t", "T", "M"}
_GREP_VALUE_LONG = {
    "regexp",
    "file",
    "after-context",
    "before-context",
    "context",
    "max-count",
    "directories",
    "devices",
    "binary-files",
    "include",
    "exclude",
    "exclude-dir",
    "include-dir",
    "label",
    "group-separator",
    "color",
    "colour",
    "glob",
    "type",
    "type-not",
    "max-depth",
}


def _shell_tokens(text):
    """Split on &&, ||, ;, newline, and a single | outside quotes.

    Each segment is (kind, text, reached_by_pipe). A segment is reached by a
    pipe only when the separator immediately before it is one `|`, not `||`.
    """
    spans = [(match.start(), match.end()) for match in _QUOTED_SPAN.finditer(text or "")]

    def quote_end(index):
        for start, end in spans:
            if start <= index < end:
                return end
        return None

    tokens = []
    buf = []
    reached = False
    i = 0
    n = len(text or "")
    source = text or ""

    def flush():
        tokens.append(("seg", "".join(buf), reached))
        buf.clear()

    while i < n:
        end = quote_end(i)
        if end is not None:
            buf.append(source[i:end])
            i = end
            continue
        if source.startswith("&&", i) or source.startswith("||", i):
            flush()
            tokens.append(("sep", source[i:i + 2], False))
            reached = False
            i += 2
            continue
        if source[i] in ";\n|":
            flush()
            tokens.append(("sep", source[i], False))
            reached = source[i] == "|"
            i += 1
            continue
        buf.append(source[i])
        i += 1
    flush()
    return tokens


def _scrub_unquoted(text, scrub):
    parts = []
    last = 0
    for match in _QUOTED_SPAN.finditer(text):
        parts.append(scrub(text[last:match.start()]))
        parts.append(match.group(0))
        last = match.end()
    parts.append(scrub(text[last:]))
    return "".join(parts)


def _take_attached_or_next(args, index, attached):
    """Return (value, index_after_option) or None when the value is missing.

    `attached` is the rest of a short cluster. An empty attachment means the
    next token is the value, even when that token starts with `-`.
    """
    if attached:
        return attached, index + 1
    if index + 1 >= len(args):
        return None
    return args[index + 1], index + 2


def _grep_is_filter(args):
    """True when grep/egrep/rg has no file operand and is not a recursive or -f read."""
    supplied = False
    saw_file = False
    recursive = False
    operands = []
    end_opts = False
    i = 0
    n = len(args)
    while i < n:
        tok = args[i]
        if end_opts or tok == "-" or not tok.startswith("-"):
            operands.append(tok)
            i += 1
            continue
        if tok == "--":
            end_opts = True
            i += 1
            continue
        if tok.startswith("--"):
            name, eq, val = tok[2:].partition("=")
            if name == "recursive":
                recursive = True
                i += 1
                continue
            if name == "file":
                saw_file = True
                if not eq:
                    taken = _take_attached_or_next(args, i, "")
                    if taken is None:
                        return False
                    i = taken[1]
                else:
                    i += 1
                continue
            if name == "regexp":
                supplied = True
                if not eq:
                    taken = _take_attached_or_next(args, i, "")
                    if taken is None:
                        return False
                    i = taken[1]
                else:
                    i += 1
                continue
            if name in _GREP_VALUE_LONG and not eq:
                taken = _take_attached_or_next(args, i, "")
                if taken is None:
                    return False
                i = taken[1]
                continue
            i += 1
            continue
        cluster = tok[1:]
        j = 0
        consumed = None
        while j < len(cluster):
            opt = cluster[j]
            if opt in {"r", "R"}:
                recursive = True
                j += 1
                continue
            if opt in _GREP_VALUE_SHORT:
                if opt == "f":
                    saw_file = True
                if opt == "e":
                    supplied = True
                taken = _take_attached_or_next(args, i, cluster[j + 1:])
                if taken is None:
                    return False
                consumed = taken[1]
                break
            j += 1
        i = i + 1 if consumed is None else consumed
    if recursive or saw_file:
        return False
    if supplied:
        return len(operands) == 0
    return len(operands) <= 1


def _no_file_operands(args, value_short, value_long):
    """True when every word is an option or an option's value, not a path operand."""
    operands = []
    end_opts = False
    i = 0
    n = len(args)
    while i < n:
        tok = args[i]
        if end_opts or tok == "-" or not tok.startswith("-"):
            operands.append(tok)
            i += 1
            continue
        if tok == "--":
            end_opts = True
            i += 1
            continue
        if tok.startswith("--"):
            name, eq, _val = tok[2:].partition("=")
            if name in value_long and not eq:
                taken = _take_attached_or_next(args, i, "")
                if taken is None:
                    return False
                i = taken[1]
                continue
            i += 1
            continue
        cluster = tok[1:]
        j = 0
        consumed = None
        while j < len(cluster):
            opt = cluster[j]
            if opt in value_short:
                taken = _take_attached_or_next(args, i, cluster[j + 1:])
                if taken is None:
                    return False
                consumed = taken[1]
                break
            j += 1
        i = i + 1 if consumed is None else consumed
    return len(operands) == 0


def _sed_is_filter(args):
    """sed -n is a stdin filter when the only operand is its script."""
    has_n = False
    has_script = False
    has_file = False
    operands = []
    end_opts = False
    i = 0
    n = len(args)
    while i < n:
        tok = args[i]
        if end_opts or tok == "-" or not tok.startswith("-"):
            operands.append(tok)
            i += 1
            continue
        if tok == "--":
            end_opts = True
            i += 1
            continue
        if tok.startswith("--"):
            name, eq, _val = tok[2:].partition("=")
            if name in {"quiet", "silent"}:
                has_n = True
                i += 1
                continue
            if name == "expression":
                has_script = True
                if not eq:
                    taken = _take_attached_or_next(args, i, "")
                    if taken is None:
                        return False
                    i = taken[1]
                else:
                    i += 1
                continue
            if name == "file":
                has_file = True
                if not eq:
                    taken = _take_attached_or_next(args, i, "")
                    if taken is None:
                        return False
                    i = taken[1]
                else:
                    i += 1
                continue
            i += 1
            continue
        cluster = tok[1:]
        j = 0
        consumed = None
        while j < len(cluster):
            opt = cluster[j]
            if opt == "n":
                has_n = True
                j += 1
                continue
            if opt in {"e", "f"}:
                if opt == "e":
                    has_script = True
                else:
                    has_file = True
                taken = _take_attached_or_next(args, i, cluster[j + 1:])
                if taken is None:
                    return False
                consumed = taken[1]
                break
            j += 1
        i = i + 1 if consumed is None else consumed
    if not has_n or has_file:
        return False
    if has_script:
        return len(operands) == 0
    return len(operands) <= 1


def _awk_is_filter(args):
    """awk is a stdin filter when the only operand is its program."""
    has_program = False
    has_file = False
    operands = []
    end_opts = False
    i = 0
    n = len(args)
    while i < n:
        tok = args[i]
        if end_opts or tok == "-" or not tok.startswith("-"):
            operands.append(tok)
            i += 1
            continue
        if tok == "--":
            end_opts = True
            i += 1
            continue
        if tok.startswith("--"):
            name, eq, _val = tok[2:].partition("=")
            if name in {"field-separator", "assign"}:
                if not eq:
                    taken = _take_attached_or_next(args, i, "")
                    if taken is None:
                        return False
                    i = taken[1]
                else:
                    i += 1
                continue
            if name == "source":
                has_program = True
                if not eq:
                    taken = _take_attached_or_next(args, i, "")
                    if taken is None:
                        return False
                    i = taken[1]
                else:
                    i += 1
                continue
            if name == "file":
                has_file = True
                if not eq:
                    taken = _take_attached_or_next(args, i, "")
                    if taken is None:
                        return False
                    i = taken[1]
                else:
                    i += 1
                continue
            i += 1
            continue
        cluster = tok[1:]
        j = 0
        consumed = None
        while j < len(cluster):
            opt = cluster[j]
            if opt in {"F", "v", "e", "f"}:
                if opt == "e":
                    has_program = True
                if opt == "f":
                    has_file = True
                taken = _take_attached_or_next(args, i, cluster[j + 1:])
                if taken is None:
                    return False
                consumed = taken[1]
                break
            j += 1
        i = i + 1 if consumed is None else consumed
    if has_file:
        return False
    if has_program:
        return len(operands) == 0
    return len(operands) <= 1


def _is_stdin_filter(segment, scrub):
    """A command on the right of one `|` that reads stdin and no file.

    Operands come from shlex so a quoted pattern is one word. A parse failure
    is a read. Option values (`head -n 5`) are not operands.
    """
    try:
        tokens = shlex.split(_scrub_unquoted(segment, scrub), posix=True)
    except ValueError:
        return False
    if not tokens:
        return False
    cmd = os.path.basename(tokens[0].replace("\\", "/"))
    args = tokens[1:]
    if cmd in {"grep", "egrep", "rg"}:
        return _grep_is_filter(args)
    if cmd == "head":
        return _no_file_operands(args, {"n", "c"}, {"lines", "bytes"})
    if cmd == "tail":
        return _no_file_operands(
            args, {"n", "c"}, {"lines", "bytes", "sleep-interval", "pid", "max-unchanged-stats"}
        )
    if cmd in {"less", "more"}:
        return _no_file_operands(args, set(), set())
    if cmd == "sed":
        return _sed_is_filter(args)
    if cmd == "awk":
        return _awk_is_filter(args)
    return False


def _without_stdin_filters(body, scanned, scrub):
    """Blank piped stdin filters so READ_CMD does not treat them as file reads."""
    body_tokens = _shell_tokens(body)
    scan_tokens = _shell_tokens(scanned)

    def seps(tokens):
        return [tok[1] for tok in tokens if tok[0] == "sep"]

    body_segs = [tok for tok in body_tokens if tok[0] == "seg"]
    scan_segs = [tok for tok in scan_tokens if tok[0] == "seg"]
    if seps(body_tokens) != seps(scan_tokens) or len(body_segs) != len(scan_segs):
        return scanned
    if any(orig[2] != scan[2] for orig, scan in zip(body_segs, scan_segs)):
        return scanned
    out = []
    index = 0
    for tok in scan_tokens:
        if tok[0] != "seg":
            out.append(tok[1])
            continue
        orig = body_segs[index]
        index += 1
        if orig[2] and _is_stdin_filter(orig[1], scrub):
            out.append(" " * len(tok[1]))
        else:
            out.append(tok[1])
    return "".join(out)


def _router_plan_result(role, raw):
    """Allow, deny, or None when the command is not router-plan."""
    verdict = router_plan_verdict(role, raw)
    if verdict == "allow":
        return 0
    if verdict == "main":
        return deny(ROUTER_PLAN_MAIN_ONLY)
    if verdict == "form":
        return deny(ROUTER_PLAN_DENY)
    return None


def handle_bash(role, d, raw, paths):
    if not isinstance(raw, str):
        return 0
    _LOG["command"] = raw
    # File-reader safe-read is decided before a router-plan mention, so an
    # exact safe-read that only names the path stays allowed.
    if role == "file-reader":
        if safe_read_exact(raw) or session_cost_exact(raw):
            return 0
        planned = _router_plan_result(role, raw)
        if planned is not None:
            return planned
        return deny(READER_BASH_DENY)
    if role == "file-writer":
        planned = _router_plan_result(role, raw)
        if planned is not None:
            return planned
        return deny(WRITER_BASH_DENY)
    if role == "other":
        planned = _router_plan_result(role, raw)
        if planned is not None:
            return planned
        return _other_bash(role, d, raw, paths)

    cmd = paths.scrub_command(paths.remove_quoted_spans(raw))
    body = paths.strip_heredoc_bodies(raw)
    scanned = paths.scrub_command(paths.remove_quoted_spans(body))
    broken = _line_break_hooks_deny(raw)
    if broken:
        return deny(broken)
    if cursor_run_ready():
        if cursor_run_exact(raw):
            return 0
        if CURSOR_AGENT_RE.search(cmd):
            return deny(CURSOR_AGENT_DENY)
    if safe_read_exact(raw):
        return 0
    if session_cost_exact(raw):
        return 0
    if _hooks_binary_mentioned(raw, "session-cost"):
        return deny(SESSION_COST_DENY)
    # Exact router-plan is allowed only after the cursor-agent deny.
    planned = _router_plan_result(role, raw)
    if planned is not None:
        return planned
    if READ_CMD.search(_without_stdin_filters(body, scanned, paths.scrub_command)):
        return read_deny("reading files or full diffs through Bash")
    return _orch_bash_document_writes(scanned, paths)


def handle_obsidian(role, d, tool, op, ti, paths, mode, session_id):
    if op in OBSIDIAN_READ_OPS:
        if role == "orchestrator":
            return read_deny("reading the vault from the main thread")
        if role == "file-writer":
            return deny(WRITER_OBSIDIAN_READ_DENY)
        return 0
    if op in OBSIDIAN_WRITE_OPS:
        return handle_write(role, d, tool, ti, paths, mode, session_id)
    return ask(f"Unclassified Obsidian operation '{op}'. Approve manually.")


_PT_ZH = "~/workplace/OlaChat/H5_repo/pt-zh-web-page"
_OBSIDIAN_SCOPE_PREFIX = "mcp__mcp-obsidian__"
_CMD_BEFORE = set(" \t\n\r\"'`|&;<>()=$")
_CMD_AFTER = _CMD_BEFORE | {"/"}


def _remember_spelling(spellings, text):
    if not isinstance(text, str) or text in ("", "~", "/", "."):
        return
    if text not in spellings:
        spellings.append(text)


def _pilot_entry(spec):
    expanded = os.path.expanduser(spec)
    real = os.path.realpath(expanded)
    spellings = []
    _remember_spelling(spellings, real)
    _remember_spelling(spellings, os.path.normpath(expanded))
    if spec == "~" or spec.startswith("~/") or spec.startswith("~\\"):
        _remember_spelling(spellings, spec.replace("\\", "/"))
    home = os.path.normpath(os.path.expanduser("~"))
    norm = os.path.normpath(expanded)
    if norm == home:
        _remember_spelling(spellings, "~")
    elif norm.startswith(home + os.sep):
        _remember_spelling(spellings, "~" + norm[len(home):].replace("\\", "/"))
    return real, spellings


def _scope_entries(vault_roots):
    vaults = [_pilot_entry(root) for root in vault_roots]
    return vaults, vaults + [_pilot_entry(_PT_ZH)]


def _matched_root(path, pilots, under):
    if not isinstance(path, str) or path == "":
        return None
    norm = os.path.normpath(path)
    for root, _spellings in pilots:
        if root and under(norm, root):
            return root
    return None


def session_is_pilot(paths):
    """True when the session project is the vault or the pt-zh pilot."""
    _vaults, pilots = _scope_entries(paths.VAULT_ROOTS)
    return _matched_root(paths.project_dir(), pilots, paths.under) is not None


def _matched_root_pair(literal, real, pilots, under):
    for candidate in (literal, real):
        found = _matched_root(candidate, pilots, under)
        if found:
            return found
    return None


def _read_raw(tool, ti):
    if tool == "Read":
        raw = ti.get("file_path")
    elif tool in ("Grep", "Glob"):
        raw = ti.get("path")
    else:
        raw = None
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw


def _spelling_at(command, spelling):
    if not spelling:
        return None
    start = 0
    while True:
        at = command.find(spelling, start)
        if at < 0:
            return None
        end = at + len(spelling)
        before_ok = at == 0 or command[at - 1] in _CMD_BEFORE
        after_ok = end == len(command) or command[end] in _CMD_AFTER
        if before_ok and after_ok:
            return at
        start = at + 1


def _pilot_in_command(command, pilots):
    best_at = None
    best_root = None
    for root, spellings in pilots:
        for spelling in spellings:
            at = _spelling_at(command, spelling)
            if at is None:
                continue
            if best_at is None or at < best_at:
                best_at = at
                best_root = root
    return best_root


def _bash_pilot(data, ti, paths, pilots):
    cwd = data.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        literal, real = resolve_like(cwd, paths.project_dir)
        found = _matched_root_pair(literal, real, pilots, paths.under)
        if found:
            return found
    command = ti.get("command")
    if not isinstance(command, str) or command == "":
        return None
    return _pilot_in_command(command, pilots)


def _pilot_for_call(tool, ti, paths, vaults, pilots, data):
    if tool.startswith(_OBSIDIAN_SCOPE_PREFIX):
        if not vaults:
            raise RuntimeError("no vault root")
        return vaults[0][0]
    if tool in READ_TOOLS:
        if _read_raw(tool, ti) is None:
            return None
        literal, real = resolve_read_target(tool, ti, paths.project_dir)
        return _matched_root_pair(literal, real, pilots, paths.under)
    if tool in WRITE_TOOLS:
        found = paths.resolve_target(tool, ti)
        if not found:
            return None
        return _matched_root_pair(found[0], found[1], pilots, paths.under)
    if tool == "Bash":
        return _bash_pilot(data, ti, paths, pilots)
    return None


def _absolutize_tool_input(ti, project):
    if not isinstance(ti, dict):
        return
    for key in ("file_path", "notebook_path", "path"):
        raw = ti.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = os.path.expanduser(raw.replace("\\", "/"))
        if os.path.isabs(text):
            continue
        ti[key] = os.path.normpath(os.path.join(project, text))


def _scope_decision(data, paths):
    """"decide", "local", or "stop".

    "stop": the session project is a pilot, or the payload cannot be read.
    "decide": file-reader, file-writer (Explore included, via role_of), an
    Agent spawn, or a call that touches a pilot. A pilot match retargets
    CLAUDE_PROJECT_DIR. "local": the call stays in the session project.
    Agent spawns are decided even when routing-reads is off. Fail open:
    callers catch errors and exit 0.
    """
    vaults, pilots = _scope_entries(paths.VAULT_ROOTS)
    project = paths.project_dir()
    if _matched_root(project, pilots, paths.under):
        return "stop"
    if not isinstance(data, dict):
        return "stop"
    tool = data.get("tool_name")
    if not isinstance(tool, str):
        return "stop"
    tool = tool.strip()
    ti = data.get("tool_input")
    if ti is None:
        ti = {}
    elif not isinstance(ti, dict):
        return "stop"
    role = role_of(data)
    if role in ("file-reader", "file-writer"):
        # Every project, not only pilot paths. Leave CLAUDE_PROJECT_DIR alone.
        # Explore maps to file-reader, so it takes this path too.
        return "decide"
    if tool == "Agent":
        # Spawn policy applies in every non-pilot project, not only when
        # routing-reads is on and not only when the call touches a pilot.
        return "decide"
    matched = _pilot_for_call(tool, ti, paths, vaults, pilots, data)
    if matched is None:
        return "local"
    _absolutize_tool_input(ti, project)
    os.environ["CLAUDE_PROJECT_DIR"] = matched
    return "decide"


def _pilot_scope_continues(data, paths):
    """True when decide() should run against the matched pilot.

    Fail open: callers catch errors and exit 0. A session that already
    started inside a pilot stays quiet. A call that does not touch a pilot
    stays quiet too, except file roles and Agent spawns.
    """
    return _scope_decision(data, paths) == "decide"


def _all_writes_local(data, paths):
    """Document writes in the session project. Reads and other tools stay quiet.

    Orchestrator Bash runs only the document-write check. Other Bash runs
    _other_bash. File roles and pilot touches never reach here.
    """
    tool = data.get("tool_name")
    if not isinstance(tool, str):
        return 0
    tool = tool.strip()
    ti = data.get("tool_input")
    if ti is None:
        ti = {}
    elif not isinstance(ti, dict):
        return 0
    role = role_of(data)
    if tool in WRITE_TOOLS:
        return decide(data, paths)
    if tool != "Bash":
        return 0
    raw = ti.get("command") or ""
    if not isinstance(raw, str):
        return 0
    broken = _line_break_hooks_deny(raw)
    if broken:
        _arm_log(role, data, tool, ti, paths)
        _LOG["command"] = raw
        return deny(broken)
    verdict = router_plan_verdict(role, raw)
    if verdict is not None:
        _arm_log(role, data, tool, ti, paths)
        _LOG["command"] = raw
        if verdict == "allow":
            cmd = paths.scrub_command(paths.remove_quoted_spans(paths.strip_heredoc_bodies(raw)))
            if cursor_run_ready() and CURSOR_AGENT_RE.search(cmd):
                return deny(CURSOR_AGENT_DENY)
            return 0
        if verdict == "main":
            return deny(ROUTER_PLAN_MAIN_ONLY)
        return deny(ROUTER_PLAN_DENY)
    if role == "other":
        _arm_log(role, data, tool, ti, paths)
        _LOG["command"] = raw
        return _other_bash(role, data, raw, paths)
    if role != "orchestrator":
        return 0
    _arm_log(role, data, tool, ti, paths)
    _LOG["command"] = raw
    cmd = paths.scrub_command(paths.remove_quoted_spans(paths.strip_heredoc_bodies(raw)))
    return _orch_bash_document_writes(cmd, paths)


def decide(d, paths):
    if not isinstance(d, dict):
        return 0
    role = role_of(d)
    tool = d.get("tool_name") or ""
    if not isinstance(tool, str):
        return 0
    tool = tool.strip()
    ti = d.get("tool_input")
    if ti is None:
        ti = {}
    elif not isinstance(ti, dict):
        return 0

    _arm_log(role, d, tool, ti, paths)
    mode, _source = paths.resolve_routing_mode()
    session_id = d.get("session_id")

    if tool.startswith(OBSIDIAN_PREFIX):
        return handle_obsidian(role, d, tool, tool[len(OBSIDIAN_PREFIX):], ti, paths, mode, session_id)
    if tool in READ_TOOLS:
        return handle_read(role, d, tool, ti, paths, mode, session_id)
    if tool in WRITE_TOOLS:
        return handle_write(role, d, tool, ti, paths, mode, session_id)
    if tool == "Bash":
        return handle_bash(role, d, ti.get("command") or "", paths)
    if tool == "Agent":
        return handle_agent(role, d, ti, mode)
    return 0


def opus_subagent_deny(effective, source):
    """Name the effective model and where it came from."""
    return (
        'effective model %s (%s); pass model: "sonnet" (or "haiku"), '
        "or use a pinned agent."
        % (effective, source)
    )


def _bind_agent_model():
    """Load the shared resolver beside this file. A copied script still starts."""
    root = os.path.dirname(os.path.realpath(__file__))
    if root not in sys.path:
        sys.path.insert(0, root)
    import _agent_model as agent_model

    return agent_model


try:
    _agent_model_mod = _bind_agent_model()
except Exception:
    _agent_model_mod = None

effective_agent_model = None
if _agent_model_mod is not None:
    _AGENT_TYPE_RE = _agent_model_mod._AGENT_TYPE_RE
    _AGENT_DEF_CAP = _agent_model_mod._AGENT_DEF_CAP
    _model_family = _agent_model_mod._model_family
    _env_subagent_family = _agent_model_mod._env_subagent_family
    _agent_project = _agent_model_mod._agent_project
    _read_frontmatter_model = _agent_model_mod._read_frontmatter_model
    _from_definition_model = _agent_model_mod._from_definition_model
    _lookup_definition = _agent_model_mod._lookup_definition
    effective_agent_model = _agent_model_mod.effective_agent_model
    _per_call_opus = _agent_model_mod._per_call_opus


def _spawn_fields(ti):
    """subagent_type, model, description logged for an Agent call.

    A missing type is general-purpose. A missing model is default. The tool
    input is not rewritten.
    """
    raw_type = ti.get("subagent_type")
    if isinstance(raw_type, str) and raw_type.strip():
        subagent_type = raw_type.strip()
    else:
        subagent_type = "general-purpose"
    raw_model = ti.get("model")
    if isinstance(raw_model, str) and raw_model.strip():
        model = raw_model.strip()
    else:
        model = "default"
    raw_desc = ti.get("description")
    description = raw_desc if isinstance(raw_desc, str) else ""
    return subagent_type, model, description


def handle_agent(role, d, ti, mode):
    """Log every Agent spawn. Strict blocks Explore and an effective Opus.

    Effective Opus is per-call opus, inherit (treated as the main model), or
    an unknown model. task-aware only logs. Never rewrites tool input. Fail
    open: a raise in the caller exits 0. mode is already on the common record.
    A missing model resolver logs one record and allows; the Opus gate decides.
    """
    subagent_type, model, description = _spawn_fields(ti)
    if effective_agent_model is None:
        extra = {
            "subagent_type": subagent_type,
            "model": model,
            "description": description,
            "effective_model": "unknown",
            "effective_source": "import-failed",
        }
        _LOG["extra"] = extra
        append_routing_log(
            role,
            _agent_type(d),
            "Agent",
            _LOG.get("path"),
            decision="allow-log",
            extra=extra,
        )
        return 0
    effective, source = effective_agent_model(d, ti, subagent_type)
    extra = {
        "subagent_type": subagent_type,
        "model": model,
        "description": description,
        "effective_model": effective,
        "effective_source": source,
    }
    _LOG["extra"] = extra
    if mode != "task-aware":
        if subagent_type == "Explore":
            return deny(EXPLORE_AGENT_DENY)
        blocked = effective in ("opus", "inherit-main", "unknown")
        if blocked:
            return deny(opus_subagent_deny(effective, source))
    append_routing_log(
        role,
        _agent_type(d),
        "Agent",
        _LOG.get("path"),
        decision="allow-log",
        extra=extra,
    )
    return 0


def main():
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        return 0

    if is_cursor_runtime(data):
        print(json.dumps({"permission": "allow"}))
        return 0

    if os.environ.get("CLAUDE_ROUTING") == "off":
        return 0

    try:
        from _paths import (
            STRICTNESS,
            VAULT_ROOTS,
            WRITE_HINT,
            _classify,
            classify_path,
            classify_write_target,
            extract_paths_from_command,
            project_dir,
            remove_quoted_spans,
            resolve_routing_mode,
            resolve_routing_reads,
            resolve_target,
            scrub_command,
            strip_heredoc_bodies,
            under,
        )
    except Exception:
        return 0

    paths = SimpleNamespace(
        STRICTNESS=STRICTNESS,
        VAULT_ROOTS=VAULT_ROOTS,
        WRITE_HINT=WRITE_HINT,
        classify=_classify,
        classify_path=classify_path,
        classify_write_target=classify_write_target,
        extract_paths_from_command=extract_paths_from_command,
        project_dir=project_dir,
        remove_quoted_spans=remove_quoted_spans,
        resolve_routing_mode=resolve_routing_mode,
        resolve_routing_reads=resolve_routing_reads,
        resolve_target=resolve_target,
        scrub_command=scrub_command,
        strip_heredoc_bodies=strip_heredoc_bodies,
        under=under,
    )

    try:
        _capture_call_context(data, paths)
        scope = os.environ.get("CLAUDE_ROUTING_SCOPE")
        if scope == "pilots":
            if not _pilot_scope_continues(data, paths):
                return 0
        elif scope == "all-writes":
            kind = _scope_decision(data, paths)
            if kind == "local":
                # Off keeps document writes only. On runs decide() for this
                # project, so read tools, Bash reads and cursor-agent apply.
                reads, _source = paths.resolve_routing_reads()
                if reads == "on":
                    return decide(data, paths)
                return _all_writes_local(data, paths)
            if kind != "decide":
                return 0
        return decide(data, paths)
    except SystemExit:
        raise
    except Exception:
        return 0


def _write_routing_mode_file(mode):
    from _paths import routing_mode_path

    path = routing_mode_path()
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, ".routing-mode.%s.tmp" % os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (mode + "\n").encode("utf-8"))
        os.fsync(fd)
    except Exception:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.close(fd)
    os.chmod(tmp, 0o600)
    try:
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def routing_mode_main(argv):
    args = list(argv[1:])
    if len(args) > 1 or (args and args[0] not in ("status", "strict", "task-aware")):
        print(_ROUTING_MODE_USAGE, file=sys.stderr)
        return 2
    if not args or args[0] == "status":
        from _paths import resolve_routing_mode

        mode, source = resolve_routing_mode()
        print("%s (%s)" % (mode, source))
        return 0
    chosen = args[0]
    try:
        _write_routing_mode_file(chosen)
    except OSError as exc:
        print("routing-mode: %s" % exc, file=sys.stderr)
        return 1
    append_mode_log(chosen)
    from _paths import resolve_routing_mode

    mode, source = resolve_routing_mode()
    print("%s (%s)" % (mode, source))
    return 0


def _write_routing_reads_file(value):
    from _paths import routing_reads_path

    path = routing_reads_path()
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, ".routing-reads.%s.tmp" % os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (value + "\n").encode("utf-8"))
        os.fsync(fd)
    except Exception:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.close(fd)
    os.chmod(tmp, 0o600)
    try:
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def routing_reads_main(argv):
    args = list(argv[1:])
    if len(args) > 1 or (args and args[0] not in ("status", "on", "off")):
        print(_ROUTING_READS_USAGE, file=sys.stderr)
        return 2
    if not args or args[0] == "status":
        from _paths import resolve_routing_reads

        value, source = resolve_routing_reads()
        print("%s (%s)" % (value, source))
        return 0
    chosen = args[0]
    try:
        _write_routing_reads_file(chosen)
    except OSError as exc:
        print("routing-reads: %s" % exc, file=sys.stderr)
        return 1
    append_reads_log(chosen)
    from _paths import resolve_routing_reads

    value, source = resolve_routing_reads()
    print("%s (%s)" % (value, source))
    return 0


if __name__ == "__main__":
    sys.exit(main())
