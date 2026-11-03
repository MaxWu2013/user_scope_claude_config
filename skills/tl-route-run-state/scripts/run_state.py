#!/usr/bin/env python3
"""Deterministic run state for skill/workflow orchestrators: the control plane in code.

Usage:
  run_state.py init   <state.json> [--workflow NAME] [--limit executor=2] [--limit verification=2]
                      [--force | --resume]
  run_state.py record <state.json> <event> [--detail TEXT] [--event-id ID] [--step ID]
                      [--file SPEC] [--hash SHA256]
  run_state.py route  <state.json> [--json]
  run_state.py get    <state.json> [field]

The orchestrator never counts or decides retries in prose: it records what happened
(`record`) and follows the single token `route` prints. Only events that are legal in
the current phase are accepted; anything else is rejected (exit 2) and changes nothing.

Events (phase where legal):
  spec-written      spec             a (new) spec version exists; --file SPEC stores its sha256
  approved          approval         the user approved the current spec version (and hash)
  approval-reused   approval         a parent approved this exact spec; when a hash is stored,
                                     --hash (or --file) must match it
  revise            approval         the user wants changes                     -> spec
  executor-success  execute          the delegate finished (status success)     -> verify
  executor-partial  execute          timed out (status partial)                 counts an executor retry
  executor-error    execute          failed (status error)                      counts an executor retry
  verify-pass       verify           every independent check passed             -> done
  verify-fail-impl  verify           the work is wrong                          counts a verification retry
  verify-fail-spec  verify           the spec is wrong                          -> spec, counters reset
  step-failed       spec|approval|execute|verify   another executed step failed; --step ID names it
  step-recovered    failed           that step (same --step, if given) was fixed -> back where it failed
  more-attempts     execute, only while the route is escalate   counters reset
  stop              any non-terminal phase                      -> stopped
`done` and `stopped` are terminal: no event is accepted after them.
While the route is `escalate`, only `more-attempts` and `stop` are accepted.
--event-id makes an event idempotent: recording the same id again is a no-op.

Routes printed by `route` (one word):
  write-spec, approve, execute, retry-execute, verify, revise-spec,
  handle-failure, escalate, done, stopped
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 2
DEFAULT_LIMITS = {"executor": 2, "verification": 2}
WORKING = ("spec", "approval", "execute", "verify")
LEGAL = {
    "spec-written": ("spec",),
    "approved": ("approval",),
    "approval-reused": ("approval",),
    "revise": ("approval",),
    "executor-success": ("execute",),
    "executor-partial": ("execute",),
    "executor-error": ("execute",),
    "verify-pass": ("verify",),
    "verify-fail-impl": ("verify",),
    "verify-fail-spec": ("verify",),
    "step-failed": WORKING,
    "step-recovered": ("failed",),
    "more-attempts": ("execute",),
    "stop": WORKING + ("failed",),
}
EVENTS = set(LEGAL)
TERMINAL = ("done", "stopped")


class Rejected(Exception):
    """An event that is not legal in the current state; the state is left unchanged."""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def new_state(workflow, limits):
    return {"schema": SCHEMA, "workflow": workflow, "phase": "spec", "spec_version": 0,
            "spec_hash": None, "approved_spec_version": None, "approved_spec_hash": None,
            "executor_status": None, "executor_retries": 0, "verification_retries": 0,
            "limits": limits, "last_event": None, "last_error": None,
            "failed_step": None, "resume_phase": None, "history": []}


def upgrade(s):
    """Accept schema 1 files: add the fields schema 2 introduced."""
    if s.get("schema") == 1:
        base = new_state(s.get("workflow", ""), s.get("limits") or dict(DEFAULT_LIMITS))
        base.update(s)
        base["schema"] = SCHEMA
        return base
    if s.get("schema") != SCHEMA:
        raise SystemExit(f"unsupported state schema: {s.get('schema')}")
    return s


def load(path):
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"no state file: {path} (run init first)")
    return upgrade(json.loads(p.read_text(encoding="utf-8")))


def save(path, s):
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(p)  # atomic on POSIX: a crash never leaves half a file


@contextlib.contextmanager
def locked(path):
    """Exclusive lock around read-modify-write, so two writers can't lose an update."""
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def over_limit(s):
    lim = {**DEFAULT_LIMITS, **(s.get("limits") or {})}
    return s["executor_retries"] > lim["executor"] or s["verification_retries"] > lim["verification"]


def check_legal(s, event, opts):
    if event not in EVENTS:
        raise Rejected(f"unknown event: {event} (one of: {', '.join(sorted(EVENTS))})")
    phase = s["phase"]
    if phase in TERMINAL:
        raise Rejected(f"the run is {phase}: no event is accepted after it")
    escalated = phase == "execute" and over_limit(s)
    if escalated and event not in ("more-attempts", "stop"):
        raise Rejected("the route is escalate: only more-attempts or stop are accepted")
    if event == "more-attempts" and not escalated:
        raise Rejected("more-attempts is only accepted while the route is escalate")
    if phase not in LEGAL[event]:
        allowed = sorted(e for e, ph in LEGAL.items() if phase in ph)
        raise Rejected(f"{event} is not legal in phase {phase} (legal here: {', '.join(allowed)})")
    if event == "approval-reused" and s.get("spec_hash"):
        given = opts.get("hash") or (sha256_file(opts["file"]) if opts.get("file") else None)
        if not given:
            raise Rejected("approval-reused needs --hash or --file: the spec's hash is recorded")
        if given != s["spec_hash"]:
            raise Rejected("approval-reused: the approved spec's hash differs from the current spec")
    if event == "step-recovered" and opts.get("step") and s.get("failed_step") not in (None, opts["step"]):
        raise Rejected(f"step-recovered --step {opts['step']}, but the failed step is {s['failed_step']}")


