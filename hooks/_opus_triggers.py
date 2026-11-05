"""Deterministic Opus trigger rules. No model calls."""

import hashlib
import json
import re

import _opus_arbitration as arb

_FOOTER_KEYS = frozenset({
    "schema_version",
    "task_id",
    "status",
    "reason_code",
    "unresolved",
    "evidence_refs",
    "attempts",
    "suggested_scope",
})
_FOOTER_V2_KEYS = _FOOTER_KEYS | frozenset({
    "plan_id",
    "plan_version",
    "stage_id",
    "workflow_run_id",
    "unresolved_question",
    "completed_stage_ids",
    "failed_requirement_ids",
    "requested_action",
})
_FOOTER_REQUIRED = frozenset({"schema_version", "task_id", "status"})
_FOOTER_STATUS = frozenset({"COMPLETED", "NEEDS_JUDGMENT", "BLOCKED", "FAILED"})
_PLAN_REASONS = frozenset({"PLAN_REQUIRED", "PLAN_REVISION_REQUIRED"})
_REQUESTED_ACTIONS = frozenset({"REPLAN", "PLAN", "NONE"})
_STEP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_ENV_STATUS = frozenset({
    "timeout",
    "launch_error",
    "check_error",
    "2",
    "4",
    "5",
})


def classify_failure(status, error_text):
    """environment, reasoning, budget, or unknown.

    Environment and budget failures are not arbitration candidates.
    """
    text = error_text if isinstance(error_text, str) else ""
    low = text.lower()
    if status is None:
        status_s = ""
    else:
        status_s = str(status).strip().lower()
    budget = "rate limit" in low or re.search(r"\bbudget\b", low) is not None
    environment = (
        "permission denied" in low
        or "network" in low
        or "enoent" in low
        or status_s in _ENV_STATUS
    )
    if budget and not environment:
        return "budget"
    if status_s == "timeout" and budget:
        return "budget"
    if environment and budget:
        return "budget" if "rate limit" in low or re.search(r"\bbudget\b", low) else "environment"
    if environment:
        return "environment"
    if budget:
        return "budget"
    if status_s in ("failed", "blocked", "error"):
        return "reasoning"
    return "unknown"


def _failure_text(footer):
    parts = []
    for key in ("reason_code", "suggested_scope"):
        value = footer.get(key)
        if isinstance(value, str):
            parts.append(value)
    unresolved = footer.get("unresolved")
    if isinstance(unresolved, list):
        parts.extend(item for item in unresolved if isinstance(item, str))
    return "\n".join(parts)


def parse_footer(text):
    """Last worker footer with schema_version 1 or 2 and status, or None.

    V1 keys and checks are unchanged. The last object that claims a version
    and a status is the candidate; it is then validated.
    """
    if not isinstance(text, str) or text.strip() == "":
        return None
    decoder = json.JSONDecoder()
    found = []
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
            found.append(obj)
        index = max(end, index + 1)
    chosen = None
    for obj in found:
        if obj.get("schema_version") in (1, 2) and "status" in obj:
            chosen = obj
    if chosen is None:
        return None
    return _validate_footer(chosen)


def _bounded_str(value, limit):
    return isinstance(value, str) and len(value) <= limit


def _stage_id_list(value):
    if not isinstance(value, list) or len(value) > 20:
        return False
    return all(isinstance(item, str) and _STEP_ID_RE.fullmatch(item) for item in value)


def _validate_footer(obj):
    version = obj.get("schema_version")
    if type(version) is not int or version not in (1, 2):
        return None
    allowed = _FOOTER_KEYS if version == 1 else _FOOTER_V2_KEYS
    if set(obj) - allowed:
        return None
    if not _FOOTER_REQUIRED <= set(obj):
        return None
    if not isinstance(obj.get("task_id"), str) or obj.get("task_id") == "":
        return None
    if obj.get("status") not in _FOOTER_STATUS:
        return None
    if "reason_code" in obj and not isinstance(obj.get("reason_code"), str):
        return None
    reason = obj.get("reason_code")
    if reason in _PLAN_REASONS and obj.get("status") != "NEEDS_JUDGMENT":
        return None
    if "suggested_scope" in obj and not isinstance(obj.get("suggested_scope"), str):
        return None
    for key in ("unresolved", "evidence_refs"):
        if key not in obj:
            continue
        value = obj.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            return None
    if "attempts" in obj:
        attempts = obj.get("attempts")
        if not isinstance(attempts, list) or not all(isinstance(item, dict) for item in attempts):
            return None
    if version == 1:
        return obj
    if "plan_id" in obj and not _bounded_str(obj.get("plan_id"), 64):
        return None
    if "plan_version" in obj:
        plan_version = obj.get("plan_version")
        if type(plan_version) is not int or plan_version < 1:
            return None
    if "stage_id" in obj:
        stage_id = obj.get("stage_id")
        if not isinstance(stage_id, str) or _STEP_ID_RE.fullmatch(stage_id) is None:
            return None
    if "workflow_run_id" in obj and not _bounded_str(obj.get("workflow_run_id"), 128):
        return None
    if "unresolved_question" in obj and not _bounded_str(obj.get("unresolved_question"), 300):
        return None
    if "completed_stage_ids" in obj and not _stage_id_list(obj.get("completed_stage_ids")):
        return None
    if "failed_requirement_ids" in obj:
        failed = obj.get("failed_requirement_ids")
        if (
            not isinstance(failed, list)
            or len(failed) > 20
            or not all(_bounded_str(item, 100) for item in failed)
        ):
            return None
    if "requested_action" in obj and obj.get("requested_action") not in _REQUESTED_ACTIONS:
        return None
    return obj


