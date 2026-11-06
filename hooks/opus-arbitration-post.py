#!/usr/bin/env python3
"""PostToolUse verifier for Opus arbitration.

Never blocks and never emits a permission decision. Prints additionalContext
only when the main session has a next step. Any exception records
post_hook_error when it can, prints nothing, and exits 0.
"""

import json
import os
import sys

sys.dont_write_bytecode = True

_ROOT = os.path.dirname(os.path.realpath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

arb = None
triggers = None


def _emit(event_name, text):
    if not text:
        return 0
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": text,
        }
    }, ensure_ascii=False))
    sys.stdout.write("\n")
    return 0


def _event_name(payload):
    name = payload.get("hook_event_name") if isinstance(payload, dict) else None
    if name in ("PostToolUse", "PostToolUseFailure"):
        return name
    return "PostToolUse"


def _inside(payload):
    agent_id = payload.get("agent_id") if isinstance(payload, dict) else None
    return isinstance(agent_id, str) and agent_id.strip() != ""


def _tool_input(payload):
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    if isinstance(tool_input, dict):
        return tool_input
    return {}


def _response(payload):
    response = payload.get("tool_response") if isinstance(payload, dict) else None
    if isinstance(response, dict):
        return response
    return {}


def _text(response):
    parts = []
    content = response.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
    elif isinstance(content, str):
        parts.append(content)
    return "".join(parts)


def _session(payload):
    session_id = payload.get("session_id")
    if isinstance(session_id, str):
        return session_id
    return ""


def _crash(payload):
    try:
        session_id = _session(payload) if isinstance(payload, dict) else ""
        project = arb.project_of(payload) if isinstance(payload, dict) else ""
        arb.record_event("post_hook_error", session_id=session_id, project=project)
    except Exception:
        return


def _candidate_text(result, payload=None):
    if not isinstance(result, dict) or not result.get("ok") or not result.get("created"):
        return ""
    request = result.get("request") or {}
    if request.get("state") != "JUDGE_PENDING":
        return ""
    if not request.get("packet_text") and isinstance(result.get("packet"), str):
        request = dict(request)
        request["packet_text"] = result["packet"]
    mode = payload.get("permission_mode") if isinstance(payload, dict) else None
    return arb.format_candidate_request(request, mode)


def _retry_target(model):
    """Where a RETRY runs. Opus, missing, and anything else stay off Opus."""
    text = model.strip() if isinstance(model, str) else ""
    low = text.lower()
    if text and "opus" not in low and ("sonnet" in low or "haiku" in low):
        return "on %s (or answer directly)" % text
    return "on the current non-Opus model"