def apply(s, event, detail=None, **opts):
    """Pure, validated state transition; returns the new state or raises Rejected."""
    s = upgrade(json.loads(json.dumps(s)))
    eid = opts.get("event_id")
    if eid and any(h.get("event_id") == eid for h in s["history"]):
        return s  # idempotent: this exact event was already recorded
    check_legal(s, event, opts)
    entry = {"at": now(), "event": event}
    for k, v in (("detail", detail), ("event_id", eid), ("step", opts.get("step"))):
        if v:
            entry[k] = v
    s["history"].append(entry)
    s["last_event"] = event
    if event in ("executor-partial", "executor-error", "verify-fail-impl", "verify-fail-spec", "step-failed"):
        s["last_error"] = detail or event
    if event == "spec-written":
        s.update(spec_version=s["spec_version"] + 1, phase="approval", executor_status=None,
                 spec_hash=sha256_file(opts["file"]) if opts.get("file") else None)
    elif event in ("approved", "approval-reused"):
        s.update(approved_spec_version=s["spec_version"], approved_spec_hash=s.get("spec_hash"), phase="execute")
    elif event == "revise":
        s.update(phase="spec")
    elif event == "executor-success":
        s.update(executor_status="success", phase="verify", last_error=None)
    elif event in ("executor-partial", "executor-error"):
        s.update(executor_status=event.split("-")[1], executor_retries=s["executor_retries"] + 1)
    elif event == "verify-pass":
        s.update(phase="done", last_error=None)
    elif event == "verify-fail-impl":
        s.update(verification_retries=s["verification_retries"] + 1, executor_status=None, phase="execute")
    elif event == "verify-fail-spec":
        s.update(phase="spec", executor_status=None, verification_retries=0, executor_retries=0)
    elif event == "step-failed":
        s.update(resume_phase=s["phase"], failed_step=opts.get("step"), phase="failed")
    elif event == "step-recovered":
        s.update(phase=s["resume_phase"], resume_phase=None, failed_step=None, last_error=None)
    elif event == "more-attempts":
        s.update(executor_retries=0, verification_retries=0)  # the next run is a retry of the same spec
    elif event == "stop":
        s.update(phase="stopped")
    return s


def route(s):
    """The single next step, decided from state only."""
    phase = s["phase"]
    if phase in TERMINAL:
        return phase
    if phase == "failed":
        return "handle-failure"
    if phase == "spec":
        return "revise-spec" if s["spec_version"] > 0 else "write-spec"
    if phase == "approval":
        return "approve"
    if s["approved_spec_version"] != s["spec_version"] or s.get("approved_spec_hash") != s.get("spec_hash"):
        return "approve"
    if over_limit(s):
        return "escalate"
    if phase == "verify":
        return "verify"
    if phase == "execute":
        failed_before = s["executor_status"] in ("partial", "error") or s["last_event"] in ("verify-fail-impl", "more-attempts")
        return "retry-execute" if failed_before else "execute"
    raise SystemExit(f"inconsistent state: phase={phase}")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init"); i.add_argument("state"); i.add_argument("--workflow", default="")
    i.add_argument("--limit", action="append", default=[])
    mode = i.add_mutually_exclusive_group(); mode.add_argument("--force", action="store_true")
    mode.add_argument("--resume", action="store_true")
    r = sub.add_parser("record"); r.add_argument("state"); r.add_argument("event"); r.add_argument("--detail")
    r.add_argument("--event-id"); r.add_argument("--step"); r.add_argument("--file"); r.add_argument("--hash")
    o = sub.add_parser("route"); o.add_argument("state"); o.add_argument("--json", action="store_true")
    g = sub.add_parser("get"); g.add_argument("state"); g.add_argument("field", nargs="?")
    a = ap.parse_args(argv)
    if a.cmd == "init":
        p = Path(a.state)
        with locked(p):
            if p.exists():
                if a.resume:
                    print(route(load(p)))
                    return 0
                if not a.force:
                    raise SystemExit(f"state already exists: {p} (--resume to continue it, --force to start over)")
            limits = dict(DEFAULT_LIMITS)
            for item in a.limit:
                k, _, v = item.partition("=")
                if k not in DEFAULT_LIMITS or not v.isdigit():
                    raise SystemExit(f"bad --limit {item!r}: use executor=N or verification=N")
                limits[k] = int(v)
            save(p, new_state(a.workflow, limits))
            print(route(load(p)))
    elif a.cmd == "record":
        if a.file and not Path(a.file).is_file():
            raise SystemExit(f"--file not found: {a.file}")
        with locked(a.state):
            try:
                s = apply(load(a.state), a.event, a.detail, event_id=a.event_id, step=a.step, file=a.file, hash=a.hash)
            except Rejected as e:
                print(f"rejected: {e}", file=sys.stderr)
                return 2
            save(a.state, s)
        print(route(s))
    elif a.cmd == "route":
        s = load(a.state)
        nxt = route(s)
        print(json.dumps({"route": nxt, "phase": s["phase"], "spec_version": s["spec_version"],
                          "approved_spec_version": s["approved_spec_version"],
                          "executor_retries": s["executor_retries"], "verification_retries": s["verification_retries"],
                          "failed_step": s.get("failed_step"), "last_error": s["last_error"]}) if a.json else nxt)
    elif a.cmd == "get":
        s = load(a.state)
        print(json.dumps(s if a.field is None else s.get(a.field), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
