#!/usr/bin/env python3
"""Guard workflow workers and surface their judge candidates on the main thread.

The fast path imports only json, sys, and os. It returns immediately unless the
payload is a workflow-subagent event, a pinned Opus role PreToolUse, or a
main-thread PreToolUse whose session has a surface flag. Worker PreToolUse
failures deny. Start and Stop never block. The opus-judge and opus-planner
roles may use Read and no other tool.
"""

import json
import os
import sys

_DELEGATION = frozenset({"Agent", "Task", "SendMessage", "Workflow", "Skill"})


def _safe_id(value):
    if not isinstance(value, str) or value == "":
        return None
    if "/" in value or "\\" in value or ".." in value or value.startswith("."):
        return None
    return value


def _log_dir(*parts):
    return os.path.join(os.path.expanduser("~"), ".claude", "logs", "opus-arbitration", *parts)


def _flag_path(session_id):
    if _safe_id(session_id) is None:
        return None
    return _log_dir("surface", session_id)


def _guard_attest_path(session_id, tool_use_id):
    if _safe_id(session_id) is None or _safe_id(tool_use_id) is None:
        return None
    return _log_dir("guard", "attest-%s-%s" % (session_id, tool_use_id))


def _spawns_path(session_id):
    if _safe_id(session_id) is None:
        return None
    return _log_dir("mod", "spawns-%s.jsonl" % session_id)


def _main_workflow(data):
    if not isinstance(data, dict):
        return False
    if data.get("hook_event_name") != "PreToolUse" or data.get("tool_name") != "Workflow":
        return False
    return not _has_agent(data)


def _write_guard_attest(data):
    path = _guard_attest_path(data.get("session_id"), data.get("tool_use_id"))
    if path is None:
        return
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, 0o700, exist_ok=True)
        fd = os.open(
            path,
            os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
    except OSError:
        return
    try:
        os.write(fd, b"1\n")
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        return


def _has_agent(data):
    agent_id = data.get("agent_id")
    return isinstance(agent_id, str) and agent_id.strip() != ""


def _policy_role_names():
    """(judge type, planner type) from policy, or the defaults when unreadable."""
    judge = "opus-judge"
    planner = "opus-planner"
    path = os.path.join(
        os.path.expanduser("~"), ".claude", "hooks", "data", "opus-arbitration-policy.json",
    )
    try:
        import stat
        info = os.lstat(path)
    except OSError:
        return judge, planner
    if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
        return judge, planner
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return judge, planner
    try:
        try:
            blob = os.read(fd, 65537)
        except OSError:
            return judge, planner
    finally:
        os.close(fd)
    if len(blob) > 65536:
        return judge, planner
    try:
        data = json.loads(blob.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, ValueError):
        return judge, planner
    if not isinstance(data, dict):
        return judge, planner
    raw_judge = data.get("judge_agent_type")
    if isinstance(raw_judge, str) and raw_judge != "":
        judge = raw_judge
    planning = data.get("planning")
    if isinstance(planning, dict):
        raw_planner = planning.get("planner_agent_type")
        if isinstance(raw_planner, str) and raw_planner != "":
            planner = raw_planner
    return judge, planner


def _role_tool_result(data):
    """0 when a pinned Opus role may use this tool, deny-code when it may not.

    None means the payload is not a PreToolUse from the judge or planner.
    Policy is read only on this branch. A missing or malformed policy uses
    opus-judge and opus-planner.
    """
    if not isinstance(data, dict) or data.get("hook_event_name") != "PreToolUse":
        return None
    agent_type = data.get("agent_type")
    if not isinstance(agent_type, str) or agent_type == "":
        return None
    judge, planner = _policy_role_names()
    if agent_type != judge and agent_type != planner:
        return None
    tool = data.get("tool_name")
    if tool == "Read":
        return 0
    shown = tool if isinstance(tool, str) else ""
    return _deny("OPUS_ROLE_TOOL_DENIED %s" % shown)


def _interesting(data):
    if not isinstance(data, dict):
        return False
    if data.get("agent_type") == "workflow-subagent":
        return True
    if data.get("hook_event_name") != "PreToolUse" or _has_agent(data):
        return False
    path = _flag_path(data.get("session_id"))
    return path is not None and os.path.exists(path)


def _deny(reason):
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }, ensure_ascii=False))
    sys.stdout.write("\n")
    return 0


def _context(text):
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": text,
        }
    }, ensure_ascii=False))
    sys.stdout.write("\n")
    return 0


def _modules():
    root = os.path.dirname(os.path.realpath(__file__))
    if root not in sys.path:
        sys.path.insert(0, root)
    import _agent_model as agent_model
    import _opus_arbitration as arbitration

    return agent_model, arbitration


def _worker_model(agent_model, data, direct_path):
    path = direct_path
    if not isinstance(path, str) or path == "":
        path = agent_model.find_workflow_transcript(
            data.get("transcript_path"),
            data.get("session_id"),
            data.get("agent_id"),
        )
    if not isinstance(path, str) or path == "":
        return None
    return agent_model.read_worker_model_id(path)


def _remember_model(arb, agent_id, model_id, family, status=None):
    if not isinstance(model_id, str) or model_id == "":
        return
    arb.set_worker_model(agent_id, model_id, family, status)


def _opus_event(arb, data, model_id):
    session_id = data.get("session_id") if isinstance(data.get("session_id"), str) else ""
    agent_id = data.get("agent_id") if isinstance(data.get("agent_id"), str) else ""
    arb.record_event(
        "unarbitrated_opus_run",
        session_id=session_id,
        project=arb.project_of(data),
        extra={"agent_id": agent_id, "model": model_id or "unknown"},
    )