def _stop(kind):
    return {"ok": False, "reason": "STOP", "class": kind, "request": None}


def _ready(result):
    """Promote a new candidate to JUDGE_PENDING and attach its packet."""
    if not isinstance(result, dict) or not result.get("ok"):
        return result
    request = result.get("request") or {}
    if request.get("state") == "CANDIDATE" and request.get("request_id"):
        promoted = arb.promote_pending(request["request_id"])
        result = dict(result)
        if isinstance(promoted, dict):
            result["request"] = promoted
            request = promoted
    if request.get("state") == "JUDGE_PENDING":
        result = dict(result)
        result["packet"] = arb.build_judge_packet(request)
    return result


def admission_candidate(skill_name, session_id, task_id, requester_agent_type=""):
    """T01. The shipped mandatory-review list is empty."""
    policy = arb.load_policy()
    if policy is None:
        return None
    workflows = policy.get("mandatory_review_workflows") or []
    if skill_name not in workflows:
        return None
    return arb.register_candidate(
        session_id=session_id,
        task_id=task_id,
        trigger="MANDATORY_REVIEW",
        reason_code="MANDATORY_REVIEW",
        requester_agent_type=requester_agent_type,
        scope_digest=hashlib.sha256((skill_name or "").encode("utf-8")).hexdigest(),
        task_summary=skill_name or "",
    )


def _cursor_attempt_rows(items):
    """T04/T05 packet rows: 12-char strategy digest and the cursor-run status."""
    prior = []
    evidence = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        digest = item.get("strategy_digest") if isinstance(item.get("strategy_digest"), str) else ""
        outcome = item.get("outcome") if isinstance(item.get("outcome"), str) else ""
        if digest == "" and outcome == "":
            continue
        prior.append({"strategy_digest": digest[:12], "outcome": outcome})
        evidence.append({"status": outcome, "source": "cursor-run"})
    return prior, evidence


def distinct_failures_candidate(session_id, task_id, attempts, requester_agent_type="", project=""):
    """T04. Identical strategy digests never count."""
    seen = []
    for item in attempts or []:
        if not isinstance(item, dict):
            continue
        if item.get("task_id") != task_id or item.get("outcome") != "failed":
            continue
        digest = item.get("strategy_digest")
        if isinstance(digest, str) and digest and digest not in seen:
            seen.append(digest)
    if len(seen) < 2:
        return None
    scope = hashlib.sha256("|".join(sorted(seen)).encode("utf-8")).hexdigest()
    rows = [{"strategy_digest": item, "outcome": "failed"} for item in seen]
    prior, evidence = _cursor_attempt_rows(rows)
    return _ready(arb.register_candidate(
        session_id=session_id,
        task_id=task_id,
        trigger="DISTINCT_FAILURES",
        reason_code="REPEATED_DISTINCT_FAILURE",
        requester_agent_type=requester_agent_type,
        scope_digest=scope,
        task_summary=task_id,
        prior_attempts=prior,
        objective_failure_evidence=evidence,
        allowed_scope_digest=scope,
        project=project if isinstance(project, str) else "",
    ))


def validator_failure_candidate(session_id, footer, attempts, requester_agent_type="", project="", resolved_model=""):
    """T05. A COMPLETED footer plus a later failed run of the same task."""
    if not isinstance(footer, dict) or footer.get("status") != "COMPLETED":
        return None
    task_id = footer.get("task_id")
    if not isinstance(task_id, str) or task_id == "":
        return None
    failed = [
        item for item in (attempts or [])
        if isinstance(item, dict) and item.get("task_id") == task_id and item.get("outcome") == "failed"
    ]
    if not failed:
        return None
    scope = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    prior, evidence = _cursor_attempt_rows(failed)
    observed = resolved_model if isinstance(resolved_model, str) else ""
    return _ready(arb.register_candidate(
        session_id=session_id,
        task_id=task_id,
        trigger="VALIDATOR_FAILURE",
        reason_code="VALIDATOR_FAILURE",
        requester_agent_type=requester_agent_type,
        scope_digest=scope,
        task_summary=task_id,
        current_model_observed=observed,
        prior_attempts=prior,
        objective_failure_evidence=evidence,
        allowed_scope_digest=scope,
        project=project if isinstance(project, str) else "",
    ))


