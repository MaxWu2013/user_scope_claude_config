#!/usr/bin/env python3
"""Fail-closed PreToolUse guard for Opus subagents.

Prints a deny decision, rewrites a short arbitration reference, or nothing when
the call may proceed. Never prints allow. Invalid stdin exits 2. An unexpected
error denies only when Opus is still possible.
"""

import hashlib
import json
import os
import sqlite3
import stat
import sys
import time

sys.dont_write_bytecode = True

_ROOT = os.path.dirname(os.path.realpath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_agent_model = None
arb = None
resolve_routing_mode = None


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


def _rewrite(tool_input):
    """Replace the tool input. No permission decision, so deny from any hook still wins."""
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": tool_input,
        }
    }, ensure_ascii=False))
    sys.stdout.write("\n")
    return 0


def _reason_code(reason):
    if not isinstance(reason, str) or reason.strip() == "":
        return "DENIED"
    return reason.split()[0]


def _deny_agent(data, reason):
    """Deny an Agent or Task call and record gate_denied. Never stores the prompt."""
    try:
        session_id = ""
        tool_use_id = ""
        project = ""
        if isinstance(data, dict):
            if isinstance(data.get("session_id"), str):
                session_id = data["session_id"]
            if isinstance(data.get("tool_use_id"), str):
                tool_use_id = data["tool_use_id"]
            project = arb.project_of(data)
        arb.record_event(
            "gate_denied",
            session_id=session_id,
            project=project,
            extra={"tool_use_id": tool_use_id, "reason_code": _reason_code(reason)},
        )
    except Exception:
        pass
    return _deny(reason)


def _tool_input(data):
    tool_input = data.get("tool_input")
    if isinstance(tool_input, dict):
        return tool_input
    return {}


def _legacy_override():
    if os.environ.get("CLAUDE_ROUTING") == "off":
        return True
    if os.environ.get("CLAUDE_ROUTING_ALLOW_OPUS_SUBAGENT"):
        return True
    scope = os.environ.get("CLAUDE_ROUTING_SCOPE")
    return isinstance(scope, str) and scope.strip() != ""


def _note_legacy(session_id, project=""):
    if not _legacy_override():
        return
    try:
        arb.record_event("legacy_override_ignored", session_id=session_id, project=project)
    except Exception:
        pass


def _targets(tool_input):
    """recipient, to, recipients, and every nested agent id (at most 200 strings)."""
    return arb.send_targets(tool_input)


def _background(tool_input):
    """Background unless the flag is absent or exactly False."""
    if "run_in_background" not in tool_input:
        return False
    return tool_input.get("run_in_background") is not False


def _send_message(data, proved):
    tool_input = _tool_input(data)
    session_id = data.get("session_id") if isinstance(data.get("session_id"), str) else ""
    for target in _targets(tool_input):
        if arb.resume_blocked(data, target):
            _note_legacy(session_id, arb.project_of(data))
            return _deny("OPUS_RESUME_DENIED; start a new Agent call (it will be arbitrated)")
    proved["non_opus"] = True
    return 0


def _subagent_type(tool_input):
    subagent_type = tool_input.get("subagent_type")
    if isinstance(subagent_type, str) and subagent_type.strip():
        return subagent_type.strip()
    return "general-purpose"


def _prompt(tool_input):
    prompt = tool_input.get("prompt")
    if isinstance(prompt, str):
        return prompt
    return ""


def _remember_launched(data, kind, request_id):
    """Record the allowed run before the gate returns."""
    arb.record_launched_run(data, kind, request_id=request_id)


def _auto(data):
    return isinstance(data, dict) and data.get("permission_mode") == "auto"


def _accept_grant_reference(data, tool_input, session_id, request_id, tool_use_id):
    """Expand a composed-grant reference, or deny without consuming it."""
    if _auto(data):
        raw = arb.peek_grant_invocation(session_id, request_id) or ""
        reason = "GRANT_REFERENCE_UNSUPPORTED_IN_AUTO"
        if raw:
            reason = reason + " " + raw
        return _deny_agent(data, reason)
    outcome = arb.consume_grant_reference(session_id, request_id, tool_input, tool_use_id)
    if isinstance(outcome, dict) and outcome.get("allow") is True and isinstance(outcome.get("invocation"), dict):
        kind = "planner" if outcome.get("purpose") == "PLAN_ONLY" else "escalated"
        _remember_launched(data, kind, outcome.get("request_id"))
        return _rewrite(outcome["invocation"])
    if isinstance(outcome, dict) and outcome.get("reason") == "GRANT_PURPOSE_MISMATCH":
        return _deny_agent(data, "GRANT_PURPOSE_MISMATCH")
    return _deny_agent(data, "GRANT_MISMATCH")