def _spawn_has(session_id, agent_id):
    import stat
    path = _spawns_path(session_id)
    if path is None or not isinstance(agent_id, str) or agent_id == "":
        return False
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if text == "":
                    continue
                try:
                    row = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and row.get("agentId") == agent_id:
                    return True
    except OSError:
        return False
    return False


def _mod_spawned(session_id, agent_id):
    if _spawn_has(session_id, agent_id):
        return True
    import time
    time.sleep(1)
    return _spawn_has(session_id, agent_id)


def _unarbitrated_spawn(arb, data):
    session_id = data.get("session_id") if isinstance(data.get("session_id"), str) else ""
    agent_id = data.get("agent_id") if isinstance(data.get("agent_id"), str) else ""
    try:
        arb.record_event(
            "unarbitrated_workflow_spawn",
            session_id=session_id,
            project=arb.project_of(data),
            extra={"agent_id": agent_id},
        )
    except Exception:
        pass


def _start(arb, data):
    session_id = data.get("session_id") if isinstance(data.get("session_id"), str) else ""
    agent_id = data.get("agent_id") if isinstance(data.get("agent_id"), str) else ""
    if not _mod_spawned(session_id, agent_id):
        _unarbitrated_spawn(arb, data)
    arb.record_workflow_worker(data)
    return 0


def _stop(agent_model, arb, data):
    agent_id = data.get("agent_id") if isinstance(data.get("agent_id"), str) else ""
    model_id = _worker_model(agent_model, data, data.get("agent_transcript_path"))
    family = agent_model.model_family(model_id) if model_id else "unknown"
    if agent_id:
        if model_id:
            arb.set_worker_model(agent_id, model_id, family, "stopped")
        else:
            arb.set_worker_model(agent_id, "", family, "stopped")
    if family == "opus":
        _opus_event(arb, data, model_id)
    arb.register_workflow_candidate(data, model_id or "")
    return 0


def _worker_tool(agent_model, arb, data):
    tool = data.get("tool_name")
    if isinstance(tool, str) and tool.strip() in _DELEGATION:
        return _deny(arb.WORKER_NO_DELEGATION)
    agent_id = data.get("agent_id") if isinstance(data.get("agent_id"), str) else ""
    row = arb.workflow_worker_row(agent_id)
    if row is None or not row.get("workflow_id"):
        return _deny("WORKFLOW_UNTRACKED")
    if row.get("status") == "over_cap":
        return _deny("WORKFLOW_FANOUT_CAP")
    session_id = data.get("session_id") if isinstance(data.get("session_id"), str) else ""
    if not _mod_spawned(session_id, agent_id):
        _unarbitrated_spawn(arb, data)
        return _deny("WORKFLOW_WORKER_NOT_MOD_SPAWNED")
    cached = row.get("verified_family")
    if isinstance(cached, str) and cached != "":
        family = cached
        model_id = row.get("resolved_model") if isinstance(row.get("resolved_model"), str) else ""
        model_id = model_id or None
    else:
        model_id = _worker_model(agent_model, data, None)
        family = agent_model.model_family(model_id) if model_id else "unknown"
        _remember_model(arb, agent_id, model_id, family)
    allowed = arb.workflow_config().get("allowed_worker_families") or []
    if family not in allowed:
        if family == "opus":
            _opus_event(arb, data, model_id)
        shown = model_id if isinstance(model_id, str) and model_id else "unknown"
        return _deny("WORKFLOW_WORKER_MODEL_DENIED model=%s" % shown)
    return 0


def _surface(arb, data):
    session_id = data.get("session_id") if isinstance(data.get("session_id"), str) else ""
    path = _flag_path(session_id)
    texts = []
    mode = data.get("permission_mode")
    for request in arb.pending_workflow_candidates(session_id):
        text = arb.format_candidate_request(request, mode)
        if text:
            texts.append(text)
    for request in arb.pending_replan_requests(session_id):
        text = arb.format_replan_required(request)
        if text:
            texts.append(text)
    for line in arb.take_surface_lines(session_id):
        if line:
            texts.append(line)
    if path and os.path.lexists(path):
        try:
            os.unlink(path)
        except OSError:
            pass
    if not texts:
        return 0
    return _context("\n".join(texts))


def _handle(data):
    agent_model, arb = _modules()
    if data.get("agent_type") == "workflow-subagent":
        event = data.get("hook_event_name")
        if event == "SubagentStart":
            return _start(arb, data)
        if event == "SubagentStop":
            return _stop(agent_model, arb, data)
        return _worker_tool(agent_model, arb, data)
    return _surface(arb, data)


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw else None
    except Exception:
        if "workflow-subagent" in raw:
            return 2
        return 0
    if isinstance(data, dict):
        role = _role_tool_result(data)
        if role is not None:
            return role
    if isinstance(data, dict) and _main_workflow(data):
        _write_guard_attest(data)
    if not _interesting(data):
        return 0
    try:
        return _handle(data)
    except Exception:
        if isinstance(data, dict) and data.get("agent_type") == "workflow-subagent":
            event = data.get("hook_event_name")
            if event in ("SubagentStart", "SubagentStop"):
                return 0
            return _deny("WORKFLOW_GUARD_ERROR")
        return 0


if __name__ == "__main__":
    sys.exit(main())
