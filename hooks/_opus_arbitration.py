"""Opus arbitration ledger. Paths come from expanduser("~"); nothing redirects them."""

import contextvars
import datetime
import hashlib
import json
import os
import re
import secrets
import shlex
import sqlite3
import stat
import sys
import time
import unicodedata
from contextlib import contextmanager

import _agent_model

_LIMIT_KEYS = (
    "max_judgments_per_task",
    "max_judgments_per_session",
    "max_opus_worker_invocations_per_task",
    "max_opus_worker_invocations_per_session",
    "max_same_model_retry_after_judgment",
    "grant_ttl_seconds",
    "judge_pending_ttl_seconds",
    "judge_running_ttl_seconds",
    "max_concurrent_judges_per_session",
    "max_concurrent_escalated_workers_per_session",
)
_CANDIDATE_TTL_SECONDS = 1800
_PLANNING_DEFAULTS = {
    "enabled": False,
    "planner_agent_type": "opus-planner",
    "max_plan_versions_per_task": 3,
    "max_replans_per_task": 2,
    "max_steps": 12,
}
_STAGE_FINISH = frozenset({"completed", "failed", "cancelled", "unknown"})
_PURGE_INTERVAL = 3600
_TERMINAL = frozenset({
    "JUDGED_CONTINUE",
    "JUDGED_RETRY",
    "JUDGED_STOP",
    "GRANT_CONSUMED",
    "WORKER_COMPLETED",
    "WORKER_FAILED",
    "WORKER_UNKNOWN",
    "EXPIRED",
    "CANCELLED",
    "FAILED_CLOSED",
    "REPLAN_RESOLVED",
})
_EDGES = {
    ("CANDIDATE", "JUDGE_PENDING"),
    ("CANDIDATE", "REPLAN_PENDING"),
    ("REPLAN_PENDING", "REPLAN_RESOLVED"),
    ("JUDGE_PENDING", "JUDGE_RUNNING"),
    ("JUDGE_RUNNING", "JUDGED_CONTINUE"),
    ("JUDGE_RUNNING", "JUDGED_RETRY"),
    ("JUDGE_RUNNING", "JUDGED_STOP"),
    ("JUDGE_RUNNING", "JUDGED_ESCALATE"),
    ("JUDGED_ESCALATE", "GRANT_READY"),
    ("JUDGED_ESCALATE", "JUDGED_STOP"),
    ("GRANT_READY", "GRANT_CONSUMED"),
    ("GRANT_CONSUMED", "WORKER_COMPLETED"),
    ("GRANT_CONSUMED", "WORKER_FAILED"),
    ("GRANT_CONSUMED", "WORKER_UNKNOWN"),
}
_UPDATE_FIELDS = frozenset({
    "judge_tool_use_id",
    "judge_agent_id",
    "judge_model",
    "decision",
    "expires",
    "packet_digest",
    "packet_text",
    "pending_digest",
    "pending_summary",
})
_DECISION_KEYS = (
    "schema_version",
    "request_id",
    "decision",
    "recommended_model",
    "reason_code",
    "rationale",
    "evidence_used",
    "execution_scope",
    "suggested_next_step",
)
_JUDGE_DECISIONS = frozenset({"CONTINUE", "RETRY", "ESCALATE", "STOP"})
_WORKER_TYPES = frozenset({"general-purpose", "file-reader", "file-writer"})
_AGENT_ID_RE = re.compile(r"^a[0-9a-f]{16,}$")
_SECRET_RES = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[abposr]-[A-Za-z0-9-]+"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*", re.DOTALL),
)
_INVOCATION_SKIP = frozenset({
    "prompt",
    "description",
    "run_in_background",
    "subagent_type",
})
_NEXT = {
    "CONTINUE": "Continue on the current model. Do not call Opus.",
    "RETRY": "Retry once on the same model with a different approach. Do not call Opus.",
    "STOP": "Stop. Record the blocker. Do not call Opus.",
    "ESCALATE": "Re-issue the approved Opus call once. The grant is single-use.",
}
_JUDGE_STUCK_SECONDS = 120
WORKER_NO_DELEGATION = (
    "WORKFLOW_WORKER_NO_DELEGATION; report NEEDS_JUDGMENT in your status footer instead, "
    "and the main session will arbitrate. When a checkpoint shows the plan is wrong, "
    "report NEEDS_JUDGMENT with reason_code PLAN_REVISION_REQUIRED, the stage_id, a narrow "
    "unresolved_question and evidence_refs, then stop. Do not delegate."
)
_WORKFLOW_DEFAULTS = {
    "opus_main_mode": "deny",
    "allowed_worker_families": ["sonnet", "haiku"],
    "max_concurrent_agents_required": 4,
    "max_workflows_per_session": 3,
    "max_workers_per_workflow": 12,
    "max_workers_per_session": 30,
    "require_mod_call_attestation": True,
}
# A workflow recorded before stage_start denied it. The session cap ignores this.
_WORKFLOW_CAP_IGNORED = "denied_stage"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    session_id TEXT,
    project TEXT,
    task_id TEXT,
    candidate_key TEXT UNIQUE,
    trigger TEXT,
    reason_code TEXT,
    requester_agent_type TEXT,
    state TEXT,
    pending_digest TEXT,
    pending_summary TEXT,
    packet_digest TEXT,
    packet_text TEXT,
    judge_tool_use_id TEXT,
    judge_agent_id TEXT,
    judge_model TEXT,
    decision TEXT,
    created REAL,
    updated REAL,
    expires REAL,
    context_json TEXT,
    candidate_type TEXT DEFAULT 'EXECUTOR_ESCALATION',
    plan_task_id TEXT
);
CREATE TABLE IF NOT EXISTS grants (
    grant_id TEXT PRIMARY KEY,
    request_id TEXT,
    session_id TEXT,
    invocation_digest TEXT,
    invocation_json TEXT,
    expires REAL,
    uses_left INTEGER,
    consumed_tool_use_id TEXT,
    consumed_at REAL,
    purpose TEXT DEFAULT 'EXECUTOR'
);
CREATE TABLE IF NOT EXISTS agent_runs (
    agent_id TEXT PRIMARY KEY,
    session_id TEXT,
    project TEXT,
    agent_type TEXT,
    resolved_model TEXT,
    kind TEXT,
    request_id TEXT,
    tool_use_id TEXT,
    status TEXT,
    created REAL,
    name TEXT,
    workflow_id TEXT,
    verified_family TEXT
);
CREATE TABLE IF NOT EXISTS workflows (
    tool_use_id TEXT PRIMARY KEY,
    session_id TEXT,
    prompt_id TEXT,
    project TEXT,
    created REAL,
    expected_family TEXT,
    status TEXT,
    run_id TEXT,
    task_id TEXT,
    workflow_name TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    session_id TEXT,
    task_id TEXT,
    strategy_digest TEXT,
    outcome TEXT,
    created REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    session_id TEXT,
    event TEXT,
    request_id TEXT,
    detail_json TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS plans (
    task_id TEXT,
    version INTEGER,
    content_sha256 TEXT,
    scope_digest TEXT,
    status TEXT,
    session_id TEXT,
    created REAL,
    PRIMARY KEY (task_id, version)
);
CREATE TABLE IF NOT EXISTS stages (
    task_id TEXT,
    plan_version INTEGER,
    stage_id TEXT,
    attempt INTEGER,
    session_id TEXT,
    workflow_tool_use_id TEXT,
    run_id TEXT,
    status TEXT,
    created REAL,
    updated REAL,
    PRIMARY KEY (task_id, plan_version, stage_id, attempt)
);
"""


class TransitionError(Exception):
    """A state change outside the allowed table. The row is FAILED_CLOSED."""


def _home():
    return os.path.expanduser("~")


def ledger_dir():
    return os.path.join(_home(), ".claude", "logs", "opus-arbitration")


def ledger_path():
    return os.path.join(ledger_dir(), "ledger.db")


def project_of(payload):
    """Real path of the payload cwd, or empty when it is missing."""
    if not isinstance(payload, dict):
        return ""
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or cwd.strip() == "":
        return ""
    try:
        return os.path.realpath(os.path.expanduser(cwd.strip()))
    except OSError:
        return ""


def events_path():
    return os.path.join(ledger_dir(), "events.jsonl")


def policy_path():
    return os.path.join(_home(), ".claude", "hooks", "data", "opus-arbitration-policy.json")


def switch_path():
    return os.path.join(_home(), ".claude", "opus-arbitration-mode")


def _pos_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _iso(now=None):
    moment = datetime.datetime.fromtimestamp(now if now is not None else time.time(), datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def redact(text, limit=None):
    """Replace secret-shaped substrings. Optionally cap the length afterwards."""
    if not isinstance(text, str):
        text = ""
    for pattern in _SECRET_RES:
        text = pattern.sub("[REDACTED]", text)
    if limit is not None and len(text) > limit:
        text = text[:limit]
    return text


def _redact_obj(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_obj(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_obj(item) for key, item in value.items()}
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return redact(str(value))


def normalize_text(value):
    """NFC, CRLF to LF, trailing whitespace removed per line and overall."""
    if not isinstance(value, str):
        value = ""
    value = unicodedata.normalize("NFC", value).replace("\r\n", "\n")
    lines = [line.rstrip() for line in value.split("\n")]
    return "\n".join(lines).rstrip()


_EXCERPT_LIMIT = 1500
_EXCERPT_MARK = "…[truncated]"
_LIST_LIMIT = 5
_FIELD_LIMIT = 200
_EVIDENCE_LIMIT = 10


def _bounded_excerpt(text):
    """Redacted, normalized prompt, at most 1,500 characters.

    A longer prompt keeps a prefix and ends with the truncation marker.
    Applying this twice is stable.
    """
    text = redact(normalize_text(text if isinstance(text, str) else ""))
    if len(text) <= _EXCERPT_LIMIT:
        return text
    keep = _EXCERPT_LIMIT - len(_EXCERPT_MARK)
    if keep < 1:
        return _EXCERPT_MARK[:_EXCERPT_LIMIT]
    return text[:keep] + _EXCERPT_MARK


def _clip_scalar(value, limit=_FIELD_LIMIT):
    if isinstance(value, str):
        return redact(value, limit)
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    return redact(str(value), limit)


def _clip_attempts(attempts):
    """At most five attempts. Each string field is at most 200 characters.

    strategy_digest is the first 12 characters, which is what T04 and T05 show.
    """
    if not isinstance(attempts, list):
        return []
    clipped = []
    for item in attempts:
        if len(clipped) >= _LIST_LIMIT:
            break
        if not isinstance(item, dict):
            continue
        row = {}
        for key in sorted(item.keys(), key=str):
            name = str(key)
            value = item[key]
            if name == "strategy_digest" and isinstance(value, str):
                value = value[:12]
            row[name] = _clip_scalar(value)
        clipped.append(row)
    return clipped


def _clip_evidence(evidence):
    """At most ten evidence rows. Each string field is at most 200 characters."""
    if not isinstance(evidence, list):
        return []
    clipped = []
    for item in evidence:
        if len(clipped) >= _EVIDENCE_LIMIT:
            break
        if isinstance(item, str):
            clipped.append(redact(item, _FIELD_LIMIT))
            continue
        if not isinstance(item, dict):
            continue
        row = {}
        for key in sorted(item.keys(), key=str):
            row[str(key)] = _clip_scalar(item[key])
        clipped.append(row)
    return clipped


def _subagent_type(tool_input):
    if not isinstance(tool_input, dict):
        return "general-purpose"
    raw = tool_input.get("subagent_type")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return "general-purpose"


def invocation_digest(session_id, tool_input, family):
    """SHA-256 of the canonical Agent invocation."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    other = {}
    for key in sorted(ti.keys(), key=str):
        if key in _INVOCATION_SKIP:
            continue
        other[str(key)] = ti[key]
    body = {
        "session_id": session_id if isinstance(session_id, str) else "",
        "subagent_type": _subagent_type(ti),
        "family": family if isinstance(family, str) else "",
        "prompt": normalize_text(ti.get("prompt")),
        "description": normalize_text(ti.get("description")),
        "run_in_background": ti.get("run_in_background") is True,
        "other": other,
    }
    blob = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def candidate_key(session_id, task_id, reason_code, scope_digest, failed_requirement_id):
    raw = "|".join([
        session_id or "",
        task_id or "",
        reason_code or "",
        scope_digest or "",
        failed_requirement_id or "",
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def canonicalize_packet(text):
    if not isinstance(text, str):
        text = ""
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n"))
    return text.strip("\n")


def packet_digest(text):
    return hashlib.sha256(canonicalize_packet(text).encode("utf-8")).hexdigest()


def strip_packet_markers(prompt):
    """Accept a judge prompt with or without the <<<PACKET markers."""
    if not isinstance(prompt, str):
        return ""
    text = prompt.replace("\r\n", "\n")
    lines = text.split("\n")
    if lines and lines[0].strip() == "<<<PACKET":
        lines = lines[1:]
    if lines and lines[-1].strip() == "PACKET>>>":
        lines = lines[:-1]
    return "\n".join(lines)


def read_switch():
    """active, or disabled when the file is missing-or-not.

    A missing file is active. Anything that is not a regular file of at most
    64 bytes, or unrecognised content, is disabled. The open is non-blocking
    so a FIFO cannot stall the hook.
    """
    path = switch_path()
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "active"
    except OSError:
        return "disabled"
    if not stat.S_ISREG(info.st_mode) or info.st_size > 64:
        return "disabled"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return "disabled"
    try:
        try:
            blob = os.read(fd, 64)
        except OSError:
            return "disabled"
    finally:
        os.close(fd)
    text = blob.decode("utf-8", errors="replace")
    line = text.splitlines()[0].strip() if text.splitlines() else ""
    if line in ("active", "disabled"):
        return line
    return "disabled"


def write_switch(value):
    """Atomically replace the switch file. Does not touch grants."""
    if value not in ("active", "disabled"):
        raise ValueError(value)
    path = switch_path()
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, ".opus-arbitration-mode.%s.tmp" % os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
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


def load_policy():
    """Parsed policy, or None when the file is missing or invalid.

    None means arbitration is disabled: deny Opus and do not run a judge.
    """
    path = policy_path()
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        try:
            blob = os.read(fd, 65537)
        except OSError:
            return None
    finally:
        os.close(fd)
    if len(blob) > 65536:
        return None
    try:
        data = json.loads(blob.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return None
    if not isinstance(data.get("enabled"), bool):
        return None
    models = data.get("accepted_opus_models")
    if not isinstance(models, list) or not models or not all(isinstance(item, str) and item for item in models):
        return None
    if not isinstance(data.get("judge_agent_type"), str) or data.get("judge_agent_type") == "":
        return None
    limits = data.get("limits")
    if not isinstance(limits, dict) or any(not _pos_int(limits.get(key)) for key in _LIMIT_KEYS):
        return None
    if "candidate_ttl_seconds" in limits and not _pos_int(limits.get("candidate_ttl_seconds")):
        return None
    unattended = data.get("unattended")
    if not isinstance(unattended, dict):
        return None
    for key, value in unattended.items():
        if key in _LIMIT_KEYS and not _pos_int(value):
            return None
    workflows = data.get("mandatory_review_workflows")
    if not isinstance(workflows, list) or not all(isinstance(item, str) for item in workflows):
        return None
    if not _pos_int(data.get("retention_days")):
        return None
    return data


def _candidate_ttl(policy):
    """Seconds a CANDIDATE may sit with no follow-through. Default 1800."""
    if not isinstance(policy, dict):
        return _CANDIDATE_TTL_SECONDS
    limits = policy.get("limits")
    if not isinstance(limits, dict) or "candidate_ttl_seconds" not in limits:
        return _CANDIDATE_TTL_SECONDS
    value = limits.get("candidate_ttl_seconds")
    if _pos_int(value):
        return value
    return _CANDIDATE_TTL_SECONDS


def effective_limits(policy):
    """Policy limits. Unattended env may only tighten a numeric limit."""
    limits = {key: policy["limits"][key] for key in _LIMIT_KEYS}
    if os.environ.get("CLAUDE_ROUTING_UNATTENDED") == "1":
        for key, value in policy.get("unattended", {}).items():
            if key in limits and _pos_int(value):
                limits[key] = min(limits[key], value)
    return limits


def _routing_mode():
    try:
        from _paths import resolve_routing_mode

        mode, _source = resolve_routing_mode()
        return mode
    except Exception:
        return "unknown"


def _chmod_store():
    directory = ledger_dir()
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    for name in ("ledger.db", "ledger.db-wal", "ledger.db-shm", "events.jsonl"):
        path = os.path.join(directory, name)
        if os.path.lexists(path):
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass


def _ensure_store():
    os.makedirs(ledger_dir(), exist_ok=True)
    os.chmod(ledger_dir(), 0o700)


def _append_jsonl(payload):
    _ensure_store()
    path = events_path()
    line = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


_HOLDER = contextvars.ContextVar("opus_ledger_holder", default=None)


class GateBusy(Exception):
    """A ledger call would start after the gate deadline."""


def _open_conn(busy_ms):
    """One autocommit connection. busy_ms is the SQLite busy timeout."""
    _ensure_store()
    conn = sqlite3.connect(ledger_path(), isolation_level=None, timeout=busy_ms / 1000.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=%d" % int(busy_ms))
        conn.execute("PRAGMA journal_mode=WAL").fetchone()
        conn.executescript(_SCHEMA)
        _migrate(conn)
        _chmod_store()
        return conn
    except Exception:
        conn.close()
        raise


@contextmanager
def invocation(deadline=None):
    """Reuse one SQLite connection for every ledger call in this gate or post run.

    The connection opens on the first ledger call. deadline is seconds from
    now; a ledger call that would start after it raises GateBusy. busy_timeout
    for this connection is 1000 ms.
    """
    holder = {
        "conn": None,
        "deadline": None if deadline is None else time.monotonic() + float(deadline),
    }
    token = _HOLDER.set(holder)
    try:
        yield holder
    finally:
        _HOLDER.reset(token)
        conn = holder.get("conn")
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            _chmod_store()


def remaining_budget():
    """Seconds left in the current gate or post deadline, or None."""
    holder = _HOLDER.get()
    if holder is None or holder.get("deadline") is None:
        return None
    return holder["deadline"] - time.monotonic()


@contextmanager
def _db():
    holder = _HOLDER.get()
    if holder is not None:
        if holder["deadline"] is not None and time.monotonic() >= holder["deadline"]:
            raise GateBusy()
        if holder["conn"] is None:
            holder["conn"] = _open_conn(1000)
        yield holder["conn"]
        return
    conn = _open_conn(3000)
    try:
        yield conn
    finally:
        conn.close()
        _chmod_store()


class _Tx:
    def __init__(self, conn):
        self.conn = conn
        self.began = False

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        self.began = True
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if not self.began:
            return False
        if exc_type is None or exc_type is TransitionError:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False


def _request_dict(row):
    if row is None:
        return None
    data = {key: row[key] for key in row.keys()}
    raw = data.get("context_json")
    if isinstance(raw, str) and raw:
        try:
            extra = json.loads(raw)
        except json.JSONDecodeError:
            extra = None
        if isinstance(extra, dict):
            for key, value in extra.items():
                data.setdefault(key, value)
    return data


def _migrate(conn):
    """Add columns introduced after the first ledger schema."""
    for table in ("requests", "agent_runs"):
        found = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
        if "project" not in found:
            conn.execute("ALTER TABLE %s ADD COLUMN project TEXT" % table)
    requests = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
    if "packet_text" not in requests:
        conn.execute("ALTER TABLE requests ADD COLUMN packet_text TEXT")
    grants = {row[1] for row in conn.execute("PRAGMA table_info(grants)")}
    if "invocation_json" not in grants:
        conn.execute("ALTER TABLE grants ADD COLUMN invocation_json TEXT")
    runs = {row[1] for row in conn.execute("PRAGMA table_info(agent_runs)")}
    if "name" not in runs:
        conn.execute("ALTER TABLE agent_runs ADD COLUMN name TEXT")
    if "workflow_id" not in runs:
        conn.execute("ALTER TABLE agent_runs ADD COLUMN workflow_id TEXT")
    if "verified_family" not in runs:
        conn.execute("ALTER TABLE agent_runs ADD COLUMN verified_family TEXT")
    requests = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
    if "candidate_type" not in requests:
        conn.execute(
            "ALTER TABLE requests ADD COLUMN candidate_type TEXT DEFAULT 'EXECUTOR_ESCALATION'"
        )
    if "plan_task_id" not in requests:
        conn.execute("ALTER TABLE requests ADD COLUMN plan_task_id TEXT")
    grants = {row[1] for row in conn.execute("PRAGMA table_info(grants)")}
    if "purpose" not in grants:
        conn.execute("ALTER TABLE grants ADD COLUMN purpose TEXT DEFAULT 'EXECUTOR'")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS plans (
            task_id TEXT,
            version INTEGER,
            content_sha256 TEXT,
            scope_digest TEXT,
            status TEXT,
            session_id TEXT,
            created REAL,
            PRIMARY KEY (task_id, version)
        );
        CREATE TABLE IF NOT EXISTS stages (
            task_id TEXT,
            plan_version INTEGER,
            stage_id TEXT,
            attempt INTEGER,
            session_id TEXT,
            workflow_tool_use_id TEXT,
            run_id TEXT,
            status TEXT,
            created REAL,
            updated REAL,
            PRIMARY KEY (task_id, plan_version, stage_id, attempt)
        );
        CREATE TABLE IF NOT EXISTS surface_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            line TEXT,
            created REAL
        );
        """
    )


def _event(conn, event, request_id=None, session_id=None, task_id=None, trigger=None, extra=None, project=None):
    row = None
    if request_id:
        row = conn.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
    if row is not None:
        if session_id is None:
            session_id = row["session_id"]
        if task_id is None:
            task_id = row["task_id"]
        if trigger is None:
            trigger = row["trigger"]
        if project is None and "project" in row.keys():
            project = row["project"]
    if not isinstance(project, str):
        project = ""
    safe = {}
    for key, value in (extra or {}).items():
        if key in ("prompt", "message", "content", "text", "command"):
            continue
        if isinstance(value, str):
            safe[key] = redact(value, 200)
        elif isinstance(value, (int, float, bool)) or value is None:
            safe[key] = value
        else:
            safe[key] = redact(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str), 200)
    now = time.time()
    ts = _iso(now)
    detail = json.dumps(safe, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    conn.execute(
        "INSERT INTO events (ts, session_id, event, request_id, detail_json) VALUES (?,?,?,?,?)",
        (ts, session_id, event, request_id, detail),
    )
    payload = {
        "schema_version": 2,
        "ts": ts,
        "event": event,
        "session_id": session_id,
        "task_id": task_id,
        "request_id": request_id,
        "trigger": trigger,
        "mode": _routing_mode(),
        "project": project,
    }
    payload.update(safe)
    _append_jsonl(payload)


def record_event(event, session_id=None, request_id=None, task_id=None, trigger=None, extra=None, project=None):
    with _db() as conn:
        with _Tx(conn):
            _sweep(conn, time.time())
            _event(conn, event, request_id, session_id, task_id, trigger, extra, project)


def _allowed(old, new):
    if new in ("EXPIRED", "CANCELLED", "FAILED_CLOSED"):
        return True
    return (old, new) in _EDGES


def _transition(conn, request_id, new_state, **fields):
    row = conn.execute("SELECT state FROM requests WHERE request_id=?", (request_id,)).fetchone()
    if row is None:
        raise TransitionError("missing request")
    old = row["state"]
    unknown = set(fields) - _UPDATE_FIELDS
    if unknown:
        raise TransitionError("bad field")
    if not _allowed(old, new_state):
        now = time.time()
        if old != "FAILED_CLOSED":
            conn.execute(
                "UPDATE requests SET state='FAILED_CLOSED', updated=? WHERE request_id=?",
                (now, request_id),
            )
            _event(
                conn,
                "failed_closed",
                request_id=request_id,
                extra={"from_state": old, "to_state": new_state},
            )
        raise TransitionError("%s -> %s" % (old, new_state))
    now = time.time()
    assignments = ["state=?", "updated=?"]
    values = [new_state, now]
    for key, value in fields.items():
        assignments.append(key + "=?")
        values.append(value)
    values.append(request_id)
    conn.execute(
        "UPDATE requests SET %s WHERE request_id=?" % ", ".join(assignments),
        values,
    )


def transition_to(request_id, new_state, **fields):
    """Apply one transition. An illegal edge commits FAILED_CLOSED and raises."""
    with _db() as conn:
        with _Tx(conn):
            _sweep(conn, time.time())
            _transition(conn, request_id, new_state, **fields)


def _last_purge(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='last_purge'").fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return None


def _mark_purged(conn, now):
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('last_purge', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(now),),
    )


def _purge(conn, now, policy):
    if not policy:
        return
    last = _last_purge(conn)
    if last is not None and now - last < _PURGE_INTERVAL:
        return
    cutoff = now - policy["retention_days"] * 86400
    cutoff_iso = _iso(cutoff)
    conn.execute(
        "DELETE FROM requests WHERE created < ? AND state IN (%s)"
        % ",".join("?" for _ in _TERMINAL),
        (cutoff, *_TERMINAL),
    )
    conn.execute("DELETE FROM events WHERE ts < ?", (cutoff_iso,))
    conn.execute("DELETE FROM attempts WHERE created < ?", (cutoff,))
    conn.execute(
        "DELETE FROM grants WHERE request_id NOT IN (SELECT request_id FROM requests)"
    )
    conn.execute("DELETE FROM agent_runs WHERE created < ?", (cutoff,))
    conn.execute("DELETE FROM workflows WHERE created < ?", (cutoff,))
    _mark_purged(conn, now)


def _expire_follow_through(conn, request_id, from_state, decision=None):
    fields = {}
    if decision is not None:
        fields["decision"] = decision
    _transition(conn, request_id, "EXPIRED", **fields)
    _event(conn, "no_follow_through", request_id=request_id, extra={"from_state": from_state})


def _sweep(conn, now):
    policy = load_policy()
    if policy:
        _purge(conn, now, policy)
    ttl = _candidate_ttl(policy)
    stale = conn.execute(
        """SELECT request_id, state FROM requests
           WHERE state IN ('CANDIDATE', 'JUDGE_PENDING', 'REPLAN_PENDING')
             AND (
                 (expires IS NOT NULL AND expires < ?)
                 OR (state='CANDIDATE' AND expires IS NULL AND COALESCE(created, 0) + ? < ?)
             )""",
        (now, ttl, now),
    ).fetchall()
    for row in stale:
        _expire_follow_through(conn, row["request_id"], row["state"], "NO_FOLLOW_THROUGH")
    ready = conn.execute(
        """SELECT request_id FROM requests
           WHERE state='GRANT_READY' AND expires IS NOT NULL AND expires < ?""",
        (now,),
    ).fetchall()
    for row in ready:
        _expire_follow_through(conn, row["request_id"], "GRANT_READY")
    conn.execute(
        """UPDATE grants SET uses_left=0
           WHERE expires < ? AND uses_left > 0
             AND request_id IN (SELECT request_id FROM requests WHERE state='EXPIRED')""",
        (now,),
    )
    running_ttl = 900
    if policy:
        running_ttl = effective_limits(policy)["judge_running_ttl_seconds"]
    cutoff = now - running_ttl
    stuck = conn.execute(
        """SELECT request_id FROM requests
           WHERE state='JUDGE_RUNNING' AND COALESCE(updated, created, 0) < ?""",
        (cutoff,),
    ).fetchall()
    for row in stuck:
        _fail_closed(conn, row["request_id"], "judge_timeout")


# Opus-main replans never enter the judge. promote_pending is the only writer
# of packet_digest, and it runs on the transition into JUDGE_PENDING. The judge
# run then stores judge_tool_use_id. An Opus-main REPLAN_REQUEST takes
# REPLAN_PENDING instead, so both columns stay null through REPLAN_RESOLVED and
# EXPIRED. Those rows did not spend a judgment. A Sonnet-main replan that did
# reach the judge has a packet_digest and still counts. No extra column: the
# two nulls are the marker that the judge path was never entered.
_NOT_UNJUDGED_REPLAN = (
    "AND NOT (IFNULL(candidate_type,'')='REPLAN_REQUEST' "
    "AND packet_digest IS NULL AND judge_tool_use_id IS NULL)"
)


def _judgment_blocked(conn, limits, session_id, task_id):
    not_replan = _NOT_UNJUDGED_REPLAN
    session_n = conn.execute(
        "SELECT COUNT(*) AS n FROM requests WHERE session_id=? AND IFNULL(state,'')!='CANCELLED' "
        + not_replan,
        (session_id,),
    ).fetchone()["n"]
    task_n = conn.execute(
        """SELECT COUNT(*) AS n FROM requests
           WHERE session_id=? AND task_id=? AND IFNULL(state,'')!='CANCELLED' """
        + not_replan,
        (session_id, task_id),
    ).fetchone()["n"]
    if session_n >= limits["max_judgments_per_session"]:
        return True
    if task_n >= limits["max_judgments_per_task"]:
        return True
    return False


def _mark_stage_needs_judgment(conn, binding):
    """Move the latest started attempt to needs_judgment. binding is (task, version, stage)."""
    if not isinstance(binding, (tuple, list)) or len(binding) != 3:
        return False
    task_id, plan_version, stage_id = binding
    now = time.time()
    row = conn.execute(
        """SELECT attempt FROM stages
           WHERE task_id=? AND plan_version=? AND stage_id=? AND status='started'
           ORDER BY attempt DESC LIMIT 1""",
        (task_id, plan_version, stage_id),
    ).fetchone()
    if row is None:
        return False
    conn.execute(
        """UPDATE stages SET status='needs_judgment', updated=?
           WHERE task_id=? AND plan_version=? AND stage_id=? AND attempt=?""",
        (now, task_id, plan_version, stage_id, row["attempt"]),
    )
    return True


def _queue_surface(conn, session_id, line):
    conn.execute(
        "INSERT INTO surface_lines (session_id, line, created) VALUES (?,?,?)",
        (session_id if isinstance(session_id, str) else "", line, time.time()),
    )


def register_candidate(
    session_id,
    task_id,
    trigger,
    reason_code,
    requester_agent_type="",
    scope_digest="",
    failed_requirement_id="",
    pending_digest=None,
    summary="",
    task_summary="",
    current_model_observed="",
    approved_role_constraints="",
    objective_failure_evidence=None,
    prior_attempts=None,
    allowed_scope_digest="",
    project="",
    task_excerpt="",
    description="",
    candidate_type="EXECUTOR_ESCALATION",
    plan_task_id=None,
    key_override=None,
    skip_judgment_quota=False,
    mark_stage=None,
    follow_state=None,
    replan_cap=None,
    surface_line=None,
    context_extra=None,
):
    """Return the open request for this key, a new one, or QUOTA_EXHAUSTED."""
    policy = load_policy()
    if policy is None or not policy.get("enabled") or read_switch() != "active":
        return {"ok": False, "reason": "DISABLED", "request": None}
    if isinstance(key_override, str) and key_override:
        key = key_override
    else:
        key = candidate_key(session_id, task_id, reason_code, scope_digest, failed_requirement_id)
    shown_description = description or summary or task_summary or ""
    context = {
        "task_summary": redact(task_summary or summary or "", 600),
        "description": redact(normalize_text(shown_description), 200),
        "task_excerpt": _bounded_excerpt(task_excerpt),
        "approved_role_constraints": redact(approved_role_constraints or ""),
        "objective_failure_evidence": _clip_evidence(objective_failure_evidence or []),
        "prior_attempts": _clip_attempts(prior_attempts or []),
        "allowed_scope_digest": allowed_scope_digest or "",
        "current_model_observed": current_model_observed or "",
        "failed_requirement_id": failed_requirement_id or "",
        "scope_digest": scope_digest or "",
    }
    if isinstance(context_extra, dict):
        for extra_key, extra_value in context_extra.items():
            context[str(extra_key)] = extra_value
    with _db() as conn:
        with _Tx(conn):
            now = time.time()
            _sweep(conn, now)
            existing = conn.execute(
                "SELECT * FROM requests WHERE candidate_key=?", (key,)
            ).fetchone()
            if existing is not None and existing["state"] not in _TERMINAL:
                if mark_stage is not None:
                    _mark_stage_needs_judgment(conn, mark_stage)
                _event(conn, "candidate_deduped", request_id=existing["request_id"])
                return {"ok": True, "created": False, "request": _request_dict(existing), "reason": None}
            if existing is not None:
                decided = existing["decision"] or existing["state"]
                _event(
                    conn,
                    "already_decided",
                    request_id=existing["request_id"],
                    extra={"decision": decided},
                )
                return {
                    "ok": False,
                    "reason": "ALREADY_DECIDED",
                    "decision": decided,
                    "request": _request_dict(existing),
                }
            if type(replan_cap) is int:
                counted = conn.execute(
                    """SELECT COUNT(*) AS n FROM requests
                       WHERE plan_task_id=? AND candidate_type='REPLAN_REQUEST'
                         AND IFNULL(state,'')!='CANCELLED'""",
                    (plan_task_id if isinstance(plan_task_id, str) and plan_task_id else task_id,),
                ).fetchone()["n"]
                if counted >= replan_cap:
                    if mark_stage is not None:
                        _mark_stage_needs_judgment(conn, mark_stage)
                    if isinstance(surface_line, str) and surface_line:
                        _queue_surface(conn, session_id, surface_line)
                    _event(
                        conn,
                        "replan_limit",
                        session_id=session_id,
                        task_id=task_id,
                        extra={"reason": "replan_limit"},
                    )
                    return {"ok": False, "reason": "REPLAN_BLOCKED", "request": None}
            limits = effective_limits(policy)
            candidate_expires = now + _candidate_ttl(policy)
            if not skip_judgment_quota and _judgment_blocked(conn, limits, session_id, task_id):
                _event(
                    conn,
                    "quota_exhausted",
                    session_id=session_id,
                    task_id=task_id,
                    trigger=trigger,
                    extra={"reason": "QUOTA_EXHAUSTED"},
                )
                return {"ok": False, "reason": "QUOTA_EXHAUSTED", "request": None}
            request_id = "esc-" + secrets.token_hex(8)
            stored_project = project if isinstance(project, str) else ""
            stored_type = candidate_type if isinstance(candidate_type, str) and candidate_type else "EXECUTOR_ESCALATION"
            stored_plan_task = plan_task_id if isinstance(plan_task_id, str) and plan_task_id else None
            conn.execute(
                """INSERT INTO requests (
                       request_id, session_id, project, task_id, candidate_key, trigger, reason_code,
                       requester_agent_type, state, pending_digest, pending_summary,
                       created, updated, expires, context_json, candidate_type, plan_task_id
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    request_id,
                    session_id,
                    stored_project,
                    task_id,
                    key,
                    trigger,
                    reason_code,
                    requester_agent_type,
                    "CANDIDATE",
                    pending_digest,
                    redact(summary or "", 200),
                    now,
                    now,
                    candidate_expires,
                    json.dumps(context, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
                    stored_type,
                    stored_plan_task,
                ),
            )
            _event(conn, "candidate_registered", request_id=request_id, extra={"summary": redact(summary or "", 200)})
            if mark_stage is not None:
                _mark_stage_needs_judgment(conn, mark_stage)
            if follow_state == "REPLAN_PENDING":
                _transition(conn, request_id, "REPLAN_PENDING")
            row = conn.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
            return {"ok": True, "created": True, "request": _request_dict(row), "reason": None}


def _packet_body(request):
    """Canonical packet object. Same inputs always produce the same object."""
    if not isinstance(request, dict):
        request = {}
    description = request.get("description")
    if not isinstance(description, str) or description == "":
        description = request.get("pending_summary") or request.get("task_summary") or ""
    return {
        "schema_version": 1,
        "request_id": request.get("request_id") or "",
        "session_id": request.get("session_id") or "",
        "task_id": request.get("task_id") or "",
        "candidate_key": request.get("candidate_key") or "",
        "trigger": request.get("trigger") or "",
        "requester_agent_type": request.get("requester_agent_type") or "",
        # The model that would otherwise run, not the model the caller asked for.
        # T02 stores the resolver baseline (general-purpose env default, a
        # definition's own model, otherwise the main-session model id or family).
        # Footer and validator candidates store the worker's resolvedModel.
        "current_model_observed": request.get("current_model_observed") or "",
        "requested_target_model": "opus",
        "description": redact(normalize_text(description), 200),
        "task_excerpt": _bounded_excerpt(request.get("task_excerpt") or ""),
        "task_summary": redact(request.get("task_summary") or request.get("pending_summary") or "", 600),
        "approved_role_constraints": redact(request.get("approved_role_constraints") or ""),
        "objective_failure_evidence": _clip_evidence(request.get("objective_failure_evidence") or []),
        "prior_attempts": _clip_attempts(request.get("prior_attempts") or []),
        "allowed_scope_digest": request.get("allowed_scope_digest") or "",
    }


def evidence_sizes(request):
    """Character and row counts for the packet. Never the evidence text."""
    body = _packet_body(request)
    return {
        "task_excerpt_chars": len(body.get("task_excerpt") or ""),
        "description_chars": len(body.get("description") or ""),
        "prior_attempts_count": len(body.get("prior_attempts") or []),
        "objective_failure_evidence_count": len(body.get("objective_failure_evidence") or []),
    }


def _requested_capability(request):
    kind = request.get("candidate_type") if isinstance(request, dict) else None
    if kind in ("PLAN_REQUEST", "REPLAN_REQUEST"):
        return "PLAN_ONLY"
    return "EXECUTOR"


def build_judge_packet(request):
    """Deterministic judge prompt body. No markers.

    current_model_observed is the model that would otherwise run, not the
    requested Opus family. See _packet_body. The digest is the hash of this
    final text, including the candidate_type and requested_capability lines.
    """
    if not isinstance(request, dict):
        request = {}
    obj = _packet_body(request)
    body = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    kind = request.get("candidate_type") or "EXECUTOR_ESCALATION"
    text = (
        "ARBITRATION-PACKET request_id=%s\n%s\n"
        "candidate_type: %s\n"
        "requested_capability: %s\n"
        "Return only the decision JSON described in your instructions."
        % (obj["request_id"], body, kind, _requested_capability(request))
    )
    return canonicalize_packet(text)


def format_candidate_request(request, permission_mode=None):
    """ARBITRATION_CANDIDATE text. Auto mode carries the full packet."""
    if not isinstance(request, dict) or request.get("state") != "JUDGE_PENDING":
        return ""
    request_id = request.get("request_id") or ""
    trigger = request.get("trigger") or ""
    kind_note = ""
    if request.get("candidate_type") == "REPLAN_REQUEST":
        kind_note = " candidate_type: REPLAN_REQUEST."
    if permission_mode == "auto":
        packet = request.get("packet_text") or ""
        return (
            "ARBITRATION_CANDIDATE request_id=%s trigger=%s.%s Launch the Agent tool now, "
            'foreground, subagent_type "opus-judge", description "Judge %s", and copy this prompt exactly:\n'
            "<<<PACKET\n%s\nPACKET>>>"
            % (request_id, trigger, kind_note, request_id, packet)
        )
    return (
        "ARBITRATION_CANDIDATE request_id=%s trigger=%s.%s Launch the Agent tool now, "
        'foreground, subagent_type "opus-judge", description "Judge %s", '
        'prompt exactly "ARBITRATION-PACKET request_id=%s" (the hook inserts the full packet).'
        % (request_id, trigger, kind_note, request_id, request_id)
    )


def promote_pending(request_id):
    """Move CANDIDATE to JUDGE_PENDING and store the redacted packet."""
    policy = load_policy()
    ttl = 600
    if policy:
        ttl = effective_limits(policy)["judge_pending_ttl_seconds"]
    with _db() as conn:
        with _Tx(conn):
            now = time.time()
            _sweep(conn, now)
            row = conn.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                return None
            current = _request_dict(row)
            if current["state"] != "CANDIDATE":
                return current
            packet = build_judge_packet(current)
            digest = packet_digest(packet)
            _transition(
                conn,
                request_id,
                "JUDGE_PENDING",
                packet_digest=digest,
                packet_text=packet,
                expires=now + ttl,
            )
            pending_extra = {"packet_digest": digest}
            pending_extra.update(evidence_sizes(current))
            _event(conn, "judge_pending", request_id=request_id, extra=pending_extra)
            fresh = conn.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
            return _request_dict(fresh)


def plan_task_from_prompt(prompt):
    """Task id from a leading ROUTER-TASK line, or None when it does not match."""
    if not isinstance(prompt, str) or prompt == "":
        return None
    lines = prompt.splitlines()
    if not lines:
        return None
    match = re.fullmatch(r"ROUTER-TASK[ \t]+([A-Za-z0-9._-]{1,64})", lines[0])
    if match is None:
        return None
    return match.group(1)


def register_unapproved(
    session_id, tool_input, family, observed, project="",
    candidate_type="EXECUTOR_ESCALATION", plan_task_id=None,
):
    """T02: record the exact Opus call and return its judge packet."""
    digest = invocation_digest(session_id, tool_input, family)
    subagent_type = _subagent_type(tool_input)
    raw_tool = tool_input if isinstance(tool_input, dict) else {}
    description = normalize_text(raw_tool.get("description") if isinstance(raw_tool.get("description"), str) else "")
    prompt = raw_tool.get("prompt") if isinstance(raw_tool.get("prompt"), str) else ""
    task_hash = hashlib.sha256((subagent_type + "|" + description).encode("utf-8")).hexdigest()[:16]
    result = register_candidate(
        session_id=session_id,
        task_id="agent:" + task_hash,
        trigger="T02",
        reason_code="UNAPPROVED_OPUS",
        requester_agent_type=subagent_type,
        scope_digest=digest,
        pending_digest=digest,
        summary=description,
        task_summary=description,
        description=description,
        task_excerpt=prompt,
        # observed is the non-Opus baseline, not the requested family.
        current_model_observed=observed or "",
        allowed_scope_digest=digest,
        project=project if isinstance(project, str) else "",
        candidate_type=candidate_type,
        plan_task_id=plan_task_id,
    )
    if not result["ok"]:
        return result
    request = result["request"]
    if request.get("state") == "CANDIDATE":
        request = promote_pending(request["request_id"])
        result["request"] = request
    result["packet"] = build_judge_packet(request)
    return result


def _judge_stuck(row, now):
    """True when a running judge has had no completing PostToolUse for 120s."""
    if row is None:
        return False
    stamp = row["updated"]
    if not isinstance(stamp, (int, float)):
        stamp = row["created"]
    if not isinstance(stamp, (int, float)):
        return False
    return (now - float(stamp)) >= _JUDGE_STUCK_SECONDS


def _reset_running_judge(conn, request_id, tool_use_id, now):
    """Point a stuck running judge at a new tool use without leaving JUDGE_RUNNING."""
    conn.execute(
        "UPDATE requests SET judge_tool_use_id=?, updated=? WHERE request_id=? AND state='JUDGE_RUNNING'",
        (tool_use_id or "", now, request_id),
    )
    _event(
        conn,
        "judge_running",
        request_id=request_id,
        extra={"tool_use_id": tool_use_id or "", "retry": True},
    )


def begin_judge(session_id, prompt, tool_use_id):
    """Match a pending packet and mark the judge running. None if it does not match.

    A JUDGE_RUNNING row with no result for 120 seconds accepts one retry and
    resets judge_tool_use_id. The running TTL still fail-closes the row later.
    """
    policy = load_policy()
    if policy is None or not policy.get("enabled") or read_switch() != "active":
        return None
    canon = canonicalize_packet(strip_packet_markers(prompt))
    digest = packet_digest(canon)
    first = canon.split("\n", 1)[0] if canon else ""
    with _db() as conn:
        with _Tx(conn):
            now = time.time()
            _sweep(conn, now)
            rows = conn.execute(
                "SELECT * FROM requests WHERE session_id=? AND state IN ('JUDGE_PENDING', 'JUDGE_RUNNING')",
                (session_id,),
            ).fetchall()
            match = None
            for row in rows:
                marker = "request_id=%s" % row["request_id"]
                if marker in first and row["packet_digest"] == digest:
                    match = row
                    break
            if match is None:
                return None
            if match["state"] == "JUDGE_RUNNING":
                if not _judge_stuck(match, now):
                    return None
                _reset_running_judge(conn, match["request_id"], tool_use_id or "", now)
            else:
                limits = effective_limits(policy)
                running = conn.execute(
                    "SELECT COUNT(*) AS n FROM requests WHERE session_id=? AND state='JUDGE_RUNNING'",
                    (session_id,),
                ).fetchone()["n"]
                if running >= limits["max_concurrent_judges_per_session"]:
                    return None
                _transition(conn, match["request_id"], "JUDGE_RUNNING", judge_tool_use_id=tool_use_id or "")
                _event(conn, "judge_running", request_id=match["request_id"], extra={"tool_use_id": tool_use_id or ""})
            fresh = conn.execute(
                "SELECT * FROM requests WHERE request_id=?", (match["request_id"],)
            ).fetchone()
            return _request_dict(fresh)


def _reference_id(prompt, label):
    """Request id when the prompt is exactly the short reference, else None.

    Markers are stripped and the text is normalized. Trailing whitespace is
    ignored. Any other character keeps the prompt on the exact-copy path.
    """
    text = normalize_text(strip_packet_markers(prompt if isinstance(prompt, str) else ""))
    prefix = label + " request_id="
    if not text.startswith(prefix):
        return None
    request_id = text[len(prefix):]
    if request_id == "" or any(ch.isspace() for ch in request_id):
        return None
    return request_id


def packet_reference_id(prompt):
    """Id of an ARBITRATION-PACKET reference, or None when it is not one."""
    return _reference_id(prompt, "ARBITRATION-PACKET")


def grant_reference_id(prompt):
    """Id of an ARBITRATION-GRANT reference, or None when it is not one."""
    return _reference_id(prompt, "ARBITRATION-GRANT")


def begin_judge_reference(session_id, request_id, tool_use_id):
    """Mark one pending request running from its short packet reference.

    The request must be JUDGE_PENDING in this session and still hold packet_text.
    Unknown ids, other sessions, and any other state return None and do not change
    the row.
    """
    policy = load_policy()
    if policy is None or not policy.get("enabled") or read_switch() != "active":
        return None
    if not isinstance(session_id, str) or not isinstance(request_id, str) or request_id == "":
        return None
    if not isinstance(tool_use_id, str) or tool_use_id == "":
        return None
    with _db() as conn:
        with _Tx(conn):
            now = time.time()
            _sweep(conn, now)
            row = conn.execute(
                "SELECT * FROM requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None or row["session_id"] != session_id:
                return None
            if row["state"] == "JUDGE_RUNNING":
                if not _judge_stuck(row, now):
                    return None
                packet = row["packet_text"] if "packet_text" in row.keys() else None
                if not isinstance(packet, str) or packet == "":
                    return None
                _reset_running_judge(conn, request_id, tool_use_id, now)
                fresh = conn.execute(
                    "SELECT * FROM requests WHERE request_id=?", (request_id,)
                ).fetchone()
                return _request_dict(fresh)
            if row["state"] != "JUDGE_PENDING":
                return None
            packet = row["packet_text"] if "packet_text" in row.keys() else None
            if not isinstance(packet, str) or packet == "":
                return None
            limits = effective_limits(policy)
            running = conn.execute(
                "SELECT COUNT(*) AS n FROM requests WHERE session_id=? AND state='JUDGE_RUNNING'",
                (session_id,),
            ).fetchone()["n"]
            if running >= limits["max_concurrent_judges_per_session"]:
                return None
            _transition(conn, request_id, "JUDGE_RUNNING", judge_tool_use_id=tool_use_id)
            _event(conn, "judge_running", request_id=request_id, extra={"tool_use_id": tool_use_id})
            fresh = conn.execute(
                "SELECT * FROM requests WHERE request_id=?", (request_id,)
            ).fetchone()
            return _request_dict(fresh)


def mark_judge_running(request, tool_use_id):
    """Mark a known pending request running. False when the transition is refused."""
    if not isinstance(request, dict) or not request.get("request_id"):
        return False
    try:
        begun = begin_judge(
            request.get("session_id") or "",
            build_judge_packet(request),
            tool_use_id,
        )
    except TransitionError:
        return False
    return begun is not None


def _result(decision, worker=None, **extra):
    out = {
        "decision": decision,
        "next_action_text": _NEXT.get(decision, ""),
        "worker_invocation": worker,
    }
    out.update(extra)
    return out


def _fail_closed(conn, request_id, reason):
    try:
        _transition(conn, request_id, "FAILED_CLOSED", decision=reason)
    except TransitionError:
        pass
    _event(conn, reason, request_id=request_id, extra={"result": "FAILED_CLOSED"})


def _assistant_models(path):
    """(ok, models). ok False means the transcript could not be read."""
    if not isinstance(path, str):
        return False, []
    real = _agent_model._transcript_file(path)
    if real is None:
        return False, []
    blob = _agent_model._read_capped(real, _agent_model._TRANSCRIPT_CAP)
    if blob is None:
        return False, []
    models = []
    for line in blob.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        message = obj.get("message")
        model = message.get("model") if isinstance(message, dict) else None
        models.append(model.strip() if isinstance(model, str) else "")
    return True, models


def subagent_transcript_path(payload, agent_id):
    if not isinstance(payload, dict) or not isinstance(agent_id, str) or agent_id == "":
        return None
    transcript = payload.get("transcript_path")
    session = payload.get("session_id")
    if not isinstance(transcript, str) or not isinstance(session, str):
        return None
    if session in (".", "..") or "/" in session or "\\" in session:
        return None
    return os.path.join(
        os.path.dirname(transcript), session, "subagents", "agent-%s.jsonl" % agent_id
    )


def _parse_decision_text(text):
    if not isinstance(text, str) or "```" in text:
        return None
    try:
        obj = json.loads(text.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or set(obj) != set(_DECISION_KEYS):
        return None
    if type(obj.get("schema_version")) is not int or obj.get("schema_version") != 1:
        return None
    if obj.get("decision") not in _JUDGE_DECISIONS:
        return None
    for key in ("request_id", "recommended_model", "reason_code", "rationale", "execution_scope", "suggested_next_step"):
        if not isinstance(obj.get(key), str):
            return None
    evidence = obj.get("evidence_used")
    if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
        return None
    return obj


def _worker_quota_ok(conn, policy, session_id, task_id):
    limits = effective_limits(policy)
    per_task = conn.execute(
        """SELECT COUNT(*) AS n FROM grants g
           JOIN requests r ON r.request_id = g.request_id
           WHERE r.session_id=? AND r.task_id=?""",
        (session_id, task_id),
    ).fetchone()["n"]
    per_session = conn.execute(
        """SELECT COUNT(*) AS n FROM grants g
           JOIN requests r ON r.request_id = g.request_id
           WHERE r.session_id=?""",
        (session_id,),
    ).fetchone()["n"]
    concurrent = conn.execute(
        "SELECT COUNT(*) AS n FROM requests WHERE session_id=? AND state='GRANT_READY'",
        (session_id,),
    ).fetchone()["n"]
    running = conn.execute(
        """SELECT COUNT(*) AS n FROM agent_runs
           WHERE session_id=? AND kind='escalated' AND status NOT IN ('completed', 'failed')""",
        (session_id,),
    ).fetchone()["n"]
    if per_task >= limits["max_opus_worker_invocations_per_task"]:
        return False
    if per_session >= limits["max_opus_worker_invocations_per_session"]:
        return False
    if concurrent + running >= limits["max_concurrent_escalated_workers_per_session"]:
        return False
    return True


def _compose_replan_invocation(request):
    """One PLAN_ONLY opus-planner call. The prompt is bounded and redacted."""
    import _router_plans as plans

    planner = planning_config()["planner_agent_type"]
    task_id = request.get("plan_task_id") or request.get("task_id") or ""
    stage_id = request.get("stage_id") if isinstance(request.get("stage_id"), str) else ""
    question = redact(request.get("unresolved_question") or "", 300)
    failed = request.get("failed_requirement_ids") if isinstance(request.get("failed_requirement_ids"), list) else []
    completed = request.get("completed_stage_ids") if isinstance(request.get("completed_stage_ids"), list) else []
    evidence = request.get("evidence_refs") if isinstance(request.get("evidence_refs"), list) else []
    record = None
    snap = ""
    try:
        record = plans.load_current(task_id)
        if isinstance(record, dict):
            snap = plans.snapshot(record)
    except Exception:
        record = None
        snap = ""
    version = request.get("plan_version")
    if isinstance(record, dict) and type(record.get("version")) is int:
        version = record["version"]
    prompt = "\n".join((
        "ROUTER-TASK %s" % task_id,
        redact(snap, 4000),
        "unresolved_question: %s" % question,
        "failed_requirement_ids: %s" % redact(json.dumps(failed, sort_keys=True), 500),
        "completed_stage_ids: %s" % redact(json.dumps(completed, sort_keys=True), 500),
        "evidence_refs: %s" % redact(json.dumps(evidence, sort_keys=True), 500),
        "Return plan schema v1 with base_plan_version=%s." % version,
    ))
    return {
        "subagent_type": planner,
        "model": "opus",
        "run_in_background": False,
        "description": "Router replan %s %s" % (task_id, stage_id),
        "prompt": prompt,
    }


def compose_worker_invocation(request, decision):
    """Controller-owned worker call used when the candidate had no pending digest."""
    if isinstance(request, dict) and request.get("candidate_type") == "REPLAN_REQUEST":
        return _compose_replan_invocation(request)
    requester = request.get("requester_agent_type") or ""
    subagent_type = requester if requester in _WORKER_TYPES else "general-purpose"
    evidence = decision.get("evidence_used") if isinstance(decision, dict) else []
    if not isinstance(evidence, list):
        evidence = []
    evidence_text = ", ".join(item for item in evidence if isinstance(item, str))
    scope = decision.get("execution_scope") if isinstance(decision, dict) else ""
    summary = request.get("task_summary") or request.get("pending_summary") or ""
    prompt = (
        "Escalated analysis approved by opus-judge (request %s). "
        "Scope: %s. Task: %s. Evidence: %s. "
        "Stay inside your normal role rules; code changes still go through cursor-run specs "
        "written by the main session. End with the JSON status footer."
        % (request.get("request_id") or "", scope or "", summary, evidence_text)
    )
    return {
        "subagent_type": subagent_type,
        "model": "opus",
        "description": "Escalated %s" % (request.get("task_id") or ""),
        "prompt": prompt,
    }


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _load_invocation(raw):
    if not isinstance(raw, str) or raw == "":
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def grant_call_matches(tool_input, stored):
    """True when a grant reference names the stored invocation.

    subagent_type, model, and description must equal the stored values.
    run_in_background may be absent or false. Any other extra or different
    key does not match. The prompt is the reference, not the stored prompt.
    """
    if not isinstance(tool_input, dict) or not isinstance(stored, dict):
        return False
    for key in ("subagent_type", "model", "description"):
        if tool_input.get(key) != stored.get(key):
            return False
    if "run_in_background" in tool_input and tool_input.get("run_in_background") is not False:
        return False
    for key, value in tool_input.items():
        if key in ("prompt", "run_in_background"):
            continue
        if key not in stored or stored[key] != value:
            return False
    return True


def _issue_grant(conn, request, decision, policy):
    pending = request.get("pending_digest") or ""
    worker = None
    invocation_json = None
    # A missing digest, including REPLAN_REQUEST, is composed by compose_worker_invocation.
    if pending:
        digest = pending
    else:
        worker = compose_worker_invocation(request, decision)
        digest = invocation_digest(request.get("session_id") or "", worker, "opus")
        invocation_json = _canonical_json(worker)
    purpose = (
        "PLAN_ONLY"
        if request.get("candidate_type") in ("PLAN_REQUEST", "REPLAN_REQUEST")
        else "EXECUTOR"
    )
    limits = effective_limits(policy)
    now = time.time()
    expires = now + limits["grant_ttl_seconds"]
    _transition(conn, request["request_id"], "JUDGED_ESCALATE", decision="ESCALATE")
    _transition(conn, request["request_id"], "GRANT_READY", decision="ESCALATE", expires=expires)
    grant_id = "grn-" + secrets.token_hex(8)
    conn.execute(
        """INSERT INTO grants (
               grant_id, request_id, session_id, invocation_digest, invocation_json,
               expires, uses_left, purpose
           ) VALUES (?,?,?,?,?,?,1,?)""",
        (
            grant_id,
            request["request_id"],
            request.get("session_id") or "",
            digest,
            invocation_json,
            expires,
            purpose,
        ),
    )
    _event(
        conn,
        "grant_issued",
        request_id=request["request_id"],
        extra={"grant_id": grant_id, "invocation_digest": digest},
    )
    return worker, digest


def _closed_result(conn, request, reason):
    _fail_closed(conn, request["request_id"], reason)
    return _result(
        None,
        failed_closed=True,
        reason=reason,
        request_id=request["request_id"],
    )


def _decision_fields(decision, request):
    model = decision.get("recommended_model") if isinstance(decision, dict) else ""
    return {
        "request_id": request.get("request_id") or "",
        "suggested_next_step": decision.get("suggested_next_step") or "",
        "rationale": decision.get("rationale") or "",
        "recommended_model": model if isinstance(model, str) else "",
    }


def _ingest_judge_result(payload):
    """Validate a finished judge and maybe mint one grant. Uncorrelated input is a no-op."""
    if not isinstance(payload, dict):
        return _result(None, uncorrelated=True)
    tool_use_id = payload.get("tool_use_id")
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    policy = load_policy()
    with _db() as conn:
        with _Tx(conn):
            _sweep(conn, time.time())
            row = None
            if isinstance(tool_use_id, str) and tool_use_id:
                row = conn.execute(
                    """SELECT * FROM requests
                       WHERE judge_tool_use_id=? AND state='JUDGE_RUNNING'""",
                    (tool_use_id,),
                ).fetchone()
            if row is None:
                _event(
                    conn,
                    "judge_uncorrelated",
                    session_id=session_id,
                    extra={"tool_use_id": tool_use_id if isinstance(tool_use_id, str) else ""},
                )
                return _result(None, uncorrelated=True)
            request = _request_dict(row)
            response = payload.get("tool_response")
            if not isinstance(response, dict) or response.get("status") != "completed":
                return _closed_result(conn, request, "judge_incomplete")
            if policy is None or not policy.get("enabled") or read_switch() != "active":
                return _closed_result(conn, request, "judge_disabled")
            resolved = response.get("resolvedModel")
            agent_id = response.get("agentId") if isinstance(response.get("agentId"), str) else ""
            model_text = resolved if isinstance(resolved, str) else ""
            conn.execute(
                "UPDATE requests SET judge_agent_id=?, judge_model=? WHERE request_id=?",
                (agent_id, model_text, request["request_id"]),
            )
            if resolved not in policy["accepted_opus_models"]:
                return _closed_result(conn, request, "judge_wrong_model")
            path = subagent_transcript_path(payload, agent_id)
            readable, models = _assistant_models(path)
            if not readable or not models:
                _event(conn, "coverage_warning", request_id=request["request_id"], extra={"agent_id": agent_id})
            elif any(model not in policy["accepted_opus_models"] for model in models):
                return _closed_result(conn, request, "judge_wrong_model")
            parts = []
            content = response.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        parts.append(item["text"])
            decision = _parse_decision_text("".join(parts))
            if decision is None or decision.get("request_id") != request["request_id"]:
                return _closed_result(conn, request, "judge_invalid_json")
            chosen = decision["decision"]
            fields = _decision_fields(decision, request)
            if chosen in ("CONTINUE", "RETRY", "STOP"):
                _transition(conn, request["request_id"], "JUDGED_" + chosen, decision=chosen)
                _event(conn, "judge_decided", request_id=request["request_id"], extra={"decision": chosen})
                return _result(chosen, **fields)
            if not _worker_quota_ok(conn, policy, request.get("session_id") or "", request.get("task_id") or ""):
                _transition(conn, request["request_id"], "JUDGED_STOP", decision="QUOTA_EXHAUSTED")
                _event(conn, "quota_exhausted", request_id=request["request_id"], extra={"reason": "QUOTA_EXHAUSTED"})
                fields["rationale"] = decision.get("rationale") or ""
                return _result("STOP", reason="QUOTA_EXHAUSTED", **fields)
            worker, _digest = _issue_grant(conn, request, decision, policy)
            _event(conn, "judge_decided", request_id=request["request_id"], extra={"decision": "ESCALATE"})
            ttl = effective_limits(policy)["grant_ttl_seconds"]
            pending = bool(request.get("pending_digest"))
            if pending:
                return _result("ESCALATE", None, ttl=ttl, pending=True, **fields)
            return _result("ESCALATE", worker, ttl=ttl, pending=False, **fields)


def _close_running(conn, tool_use_id, reason):
    """Move the JUDGE_RUNNING request for this tool use to FAILED_CLOSED."""
    if not isinstance(tool_use_id, str) or tool_use_id == "":
        return False
    row = conn.execute(
        """SELECT request_id FROM requests
           WHERE judge_tool_use_id=? AND state='JUDGE_RUNNING'""",
        (tool_use_id,),
    ).fetchone()
    if row is None:
        return False
    _fail_closed(conn, row["request_id"], reason)
    return True


def _fail_ingest(payload):
    """Fresh transaction: an ingest crash fail-closes the correlated judge.

    Returns the request id when the tool use was correlated, else "".
    A TransitionError that already moved the row still writes judge_ingest_error.
    """
    tool_use_id = ""
    if isinstance(payload, dict) and isinstance(payload.get("tool_use_id"), str):
        tool_use_id = payload["tool_use_id"]
    request_id = ""
    try:
        with _db() as conn:
            with _Tx(conn):
                _sweep(conn, time.time())
                row = None
                if tool_use_id:
                    row = conn.execute(
                        "SELECT request_id, state FROM requests WHERE judge_tool_use_id=?",
                        (tool_use_id,),
                    ).fetchone()
                if row is None:
                    return ""
                request_id = row["request_id"]
                if row["state"] == "JUDGE_RUNNING":
                    _fail_closed(conn, request_id, "judge_ingest_error")
                else:
                    if row["state"] != "FAILED_CLOSED":
                        try:
                            _transition(conn, request_id, "FAILED_CLOSED", decision="judge_ingest_error")
                        except TransitionError:
                            pass
                    _event(
                        conn,
                        "judge_ingest_error",
                        request_id=request_id,
                        extra={"result": "FAILED_CLOSED"},
                    )
    except Exception:
        return request_id
    return request_id


def ingest_judge_result(payload):
    """Validate a finished judge. An unexpected error fail-closes that judge.

    TransitionError is not re-raised. The request ends FAILED_CLOSED, the
    ledger records judge_ingest_error, and the returned result is what the
    post hook turns into ARBITRATION_FAILED.
    """
    try:
        return _ingest_judge_result(payload)
    except TransitionError:
        request_id = _fail_ingest(payload)
        return _result(None, failed_closed=True, reason="judge_ingest_error", request_id=request_id or "")
    except Exception:
        request_id = _fail_ingest(payload)
        return _result(None, failed_closed=True, reason="judge_ingest_error", request_id=request_id or "")


def _grant_purpose(row):
    if row is None:
        return "EXECUTOR"
    try:
        value = row["purpose"]
    except (IndexError, KeyError):
        return "EXECUTOR"
    if value in ("PLAN_ONLY", "EXECUTOR"):
        return value
    return "EXECUTOR"


def _purpose_allows(purpose, subagent_type):
    """False when a grant's purpose does not match the caller."""
    planner = planning_config()["planner_agent_type"]
    is_planner = isinstance(planner, str) and planner != "" and subagent_type == planner
    if purpose == "PLAN_ONLY" and not is_planner:
        return False
    if is_planner and purpose != "PLAN_ONLY":
        return False
    return True


def _ready_grant(conn, session_id, digest, now):
    return conn.execute(
        """SELECT g.grant_id, g.request_id, g.purpose, g.invocation_digest FROM grants g
           JOIN requests r ON r.request_id = g.request_id
           WHERE g.session_id=? AND g.invocation_digest=? AND g.uses_left>0
             AND g.expires>? AND r.state='GRANT_READY'
           ORDER BY g.expires
           LIMIT 1""",
        (session_id, digest, now),
    ).fetchone()


def _purpose_conflict_grant(conn, session_id, tool_input, family, now):
    """A ready grant for the same prompt under another role, with the wrong purpose.

    The invocation digest includes subagent_type, so a general-purpose call
    does not match a planner grant by digest. Compare the digest the call
    would have had as the other role. A conflict does not consume the grant.
    """
    if not isinstance(tool_input, dict):
        return None
    caller = _subagent_type(tool_input)
    planner = planning_config()["planner_agent_type"]
    if not isinstance(planner, str) or planner == "":
        return None
    if caller == planner:
        others = ("general-purpose", "file-reader", "file-writer")
    else:
        others = (planner,)
    stored_family = family if isinstance(family, str) else ""
    for other in others:
        alt = dict(tool_input)
        alt["subagent_type"] = other
        row = _ready_grant(conn, session_id, invocation_digest(session_id, alt, stored_family), now)
        if row is None:
            continue
        if not _purpose_allows(_grant_purpose(row), caller):
            return row
    return None


def _purpose_mismatch():
    return {"allow": False, "reason": "GRANT_PURPOSE_MISMATCH"}


def consume_grant(session_id, digest, tool_use_id, tool_input=None, family=""):
    """Atomically use one matching grant.

    A repeated tool_use_id allows only when that grant's session and invocation
    digest both match this call. Any other replay denies. A purpose mismatch
    denies and does not consume the grant.
    """
    if not isinstance(session_id, str) or not isinstance(digest, str) or not isinstance(tool_use_id, str):
        return None
    if tool_use_id == "":
        return None
    with _db() as conn:
        with _Tx(conn):
            now = time.time()
            _sweep(conn, now)
            replay = conn.execute(
                "SELECT * FROM grants WHERE consumed_tool_use_id=?",
                (tool_use_id,),
            ).fetchone()
            if replay is not None:
                if replay["session_id"] == session_id and replay["invocation_digest"] == digest:
                    return {
                        "allow": True,
                        "grant_id": replay["grant_id"],
                        "request_id": replay["request_id"],
                        "purpose": _grant_purpose(replay),
                        "idempotent": True,
                    }
                return {"allow": False, "reason": "replay_mismatch"}
            row = _ready_grant(conn, session_id, digest, now)
            if row is None:
                if _purpose_conflict_grant(conn, session_id, tool_input, family, now) is not None:
                    return _purpose_mismatch()
                return None
            if not _purpose_allows(_grant_purpose(row), _subagent_type(tool_input)):
                return _purpose_mismatch()
            updated = conn.execute(
                """UPDATE grants SET uses_left=uses_left-1, consumed_tool_use_id=?, consumed_at=?
                   WHERE grant_id=? AND uses_left>0""",
                (tool_use_id, now, row["grant_id"]),
            )
            if updated.rowcount != 1:
                return None
            _transition(conn, row["request_id"], "GRANT_CONSUMED")
            _event(
                conn,
                "arbitration_grant_consumed",
                request_id=row["request_id"],
                extra={"grant_id": row["grant_id"], "invocation_digest": digest},
            )
            return {
                "allow": True,
                "grant_id": row["grant_id"],
                "request_id": row["request_id"],
                "purpose": _grant_purpose(row),
                "idempotent": False,
            }


def consume_grant_reference(session_id, request_id, tool_input, tool_use_id):
    """Consume one composed grant addressed by ARBITRATION-GRANT.

    A field mismatch returns GRANT_MISMATCH and leaves the grant unused.
    T02 grants have no invocation_json, so the reference does not match them.
    A repeated tool_use_id for the same matching call is idempotent.
    """
    if not isinstance(session_id, str) or not isinstance(request_id, str) or request_id == "":
        return {"allow": False, "reason": "GRANT_MISMATCH"}
    if not isinstance(tool_use_id, str) or tool_use_id == "" or not isinstance(tool_input, dict):
        return {"allow": False, "reason": "GRANT_MISMATCH"}
    with _db() as conn:
        with _Tx(conn):
            now = time.time()
            _sweep(conn, now)
            replay = conn.execute(
                "SELECT * FROM grants WHERE consumed_tool_use_id=?",
                (tool_use_id,),
            ).fetchone()
            if replay is not None:
                stored = _load_invocation(replay["invocation_json"])
                if (
                    replay["session_id"] == session_id
                    and replay["request_id"] == request_id
                    and stored is not None
                    and grant_call_matches(tool_input, stored)
                ):
                    return {
                        "allow": True,
                        "invocation": stored,
                        "grant_id": replay["grant_id"],
                        "request_id": replay["request_id"],
                        "purpose": _grant_purpose(replay),
                        "idempotent": True,
                    }
                return {"allow": False, "reason": "GRANT_MISMATCH"}
            row = conn.execute(
                """SELECT g.grant_id, g.request_id, g.invocation_json, g.invocation_digest, g.purpose
                   FROM grants g
                   JOIN requests r ON r.request_id = g.request_id
                   WHERE g.request_id=? AND g.session_id=? AND g.uses_left>0
                     AND g.expires>? AND r.state='GRANT_READY'""",
                (request_id, session_id, now),
            ).fetchone()
            if row is None:
                return {"allow": False, "reason": "GRANT_MISMATCH"}
            if not _purpose_allows(_grant_purpose(row), _subagent_type(tool_input)):
                return _purpose_mismatch()
            stored = _load_invocation(row["invocation_json"])
            if stored is None or not grant_call_matches(tool_input, stored):
                return {"allow": False, "reason": "GRANT_MISMATCH"}
            updated = conn.execute(
                """UPDATE grants SET uses_left=uses_left-1, consumed_tool_use_id=?, consumed_at=?
                   WHERE grant_id=? AND uses_left>0""",
                (tool_use_id, now, row["grant_id"]),
            )
            if updated.rowcount != 1:
                return {"allow": False, "reason": "GRANT_MISMATCH"}
            _transition(conn, row["request_id"], "GRANT_CONSUMED")
            _event(
                conn,
                "arbitration_grant_consumed",
                request_id=row["request_id"],
                extra={"grant_id": row["grant_id"], "invocation_digest": row["invocation_digest"]},
            )
            return {
                "allow": True,
                "invocation": stored,
                "grant_id": row["grant_id"],
                "request_id": row["request_id"],
                "purpose": _grant_purpose(row),
                "idempotent": False,
            }


def note_updated_input(tool_use_id, prompt):
    """Record whether a correlated PostToolUse saw the short reference or the expanded text.

    Correlation stays by tool_use_id. An uncorrelated call records nothing.
    """
    if not isinstance(tool_use_id, str) or tool_use_id == "" or not isinstance(prompt, str):
        return None
    if not os.path.lexists(ledger_path()):
        return None
    form = "reference" if packet_reference_id(prompt) or grant_reference_id(prompt) else "expanded"
    with _db() as conn:
        with _Tx(conn):
            _sweep(conn, time.time())
            row = conn.execute(
                "SELECT request_id, session_id FROM requests WHERE judge_tool_use_id=?",
                (tool_use_id,),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT request_id, session_id FROM grants WHERE consumed_tool_use_id=?",
                    (tool_use_id,),
                ).fetchone()
            if row is None:
                return None
            _event(
                conn,
                "updated_input_observed",
                request_id=row["request_id"],
                session_id=row["session_id"],
                extra={"value": form},
            )
            return form


def _run_blocks_resume(run):
    """True when a recorded agent must not be resumed."""
    if run["kind"] in ("judge", "escalated"):
        return True
    family = _agent_model._model_family(run["resolved_model"] or "")
    return family not in ("sonnet", "haiku")


def resume_blocked(payload, target):
    """True when SendMessage would resume an Opus, judge, escalated, or unknown agent.

    A target that is not an agent id is a name. A name match in this session
    uses the same rule. An unknown name is allowed.
    """
    if not isinstance(target, str) or target == "":
        return False
    session_id = ""
    if isinstance(payload, dict) and isinstance(payload.get("session_id"), str):
        session_id = payload["session_id"]
    if _AGENT_ID_RE.fullmatch(target) is None:
        if not os.path.lexists(ledger_path()):
            return False
        with _db() as conn:
            with _Tx(conn):
                _sweep(conn, time.time())
                found = conn.execute(
                    """SELECT * FROM agent_runs
                       WHERE (session_id=? AND lower(name)=?) OR tool_use_id=?""",
                    (session_id, target.lower(), target),
                ).fetchall()
        if not found:
            return False
        return any(_run_blocks_resume(run) for run in found)
    run = None
    if os.path.lexists(ledger_path()):
        with _db() as conn:
            with _Tx(conn):
                _sweep(conn, time.time())
                run = conn.execute("SELECT * FROM agent_runs WHERE agent_id=?", (target,)).fetchone()
    if run is not None:
        return _run_blocks_resume(run)
    path = subagent_transcript_path(payload, target)
    raw = _agent_model._last_assistant_model(path) if path else None
    if raw is None:
        return True
    return _agent_model._model_family(raw) not in ("sonnet", "haiku")


def _resolved_is_opus(resolved, policy):
    """True when a finished model id is an accepted Opus id or contains opus."""
    if not isinstance(resolved, str) or resolved.strip() == "":
        return False
    models = policy.get("accepted_opus_models") if isinstance(policy, dict) else None
    if isinstance(models, list) and resolved in models:
        return True
    return "opus" in resolved.lower()


def _agent_name(payload):
    """tool_input.name when it is a non-empty string, else None."""
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    if not isinstance(tool_input, dict):
        return None
    raw = tool_input.get("name")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _stored_agent_name(name):
    """Lowercase name stored on agent_runs. Comparison is case-insensitive."""
    if isinstance(name, str) and name.strip():
        return name.strip().lower()
    return None


_SEND_STRING_LIMIT = 200


def _walk_strings(value, found):
    """Collect up to _SEND_STRING_LIMIT string values, depth-first."""
    if len(found) >= _SEND_STRING_LIMIT:
        return
    if isinstance(value, str):
        found.append(value)
        return
    if isinstance(value, dict):
        for item in value.values():
            _walk_strings(item, found)
            if len(found) >= _SEND_STRING_LIMIT:
                return
    elif isinstance(value, list):
        for item in value:
            _walk_strings(item, found)
            if len(found) >= _SEND_STRING_LIMIT:
                return


def send_targets(tool_input):
    """SendMessage targets: recipient, to, recipients, and nested agent ids.

    Nested strings are capped at 200. Only those that match an agent id are
    added. Names from the three explicit fields are kept as typed.
    """
    if not isinstance(tool_input, dict):
        return []
    found = []
    seen = set()

    def add(value):
        if not isinstance(value, str):
            return
        text = value.strip()
        if text == "" or text in seen:
            return
        seen.add(text)
        found.append(text)

    for key in ("recipient", "to"):
        add(tool_input.get(key))
    recipients = tool_input.get("recipients")
    if isinstance(recipients, list):
        for item in recipients:
            add(item)
    nested = []
    _walk_strings(tool_input, nested)
    for text in nested:
        stripped = text.strip()
        if _AGENT_ID_RE.fullmatch(stripped):
            add(stripped)
    return found


def _save_agent_run(
    conn, agent_id, session_id, project, agent_type, resolved, kind, request_id,
    tool_use_id, status, now, name,
):
    """Insert or update one run. A gate row with the same tool_use_id is filled in."""
    stored_name = _stored_agent_name(name)
    if not isinstance(agent_id, str) or agent_id == "":
        return None
    if isinstance(tool_use_id, str) and tool_use_id:
        rows = conn.execute(
            "SELECT agent_id FROM agent_runs WHERE tool_use_id=?",
            (tool_use_id,),
        ).fetchall()
    else:
        rows = []
    if rows:
        primary = rows[0]["agent_id"]
        if primary != agent_id:
            clash = conn.execute(
                "SELECT agent_id FROM agent_runs WHERE agent_id=?",
                (agent_id,),
            ).fetchone()
            if clash is not None:
                conn.execute("DELETE FROM agent_runs WHERE agent_id=?", (primary,))
                primary = agent_id
            else:
                conn.execute(
                    "UPDATE agent_runs SET agent_id=? WHERE agent_id=?",
                    (agent_id, primary),
                )
                primary = agent_id
        for extra in rows[1:]:
            if extra["agent_id"] != primary:
                conn.execute("DELETE FROM agent_runs WHERE agent_id=?", (extra["agent_id"],))
        conn.execute(
            """UPDATE agent_runs SET
                   session_id=?, project=?, agent_type=?, resolved_model=?, kind=?,
                   request_id=COALESCE(?, request_id), tool_use_id=?, status=?,
                   name=COALESCE(?, name)
               WHERE agent_id=?""",
            (
                session_id, project, agent_type, resolved, kind, request_id,
                tool_use_id or "", status, stored_name, primary,
            ),
        )
        return primary
    conn.execute(
        """INSERT INTO agent_runs (
               agent_id, session_id, project, agent_type, resolved_model, kind, request_id,
               tool_use_id, status, created, name
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(agent_id) DO UPDATE SET
               session_id=excluded.session_id,
               project=excluded.project,
               agent_type=excluded.agent_type,
               resolved_model=excluded.resolved_model,
               kind=excluded.kind,
               request_id=COALESCE(excluded.request_id, agent_runs.request_id),
               tool_use_id=excluded.tool_use_id,
               status=excluded.status,
               name=COALESCE(excluded.name, agent_runs.name)""",
        (
            agent_id, session_id, project, agent_type, resolved, kind, request_id,
            tool_use_id or "", status, now, stored_name,
        ),
    )
    return agent_id


def record_launched_run(payload, kind, request_id=None):
    """Remember an allowed judge or granted worker before the tool runs.

    The row is keyed by tool_use_id with status launched. SendMessage can match
    its name or tool_use_id when PostToolUse never runs. PostToolUse fills in
    agent_id, resolved_model, and the final status.
    """
    if kind not in ("judge", "escalated", "planner") or not isinstance(payload, dict):
        return None
    tool_use_id = payload.get("tool_use_id") if isinstance(payload.get("tool_use_id"), str) else ""
    if tool_use_id == "":
        return None
    if request_id is not None and not isinstance(request_id, str):
        request_id = None
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    name = _stored_agent_name(_agent_name(payload))
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    agent_type = tool_input.get("subagent_type")
    agent_type = agent_type.strip() if isinstance(agent_type, str) else ""
    project = project_of(payload)
    with _db() as conn:
        with _Tx(conn):
            now = time.time()
            _sweep(conn, now)
            row = conn.execute(
                "SELECT agent_id FROM agent_runs WHERE tool_use_id=?",
                (tool_use_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO agent_runs (
                           agent_id, session_id, project, agent_type, resolved_model, kind,
                           request_id, tool_use_id, status, created, name
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "launched:" + tool_use_id,
                        session_id,
                        project,
                        agent_type,
                        "",
                        kind,
                        request_id,
                        tool_use_id,
                        "launched",
                        now,
                        name,
                    ),
                )
            else:
                conn.execute(
                    """UPDATE agent_runs SET
                           session_id=?,
                           project=?,
                           agent_type=CASE WHEN ? != '' THEN ? ELSE agent_type END,
                           kind=?,
                           request_id=COALESCE(?, request_id),
                           status='launched',
                           name=COALESCE(?, name)
                       WHERE tool_use_id=?""",
                    (
                        session_id, project, agent_type, agent_type, kind,
                        request_id, name, tool_use_id,
                    ),
                )
            return tool_use_id


def record_agent_result(payload):
    """Upsert agent_runs and, for a consumed grant, record the worker outcome."""
    if not isinstance(payload, dict):
        return None
    response = payload.get("tool_response")
    if not isinstance(response, dict):
        return None
    agent_id = response.get("agentId")
    if not isinstance(agent_id, str) or agent_id == "":
        return None
    agent_type = response.get("agentType") if isinstance(response.get("agentType"), str) else ""
    resolved = response.get("resolvedModel") if isinstance(response.get("resolvedModel"), str) else ""
    status = response.get("status") if isinstance(response.get("status"), str) else ""
    tool_use_id = payload.get("tool_use_id") if isinstance(payload.get("tool_use_id"), str) else ""
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    agent_name = _agent_name(payload)
    if status == "completed":
        run_status = "completed"
        worker_state = "WORKER_COMPLETED"
    elif status in ("failed", "error"):
        run_status = "failed"
        worker_state = "WORKER_FAILED"
    else:
        run_status = "unknown"
        worker_state = "WORKER_UNKNOWN"
    policy = load_policy()
    judge_type = policy["judge_agent_type"] if policy else "opus-judge"
    project = project_of(payload)
    with _db() as conn:
        with _Tx(conn):
            now = time.time()
            _sweep(conn, now)
            kind = "routine"
            request_id = None
            judge = None
            if tool_use_id:
                judge = conn.execute(
                    "SELECT * FROM requests WHERE judge_tool_use_id=?", (tool_use_id,)
                ).fetchone()
            grant = None
            if tool_use_id:
                grant = conn.execute(
                    "SELECT * FROM grants WHERE consumed_tool_use_id=?", (tool_use_id,)
                ).fetchone()
            if judge is not None or agent_type == judge_type:
                kind = "judge"
                if judge is not None:
                    request_id = judge["request_id"]
            elif grant is not None:
                kind = "planner" if _grant_purpose(grant) == "PLAN_ONLY" else "escalated"
                request_id = grant["request_id"]
            _save_agent_run(
                conn, agent_id, session_id, project, agent_type, resolved, kind, request_id,
                tool_use_id, run_status, now, agent_name,
            )
            if kind in ("escalated", "planner") and request_id:
                state = conn.execute(
                    "SELECT state FROM requests WHERE request_id=?", (request_id,)
                ).fetchone()
                if state is not None and state["state"] == "GRANT_CONSUMED":
                    _transition(conn, request_id, worker_state)
            _event(
                conn,
                "agent_result",
                request_id=request_id,
                session_id=session_id,
                extra={"agent_id": agent_id, "kind": kind, "status": run_status, "resolved_model": resolved},
            )
            unarbitrated = False
            if (
                status in ("completed", "async_launched")
                and _resolved_is_opus(resolved, policy)
                and judge is None
                and grant is None
            ):
                unarbitrated = True
                _event(
                    conn,
                    "unarbitrated_opus_run",
                    request_id=request_id,
                    session_id=session_id,
                    extra={"agentId": agent_id, "agentType": agent_type, "tool_use_id": tool_use_id},
                )
            return {
                "agent_id": agent_id,
                "kind": kind,
                "status": run_status,
                "request_id": request_id,
                "unarbitrated": unarbitrated,
            }


def _last_json_object(text):
    """Last JSON object in text, using the same raw_decode scan as footers."""
    if not isinstance(text, str) or text.strip() == "":
        return None
    decoder = json.JSONDecoder()
    found = None
    index = 0
    while index < len(text):
        if text[index] != "{":
            index += 1
            continue
        try:
            obj, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index += 1
            continue
        if isinstance(obj, dict):
            found = obj
        index = max(end, index + 1)
    return found


def ingest_planner_result(payload):
    """Validate a finished planner. Never promotes, mints a grant, or writes a plan."""
    if not isinstance(payload, dict):
        return ""
    response = payload.get("tool_response")
    if not isinstance(response, dict):
        return ""
    policy = load_policy()
    accepted = policy.get("accepted_opus_models") if isinstance(policy, dict) else None
    if not isinstance(accepted, list):
        accepted = []
    resolved = response.get("resolvedModel") if isinstance(response.get("resolvedModel"), str) else ""
    agent_id = response.get("agentId") if isinstance(response.get("agentId"), str) else ""
    tool_use_id = payload.get("tool_use_id") if isinstance(payload.get("tool_use_id"), str) else ""
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    parts = []
    content = response.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
    text = "".join(parts)
    with _db() as conn:
        with _Tx(conn):
            grant = None
            if tool_use_id:
                grant = conn.execute(
                    "SELECT request_id FROM grants WHERE consumed_tool_use_id=?",
                    (tool_use_id,),
                ).fetchone()
            request_id = grant["request_id"] if grant is not None else None
            if resolved not in accepted:
                _event(
                    conn, "planner_wrong_model", request_id=request_id, session_id=session_id,
                    extra={"resolved_model": resolved},
                )
                return "PLANNER_RESULT_REJECTED reason=wrong_model"
            path = subagent_transcript_path(payload, agent_id)
            readable, models = _assistant_models(path)
            if not readable or not models:
                _event(
                    conn, "coverage_warning", request_id=request_id, session_id=session_id,
                    extra={"agent_id": agent_id},
                )
            elif any(model not in accepted for model in models):
                _event(
                    conn, "planner_wrong_model", request_id=request_id, session_id=session_id,
                    extra={"resolved_model": resolved, "transcript_model": models[0]},
                )
                return "PLANNER_RESULT_REJECTED reason=wrong_model"
            import _router_plans as plans

            obj = _last_json_object(text)
            max_steps = planning_config(policy)["max_steps"]
            errors = plans.validate_plan(obj, max_steps=max_steps) if obj is not None else ["PLAN_NOT_OBJECT"]
            if errors:
                shown = errors[:5]
                _event(
                    conn, "planner_output_invalid", request_id=request_id, session_id=session_id,
                    extra={"errors": shown},
                )
                return (
                    "PLANNER_PLAN_INVALID: %s. No new planner grant; plan at Sonnet level or stop."
                    % ", ".join(shown)
                )
            sha = plans.content_sha256(obj)
            task_id = obj.get("task_id") if isinstance(obj.get("task_id"), str) else ""
            _event(
                conn, "planner_output_valid", request_id=request_id, session_id=session_id,
                extra={"content_sha256": sha, "task_id": task_id},
            )
            return (
                "PLANNER_PLAN_VALID task=%s sha256=%s. Review it, save it to "
                "~/.claude/plans/router/drafts/%s.json and run "
                "~/.claude/hooks/bin/router-plan promote %s <file>."
                % (task_id, sha, task_id, task_id)
            )


def judge_is_running(tool_use_id):
    """True when this tool use is the live judge call."""
    if not isinstance(tool_use_id, str) or tool_use_id == "" or not os.path.lexists(ledger_path()):
        return False
    with _db() as conn:
        row = conn.execute(
            """SELECT 1 FROM requests
               WHERE judge_tool_use_id=? AND state='JUDGE_RUNNING'""",
            (tool_use_id,),
        ).fetchone()
    return row is not None


def _failure_response(payload):
    response = payload.get("tool_response") if isinstance(payload, dict) else None
    if isinstance(response, dict):
        return response
    return {}


def _failure_agent_id(payload, tool_use_id):
    """agentId from the failure payload, or a synthetic id so the name is stored."""
    response = _failure_response(payload)
    for key in ("agentId", "agent_id"):
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if tool_use_id:
        return "failure:" + tool_use_id
    name = _agent_name(payload) or ""
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    if name or session_id:
        return "failure:%s:%s" % (session_id, name)
    return None


def _upsert_failed_agent(conn, payload):
    """Upsert a failed judge or escalated worker. Other failures are ignored."""
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    response = _failure_response(payload)
    agent_type = ""
    if isinstance(response.get("agentType"), str) and response.get("agentType").strip():
        agent_type = response["agentType"].strip()
    elif isinstance(tool_input.get("subagent_type"), str) and tool_input.get("subagent_type").strip():
        agent_type = tool_input["subagent_type"].strip()
    tool_use_id = payload.get("tool_use_id") if isinstance(payload.get("tool_use_id"), str) else ""
    policy = load_policy()
    judge_type = policy["judge_agent_type"] if policy else "opus-judge"
    judge = None
    grant = None
    if tool_use_id:
        judge = conn.execute(
            "SELECT request_id FROM requests WHERE judge_tool_use_id=?",
            (tool_use_id,),
        ).fetchone()
        grant = conn.execute(
            "SELECT request_id, purpose FROM grants WHERE consumed_tool_use_id=?",
            (tool_use_id,),
        ).fetchone()
    if judge is not None or agent_type == judge_type:
        kind = "judge"
        request_id = judge["request_id"] if judge is not None else None
    elif grant is not None:
        kind = "planner" if _grant_purpose(grant) == "PLAN_ONLY" else "escalated"
        request_id = grant["request_id"]
    else:
        return None
    agent_id = _failure_agent_id(payload, tool_use_id)
    if not isinstance(agent_id, str) or agent_id == "":
        return None
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    resolved = ""
    if isinstance(response.get("resolvedModel"), str):
        resolved = response["resolvedModel"]
    elif isinstance(tool_input.get("model"), str):
        resolved = tool_input["model"]
    _save_agent_run(
        conn,
        agent_id,
        session_id,
        project_of(payload),
        agent_type,
        resolved,
        kind,
        request_id,
        tool_use_id,
        "failed",
        time.time(),
        _agent_name(payload),
    )
    return agent_id


def note_agent_failure(payload):
    """PostToolUseFailure: judge crash fail-closes; an escalated worker fails.

    A judge or escalated failure is also stored on agent_runs as status failed,
    with agentId when the payload has one and the name from tool_input.
    """
    if not isinstance(payload, dict):
        return None
    tool_use_id = payload.get("tool_use_id") if isinstance(payload.get("tool_use_id"), str) else ""
    with _db() as conn:
        with _Tx(conn):
            _sweep(conn, time.time())
            _upsert_failed_agent(conn, payload)
            if _close_running(conn, tool_use_id, "judge_tool_failure"):
                return "FAILED_CLOSED"
            if tool_use_id == "":
                return None
            grant = conn.execute(
                "SELECT request_id FROM grants WHERE consumed_tool_use_id=?",
                (tool_use_id,),
            ).fetchone()
            if grant is None:
                return None
            state = conn.execute(
                "SELECT state FROM requests WHERE request_id=?",
                (grant["request_id"],),
            ).fetchone()
            if state is None or state["state"] != "GRANT_CONSUMED":
                return None
            _transition(conn, grant["request_id"], "WORKER_FAILED")
            _event(conn, "worker_failed", request_id=grant["request_id"])
            return "WORKER_FAILED"


def record_agent_resumed(payload):
    """Record SendMessage resumedAgentId against the known agent."""
    if not isinstance(payload, dict):
        return None
    response = payload.get("tool_response") if isinstance(payload.get("tool_response"), dict) else {}
    resumed = response.get("resumedAgentId")
    if not isinstance(resumed, str) or resumed == "":
        return None
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    record_event(
        "agent_resumed",
        session_id=session_id,
        extra={"resumedAgentId": resumed, "agent_id": resumed},
        project=project_of(payload),
    )
    return resumed


def list_attempts(session_id, task_id=None):
    with _db() as conn:
        if task_id is None:
            rows = conn.execute(
                "SELECT session_id, task_id, strategy_digest, outcome, created FROM attempts WHERE session_id=?",
                (session_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT session_id, task_id, strategy_digest, outcome, created FROM attempts
                   WHERE session_id=? AND task_id=?""",
                (session_id, task_id),
            ).fetchall()
    return [dict(row) for row in rows]


def record_attempt(session_id, task_id, strategy_digest, outcome):
    with _db() as conn:
        with _Tx(conn):
            conn.execute(
                "INSERT INTO attempts (session_id, task_id, strategy_digest, outcome, created) VALUES (?,?,?,?,?)",
                (session_id, task_id, strategy_digest, outcome, time.time()),
            )


def _cursor_spec(command):
    if not isinstance(command, str) or command.strip() == "":
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    binary = os.path.normpath(os.path.join(_home(), ".claude", "hooks", "bin", "cursor-run"))
    for index, part in enumerate(parts):
        expanded = os.path.normpath(os.path.expanduser(part))
        if expanded != binary and not str(part).endswith("/.claude/hooks/bin/cursor-run"):
            continue
        if index + 1 >= len(parts):
            return None
        arg = parts[index + 1]
        if arg == "new":
            return None
        return arg
    return None


def _response_stdout(payload):
    response = payload.get("tool_response") if isinstance(payload, dict) else None
    if isinstance(response, dict):
        for key in ("stdout", "output"):
            if isinstance(response.get(key), str):
                return response[key]
    if isinstance(response, str):
        return response
    output = payload.get("tool_output") if isinstance(payload, dict) else None
    if isinstance(output, str):
        return output
    return ""


def record_cursor_run_result(payload):
    """Record a cursor-run attempt. `new` and unreadable specs are skipped."""
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    spec = _cursor_spec(tool_input.get("command"))
    if spec is None:
        return None
    try:
        info = os.lstat(spec)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    try:
        fd = os.open(spec, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        try:
            blob = os.read(fd, 1024 * 1024)
        except OSError:
            return None
    finally:
        os.close(fd)
    if not blob and info.st_size > 0:
        return None
    last = None
    for line in _response_stdout(payload).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "status" in obj:
            last = obj
    if last is None:
        return None
    outcome = last.get("status")
    if not isinstance(outcome, str):
        return None
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    task_id = "cursor:" + os.path.splitext(os.path.basename(spec))[0]
    digest = hashlib.sha256(blob).hexdigest()
    record_attempt(session_id, task_id, digest, outcome)
    return {"task_id": task_id, "strategy_digest": digest, "outcome": outcome}


def _cancel_open():
    if not os.path.lexists(ledger_path()):
        return
    with _db() as conn:
        with _Tx(conn):
            now = time.time()
            rows = conn.execute(
                "SELECT request_id FROM requests WHERE state IN ('JUDGE_PENDING', 'GRANT_READY', 'REPLAN_PENDING')"
            ).fetchall()
            for row in rows:
                _transition(conn, row["request_id"], "CANCELLED")
                conn.execute(
                    "UPDATE requests SET candidate_key=NULL WHERE request_id=?",
                    (row["request_id"],),
                )
                conn.execute(
                    "UPDATE grants SET uses_left=0 WHERE request_id=?",
                    (row["request_id"],),
                )
                _event(conn, "cancelled", request_id=row["request_id"])
            if rows:
                conn.execute("UPDATE requests SET updated=? WHERE request_id=?", (now, rows[0]["request_id"]))


def disable_arbitration():
    """Flip the switch off and cancel pending judgments and unused grants."""
    write_switch("disabled")
    _cancel_open()


def requests_open():
    if not os.path.lexists(ledger_path()):
        return []
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM requests WHERE state NOT IN (%s) ORDER BY created" % ",".join("?" for _ in _TERMINAL),
            tuple(_TERMINAL),
        ).fetchall()
    return [_request_dict(row) for row in rows]


def requests_recent(limit=20):
    """Most recently created requests of any state."""
    if not os.path.lexists(ledger_path()):
        return []
    size = limit if isinstance(limit, int) and limit > 0 else 20
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM requests ORDER BY created DESC, request_id DESC LIMIT ?",
            (size,),
        ).fetchall()
    return [_request_dict(row) for row in rows]


def grants_open():
    if not os.path.lexists(ledger_path()):
        return []
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM grants WHERE uses_left>0 ORDER BY expires"""
        ).fetchall()
    return [dict(row) for row in rows]


def show_request(request_id):
    if not os.path.lexists(ledger_path()):
        return None
    with _db() as conn:
        row = conn.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
        grants = []
        worker = None
        if row is not None:
            grants = conn.execute(
                "SELECT grant_id, invocation_digest, expires, uses_left, consumed_tool_use_id FROM grants WHERE request_id=?",
                (request_id,),
            ).fetchall()
            run = conn.execute(
                """SELECT agent_id, agent_type, resolved_model, kind, tool_use_id, status
                   FROM agent_runs
                   WHERE request_id=? AND IFNULL(kind,'')!='judge'
                   ORDER BY created""",
                (request_id,),
            ).fetchall()
            if run:
                worker = dict(run[-1])
    if row is None:
        return None
    data = _request_dict(row)
    data.pop("context_json", None)
    data["grants"] = [dict(item) for item in grants]
    data["worker"] = worker
    return data


def _public_request(data):
    return {
        "request_id": data.get("request_id"),
        "session_id": data.get("session_id"),
        "task_id": data.get("task_id"),
        "state": data.get("state"),
        "trigger": data.get("trigger"),
        "reason_code": data.get("reason_code"),
        "expires": data.get("expires"),
    }


def _project_base(project):
    if not isinstance(project, str) or project.strip() == "":
        return ""
    return os.path.basename(project.rstrip("/\\"))


def _request_line(item):
    """Status fields only. Description is allowed; prompt text is not."""
    return (
        "  %s state=%s trigger=%s decision=%s judge_model=%s created=%s project=%s"
        % (
            item.get("request_id") or "",
            item.get("state") or "",
            item.get("trigger") or "",
            item.get("decision") or "",
            item.get("judge_model") or "",
            item.get("created") if item.get("created") is not None else "",
            _project_base(item.get("project")),
        )
    )


def _grant_public(item, now):
    uses = item.get("uses_left")
    expires = item.get("expires")
    consumed = item.get("consumed_tool_use_id") or ""
    if not isinstance(consumed, str):
        consumed = ""
    if consumed:
        state = "consumed"
    elif not isinstance(uses, int) or isinstance(uses, bool) or uses <= 0:
        state = "expired"
    elif isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires < now:
        state = "expired"
    else:
        state = "open"
    return {
        "state": state,
        "expiry": expires,
        "uses_left": uses,
        "consumed_tool_use_id": consumed,
    }


def record_plan_version(session_id, task_id, version, content_sha256, scope_digest):
    """Insert an active plan row and supersede older versions of the task."""
    with _db() as conn:
        with _Tx(conn):
            now = time.time()
            _sweep(conn, now)
            conn.execute(
                "UPDATE plans SET status='superseded' WHERE task_id=? AND status='active'",
                (task_id,),
            )
            conn.execute(
                """INSERT INTO plans (
                       task_id, version, content_sha256, scope_digest, status, session_id, created
                   ) VALUES (?,?,?,?, 'active', ?, ?)""",
                (task_id, version, content_sha256, scope_digest, session_id, now),
            )


def active_plan_version(task_id):
    if not os.path.lexists(ledger_path()):
        return None
    with _db() as conn:
        row = conn.execute(
            "SELECT version FROM plans WHERE task_id=? AND status='active'",
            (task_id,),
        ).fetchone()
    if row is None:
        return None
    return row["version"]


def _stage_block(conn, task_id, plan_version, stage_id):
    """(ok, code, rows) for one stage. A yielded or finished attempt is never re-dispatched."""
    rows = conn.execute(
        """SELECT status, attempt FROM stages
           WHERE task_id=? AND plan_version=? AND stage_id=?""",
        (task_id, plan_version, stage_id),
    ).fetchall()
    statuses = [row["status"] for row in rows]
    if any(item in ("started", "completed", "needs_judgment") for item in statuses):
        return (False, "ROUTER_STAGE_DUPLICATE", rows)
    if any(item == "unknown" for item in statuses):
        return (False, "ROUTER_STAGE_UNKNOWN", rows)
    active = conn.execute(
        "SELECT version FROM plans WHERE task_id=? AND status='active'",
        (task_id,),
    ).fetchone()
    if active is None or active["version"] != plan_version:
        return (False, "ROUTER_PLAN_STALE", rows)
    return (True, "OK", rows)


def stage_check(task_id, plan_version, stage_id):
    """Read-only stage_start. Same deny codes, no insert and no sweep.

    The gate runs this before accept_workflow. stage_start is still the
    atomic reservation and denies a race this pre-check missed. A missing
    ledger has no active plan, so the result is ROUTER_PLAN_STALE and the
    store is left uncreated.
    """
    if not os.path.lexists(ledger_path()):
        return (False, "ROUTER_PLAN_STALE")
    with _db() as conn:
        ok, code, _rows = _stage_block(conn, task_id, plan_version, stage_id)
        return (ok, code)


def stage_start(session_id, task_id, plan_version, stage_id, workflow_tool_use_id=None):
    """Start one stage attempt. Returns (ok, code, attempt)."""
    with _db() as conn:
        with _Tx(conn):
            now = time.time()
            _sweep(conn, now)
            ok, code, rows = _stage_block(conn, task_id, plan_version, stage_id)
            if not ok:
                return (False, code, None)
            attempt = 1
            if rows:
                attempt = max(row["attempt"] for row in rows) + 1
            try:
                conn.execute(
                    """INSERT INTO stages (
                           task_id, plan_version, stage_id, attempt, session_id,
                           workflow_tool_use_id, run_id, status, created, updated
                       ) VALUES (?,?,?,?,?,?,NULL,'started',?,?)""",
                    (
                        task_id,
                        plan_version,
                        stage_id,
                        attempt,
                        session_id,
                        workflow_tool_use_id,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                return (False, "ROUTER_STAGE_DUPLICATE", None)
            return (True, "OK", attempt)


def stage_finish(task_id, plan_version, stage_id, status):
    """Close the latest started or needs_judgment attempt. False if there is none."""
    if status not in _STAGE_FINISH:
        return False
    with _db() as conn:
        with _Tx(conn):
            now = time.time()
            _sweep(conn, now)
            row = conn.execute(
                """SELECT attempt FROM stages
                   WHERE task_id=? AND plan_version=? AND stage_id=?
                     AND status IN ('started', 'needs_judgment')
                   ORDER BY attempt DESC LIMIT 1""",
                (task_id, plan_version, stage_id),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                """UPDATE stages SET status=?, updated=?
                   WHERE task_id=? AND plan_version=? AND stage_id=? AND attempt=?""",
                (status, now, task_id, plan_version, stage_id, row["attempt"]),
            )
            return True


def stages_open(session_id=None):
    """Rows in started or needs_judgment. Optional session filter."""
    if not os.path.lexists(ledger_path()):
        return []
    with _db() as conn:
        if session_id is None:
            rows = conn.execute(
                """SELECT * FROM stages
                   WHERE status IN ('started', 'needs_judgment')
                   ORDER BY created"""
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM stages
                   WHERE status IN ('started', 'needs_judgment') AND session_id=?
                   ORDER BY created""",
                (session_id,),
            ).fetchall()
    return [dict(row) for row in rows]


def task_stages(task_id):
    if not os.path.lexists(ledger_path()):
        return []
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM stages WHERE task_id=?
               ORDER BY plan_version, stage_id, attempt""",
            (task_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def resolve_open_replans(task_id, reason):
    """Resolve every open REPLAN_PENDING row for this plan task. Returns the count."""
    if not isinstance(task_id, str) or task_id == "":
        return 0
    with _db() as conn:
        with _Tx(conn):
            _sweep(conn, time.time())
            rows = conn.execute(
                """SELECT request_id FROM requests
                   WHERE plan_task_id=? AND state='REPLAN_PENDING'""",
                (task_id,),
            ).fetchall()
            for row in rows:
                _transition(conn, row["request_id"], "REPLAN_RESOLVED", decision=reason)
            return len(rows)


def resolve_replan(request_id, reason):
    """Move REPLAN_PENDING to REPLAN_RESOLVED. Any other state returns False."""
    with _db() as conn:
        with _Tx(conn):
            _sweep(conn, time.time())
            row = conn.execute(
                "SELECT state FROM requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None or row["state"] != "REPLAN_PENDING":
                return False
            _transition(conn, request_id, "REPLAN_RESOLVED", decision=reason)
            return True


def unresolved_follow_through(now=None):
    """EXPIRED no-follow-through rows created in the last 7 days."""
    if not os.path.lexists(ledger_path()):
        return []
    moment = time.time() if now is None else now
    cutoff = moment - 7 * 86400
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM requests
               WHERE state='EXPIRED' AND created >= ?
                 AND (
                     decision='NO_FOLLOW_THROUGH'
                     OR request_id IN (
                         SELECT request_id FROM events WHERE event='no_follow_through'
                     )
                 )
               ORDER BY created""",
            (cutoff,),
        ).fetchall()
        items = []
        for row in rows:
            data = _request_dict(row)
            event = conn.execute(
                """SELECT detail_json FROM events
                   WHERE request_id=? AND event='no_follow_through'
                   ORDER BY id DESC LIMIT 1""",
                (row["request_id"],),
            ).fetchone()
            from_state = ""
            if event is not None and isinstance(event["detail_json"], str) and event["detail_json"]:
                try:
                    detail = json.loads(event["detail_json"])
                except json.JSONDecodeError:
                    detail = None
                if isinstance(detail, dict) and isinstance(detail.get("from_state"), str):
                    from_state = detail["from_state"]
            data["from_state"] = from_state
            items.append(data)
    return items


def cmd_status():
    print("switch: %s" % read_switch())
    policy = load_policy()
    if policy is None or not policy.get("enabled"):
        print("policy: disabled")
    else:
        limits = effective_limits(policy)
        print(
            "policy: enabled judge=%s judgments_per_session=%s worker_per_session=%s"
            % (
                policy["judge_agent_type"],
                limits["max_judgments_per_session"],
                limits["max_opus_worker_invocations_per_session"],
            )
        )
    print("requests:")
    open_items = requests_open()
    if not open_items:
        print("  (none open)")
    for item in open_items:
        print(_request_line(item))
    print("recent:")
    for item in requests_recent(20):
        print(_request_line(item))
    print("grants:")
    for item in grants_open():
        print(
            "  %s request=%s session=%s expires=%s uses_left=%s"
            % (item["grant_id"], item["request_id"], item["session_id"], item["expires"], item["uses_left"])
        )
    print("unresolved (no follow-through, 7d):")
    unresolved = unresolved_follow_through()
    if not unresolved:
        print("  (none)")
    for item in unresolved:
        print(
            "  %s trigger=%s reason_code=%s from_state=%s created=%s"
            % (
                item.get("request_id") or "",
                item.get("trigger") or "",
                item.get("reason_code") or "",
                item.get("from_state") or "",
                item.get("created") if item.get("created") is not None else "",
            )
        )
    print("stages open:")
    # Workflow PostToolUseFailure does not reach the post hook: settings.json
    # matches Agent|Bash only. A stage left started is closed with
    # router-plan stage finish <task> <version> <stage> --status failed.
    # Rows open for more than an hour are marked stale.
    open_stages = stages_open()
    if not open_stages:
        print("  (none)")
    moment = time.time()
    for item in open_stages:
        created = item.get("created")
        stale = ""
        if isinstance(created, (int, float)) and not isinstance(created, bool) and moment - created >= 3600:
            stale = " stale"
        print(
            "  task=%s plan_version=%s stage=%s attempt=%s status=%s session=%s created=%s%s"
            % (
                item.get("task_id") or "",
                item.get("plan_version") if item.get("plan_version") is not None else "",
                item.get("stage_id") or "",
                item.get("attempt") if item.get("attempt") is not None else "",
                item.get("status") or "",
                item.get("session_id") or "",
                created if created is not None else "",
                stale,
            )
        )
    return 0


def cmd_show(request_id):
    data = show_request(request_id)
    if data is None:
        print("opus-arbitration: unknown request", file=sys.stderr)
        return 1
    view = _public_request(data)
    view["decision"] = data.get("decision")
    view["packet_digest"] = data.get("packet_digest")
    view["pending_digest"] = data.get("pending_digest")
    view["judge_model"] = data.get("judge_model") or ""
    view["judge_agent_id"] = data.get("judge_agent_id") or ""
    view["judge_tool_use_id"] = data.get("judge_tool_use_id") or ""
    description = data.get("description") or data.get("pending_summary") or ""
    if isinstance(description, str) and description:
        view["description"] = description
    now = time.time()
    view["grants"] = [_grant_public(item, now) for item in (data.get("grants") or [])]
    worker = data.get("worker")
    if isinstance(worker, dict):
        view["worker"] = {
            "agent_id": worker.get("agent_id") or "",
            "agent_type": worker.get("agent_type") or "",
            "resolved_model": worker.get("resolved_model") or "",
            "kind": worker.get("kind") or "",
            "tool_use_id": worker.get("tool_use_id") or "",
            "status": worker.get("status") or "",
        }
    print(json.dumps(view, sort_keys=True, ensure_ascii=False, indent=2))
    return 0


def _env_positive_int(name):
    raw = os.environ.get(name)
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text == "" or not text.isdigit():
        return None
    return int(text)


def planning_config(policy=None):
    """Planning caps. A missing or malformed block is disabled and never raises.

    load_policy does not require the block.
    """
    config = dict(_PLANNING_DEFAULTS)
    if policy is None:
        policy = load_policy()
    if not isinstance(policy, dict):
        return config
    if "planning" not in policy or policy.get("planning") is None:
        return config
    raw = policy.get("planning")
    if not isinstance(raw, dict):
        return dict(_PLANNING_DEFAULTS)
    enabled = raw.get("enabled", False)
    planner = raw.get("planner_agent_type", config["planner_agent_type"])
    versions = raw.get("max_plan_versions_per_task", config["max_plan_versions_per_task"])
    replans = raw.get("max_replans_per_task", config["max_replans_per_task"])
    steps = raw.get("max_steps", config["max_steps"])
    if not isinstance(enabled, bool):
        return dict(_PLANNING_DEFAULTS)
    if not isinstance(planner, str) or planner == "":
        return dict(_PLANNING_DEFAULTS)
    if not _pos_int(versions) or not _pos_int(replans) or not _pos_int(steps):
        return dict(_PLANNING_DEFAULTS)
    config["enabled"] = enabled
    config["planner_agent_type"] = planner
    config["max_plan_versions_per_task"] = versions
    config["max_replans_per_task"] = replans
    config["max_steps"] = steps
    return config


def workflow_config(policy=None):
    """Workflow caps. A missing or invalid opus_main_mode is deny.

    CLAUDE_ROUTING_WORKFLOW_MAX_WORKERS may only lower the per-workflow cap.
    """
    config = dict(_WORKFLOW_DEFAULTS)
    config["allowed_worker_families"] = list(_WORKFLOW_DEFAULTS["allowed_worker_families"])
    if policy is None:
        policy = load_policy()
    raw = policy.get("workflow") if isinstance(policy, dict) else None
    if isinstance(raw, dict):
        mode = raw.get("opus_main_mode")
        if mode in ("deny", "pinned_env"):
            config["opus_main_mode"] = mode
        families = raw.get("allowed_worker_families")
        if isinstance(families, list) and families and all(item in ("sonnet", "haiku") for item in families):
            config["allowed_worker_families"] = list(families)
        elif "allowed_worker_families" in raw:
            config["allowed_worker_families"] = []
        for key in (
            "max_concurrent_agents_required",
            "max_workflows_per_session",
            "max_workers_per_workflow",
            "max_workers_per_session",
        ):
            value = raw.get(key)
            if _pos_int(value):
                config[key] = value
        if raw.get("require_mod_call_attestation") is False:
            config["require_mod_call_attestation"] = False
        elif raw.get("require_mod_call_attestation") is True:
            config["require_mod_call_attestation"] = True
    tightened = _env_positive_int("CLAUDE_ROUTING_WORKFLOW_MAX_WORKERS")
    if tightened is not None:
        config["max_workers_per_workflow"] = min(config["max_workers_per_workflow"], tightened)
    return config


def accept_workflow(payload, expected_family, limit=None):
    """Record a launched workflow. False when a session cap is already met."""
    if not isinstance(payload, dict):
        return False
    tool_use_id = payload.get("tool_use_id") if isinstance(payload.get("tool_use_id"), str) else ""
    if tool_use_id == "":
        return False
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    prompt_id = payload.get("prompt_id") if isinstance(payload.get("prompt_id"), str) else ""
    project = project_of(payload)
    family = expected_family if isinstance(expected_family, str) else ""
    with _db() as conn:
        with _Tx(conn):
            now = time.time()
            _sweep(conn, now)
            existing = conn.execute(
                "SELECT tool_use_id FROM workflows WHERE tool_use_id=?",
                (tool_use_id,),
            ).fetchone()
            if existing is None and limit is not None:
                # denied_stage was counted, then the stage reservation lost.
                # It must not keep the slot.
                count = conn.execute(
                    "SELECT COUNT(*) AS n FROM workflows WHERE session_id=? AND IFNULL(status,'')!=?",
                    (session_id, _WORKFLOW_CAP_IGNORED),
                ).fetchone()["n"]
                if count >= limit:
                    return False
            conn.execute(
                """INSERT INTO workflows (
                       tool_use_id, session_id, prompt_id, project, created,
                       expected_family, status
                   ) VALUES (?,?,?,?,?,?, 'launched')
                   ON CONFLICT(tool_use_id) DO UPDATE SET
                       session_id=excluded.session_id,
                       prompt_id=excluded.prompt_id,
                       project=excluded.project,
                       expected_family=excluded.expected_family,
                       status='launched'""",
                (tool_use_id, session_id, prompt_id, project, now, family),
            )
            return True


def release_workflow(tool_use_id):
    """Stop a launched workflow from counting after its stage reservation lost.

    Sets status to denied_stage. accept_workflow ignores that status. A missing
    row returns False.
    """
    if not isinstance(tool_use_id, str) or tool_use_id == "":
        return False
    with _db() as conn:
        with _Tx(conn):
            cur = conn.execute(
                "UPDATE workflows SET status=? WHERE tool_use_id=? AND status='launched'",
                (_WORKFLOW_CAP_IGNORED, tool_use_id),
            )
            return cur.rowcount > 0


def note_workflow_result(payload):
    """Fill run id, task id, and name from a Workflow PostToolUse response.

    When a stage was reserved for this tool use, store run_id on that row.
    Do not mark the stage completed. Only router-plan stage finish does that,
    after the main session checks the evidence. A Workflow failure does not
    reach this hook (PostToolUseFailure matches Agent|Bash only); close a
    stuck stage with router-plan stage finish … --status failed.
    """
    if not isinstance(payload, dict):
        return None
    response = payload.get("tool_response")
    if not isinstance(response, dict):
        return None
    tool_use_id = payload.get("tool_use_id") if isinstance(payload.get("tool_use_id"), str) else ""
    if tool_use_id == "":
        return None
    run_id = response.get("runId") if isinstance(response.get("runId"), str) else ""
    task_id = response.get("taskId") if isinstance(response.get("taskId"), str) else ""
    name = response.get("workflowName") if isinstance(response.get("workflowName"), str) else ""
    with _db() as conn:
        with _Tx(conn):
            conn.execute(
                """UPDATE workflows SET run_id=?, task_id=?, workflow_name=?
                   WHERE tool_use_id=?""",
                (run_id, task_id, name, tool_use_id),
            )
            if run_id:
                conn.execute(
                    "UPDATE stages SET run_id=?, updated=? WHERE workflow_tool_use_id=?",
                    (run_id, time.time(), tool_use_id),
                )
    return tool_use_id


def record_workflow_worker(payload):
    """Link a workflow-subagent start to the newest launched workflow. Never blocks."""
    if not isinstance(payload, dict):
        return None
    agent_id = payload.get("agent_id") if isinstance(payload.get("agent_id"), str) else ""
    if agent_id.strip() == "":
        return None
    agent_id = agent_id.strip()
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    prompt_id = payload.get("prompt_id") if isinstance(payload.get("prompt_id"), str) else ""
    project = project_of(payload)
    with _db() as conn:
        with _Tx(conn):
            now = time.time()
            _sweep(conn, now)
            row = conn.execute(
                """SELECT tool_use_id FROM workflows
                   WHERE session_id=? AND prompt_id=? AND status='launched'
                   ORDER BY created DESC LIMIT 1""",
                (session_id, prompt_id),
            ).fetchone()
            workflow_id = row["tool_use_id"] if row is not None else ""
            existing = conn.execute(
                "SELECT agent_id, status FROM agent_runs WHERE agent_id=?",
                (agent_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO agent_runs (
                           agent_id, session_id, project, agent_type, resolved_model, kind,
                           request_id, tool_use_id, status, created, name, workflow_id
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        agent_id,
                        session_id,
                        project,
                        "workflow-subagent",
                        "",
                        "workflow_worker",
                        None,
                        "",
                        "launched",
                        now,
                        None,
                        workflow_id or None,
                    ),
                )
            elif workflow_id and not existing["status"]:
                conn.execute(
                    "UPDATE agent_runs SET workflow_id=COALESCE(workflow_id, ?) WHERE agent_id=?",
                    (workflow_id, agent_id),
                )
            config = workflow_config()
            over = False
            if workflow_id:
                per_workflow = conn.execute(
                    "SELECT COUNT(*) AS n FROM agent_runs WHERE kind='workflow_worker' AND workflow_id=?",
                    (workflow_id,),
                ).fetchone()["n"]
                per_session = conn.execute(
                    "SELECT COUNT(*) AS n FROM agent_runs WHERE kind='workflow_worker' AND session_id=?",
                    (session_id,),
                ).fetchone()["n"]
                if (
                    per_workflow > config["max_workers_per_workflow"]
                    or per_session > config["max_workers_per_session"]
                ):
                    over = True
                    conn.execute(
                        "UPDATE agent_runs SET status='over_cap' WHERE agent_id=?",
                        (agent_id,),
                    )
            _event(
                conn,
                "workflow_worker_started",
                session_id=session_id,
                project=project,
                extra={"agent_id": agent_id, "workflow_id": workflow_id, "over_cap": over},
            )
            return {"workflow_id": workflow_id, "over_cap": over, "agent_id": agent_id}


def workflow_worker_row(agent_id):
    if not isinstance(agent_id, str) or agent_id == "":
        return None
    with _db() as conn:
        row = conn.execute("SELECT * FROM agent_runs WHERE agent_id=?", (agent_id,)).fetchone()
        return dict(row) if row is not None else None


def set_worker_model(agent_id, model_id, family, status=None):
    if not isinstance(agent_id, str) or agent_id == "":
        return None
    model = model_id if isinstance(model_id, str) else ""
    verified = family if isinstance(family, str) else ""
    with _db() as conn:
        with _Tx(conn):
            if isinstance(status, str) and status:
                conn.execute(
                    """UPDATE agent_runs SET resolved_model=?, verified_family=?, status=?
                       WHERE agent_id=?""",
                    (model, verified, status, agent_id),
                )
            else:
                conn.execute(
                    "UPDATE agent_runs SET resolved_model=?, verified_family=? WHERE agent_id=?",
                    (model, verified, agent_id),
                )
    return agent_id


def peek_pending_packet(session_id, request_id):
    """Packet text for a pending request. Does not change the row."""
    if not isinstance(session_id, str) or not isinstance(request_id, str) or request_id == "":
        return None
    with _db() as conn:
        row = conn.execute(
            "SELECT session_id, state, packet_text FROM requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None or row["session_id"] != session_id or row["state"] != "JUDGE_PENDING":
            return None
        text = row["packet_text"]
        return text if isinstance(text, str) and text else None


def peek_grant_invocation(session_id, request_id):
    """Canonical invocation JSON for a ready grant. Does not consume it."""
    if not isinstance(session_id, str) or not isinstance(request_id, str) or request_id == "":
        return None
    with _db() as conn:
        row = conn.execute(
            """SELECT g.invocation_json FROM grants g
               JOIN requests r ON r.request_id = g.request_id
               WHERE g.request_id=? AND g.session_id=? AND g.uses_left>0
                 AND r.state='GRANT_READY'""",
            (request_id, session_id),
        ).fetchone()
        if row is None:
            return None
        raw = row["invocation_json"]
        return raw if isinstance(raw, str) and raw else None


def pending_replan_requests(session_id):
    """Open Opus-main replan requests for one session."""
    if not isinstance(session_id, str) or session_id == "":
        return []
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM requests
               WHERE session_id=? AND state='REPLAN_PENDING' AND candidate_type='REPLAN_REQUEST'
               ORDER BY created""",
            (session_id,),
        ).fetchall()
        return [_request_dict(row) for row in rows]


def format_replan_required(request):
    """One REPLAN_REQUIRED line for an open Opus-main replan."""
    if not isinstance(request, dict) or request.get("state") != "REPLAN_PENDING":
        return ""
    request_id = request.get("request_id") or ""
    task = request.get("plan_task_id") or request.get("task_id") or ""
    version = request.get("plan_version")
    stage = request.get("stage_id") if isinstance(request.get("stage_id"), str) else ""
    question = redact(request.get("unresolved_question") or "", 200)
    return (
        "REPLAN_REQUIRED request_id=%s task=%s plan_version=%s stage=%s question=%s. "
        "Revise the plan yourself (no judge needed): save it to ~/.claude/plans/router/drafts/%s.json "
        "with base_plan_version=%s, run router-plan promote %s <file> --resolves %s, "
        "then start a fresh Workflow stage only if the remaining work needs parallel fan-out."
        % (request_id, task, version, stage, question, task, version, task, request_id)
    )


def take_surface_lines(session_id):
    """Return queued surface lines for this session and delete them."""
    if not isinstance(session_id, str) or session_id == "":
        return []
    if not os.path.lexists(ledger_path()):
        return []
    with _db() as conn:
        with _Tx(conn):
            rows = conn.execute(
                "SELECT line FROM surface_lines WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
            if rows:
                conn.execute("DELETE FROM surface_lines WHERE session_id=?", (session_id,))
            return [row["line"] for row in rows if isinstance(row["line"], str) and row["line"]]


def note_replan_blocked(session_id, task_id, plan_version, stage_id, reason, project=""):
    """Record a replan that was not registered, and queue one surface line."""
    line = "REPLAN_BLOCKED task=%s stage=%s reason=%s" % (task_id, stage_id, reason)
    event = "replan_limit" if reason == "replan_limit" else "replan_blocked"
    with _db() as conn:
        with _Tx(conn):
            _sweep(conn, time.time())
            _mark_stage_needs_judgment(conn, (task_id, plan_version, stage_id))
            _queue_surface(conn, session_id, line)
            _event(
                conn,
                event,
                session_id=session_id,
                task_id=task_id,
                project=project if isinstance(project, str) else "",
                extra={"reason": reason, "stage_id": stage_id},
            )
    write_surface_flag(session_id)
    return line


def replan_candidate_key(task_id, plan_version, stage_id, reason_code):
    """One replan per task version, stage, and reason. Session is not part of the key."""
    raw = "%s|%s|%s|%s" % (task_id or "", plan_version, stage_id or "", reason_code or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def correlated_stage(agent_id, stage_id, plan_version):
    """Stage row when the worker's workflow and the footer name the same open stage.

    The row must be started or needs_judgment, and its plan_version must be the
    task's active version. needs_judgment is that same dispatch after the first
    worker asked for a replan, so a second worker still shares it. A finished
    status, or a plan version that is no longer active, does not correlate.
    Uses only ledger rows. Footer fields alone never count.
    """
    if not isinstance(agent_id, str) or agent_id == "":
        return None
    if not isinstance(stage_id, str) or stage_id == "" or type(plan_version) is not int:
        return None
    if not os.path.lexists(ledger_path()):
        return None
    with _db() as conn:
        run = conn.execute(
            "SELECT workflow_id FROM agent_runs WHERE agent_id=?",
            (agent_id,),
        ).fetchone()
        if run is None or not isinstance(run["workflow_id"], str) or run["workflow_id"] == "":
            return None
        workflow = conn.execute(
            "SELECT tool_use_id FROM workflows WHERE tool_use_id=?",
            (run["workflow_id"],),
        ).fetchone()
        if workflow is None:
            return None
        stage = conn.execute(
            """SELECT * FROM stages
               WHERE workflow_tool_use_id=?
               ORDER BY attempt DESC LIMIT 1""",
            (workflow["tool_use_id"],),
        ).fetchone()
        if stage is None:
            return None
        if stage["stage_id"] != stage_id or stage["plan_version"] != plan_version:
            return None
        if stage["status"] not in ("started", "needs_judgment"):
            return None
        active = conn.execute(
            "SELECT version FROM plans WHERE task_id=? AND status='active'",
            (stage["task_id"],),
        ).fetchone()
        if active is None or active["version"] != stage["plan_version"]:
            return None
        return dict(stage)


def pending_workflow_candidates(session_id):
    """JUDGE_PENDING workflow-worker requests for one session."""
    if not isinstance(session_id, str) or session_id == "":
        return []
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM requests
               WHERE session_id=? AND state='JUDGE_PENDING' AND trigger='WORKFLOW_WORKER_REQUEST'
               ORDER BY created""",
            (session_id,),
        ).fetchall()
        return [_request_dict(row) for row in rows]


def surface_flag_path(session_id):
    """Path of the main-session surface flag, or None when the id is unsafe."""
    if not isinstance(session_id, str) or session_id == "":
        return None
    if (
        "/" in session_id
        or "\\" in session_id
        or ".." in session_id
        or session_id.startswith(".")
    ):
        return None
    return os.path.join(ledger_dir(), "surface", session_id)


def write_surface_flag(session_id):
    """Create the surface flag (mode 0600) so the next main PreToolUse can speak."""
    path = surface_flag_path(session_id)
    if path is None:
        return None
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        os.write(fd, b"1\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return path


def _main_family(payload):
    """Main-session family from a payload model, else the transcript tail."""
    if isinstance(payload, dict):
        raw = payload.get("model")
        if isinstance(raw, str) and raw.strip():
            family = _agent_model._model_family(raw)
            if family in ("opus", "sonnet", "haiku"):
                return family
    return _agent_model.main_session_model(payload)


def _try_replan(payload, footer, model_id):
    """Register a correlated replan, or None when the footer is not a replan.

    An uncorrelated PLAN_* footer returns None so the generic path is unchanged.
    A blocked replan returns a result and does not create a candidate.
    """
    reason = footer.get("reason_code")
    if reason not in ("PLAN_REQUIRED", "PLAN_REVISION_REQUIRED"):
        return None
    refs = footer.get("evidence_refs")
    if not isinstance(refs, list) or not any(isinstance(item, str) and item for item in refs):
        return None
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    agent_id = payload.get("agent_id") if isinstance(payload.get("agent_id"), str) else ""
    project = project_of(payload)
    stage = correlated_stage(agent_id, footer.get("stage_id"), footer.get("plan_version"))
    if stage is None:
        record_event(
            "replan_uncorrelated",
            session_id=session_id,
            project=project,
            extra={"agent_id": agent_id},
        )
        return None
    task_id = stage.get("task_id") or ""
    plan_version = stage.get("plan_version")
    stage_id = stage.get("stage_id") or ""
    planning = planning_config()
    if not planning.get("enabled"):
        note_replan_blocked(session_id, task_id, plan_version, stage_id, "planning_disabled", project)
        return {"ok": False, "reason": "REPLAN_BLOCKED", "request": None}
    family = _main_family(payload)
    opus = family == "opus"
    observed = model_id if isinstance(model_id, str) else ""
    import _opus_triggers as triggers

    context_extra = {
        "stage_id": stage_id,
        "plan_version": plan_version,
        "unresolved_question": footer.get("unresolved_question") or "",
        "failed_requirement_ids": footer.get("failed_requirement_ids") or [],
        "completed_stage_ids": footer.get("completed_stage_ids") or [],
        "evidence_refs": [item for item in refs if isinstance(item, str)],
    }
    result = register_candidate(
        session_id=session_id,
        task_id=task_id,
        trigger="WORKFLOW_WORKER_REQUEST",
        reason_code=reason,
        requester_agent_type="general-purpose",
        scope_digest=replan_candidate_key(task_id, plan_version, stage_id, reason),
        task_summary=footer.get("unresolved_question") or footer.get("suggested_scope") or task_id,
        current_model_observed=observed,
        objective_failure_evidence=triggers._footer_evidence(footer),
        prior_attempts=footer.get("attempts") if isinstance(footer.get("attempts"), list) else [],
        project=project,
        candidate_type="REPLAN_REQUEST",
        plan_task_id=task_id,
        key_override=replan_candidate_key(task_id, plan_version, stage_id, reason),
        skip_judgment_quota=opus,
        mark_stage=(task_id, plan_version, stage_id),
        follow_state="REPLAN_PENDING" if opus else None,
        replan_cap=planning["max_replans_per_task"],
        surface_line="REPLAN_BLOCKED task=%s stage=%s reason=replan_limit" % (task_id, stage_id),
        context_extra=context_extra,
    )
    if not isinstance(result, dict):
        return result
    if result.get("reason") == "REPLAN_BLOCKED":
        write_surface_flag(session_id)
        return result
    if not opus and result.get("ok"):
        result = triggers._ready(result)
    request = result.get("request") if isinstance(result, dict) else None
    if isinstance(request, dict) and request.get("state") in ("REPLAN_PENDING", "JUDGE_PENDING"):
        write_surface_flag(session_id)
    return result


def register_workflow_candidate(payload, model_id):
    """Turn a worker footer into a replan or a JUDGE_PENDING candidate."""
    if not isinstance(payload, dict):
        return None
    text = payload.get("last_assistant_message")
    if not isinstance(text, str) or text.strip() == "":
        return None
    import _opus_triggers as triggers

    footer = triggers.parse_footer(text)
    if not isinstance(footer, dict) or footer.get("status") != "NEEDS_JUDGMENT":
        return None
    replan = _try_replan(payload, footer, model_id)
    if replan is not None:
        return replan
    unresolved = footer.get("unresolved") if isinstance(footer.get("unresolved"), list) else []
    attempts = footer.get("attempts") if isinstance(footer.get("attempts"), list) else []
    conflicting = footer.get("reason_code") == "CONFLICTING_REQUIREMENTS"
    if not unresolved and not attempts and not conflicting:
        return None
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    observed = model_id if isinstance(model_id, str) else ""
    result = triggers._ready(register_candidate(
        session_id=session_id,
        task_id=footer.get("task_id") or "workflow",
        trigger="WORKFLOW_WORKER_REQUEST",
        reason_code=footer.get("reason_code") or "WORKFLOW_WORKER_REQUEST",
        requester_agent_type="general-purpose",
        scope_digest=hashlib.sha256(json.dumps(footer, sort_keys=True).encode("utf-8")).hexdigest(),
        task_summary=footer.get("suggested_scope") or footer.get("task_id") or "",
        current_model_observed=observed,
        objective_failure_evidence=triggers._footer_evidence(footer),
        prior_attempts=attempts,
        project=project_of(payload),
    ))
    request = result.get("request") if isinstance(result, dict) else None
    if isinstance(request, dict) and request.get("state") == "JUDGE_PENDING":
        write_surface_flag(session_id)
    return result


def cli_main(argv):
    usage = "usage: opus-arbitration [status|show <request_id>|disable|enable]"
    args = list(argv[1:])
    if not args or args[0] not in ("status", "show", "disable", "enable"):
        print(usage, file=sys.stderr)
        return 2
    if args[0] == "show":
        if len(args) != 2:
            print(usage, file=sys.stderr)
            return 2
        return cmd_show(args[1])
    if len(args) != 1:
        print(usage, file=sys.stderr)
        return 2
    if args[0] == "status":
        return cmd_status()
    if args[0] == "disable":
        try:
            disable_arbitration()
        except OSError as exc:
            print("opus-arbitration: %s" % exc, file=sys.stderr)
            return 1
        print("disabled")
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("opus-arbitration: enable refused", file=sys.stderr)
        return 2
    print("Type enable to confirm: ", end="", flush=True)
    if sys.stdin.readline().strip() != "enable":
        print("opus-arbitration: enable refused", file=sys.stderr)
        return 2
    try:
        write_switch("active")
    except OSError as exc:
        print("opus-arbitration: %s" % exc, file=sys.stderr)
        return 1
    print("active")
    return 0
