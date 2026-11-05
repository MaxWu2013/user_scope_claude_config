"""Plan contract and plan store. Plans are data, never permissions.

Paths resolve through expanduser("~") at call time.
"""

import hashlib
import json
import os
import re
import stat
import time


_TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_RESERVED_TASK_IDS = frozenset({".", "..", "drafts"})
_ORPHAN_AGE_SECONDS = 60
_STEP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_PLAN_KEYS = frozenset({
    "schema_version",
    "task_id",
    "base_plan_version",
    "objective",
    "assumptions",
    "invariants",
    "steps",
    "risks",
    "handoff_notes",
})
_STEP_KEYS = frozenset({
    "id",
    "depends_on",
    "executor_role",
    "allowed_model",
    "instructions",
    "acceptance_checks",
    "requires_user_approval",
})
_MODELS = {
    "workflow-subagent": frozenset({"sonnet", "haiku"}),
    "file-reader": frozenset({"sonnet", "haiku"}),
    "file-writer": frozenset({"sonnet", "haiku"}),
    "cursor-run": frozenset({"cursor"}),
    "main": frozenset({"main"}),
}
_LIST_LIMIT = 20
_READ_LIMIT = 1024 * 1024
_FINISH_STATUS = frozenset({"completed", "failed", "cancelled", "unknown"})