def _footer_evidence(footer):
    """Unresolved items (max 5) and evidence refs from a worker footer."""
    evidence = []
    unresolved = footer.get("unresolved") if isinstance(footer, dict) else None
    if isinstance(unresolved, list):
        for item in unresolved:
            if len(evidence) >= 5:
                break
            if isinstance(item, str):
                evidence.append({"unresolved": item})
    refs = footer.get("evidence_refs") if isinstance(footer, dict) else None
    ref_count = 0
    if isinstance(refs, list):
        for item in refs:
            if ref_count >= 5:
                break
            if isinstance(item, str):
                evidence.append({"evidence_ref": item})
                ref_count += 1
    return evidence


def _general_purpose_footer_ok(footer, requester_agent_type):
    """general-purpose may escalate only with attempts or unresolved items.

    Owned agents keep the existing footer rules. An empty general-purpose
    footer is not a candidate.
    """
    if (requester_agent_type or "").strip() != "general-purpose":
        return True
    attempts = footer.get("attempts") if isinstance(footer, dict) else None
    unresolved = footer.get("unresolved") if isinstance(footer, dict) else None
    if isinstance(attempts, list) and len(attempts) > 0:
        return True
    if isinstance(unresolved, list) and len(unresolved) > 0:
        return True
    return False


def footer_candidate(session_id, text, requester_agent_type="", project="", resolved_model=""):
    """T03 and T06, plus reasoning-class BLOCKED/FAILED. Environment stops.

    parse_footer applies to any completed foreground agent, including
    general-purpose. A general-purpose footer is a candidate only when it
    has attempts or unresolved items.
    """
    footer = parse_footer(text)
    if footer is None:
        return None
    status = footer["status"]
    task_id = footer["task_id"]
    if status == "COMPLETED":
        return None
    observed = resolved_model if isinstance(resolved_model, str) else ""
    if status == "NEEDS_JUDGMENT":
        if not _general_purpose_footer_ok(footer, requester_agent_type):
            return None
        refs = footer.get("evidence_refs") or []
        if footer.get("reason_code") == "CONFLICTING_REQUIREMENTS" and refs:
            trigger = "CONFLICTING_REQUIREMENTS"
            reason = "CONFLICTING_REQUIREMENTS"
        else:
            trigger = "WORKER_REQUEST"
            reason = footer.get("reason_code") or "WORKER_REQUEST"
        return _ready(arb.register_candidate(
            session_id=session_id,
            task_id=task_id,
            trigger=trigger,
            reason_code=reason,
            requester_agent_type=requester_agent_type,
            scope_digest=hashlib.sha256(json.dumps(footer, sort_keys=True).encode("utf-8")).hexdigest(),
            task_summary=footer.get("suggested_scope") or task_id,
            current_model_observed=observed,
            objective_failure_evidence=_footer_evidence(footer),
            prior_attempts=footer.get("attempts") or [],
            project=project if isinstance(project, str) else "",
        ))
    kind = classify_failure(status, _failure_text(footer))
    attempts = footer.get("attempts") or []
    if kind in ("environment", "budget"):
        return _stop(kind)
    if kind == "reasoning" and attempts:
        if not _general_purpose_footer_ok(footer, requester_agent_type):
            return None
        return _ready(arb.register_candidate(
            session_id=session_id,
            task_id=task_id,
            trigger="REASONING_FAILURE",
            reason_code=footer.get("reason_code") or "REASONING_FAILURE",
            requester_agent_type=requester_agent_type,
            scope_digest=hashlib.sha256(json.dumps(footer, sort_keys=True).encode("utf-8")).hexdigest(),
            task_summary=task_id,
            current_model_observed=observed,
            objective_failure_evidence=_footer_evidence(footer),
            prior_attempts=attempts,
            project=project if isinstance(project, str) else "",
        ))
    return None


def failure_candidate(status, error_text):
    """T07/T08. Returns a stop hint and never a request."""
    kind = classify_failure(status, error_text)
    if kind in ("environment", "budget"):
        return _stop(kind)
    return {"ok": False, "reason": None, "class": kind, "request": None}