_WORKFLOW_MAIN_DENY = (
    "OPUS_WORKFLOW_DENIED; workflow agents would inherit the Opus main model. "
    "Run workflows from a sonnet/haiku main session, or run the steps as normal Agent calls (they will be arbitrated)."
)
_WORKFLOW_DENY = (
    "OPUS_WORKFLOW_DENIED; name, resumeFromRunId, an unreadable scriptPath, "
    "or script text containing opus is denied"
)


def _workflow_env_reason(limit):
    return (
        "OPUS_WORKFLOW_DENIED reason=env; set "
        "CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL=sonnet and "
        "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS=%s" % limit
    )


def _workflow_concurrency_reason(limit):
    return (
        "OPUS_WORKFLOW_DENIED reason=concurrency; "
        "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS must be an integer from 1 to %s" % limit
    )


def _workflow_session_reason(limit):
    return "OPUS_WORKFLOW_DENIED reason=session; at most %s workflows have been recorded for this session" % limit


def _workflow_agents_reason(limit):
    return "OPUS_WORKFLOW_DENIED reason=agents; static agent() count exceeds %s" % limit


def _env_concurrency(limit):
    raw = os.environ.get("CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS")
    if not isinstance(raw, str) or raw.strip() == "" or not raw.strip().isdigit():
        return None
    value = int(raw.strip())
    if value < 1 or value > limit:
        return None
    return value


def _pinned_family(config):
    """Allowed worker family from the workflow env, else None."""
    raw = os.environ.get("CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL")
    if not isinstance(raw, str) or raw.strip() == "":
        return None
    family = _agent_model.model_family(raw)
    allowed = config.get("allowed_worker_families") or []
    if family not in allowed:
        return None
    return family


def _router_stage_gate(tool_input):
    """None when args has no router_stage. Otherwise ('ok', stage) or ('deny', reason)."""
    if not isinstance(tool_input, dict):
        return None
    args = tool_input.get("args")
    if not isinstance(args, dict) or "router_stage" not in args:
        return None
    stage = args.get("router_stage")
    if not isinstance(stage, dict) or set(stage) != {"task_id", "plan_version", "stage_id"}:
        return ("deny", "ROUTER_STAGE_INVALID")
    task_id = stage.get("task_id")
    plan_version = stage.get("plan_version")
    stage_id = stage.get("stage_id")
    import _router_plans as plans

    if not isinstance(task_id, str) or plans._TASK_ID_RE.fullmatch(task_id) is None:
        return ("deny", "ROUTER_STAGE_INVALID")
    if type(plan_version) is not int or plan_version < 1:
        return ("deny", "ROUTER_STAGE_INVALID")
    if not isinstance(stage_id, str) or plans._STEP_ID_RE.fullmatch(stage_id) is None:
        return ("deny", "ROUTER_STAGE_INVALID")
    if "opus" in json.dumps(args).lower():
        return ("deny", "OPUS_WORKFLOW_DENIED reason=args")
    return ("ok", stage)


def _stage_deny_text(stage, code):
    if code in ("ROUTER_PLAN_MISSING", "ROUTER_PLAN_STALE", "ROUTER_STAGE_DUPLICATE", "ROUTER_STAGE_UNKNOWN"):
        return "%s; run router-plan show %s" % (code, stage["task_id"])
    return code or "ROUTER_STAGE_INVALID"


def _reserve_stage(data, stage):
    """Deny text when the stage cannot be reserved, else None."""
    session_id = data.get("session_id") if isinstance(data.get("session_id"), str) else ""
    tool_use_id = data.get("tool_use_id") if isinstance(data.get("tool_use_id"), str) else ""
    ok, code, _attempt = arb.stage_start(
        session_id,
        stage["task_id"],
        stage["plan_version"],
        stage["stage_id"],
        workflow_tool_use_id=tool_use_id or None,
    )
    if ok:
        return None
    return _stage_deny_text(stage, code)


def _release_counted_workflow(data):
    """Drop a slot that was counted before the stage reservation lost or raised."""
    tool_use_id = ""
    if isinstance(data, dict) and isinstance(data.get("tool_use_id"), str):
        tool_use_id = data["tool_use_id"]
    if tool_use_id == "":
        return
    try:
        arb.release_workflow(tool_use_id)
    except Exception:
        pass