class PlanError(Exception):
    """A plan was rejected or a plan command could not finish."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class PlanStoreError(PlanError):
    """The plan store is missing, stale, or corrupt."""


def plans_root():
    return os.path.join(os.path.expanduser("~"), ".claude", "plans", "router")


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_sha256(obj):
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def scope_digest(obj):
    steps = obj.get("steps") if isinstance(obj, dict) else None
    rows = []
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict):
                rows.append([
                    step.get("id"),
                    step.get("executor_role"),
                    step.get("allowed_model"),
                ])
    rows.sort(key=lambda row: "" if not isinstance(row[0], str) else row[0])
    body = {
        "task_id": obj.get("task_id") if isinstance(obj, dict) else None,
        "objective": obj.get("objective") if isinstance(obj, dict) else None,
        "steps": rows,
    }
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _append(errors, code):
    if code not in errors:
        errors.append(code)


def validate_plan(obj, max_steps=12):
    """Error strings. An empty list means the plan is valid."""
    errors = []
    if not isinstance(obj, dict):
        return ["PLAN_NOT_OBJECT"]
    if not _is_int(max_steps) or max_steps < 1:
        max_steps = 12
    for key in obj:
        if key not in _PLAN_KEYS:
            _append(errors, "PLAN_UNKNOWN_KEY")
    for key in _PLAN_KEYS:
        if key not in obj:
            _append(errors, "PLAN_MISSING_KEY")
    if "schema_version" in obj:
        if not _is_int(obj.get("schema_version")):
            _append(errors, "PLAN_BAD_TYPE")
        elif obj.get("schema_version") != 1:
            _append(errors, "PLAN_BAD_SCHEMA")
    if "task_id" in obj:
        task_id = obj.get("task_id")
        if not isinstance(task_id, str):
            _append(errors, "PLAN_BAD_TYPE")
        elif task_id in _RESERVED_TASK_IDS or _TASK_ID_RE.match(task_id) is None:
            _append(errors, "PLAN_BAD_TASK_ID")
    if "base_plan_version" in obj:
        base = obj.get("base_plan_version")
        if not _is_int(base):
            _append(errors, "PLAN_BAD_TYPE")
        elif base < 0:
            _append(errors, "PLAN_BAD_VERSION")
    _check_text(errors, obj, "objective", 1, 500)
    _check_text(errors, obj, "handoff_notes", 0, 1000)
    for key in ("assumptions", "invariants", "risks"):
        _check_str_list(errors, obj, key, 300, allow_empty_item=True)
    _check_steps(errors, obj.get("steps") if "steps" in obj else None, max_steps)
    _check_canonical(errors, obj)
    return errors


def _check_text(errors, obj, key, low, high):
    if key not in obj:
        return
    value = obj.get(key)
    if not isinstance(value, str):
        _append(errors, "PLAN_BAD_TYPE")
        return
    if len(value) < low or len(value) > high:
        _append(errors, "PLAN_BAD_LENGTH")


def _check_str_list(errors, obj, key, item_max, allow_empty_item):
    if key not in obj:
        return
    value = obj.get(key)
    if not isinstance(value, list):
        _append(errors, "PLAN_BAD_TYPE")
        return
    if len(value) > _LIST_LIMIT:
        _append(errors, "PLAN_LIST_LIMIT")
    for item in value:
        if not isinstance(item, str):
            _append(errors, "PLAN_BAD_TYPE")
            continue
        if (not allow_empty_item and len(item) < 1) or len(item) > item_max:
            _append(errors, "PLAN_BAD_LENGTH")


def _check_steps(errors, steps, max_steps):
    if not isinstance(steps, list):
        if steps is not None:
            _append(errors, "PLAN_BAD_TYPE")
        return
    if len(steps) > _LIST_LIMIT:
        _append(errors, "PLAN_LIST_LIMIT")
    if len(steps) < 1:
        _append(errors, "PLAN_TOO_FEW_STEPS")
    if len(steps) > max_steps:
        _append(errors, "PLAN_TOO_MANY_STEPS")
    ids = []
    graph_ok = True
    for step in steps:
        if not isinstance(step, dict):
            _append(errors, "PLAN_BAD_TYPE")
            graph_ok = False
            continue
        for key in step:
            if key not in _STEP_KEYS:
                _append(errors, "PLAN_UNKNOWN_KEY")
        for key in _STEP_KEYS:
            if key not in step:
                _append(errors, "PLAN_MISSING_KEY")
        step_id = step.get("id")
        if not isinstance(step_id, str) or _STEP_ID_RE.match(step_id) is None:
            _append(errors, "PLAN_BAD_STEP_ID")
            graph_ok = False
        else:
            ids.append(step_id)
        deps = step.get("depends_on")
        if not isinstance(deps, list):
            if "depends_on" in step:
                _append(errors, "PLAN_BAD_TYPE")
            graph_ok = False
        elif len(deps) > _LIST_LIMIT:
            _append(errors, "PLAN_LIST_LIMIT")
            if not all(isinstance(item, str) for item in deps):
                _append(errors, "PLAN_BAD_TYPE")
                graph_ok = False
        elif not all(isinstance(item, str) for item in deps):
            _append(errors, "PLAN_BAD_TYPE")
            graph_ok = False
        role = step.get("executor_role")
        model = step.get("allowed_model")
        if "executor_role" in step and not isinstance(role, str):
            _append(errors, "PLAN_BAD_TYPE")
        elif isinstance(role, str) and role not in _MODELS:
            _append(errors, "PLAN_BAD_ROLE")
        if "allowed_model" in step and not isinstance(model, str):
            _append(errors, "PLAN_BAD_TYPE")
        elif isinstance(role, str) and role in _MODELS and isinstance(model, str) and model not in _MODELS[role]:
            _append(errors, "PLAN_BAD_MODEL")
        if "instructions" in step:
            text = step.get("instructions")
            if not isinstance(text, str):
                _append(errors, "PLAN_BAD_TYPE")
            elif len(text) < 1 or len(text) > 2000:
                _append(errors, "PLAN_BAD_LENGTH")
        checks = step.get("acceptance_checks")
        if "acceptance_checks" in step:
            if not isinstance(checks, list):
                _append(errors, "PLAN_BAD_TYPE")
            else:
                if len(checks) < 1:
                    _append(errors, "PLAN_MISSING_CHECK")
                if len(checks) > _LIST_LIMIT:
                    _append(errors, "PLAN_LIST_LIMIT")
                for item in checks:
                    if not isinstance(item, str):
                        _append(errors, "PLAN_BAD_TYPE")
                    elif len(item) < 1 or len(item) > 200:
                        _append(errors, "PLAN_BAD_LENGTH")
        if "requires_user_approval" in step and not isinstance(step.get("requires_user_approval"), bool):
            _append(errors, "PLAN_BAD_TYPE")
    if len(ids) != len(set(ids)):
        _append(errors, "PLAN_DUPLICATE_ID")
        graph_ok = False
    if graph_ok and ids:
        _check_graph(errors, steps, set(ids))


def _check_graph(errors, steps, ids):
    indegree = {item: 0 for item in ids}
    forward = {item: [] for item in ids}
    for step in steps:
        step_id = step["id"]
        seen = set()
        for dep in step["depends_on"]:
            if dep == step_id:
                _append(errors, "PLAN_SELF_DEPENDENCY")
                continue
            if dep not in ids:
                _append(errors, "PLAN_DANGLING_DEPENDENCY")
                continue
            if dep in seen:
                continue
            seen.add(dep)
            indegree[step_id] += 1
            forward[dep].append(step_id)
    queue = [item for item in ids if indegree[item] == 0]
    seen_count = 0
    while queue:
        node = queue.pop()
        seen_count += 1
        for nxt in forward[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if seen_count != len(ids):
        _append(errors, "PLAN_CYCLE")


def _check_canonical(errors, obj):
    try:
        text = canonical(obj)
    except (TypeError, ValueError):
        _append(errors, "PLAN_NOT_SERIALIZABLE")
        return
    if "opus" in text.lower():
        _append(errors, "PLAN_NAMES_OPUS")
    if "cursor-agent -p" in text:
        _append(errors, "PLAN_BYPASSES_CURSOR_RUN")


def snapshot(record, max_chars=6000):
    """Plain text carried in Workflow args. Never contains opus."""
    if not isinstance(record, dict):
        record = {}
    plan = record.get("plan") if isinstance(record.get("plan"), dict) else record
    version = record.get("version") if isinstance(record.get("plan"), dict) else ""
    sha = record.get("content_sha256") if isinstance(record.get("plan"), dict) else ""
    if not isinstance(sha, str) or sha == "":
        sha = content_sha256(plan)
    lines = [
        "ROUTER-PLAN task=%s version=%s sha256=%s" % (plan.get("task_id"), version, sha),
        plan.get("objective") if isinstance(plan.get("objective"), str) else "",
    ]
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    for step in steps:
        if not isinstance(step, dict):
            continue
        deps = step.get("depends_on") if isinstance(step.get("depends_on"), list) else []
        checks = step.get("acceptance_checks") if isinstance(step.get("acceptance_checks"), list) else []
        dep_text = ",".join(item for item in deps if isinstance(item, str))
        check_text = "; ".join(item for item in checks if isinstance(item, str))
        lines.append(
            "- %s [%s/%s] deps=%s: %s | checks: %s"
            % (
                step.get("id"),
                step.get("executor_role"),
                step.get("allowed_model"),
                dep_text,
                step.get("instructions") if isinstance(step.get("instructions"), str) else "",
                check_text,
            )
        )
    text = "\n".join(lines)
    if _is_int(max_chars) and max_chars >= 0 and len(text) > max_chars:
        marker = "\n[truncated]"
        if max_chars < len(marker):
            text = "[truncated]"[:max_chars]
        else:
            text = text[: max_chars - len(marker)] + marker
    assert "opus" not in text.lower()
    return text


def _task_dir(task_id):
    if (
        not isinstance(task_id, str)
        or task_id in _RESERVED_TASK_IDS
        or _TASK_ID_RE.match(task_id) is None
    ):
        raise PlanError("PLAN_BAD_TASK_ID")
    root = os.path.normpath(plans_root())
    path = os.path.normpath(os.path.join(root, task_id))
    if os.path.dirname(path) != root or os.path.basename(path) != task_id:
        raise PlanError("PLAN_BAD_TASK_ID")
    return path


def _require_real_dir(path):
    """Create a real directory. A symlink, or a non-directory, is refused.

    lstat runs before makedirs and again before chmod, so chmod never follows
    a symlink that was planted at this path.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise PlanStoreError("ROUTER_PLAN_ERROR") from exc
    if info is not None and (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)):
        raise PlanStoreError("ROUTER_PLAN_ERROR")
    if info is None:
        os.makedirs(path, exist_ok=True)
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise PlanStoreError("ROUTER_PLAN_ERROR") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PlanStoreError("ROUTER_PLAN_ERROR")
    os.chmod(path, 0o700)