def _decision_text(result, payload=None):
    if not isinstance(result, dict) or result.get("uncorrelated"):
        return ""
    request_id = result.get("request_id") or ""
    if result.get("failed_closed") or result.get("decision") is None and result.get("reason"):
        reason = result.get("reason") or "FAILED_CLOSED"
        if not request_id:
            return ""
        return (
            "ARBITRATION_FAILED request_id=%s reason=%s. Opus stays denied for this task; "
            "continue without Opus or report the blocker. Do not relaunch the judge for this request."
            % (request_id, reason)
        )
    decision = result.get("decision")
    if decision == "CONTINUE":
        return (
            "ARBITRATION_DECISION request_id=%s decision=CONTINUE. Do not use Opus. "
            "Continue the task on the current model. Next step: %s"
            % (request_id, result.get("suggested_next_step") or "")
        )
    if decision == "RETRY":
        return (
            "ARBITRATION_DECISION request_id=%s decision=RETRY. Do not use Opus. "
            "Make at most ONE materially different retry %s using: %s"
            % (request_id, _retry_target(result.get("recommended_model")), result.get("suggested_next_step") or "")
        )
    if decision == "STOP":
        return (
            "ARBITRATION_DECISION request_id=%s decision=STOP. Do not use Opus. "
            "Report this blocker to the user/run log and move on: %s"
            % (request_id, result.get("rationale") or "")
        )
    if decision == "ESCALATE":
        ttl = result.get("ttl")
        ttl_text = str(ttl) if isinstance(ttl, int) else ""
        if result.get("pending"):
            return (
                "ARBITRATION_DECISION request_id=%s decision=ESCALATE. A one-use grant "
                "(expires in %ss) matches the original denied Agent call. Re-issue that call now, "
                "unchanged (same subagent_type, model, description, prompt, foreground)."
                % (request_id, ttl_text)
            )
        worker = result.get("worker_invocation")
        if not isinstance(worker, dict):
            worker = {}
        mode = payload.get("permission_mode") if isinstance(payload, dict) else None
        if mode == "auto":
            canon = json.dumps(worker, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            return (
                "ARBITRATION_DECISION request_id=%s decision=ESCALATE. A one-use grant "
                "(expires in %ss) is ready. Launch the Agent tool now, foreground, and copy this invocation exactly:\n%s"
                % (request_id, ttl_text, canon)
            )
        subagent_type = worker.get("subagent_type") if isinstance(worker.get("subagent_type"), str) else ""
        description = worker.get("description") if isinstance(worker.get("description"), str) else ""
        return (
            "ARBITRATION_DECISION request_id=%s decision=ESCALATE. A one-use grant "
            "(expires in %ss) is ready. Launch the Agent tool now, foreground, "
            'subagent_type "%s", model "opus", description "%s", '
            'prompt exactly "ARBITRATION-GRANT request_id=%s" '
            "(the hook inserts the approved prompt)."
            % (request_id, ttl_text, subagent_type, description, request_id)
        )
    return ""


_CONTROL_AGENTS = frozenset({"opus-planner", "opus-judge"})
_WORKFLOW_PARENT_KEYS = frozenset({
    "workflow",
    "workflowRunId",
    "workflow_run_id",
    "spawnedByWorkflowRunId",
    "spawned_by_workflow_run_id",
})
_PARENT_NAME_KEYS = frozenset({"parent", "caller", "parentTool", "parent_tool"})
_PARENT_TOOL_KEYS = frozenset({
    "parentTool",
    "parent_tool",
    "parentToolName",
    "parent_tool_name",
})
_PATH_KEYS = frozenset({
    "agent_transcript_path",
    "agentTranscriptPath",
    "transcriptPath",
    "transcript_path",
    "transcriptDir",
})


def _under_workflows(path):
    """True when a path sits under a subagents/workflows directory."""
    if not isinstance(path, str) or path.strip() == "":
        return False
    parts = os.path.normpath(path).split(os.sep)
    for index in range(len(parts) - 1):
        if parts[index] == "subagents" and parts[index + 1] == "workflows":
            return True
    return False


def _names_workflow(value):
    if isinstance(value, str):
        return value.strip() == "Workflow"
    if not isinstance(value, dict):
        return False
    for field in ("type", "tool", "name", "tool_name", "toolName"):
        shown = value.get(field)
        if isinstance(shown, str) and shown.strip() == "Workflow":
            return True
    return False


def _workflow_parent(obj):
    """True when a tool input or response shows a Workflow parent.

    Free-text fields such as content are not scanned. A plan that mentions
    a workflow path stays a main-session plan.
    """
    if not isinstance(obj, dict):
        return False
    for key, value in obj.items():
        if key in _WORKFLOW_PARENT_KEYS:
            if isinstance(value, str) and value.strip():
                return True
            if isinstance(value, dict) and value:
                return True
        if key in _PARENT_NAME_KEYS and _names_workflow(value):
            return True
        if key in _PARENT_TOOL_KEYS and isinstance(value, str) and value.strip() == "Workflow":
            return True
        if key in _PATH_KEYS and _under_workflows(value):
            return True
    return False


def _workflow_transcript(payload, agent_id):
    """Worker transcript under subagents/workflows, else empty."""
    for key in ("agent_transcript_path", "transcript_path"):
        path = payload.get(key)
        if _under_workflows(path):
            return path
    try:
        import _agent_model as agent_model
        found = agent_model.find_workflow_transcript(
            payload.get("transcript_path"),
            payload.get("session_id"),
            agent_id,
        )
    except Exception:
        return ""
    if isinstance(found, str) and found:
        return found
    return ""


def _workflow_control_agent(payload):
    """(agent_id, agent_type) when this result is a workflow planner or judge.

    A ledger workflow_worker row is enough. So is lineage when that row is
    missing: the tool or response shows a Workflow parent, or the worker
    transcript is under subagents/workflows/. A planner or judge started from
    the main session has neither, and still ingests.
    """
    response = _response(payload)
    agent_type = response.get("agentType") if isinstance(response.get("agentType"), str) else ""
    agent_type = agent_type.strip()
    if agent_type not in _CONTROL_AGENTS:
        return None
    agent_id = response.get("agentId")
    if not isinstance(agent_id, str) or agent_id.strip() == "":
        return None
    agent_id = agent_id.strip()
    try:
        row = arb.workflow_worker_row(agent_id)
    except Exception:
        row = None
    if isinstance(row, dict) and row.get("kind") == "workflow_worker":
        return agent_id, agent_type
    if _workflow_parent(_tool_input(payload)) or _workflow_parent(response):
        return agent_id, agent_type
    if _workflow_transcript(payload, agent_id):
        return agent_id, agent_type
    return None


def _is_judge(payload):
    response = _response(payload)
    policy = arb.load_policy()
    judge_type = policy.get("judge_agent_type") if isinstance(policy, dict) else "opus-judge"
    if response.get("agentType") == judge_type:
        return True
    return arb.judge_is_running(payload.get("tool_use_id"))


def _cursor_command(payload):
    return arb._cursor_spec(_tool_input(payload).get("command"))


def _bash(payload, event_name):
    recorded = arb.record_cursor_run_result(payload)
    if _inside(payload) or recorded is None:
        return 0
    kind = triggers.classify_failure(recorded.get("outcome"), "")
    if kind in ("environment", "budget"):
        return 0
    session_id = _session(payload)
    project = arb.project_of(payload)
    attempts = []
    for item in arb.list_attempts(session_id):
        task_id = item.get("task_id")
        if not isinstance(task_id, str) or not task_id.startswith("cursor:"):
            continue
        if item.get("outcome") != "failed":
            continue
        attempts.append({
            "task_id": "cursor-run",
            "strategy_digest": item.get("strategy_digest"),
            "outcome": "failed",
        })
    result = triggers.distinct_failures_candidate(
        session_id, "cursor-run", attempts, project=project,
    )
    return _emit(event_name, _candidate_text(result, payload))


def _agent_other(payload, event_name):
    response = _response(payload)
    if response.get("status") != "completed":
        return 0
    session_id = _session(payload)
    project = arb.project_of(payload)
    agent_type = response.get("agentType") if isinstance(response.get("agentType"), str) else ""
    resolved = response.get("resolvedModel") if isinstance(response.get("resolvedModel"), str) else ""
    text = _text(response)
    result = triggers.footer_candidate(
        session_id, text, agent_type, project=project, resolved_model=resolved,
    )
    if isinstance(result, dict) and result.get("reason") == "STOP" and result.get("class") in ("environment", "budget"):
        return _emit(
            event_name,
            "ARBITRATION_SKIPPED reason=%s: fix the environment or replan; Opus will not help."
            % result["class"],
        )
    context = _candidate_text(result, payload)
    if context:
        return _emit(event_name, context)
    footer = triggers.parse_footer(text)
    if not isinstance(footer, dict) or footer.get("status") != "COMPLETED":
        return 0
    checked = triggers.validator_failure_candidate(
        session_id, footer, arb.list_attempts(session_id), agent_type,
        project=project, resolved_model=resolved,
    )
    return _emit(event_name, _candidate_text(checked, payload))


def _handle(payload):
    tool = payload.get("tool_name")
    if not isinstance(tool, str):
        return 0
    tool = tool.strip()
    event_name = _event_name(payload)
    if tool == "Bash":
        if _cursor_command(payload) is None:
            return 0
        return _bash(payload, event_name)
    if tool == "SendMessage":
        arb.record_agent_resumed(payload)
        return 0
    if tool == "Workflow":
        # PostToolUseFailure matches Agent|Bash only (settings.json), so a
        # failed Workflow never arrives here. The reserved stage stays started
        # until `router-plan stage finish <task> <version> <stage> --status failed`.
        # opus-arbitration status marks stages open for more than an hour stale.
        try:
            arb.note_workflow_result(payload)
        except Exception:
            _crash(payload)
        return 0
    if tool != "Agent":
        return 0
    if event_name == "PostToolUseFailure":
        if not _inside(payload):
            arb.note_agent_failure(payload)
        return 0
    control = _workflow_control_agent(payload)
    if control:
        agent_id, agent_type = control
        try:
            arb.record_workflow_control_denial(
                _session(payload),
                arb.project_of(payload),
                agent_id,
                agent_type,
            )
        except Exception:
            pass
        return 0
    recorded = arb.record_agent_result(payload)
    alert = ""
    if isinstance(recorded, dict) and recorded.get("unarbitrated"):
        agent_id = recorded.get("agent_id")
        if isinstance(agent_id, str) and agent_id:
            alert = (
                "ARBITRATION_ALERT unarbitrated Opus run agent=%s; report this to the user."
                % agent_id
            )
    if not _inside(payload):
        prompt = _tool_input(payload).get("prompt")
        if isinstance(prompt, str):
            arb.note_updated_input(payload.get("tool_use_id"), prompt)
    if _inside(payload):
        return 0
    if alert:
        return _emit(event_name, alert)
    if _is_judge(payload):
        return _emit(event_name, _decision_text(arb.ingest_judge_result(payload), payload))
    if isinstance(recorded, dict) and recorded.get("kind") == "planner":
        if recorded.get("status") == "completed" and not _inside(payload):
            return _emit(event_name, arb.ingest_planner_result(payload))
        return 0
    return _agent_other(payload, event_name)


def main():
    global arb, triggers
    try:
        import _agent_model as agent_model
        import _opus_arbitration as loaded_arb
        import _opus_triggers as loaded_triggers
    except Exception:
        return 0
    arb = loaded_arb
    triggers = loaded_triggers
    try:
        payload = json.loads(sys.stdin.read() or "")
    except Exception:
        _crash(None)
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        with agent_model.strict_io():
            with arb.invocation(deadline=6.0):
                return _handle(payload)
    except arb.GateBusy:
        return 0
    except Exception:
        _crash(payload)
        return 0


if __name__ == "__main__":
    sys.exit(main())