def _finish_workflow(data, stage_gate):
    """Reserve a router stage only after every other workflow check has passed.

    Any error from the reservation denies ROUTER_STAGE_BUSY. A workflow that
    carries router_stage must not fall through to the non-opus allow.
    """
    if not isinstance(stage_gate, tuple):
        return 0
    if stage_gate[0] != "ok":
        return 0
    try:
        denied = _reserve_stage(data, stage_gate[1])
    except Exception:
        _release_counted_workflow(data)
        return _deny_agent(data, "ROUTER_STAGE_BUSY")
    if denied:
        _release_counted_workflow(data)
        return _deny_agent(data, denied)
    return 0


def _stage_precheck(stage_gate):
    """Deny text from the read-only stage check, or None when the stage can start.

    None when this call has no router_stage. Runs before accept_workflow so a
    known deny does not spend a session slot. stage_start still reserves.
    """
    if not isinstance(stage_gate, tuple) or stage_gate[0] != "ok":
        return None
    stage = stage_gate[1]
    ok, code = arb.stage_check(stage["task_id"], stage["plan_version"], stage["stage_id"])
    if ok:
        return None
    return _stage_deny_text(stage, code)


def _record_workflow(data, family, limit, proved):
    ok = arb.accept_workflow(data, family, limit)
    if ok:
        proved["non_opus"] = True
    return ok


def _safe_log_id(value):
    if not isinstance(value, str) or value == "":
        return None
    if "/" in value or "\\" in value or ".." in value or value.startswith("."):
        return None
    return value


def _arb_log(*parts):
    return os.path.join(os.path.expanduser("~"), ".claude", "logs", "opus-arbitration", *parts)


def _regular_file(path):
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


def _guard_attest_path(data):
    session_id = _safe_log_id(data.get("session_id"))
    tool_use_id = _safe_log_id(data.get("tool_use_id"))
    if session_id is None or tool_use_id is None:
        return None
    return _arb_log("guard", "attest-%s-%s" % (session_id, tool_use_id))


def _mod_heartbeat_path(data):
    session_id = _safe_log_id(data.get("session_id"))
    if session_id is None:
        return None
    return _arb_log("mod", "%s.json" % session_id)


def _mod_call_path(data):
    session_id = _safe_log_id(data.get("session_id"))
    tool_use_id = _safe_log_id(data.get("tool_use_id"))
    if session_id is None or tool_use_id is None:
        return None
    return _arb_log("mod", "attest-%s-%s.json" % (session_id, tool_use_id))


def _poll_file(path):
    """Wait up to 1.5s, in 50ms steps, without passing the gate deadline."""
    if path is None:
        return False
    started = time.monotonic()
    while True:
        if _regular_file(path):
            return True
        left = 1.5 - (time.monotonic() - started)
        budget = arb.remaining_budget()
        if budget is not None:
            left = min(left, budget)
        if left <= 0:
            return _regular_file(path)
        time.sleep(min(0.05, left))


def _policy_digest():
    path = os.path.join(
        os.path.expanduser("~"), ".claude", "hooks", "data", "opus-arbitration-policy.json",
    )
    if not _regular_file(path):
        return None
    try:
        with open(path, "rb") as handle:
            blob = handle.read()
    except OSError:
        return None
    return hashlib.sha256(blob).hexdigest()