def _ensure_task_dir(task_id):
    root = plans_root()
    _require_real_dir(root)
    path = _task_dir(task_id)
    _require_real_dir(path)
    return path


def _read_bytes(path, missing_ok):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise PlanStoreError("ROUTER_PLAN_CORRUPT")
    except OSError as exc:
        raise PlanStoreError("ROUTER_PLAN_CORRUPT") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > _READ_LIMIT:
        raise PlanStoreError("ROUTER_PLAN_CORRUPT")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise PlanStoreError("ROUTER_PLAN_CORRUPT") from exc
    try:
        blob = os.read(fd, _READ_LIMIT + 1)
    except OSError as exc:
        raise PlanStoreError("ROUTER_PLAN_CORRUPT") from exc
    finally:
        os.close(fd)
    if len(blob) > _READ_LIMIT:
        raise PlanStoreError("ROUTER_PLAN_CORRUPT")
    return blob


def _read_json(path, missing_ok):
    blob = _read_bytes(path, missing_ok)
    if blob is None:
        return None
    try:
        data = json.loads(blob.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PlanStoreError("ROUTER_PLAN_CORRUPT") from exc
    if not isinstance(data, dict):
        raise PlanStoreError("ROUTER_PLAN_CORRUPT")
    return data


def _pointer_path(task_id):
    return os.path.join(_task_dir(task_id), "current")


def _version_path(task_id, version):
    return os.path.join(_task_dir(task_id), "v%s.json" % version)


def _load_record(task_id, pointer):
    version = pointer.get("version")
    pointer_sha = pointer.get("content_sha256")
    if not _is_int(version) or version < 1 or not isinstance(pointer_sha, str) or pointer_sha == "":
        raise PlanStoreError("ROUTER_PLAN_CORRUPT")
    path = _version_path(task_id, version)
    record = _read_json(path, missing_ok=False)
    if record.get("version") != version or record.get("content_sha256") != pointer_sha:
        raise PlanStoreError("ROUTER_PLAN_CORRUPT")
    plan = record.get("plan")
    if not isinstance(plan, dict):
        raise PlanStoreError("ROUTER_PLAN_CORRUPT")
    if content_sha256(plan) != pointer_sha:
        raise PlanStoreError("ROUTER_PLAN_CORRUPT")
    return record


def load_current(task_id):
    """Current plan record, or None when this task has no pointer."""
    try:
        path = _pointer_path(task_id)
    except PlanError:
        raise
    try:
        info_missing = not os.path.lexists(path)
    except OSError as exc:
        raise PlanStoreError("ROUTER_PLAN_CORRUPT") from exc
    if info_missing:
        return None
    pointer = _read_json(path, missing_ok=False)
    record = _load_record(task_id, pointer)
    plan = record.get("plan")
    max_steps = 12
    try:
        import _opus_arbitration as arb

        max_steps = arb.planning_config()["max_steps"]
    except Exception:
        max_steps = 12
    if validate_plan(plan, max_steps=max_steps):
        raise PlanStoreError("ROUTER_PLAN_CORRUPT")
    if record.get("scope_digest") != scope_digest(plan):
        raise PlanStoreError("ROUTER_PLAN_CORRUPT")
    return record


def _current_version(task_id):
    path = _pointer_path(task_id)
    if not os.path.lexists(path):
        return 0
    pointer = _read_json(path, missing_ok=False)
    record = _load_record(task_id, pointer)
    return record["version"]


def _write_exclusive(path, blob):
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise PlanStoreError("ROUTER_PLAN_STALE") from exc
    except OSError as exc:
        raise PlanStoreError("ROUTER_PLAN_ERROR") from exc
    try:
        os.write(fd, blob)
        os.fsync(fd)
    except Exception:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    os.close(fd)
    os.chmod(path, 0o600)


def _write_pointer(directory, payload):
    blob = (json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    tmp = os.path.join(directory, ".current.%s.tmp" % os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, blob)
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
    target = os.path.join(directory, "current")
    try:
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(target, 0o600)


def _recover_orphan(directory, version):
    """Rename a stale next-version file, or raise when it is too new.

    Returns the orphan file name, or None when v<N>.json is absent.
    A file older than 60 seconds is renamed to v<N>.orphan-<epoch>.json.
    A younger file, or anything that is not a regular file, is stale.
    """
    path = os.path.join(directory, "v%s.json" % version)
    if not os.path.lexists(path):
        return None
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise PlanStoreError("ROUTER_PLAN_STALE") from exc
    if not stat.S_ISREG(info.st_mode):
        raise PlanStoreError("ROUTER_PLAN_STALE")
    if time.time() - info.st_mtime <= _ORPHAN_AGE_SECONDS:
        raise PlanStoreError("ROUTER_PLAN_STALE")
    name = "v%s.orphan-%s.json" % (version, int(time.time()))
    dest = os.path.join(directory, name)
    if os.path.lexists(dest):
        name = "v%s.orphan-%s-%s.json" % (version, int(time.time()), os.getpid())
        dest = os.path.join(directory, name)
    os.rename(path, dest)
    return name


def promote(task_id, plan_obj, max_versions=3, max_steps=12, resolves=None):
    """Validate and store the next plan version for task_id."""
    errors = validate_plan(plan_obj, max_steps=max_steps)
    if errors:
        raise PlanError(errors[0])
    if not isinstance(task_id, str) or plan_obj.get("task_id") != task_id:
        raise PlanError("PLAN_TASK_MISMATCH")
    if not _is_int(max_versions) or max_versions < 1:
        raise PlanError("PLAN_BAD_LIMIT")
    directory = _ensure_task_dir(task_id)
    current = _current_version(task_id)
    if plan_obj.get("base_plan_version") != current:
        raise PlanStoreError("ROUTER_PLAN_STALE")
    version = current + 1
    if version > max_versions:
        raise PlanStoreError("ROUTER_PLAN_VERSION_LIMIT")
    orphan = _recover_orphan(directory, version)
    record = {
        "version": version,
        "plan": plan_obj,
        "content_sha256": content_sha256(plan_obj),
        "scope_digest": scope_digest(plan_obj),
        "supersedes_version": current if current else None,
        "created": time.time(),
        "resolves": resolves,
    }
    blob = (json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    _write_exclusive(os.path.join(directory, "v%s.json" % version), blob)
    _write_pointer(directory, {"version": version, "content_sha256": record["content_sha256"]})
    if orphan:
        record["recovered_orphan"] = orphan
    return record


def resolve_draft(path):
    """Real path of a file inside the drafts directory, or None."""
    if not isinstance(path, str) or path.strip() == "":
        return None
    drafts = os.path.realpath(os.path.join(plans_root(), "drafts"))
    target = os.path.realpath(os.path.expanduser(path))
    if not target.startswith(drafts + os.sep):
        return None
    return target


def _emit(ok, code, status, **extra):
    body = {"ok": True if ok else False, "code": code}
    body.update(extra)
    print(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
    return status


def _usage():
    print(
        "usage: router-plan validate <file> | promote <task_id> <file> [--resolves <request_id>] "
        "| show <task_id> | snapshot <task_id> | stage start <task_id> <plan_version> <stage_id> "
        "| stage finish <task_id> <plan_version> <stage_id> --status completed|failed|cancelled|unknown "
        "| close <request_id> --reason <text>",
        flush=True,
    )
    print(
        'headless sessions need --allowedTools "Write(<HOME>/.claude/plans/router/drafts/*)" '
        "or a matching permission rule.",
        flush=True,
    )
    # Usage is a stdout line so the JSON object stays last. The human line is the usage.
    return _emit(False, "USAGE", 2)


def _read_promote_draft(path):
    """Draft JSON for promote. A missing or unreadable file is ROUTER_DRAFT_MISSING."""
    try:
        blob = _read_bytes(path, missing_ok=False)
    except PlanStoreError as exc:
        if exc.code == "ROUTER_PLAN_CORRUPT":
            raise PlanStoreError("ROUTER_DRAFT_MISSING") from exc
        raise
    try:
        data = json.loads(blob.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PlanError("PLAN_INVALID") from exc
    return data


def _read_plan_file(path):
    blob = _read_bytes(path, missing_ok=False)
    try:
        data = json.loads(blob.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PlanError("PLAN_INVALID") from exc
    return data


def _version_arg(text):
    if not isinstance(text, str) or text == "" or not text.isdigit():
        return None
    return int(text)


def _session_id():
    value = os.environ.get("CLAUDE_SESSION_ID")
    if not isinstance(value, str):
        return ""
    return value


def _planning_limits():
    import _opus_arbitration as arb

    config = arb.planning_config(arb.load_policy())
    return arb, config


def _cmd_validate(path):
    _arb, config = _planning_limits()
    data = _read_plan_file(path)
    errors = validate_plan(data, max_steps=config["max_steps"])
    for item in errors:
        print(item)
    if errors:
        return _emit(False, errors[0], 1)
    return _emit(True, "OK", 0)


def _cmd_promote(args):
    resolves = None
    positionals = []
    index = 0
    while index < len(args):
        item = args[index]
        if item == "--resolves":
            if resolves is not None or index + 1 >= len(args):
                return _usage()
            resolves = args[index + 1]
            index += 2
            continue
        if item.startswith("-"):
            return _usage()
        positionals.append(item)
        index += 1
    if len(positionals) != 2:
        return _usage()
    task_id, raw_path = positionals
    target = resolve_draft(raw_path)
    if target is None:
        print("router-plan: draft must live under plans/router/drafts")
        return _emit(False, "ROUTER_DRAFT_LOCATION", 2)
    data = _read_promote_draft(target)
    arb, config = _planning_limits()
    record = promote(
        task_id,
        data,
        max_versions=config["max_plan_versions_per_task"],
        max_steps=config["max_steps"],
        resolves=resolves,
    )
    orphan = record.get("recovered_orphan")
    if isinstance(orphan, str) and orphan:
        print("orphan=%s" % orphan)
    try:
        arb.record_plan_version(
            _session_id(),
            task_id,
            record["version"],
            record["content_sha256"],
            record["scope_digest"],
        )
    except Exception:
        print("ROUTER_PLAN_LEDGER_FAILED version=%s" % record["version"])
        return _emit(False, "ROUTER_PLAN_LEDGER_FAILED", 1, version=record["version"])
    resolved = None
    if resolves is not None:
        resolved = True if arb.resolve_replan(resolves, "PLAN_PROMOTED") else False
    arb.resolve_open_replans(task_id, "superseded_by_v%s" % record["version"])
    print(
        "version=%s sha256=%s scope_digest=%s"
        % (record["version"], record["content_sha256"], record["scope_digest"])
    )
    extra = {
        "version": record["version"],
        "content_sha256": record["content_sha256"],
        "scope_digest": record["scope_digest"],
    }
    if resolved is not None:
        extra["resolved"] = resolved
    return _emit(True, "OK", 0, **extra)


def _cmd_show(task_id):
    record = load_current(task_id)
    if record is None:
        print("router-plan: no current plan")
        return _emit(False, "ROUTER_PLAN_MISSING", 1)
    import _opus_arbitration as arb

    stages = arb.task_stages(task_id)
    ledger_missing = arb.active_plan_version(task_id) != record.get("version")
    if ledger_missing:
        print("ROUTER_PLAN_LEDGER_MISSING version=%s" % record.get("version"))
    print(
        "task=%s version=%s sha256=%s scope_digest=%s"
        % (task_id, record.get("version"), record.get("content_sha256"), record.get("scope_digest"))
    )
    print("stages:")
    if not stages:
        print("  (none)")
    for row in stages:
        print(
            "  stage=%s plan_version=%s attempt=%s status=%s session=%s"
            % (
                row.get("stage_id"),
                row.get("plan_version") if row.get("plan_version") is not None else "",
                row.get("attempt"),
                row.get("status"),
                row.get("session_id") or "",
            )
        )
    return _emit(
        True,
        "OK",
        0,
        version=record.get("version"),
        content_sha256=record.get("content_sha256"),
        scope_digest=record.get("scope_digest"),
        stages=len(stages),
        **({"ledger_missing": True} if ledger_missing else {}),
    )


def _cmd_snapshot(task_id):
    record = load_current(task_id)
    if record is None:
        print("router-plan: no current plan")
        return _emit(False, "ROUTER_PLAN_MISSING", 1)
    print(snapshot(record))
    return _emit(True, "OK", 0, version=record.get("version"))


def _cmd_stage(args):
    if not args:
        return _usage()
    action = args[0]
    import _opus_arbitration as arb

    if action == "start":
        if len(args) != 4:
            return _usage()
        version = _version_arg(args[2])
        if version is None:
            return _usage()
        ok, code, attempt = arb.stage_start(_session_id(), args[1], version, args[3])
        if ok:
            print("attempt=%s" % attempt)
        else:
            print("router-plan: %s" % code)
        return _emit(ok, "OK" if ok else code, 0 if ok else 1, attempt=attempt)
    if action == "finish":
        parsed = _parse_finish(args[1:])
        if parsed is None:
            return _usage()
        task_id, version_text, stage_id, status = parsed
        version = _version_arg(version_text)
        if version is None:
            return _usage()
        if status not in _FINISH_STATUS:
            print("router-plan: bad stage status")
            return _emit(False, "ROUTER_STAGE_STATUS", 1)
        ok = arb.stage_finish(task_id, version, stage_id, status)
        print("finished" if ok else "router-plan: no open stage")
        return _emit(ok, "OK" if ok else "ROUTER_STAGE_NOT_OPEN", 0 if ok else 1)
    return _usage()


def _parse_finish(args):
    status = None
    positionals = []
    index = 0
    while index < len(args):
        item = args[index]
        if item == "--status":
            if status is not None or index + 1 >= len(args):
                return None
            status = args[index + 1]
            index += 2
            continue
        if item.startswith("-"):
            return None
        positionals.append(item)
        index += 1
    if status is None or len(positionals) != 3:
        return None
    return positionals[0], positionals[1], positionals[2], status


def _cmd_close(args):
    reason = None
    positionals = []
    index = 0
    while index < len(args):
        item = args[index]
        if item == "--reason":
            if reason is not None or index + 1 >= len(args):
                return _usage()
            reason = args[index + 1]
            index += 2
            continue
        if item.startswith("-"):
            return _usage()
        positionals.append(item)
        index += 1
    if reason is None or len(positionals) != 1:
        return _usage()
    if len(reason) > 200:
        print("router-plan: reason is too long")
        return _emit(False, "REASON_TOO_LONG", 1)
    import _opus_arbitration as arb

    ok = arb.resolve_replan(positionals[0], reason)
    print("resolved" if ok else "refused")
    return _emit(ok, "OK" if ok else "REPLAN_NOT_PENDING", 0 if ok else 1)


def _cli(argv):
    args = list(argv[1:])
    if not args:
        return _usage()
    command = args[0]
    if command == "validate" and len(args) == 2:
        return _cmd_validate(args[1])
    if command == "promote":
        return _cmd_promote(args[1:])
    if command == "show" and len(args) == 2:
        return _cmd_show(args[1])
    if command == "snapshot" and len(args) == 2:
        return _cmd_snapshot(args[1])
    if command == "stage":
        return _cmd_stage(args[1:])
    if command == "close":
        return _cmd_close(args[1:])
    return _usage()


def cli_main(argv):
    try:
        return _cli(argv)
    except PlanError as exc:
        return _emit(False, exc.code, 1)
    except Exception:
        return _emit(False, "ROUTER_PLAN_ERROR", 1)