def _heartbeat_ok(data):
    path = _mod_heartbeat_path(data)
    if path is None or not _regular_file(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as handle:
            body = json.loads(handle.read())
    except (OSError, json.JSONDecodeError, UnicodeError):
        return False
    if not isinstance(body, dict) or body.get("mod_version") != "1.0.0":
        return False
    digest = _policy_digest()
    return isinstance(digest, str) and body.get("policy_sha256") == digest


def _runtime_attestations(data, config):
    """None when the guard, the mod heartbeat, and the call attestation are present."""
    if not _poll_file(_guard_attest_path(data)):
        return "WORKFLOW_GUARD_NOT_ACTIVE"
    if not _heartbeat_ok(data):
        return "WORKFLOW_SPAWN_MOD_NOT_ACTIVE"
    if config.get("require_mod_call_attestation", True) and not _poll_file(_mod_call_path(data)):
        return "WORKFLOW_SPAWN_MOD_NOT_ACTIVE call"
    return None


def _workflow(data, proved):
    """Allow a sonnet/haiku workflow, or an Opus main only under pinned env.

    A router_stage is checked, then reserved only after every other precondition
    has passed, so a denied Workflow never leaves a started stage row. A
    reservation error denies ROUTER_STAGE_BUSY. Args without router_stage
    behave as before.
    """
    tool_input = _tool_input(data)
    stage_gate = _router_stage_gate(tool_input)
    if isinstance(stage_gate, tuple) and stage_gate[0] == "deny":
        return _deny_agent(data, stage_gate[1])
    if _agent_model.workflow_structural_denies(tool_input):
        return _deny_agent(data, _WORKFLOW_DENY)
    main = _agent_model.main_session_model(data)
    if main in ("sonnet", "haiku"):
        if not _poll_file(_guard_attest_path(data)):
            return _deny_agent(data, "WORKFLOW_GUARD_NOT_ACTIVE")
        blocked = _stage_precheck(stage_gate)
        if blocked:
            return _deny_agent(data, blocked)
        proved["non_opus"] = True
        try:
            arb.accept_workflow(data, main, None)
        except Exception:
            pass
        return _finish_workflow(data, stage_gate)
    config = arb.workflow_config()
    if config.get("opus_main_mode") != "pinned_env":
        return _deny_agent(data, _WORKFLOW_MAIN_DENY)
    limit = config["max_concurrent_agents_required"]
    family = _pinned_family(config)
    if family is None:
        return _deny_agent(data, _workflow_env_reason(limit))
    if _env_concurrency(limit) is None:
        return _deny_agent(data, _workflow_concurrency_reason(limit))
    calls = _agent_model.workflow_agent_calls(tool_input)
    if calls > config["max_workers_per_workflow"]:
        return _deny_agent(data, _workflow_agents_reason(config["max_workers_per_workflow"]))
    runtime = _runtime_attestations(data, config)
    if runtime:
        return _deny_agent(data, runtime)
    blocked = _stage_precheck(stage_gate)
    if blocked:
        return _deny_agent(data, blocked)
    if not _record_workflow(data, family, config["max_workflows_per_session"], proved):
        return _deny_agent(data, _workflow_session_reason(config["max_workflows_per_session"]))
    return _finish_workflow(data, stage_gate)


def _skill(data, proved):
    """Deny an Opus fork. Inline skills run in the main session. Unknown skills are allowed."""
    found = _agent_model.resolve_skill_for_gate(data, _tool_input(data))
    status = found.get("status") if isinstance(found, dict) else "unresolved"
    if status == "unresolved":
        try:
            session_id = data.get("session_id") if isinstance(data.get("session_id"), str) else ""
            arb.record_event(
                "skill_unresolved",
                session_id=session_id,
                project=arb.project_of(data),
                extra={"skill": found.get("skill") if isinstance(found, dict) else ""},
            )
        except Exception:
            pass
        proved["non_opus"] = True
        return 0
    if status != "fork":
        proved["non_opus"] = True
        return 0
    family = found.get("family") if isinstance(found, dict) else "unknown"
    if family in ("opus", "unknown"):
        return _deny_agent(data, "OPUS_SKILL_FORK_DENIED")
    proved["non_opus"] = True
    return 0


def _agent(data, proved):
    tool_input = _tool_input(data)
    res = _agent_model.resolve_for_gate(data, tool_input)
    session_id = data.get("session_id") if isinstance(data.get("session_id"), str) else ""
    subagent_type = _subagent_type(tool_input)
    policy = arb.load_policy()
    judge_type = policy.get("judge_agent_type") if isinstance(policy, dict) else None
    is_judge = judge_type is not None and subagent_type == judge_type
    if is_judge:
        identity = _agent_model.judge_definition_status(data, judge_type)
        if identity == "shadowed":
            return _deny_agent(data, "JUDGE_SHADOWED")
        if identity != "ok":
            return _deny_agent(data, "JUDGE_DEFINITION_INVALID")
    prompt = _prompt(tool_input)
    grant_ref = None if is_judge else arb.grant_reference_id(prompt)
    if not is_judge and grant_ref is None and not _agent_model.opus_possible(res):
        proved["non_opus"] = True
        return 0
    _note_legacy(session_id, arb.project_of(data))
    switch = arb.read_switch()
    if switch != "active":
        return _deny_agent(data, "OPUS_DENIED mode=disabled")
    if policy is None or not policy.get("enabled"):
        return _deny_agent(data, "OPUS_DENIED mode=policy; use sonnet/haiku")
    mode, _source = resolve_routing_mode()
    if mode == "strict":
        return _deny_agent(data, "OPUS_DENIED mode=strict; use sonnet/haiku")
    if _background(tool_input) and grant_ref is None:
        return _deny_agent(data, "OPUS_BACKGROUND_DENIED")
    agent_id = data.get("agent_id")
    if isinstance(agent_id, str) and agent_id.strip():
        return _deny_agent(data, "OPUS_NESTED_DENIED")
    tool_use_id = data.get("tool_use_id") if isinstance(data.get("tool_use_id"), str) else ""
    if tool_use_id == "":
        if is_judge:
            return _deny_agent(data, "JUDGE_NO_TOOL_USE_ID")
        return _deny_agent(data, "OPUS_NO_TOOL_USE_ID")
    planning = arb.planning_config(policy if isinstance(policy, dict) else None)
    planner_type = planning.get("planner_agent_type")
    is_planner = (
        isinstance(planner_type, str)
        and planner_type != ""
        and subagent_type == planner_type
        and _agent_model.opus_possible(res)
    )
    if is_planner:
        if not planning.get("enabled"):
            return _deny_agent(data, "OPUS_PLANNER_DISABLED")
        identity = _agent_model.planner_definition_status(data, planner_type)
        if identity != "ok":
            return _deny_agent(data, identity)
        if _agent_model.main_session_model(data) == "opus":
            return _deny_agent(
                data,
                "OPUS_PLANNER_NOT_NEEDED Main session is already Opus: write the plan yourself, "
                "save it under ~/.claude/plans/router/drafts/ and run router-plan promote. "
                "No judge is needed for your own reasoning.",
            )
    if is_judge:
        if res.get("family") != "opus":
            return _deny_agent(data, "JUDGE_MODEL_MISMATCH")
        ref_id = arb.packet_reference_id(prompt)
        if ref_id is not None and _auto(data):
            packet = arb.peek_pending_packet(session_id, ref_id) or ""
            reason = "JUDGE_REFERENCE_UNSUPPORTED_IN_AUTO"
            if packet:
                reason += "\n<<<PACKET\n%s\nPACKET>>>" % packet
            return _deny_agent(data, reason)
        if ref_id is not None:
            begun = arb.begin_judge_reference(session_id, ref_id, tool_use_id)
            packet = begun.get("packet_text") if isinstance(begun, dict) else None
            if not isinstance(packet, str) or packet == "":
                return _deny_agent(data, "JUDGE_NOT_PENDING")
            updated = dict(tool_input)
            updated["prompt"] = packet
            _remember_launched(data, "judge", begun.get("request_id"))
            return _rewrite(updated)
        begun = arb.begin_judge(session_id, prompt, tool_use_id)
        if begun is None:
            return _deny_agent(data, "JUDGE_NOT_PENDING")
        _remember_launched(data, "judge", begun.get("request_id"))
        return 0
    if grant_ref is not None:
        return _accept_grant_reference(data, tool_input, session_id, grant_ref, tool_use_id)
    digest = arb.invocation_digest(session_id, tool_input, res.get("family") or "")
    grant = arb.consume_grant(
        session_id, digest, tool_use_id, tool_input, res.get("family") or "",
    )
    if isinstance(grant, dict) and grant.get("allow") is True:
        launched = "planner" if grant.get("purpose") == "PLAN_ONLY" else "escalated"
        _remember_launched(data, launched, grant.get("request_id"))
        return 0
    if isinstance(grant, dict) and grant.get("allow") is False:
        if grant.get("reason") == "GRANT_PURPOSE_MISMATCH":
            return _deny_agent(data, "GRANT_PURPOSE_MISMATCH")
        return _deny_agent(data, "OPUS_GRANT_REPLAY_DENIED")
    # Baseline is the model that would otherwise run, not the requested family.
    observed = _agent_model.non_opus_baseline(data, tool_input)
    result = arb.register_unapproved(
        session_id,
        tool_input,
        res.get("family") or "",
        observed,
        project=arb.project_of(data),
        candidate_type="PLAN_REQUEST" if is_planner else "EXECUTOR_ESCALATION",
        plan_task_id=arb.plan_task_from_prompt(prompt) if is_planner else None,
    )
    if not result.get("ok"):
        if result.get("reason") == "ALREADY_DECIDED":
            decided = result.get("decision") or "UNKNOWN"
            return _deny_agent(
                data,
                "ARBITRATION_ALREADY_DECIDED decision=%s; do not retry Opus for this task" % decided,
            )
        if result.get("reason") == "DISABLED":
            return _deny_agent(data, "OPUS_DENIED mode=disabled")
        return _deny_agent(data, "ARBITRATION_QUOTA_EXHAUSTED")
    request = result.get("request") or {}
    request_id = request.get("request_id") or ""
    if _auto(data):
        packet = result.get("packet") or request.get("packet_text") or ""
        reason = (
            "ARBITRATION_REQUIRED request_id=%s. Do not retry this Opus call directly. "
            'Launch the Agent tool now, foreground, subagent_type "%s", '
            'description "Judge %s", and copy this prompt exactly:\n'
            "<<<PACKET\n%s\nPACKET>>>\n"
            "If the judge returns ESCALATE, re-issue the original call unchanged; "
            "otherwise follow the judge decision without Opus."
            % (request_id, judge_type, request_id, packet)
        )
    else:
        reason = (
            "ARBITRATION_REQUIRED request_id=%s. Do not retry this Opus call directly. "
            'Launch the Agent tool now, foreground, subagent_type "%s", '
            'description "Judge %s", prompt exactly "ARBITRATION-PACKET request_id=%s" '
            "(the hook inserts the full packet). "
            "If the judge returns ESCALATE, re-issue the original call unchanged; "
            "otherwise follow the judge decision without Opus."
            % (request_id, judge_type, request_id, request_id)
        )
    return _deny_agent(data, reason)


def _custom_workflow_worker(data):
    """A worker started with some agent type other than workflow-subagent."""
    agent_id = data.get("agent_id")
    if not isinstance(agent_id, str) or agent_id.strip() == "":
        return False
    agent_id = agent_id.strip()
    try:
        if os.path.lexists(arb.ledger_path()):
            row = arb.workflow_worker_row(agent_id)
            if isinstance(row, dict) and row.get("kind") == "workflow_worker":
                return True
    except Exception:
        pass
    try:
        found = _agent_model.workflow_worker_evidence(data)
    except Exception:
        return False
    if found:
        try:
            arb.record_workflow_worker(data)
        except Exception:
            pass
    return bool(found)


def _handle(data, proved):
    tool = data.get("tool_name")
    if not isinstance(tool, str):
        proved["non_opus"] = True
        return 0
    tool = tool.strip()
    if tool in ("Agent", "Task", "SendMessage", "Workflow") and (
        data.get("agent_type") == "workflow-subagent" or _custom_workflow_worker(data)
    ):
        return _deny_agent(data, arb.WORKER_NO_DELEGATION)
    if tool == "SendMessage":
        return _send_message(data, proved)
    if tool == "Workflow":
        return _workflow(data, proved)
    if tool == "Skill":
        return _skill(data, proved)
    if tool in ("Agent", "Task"):
        return _agent(data, proved)
    proved["non_opus"] = True
    return 0


def _load():
    global _agent_model, arb, resolve_routing_mode
    import _agent_model as agent_model
    import _opus_arbitration as arbitration
    from _paths import resolve_routing_mode as resolve_mode

    _agent_model = agent_model
    arb = arbitration
    resolve_routing_mode = resolve_mode


def main():
    try:
        _load()
    except Exception:
        return _deny("OPUS_GATE_ERROR import")
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        print("opus-arbitration-gate: invalid JSON", file=sys.stderr)
        return 2
    if not isinstance(data, dict):
        print("opus-arbitration-gate: invalid JSON", file=sys.stderr)
        return 2
    proved = {"non_opus": False}
    try:
        with _agent_model.strict_io():
            with arb.invocation(deadline=6.0):
                return _handle(data, proved)
    except (arb.GateBusy, sqlite3.OperationalError):
        if proved["non_opus"]:
            return 0
        return _deny("OPUS_GATE_BUSY")
    except Exception:
        if proved["non_opus"]:
            return 0
        tool = data.get("tool_name")
        if isinstance(tool, str) and tool.strip() in ("Agent", "Task", "Workflow", "Skill"):
            return _deny_agent(data, "OPUS_GATE_ERROR")
        return _deny("OPUS_GATE_ERROR")


if __name__ == "__main__":
    sys.exit(main())
