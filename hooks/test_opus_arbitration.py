#!/usr/bin/env python3
"""Subprocess tests for the Opus arbitration gate. The hook is not registered.

Prints a table and `Tally: N passed, M failed`. Exits non-zero on failure.
HOME and the ledger are temp paths, so the live switch and ledger are never written.
"""

import hashlib
import json
import os
import random
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import _agent_model
import _opus_arbitration as arb
import _opus_triggers as triggers

GATE = ROOT / "opus-arbitration-gate.py"
CLI = ROOT / "bin" / "opus-arbitration"
POLICY = ROOT / "data" / "opus-arbitration-policy.json"
FIXTURES = ROOT / "fixtures" / "opus-arbitration"
AGENT_FIXTURE = "20261104T091940.340987Z-PreToolUse-Agent.json"
SEND_FIXTURE = "20261104T091948.954349Z-PreToolUse-SendMessage.json"
POST_FIXTURE = "20261104T091945.493916Z-PostToolUse-Agent.json"

ENV_KEYS = (
    "CLAUDE_ROUTING",
    "CLAUDE_ROUTING_MODE",
    "CLAUDE_ROUTING_ALLOW_OPUS_SUBAGENT",
    "CLAUDE_ROUTING_SCOPE",
    "CLAUDE_ROUTING_UNATTENDED",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL",
    "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS",
    "CLAUDE_ROUTING_WORKFLOW_MAX_WORKERS",
    "CLAUDE_ROUTING_OTHER",
)
WORKFLOW_ENV = (
    "OPUS_WORKFLOW_DENIED reason=env; set "
    "CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL=sonnet and "
    "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS=4"
)
WORKFLOW_MAIN = (
    "OPUS_WORKFLOW_DENIED; workflow agents would inherit the Opus main model. "
    "Run workflows from a sonnet/haiku main session, or run the steps as normal Agent calls (they will be arbitrated)."
)


class Suite:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []

    def check(self, number, name, ok, detail=""):
        status = "PASS" if ok else "FAIL"
        print("%4s  %-78s %s" % (number, name, status))
        if ok:
            self.passed += 1
        else:
            self.failed += 1
            self.failures.append("%s %s: %s" % (number, name, detail))
            if detail:
                print("      %s" % detail)


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _agent_md(path, model, extra=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nname: %s\nmodel: %s\n%s---\nbody\n" % (path.stem, model, extra), encoding="utf-8")


class World:
    def __init__(self):
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix="opus-arb-")))
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.home.mkdir()
        self.project.mkdir()
        data = self.home / ".claude" / "hooks" / "data"
        data.mkdir(parents=True)
        shutil.copy(POLICY, data / "opus-arbitration-policy.json")
        agents = self.home / ".claude" / "agents"
        _agent_md(agents / "opus-judge.md", "opus", "tools: Read\n")
        _agent_md(agents / "file-reader.md", "haiku")
        _agent_md(agents / "file-writer.md", "sonnet")
        self._plugins()
        self.opus_transcript = self._transcript("opus-main", "claude-opus-5-5")
        self.sonnet_transcript = self._transcript("sonnet-main", "claude-sonnet-5-5")
        os.environ["HOME"] = str(self.home)
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.project)

    def _plugins(self):
        path = self.home / ".claude" / "plugins"
        path.mkdir()
        opus = path / "opusplug"
        sonnet = path / "sonnetplug"
        _agent_md(opus / "agents" / "worker.md", "opus")
        _agent_md(sonnet / "agents" / "worker.md", "sonnet")
        catalog = {
            "version": 2,
            "plugins": {
                "opusplug@test": [{"scope": "user", "installPath": str(opus), "version": "1"}],
                "sonnetplug@test": [{"scope": "user", "installPath": str(sonnet), "version": "1"}],
            },
        }
        (path / "installed_plugins.json").write_text(json.dumps(catalog), encoding="utf-8")

    def _transcript(self, name, model):
        path = self.home / ".claude" / "projects" / "demo" / ("%s.jsonl" % name)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"type": "assistant", "message": {"role": "assistant", "model": "claude-haiku-4-5"}},
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "message": {"role": "assistant", "model": model, "content": [{"type": "text", "text": "ok"}]}},
        ]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def env(self, extra=None):
        env = os.environ.copy()
        for key in ENV_KEYS:
            env.pop(key, None)
        env["HOME"] = str(self.home)
        env["CLAUDE_PROJECT_DIR"] = str(self.project)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if extra:
            env.update(extra)
        return env

    def close(self):
        logs = self.home / ".claude" / "logs"
        if logs.exists():
            os.chmod(logs, 0o700)
        shutil.rmtree(self.root, ignore_errors=True)


def agent_payload(world, **kwargs):
    data = load_fixture(AGENT_FIXTURE)
    data["session_id"] = kwargs.get("session_id", "sess-1")
    data["cwd"] = str(world.project)
    data["transcript_path"] = str(kwargs.get("transcript", world.sonnet_transcript))
    tool_input = dict(data["tool_input"])
    tool_input["prompt"] = kwargs.get("prompt", "run probe")
    tool_input["description"] = kwargs.get("description", "Run notools probe")
    tool_input["subagent_type"] = kwargs.get("subagent_type", "general-purpose")
    tool_input["run_in_background"] = kwargs.get("run_in_background", False)
    if "model" in kwargs and kwargs["model"] is not None:
        tool_input["model"] = kwargs["model"]
    else:
        tool_input.pop("model", None)
    data["tool_input"] = tool_input
    data["tool_use_id"] = kwargs.get("tool_use_id", "toolu_test")
    if kwargs.get("agent_id"):
        data["agent_id"] = kwargs["agent_id"]
    return data


def send_payload(world, target, session_id="sess-1", transcript=None):
    data = load_fixture(SEND_FIXTURE)
    data["session_id"] = session_id
    data["cwd"] = str(world.project)
    data["transcript_path"] = str(transcript or world.sonnet_transcript)
    tool_input = dict(data["tool_input"])
    tool_input["to"] = target
    tool_input["recipient"] = target
    data["tool_input"] = tool_input
    data["tool_use_id"] = "toolu_send_%s" % target
    return data


GATE_STDOUTS = []


def _records_allow(text):
    if not isinstance(text, str) or text.strip() == "":
        return False
    if '"permissionDecision":"allow"' in text or '"permissionDecision": "allow"' in text:
        return True
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return False
    hook = data.get("hookSpecificOutput") if isinstance(data, dict) else None
    return isinstance(hook, dict) and hook.get("permissionDecision") == "allow"


def run_gate(world, payload, extra=None):
    proc = subprocess.run(
        [sys.executable, str(GATE)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=world.env(extra),
        cwd=str(world.project),
        timeout=30,
        check=False,
    )
    GATE_STDOUTS.append(proc.stdout)
    return proc


def verdict(proc):
    if proc.returncode != 0:
        return "exit:%s" % proc.returncode, proc.stderr
    text = proc.stdout.strip()
    if text == "":
        if proc.stderr != "":
            return "stderr", proc.stderr
        return "allow", ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return "bad-json", text
    hook = data.get("hookSpecificOutput") or {}
    return hook.get("permissionDecision"), hook.get("permissionDecisionReason") or ""


def rows(world, table):
    path = world.home / ".claude" / "logs" / "opus-arbitration" / "ledger.db"
    if not path.exists():
        return []
    import sqlite3

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM %s" % table)]
    finally:
        conn.close()


def decision_body(request_id, decision, **extra):
    body = {
        "schema_version": 1,
        "request_id": request_id,
        "decision": decision,
        "recommended_model": "opus",
        "reason_code": "REASONING_COMPLEXITY",
        "rationale": "needs a closer look",
        "evidence_used": ["note"],
        "execution_scope": "analysis only",
        "suggested_next_step": "follow the decision",
    }
    body.update(extra)
    return body


def judge_result(world, request_id, tool_use_id, decision, **kwargs):
    if kwargs.get("request_id_override"):
        request_id = kwargs["request_id_override"]
    body = decision_body(request_id, decision)
    if "recommended_model" in kwargs:
        body["recommended_model"] = kwargs["recommended_model"]
    if kwargs.get("extra_key"):
        body["grant_uses"] = 9
    text = json.dumps(body)
    if kwargs.get("fence"):
        text = "```json\n%s\n```" % text
    if "text" in kwargs:
        text = kwargs["text"]
    post = load_fixture(POST_FIXTURE)
    post["session_id"] = kwargs.get("session_id", "sess-1")
    post["cwd"] = str(world.project)
    post["transcript_path"] = str(kwargs.get("transcript", world.sonnet_transcript))
    post["tool_use_id"] = tool_use_id
    post["tool_response"] = {
        "status": kwargs.get("status", "completed"),
        "agentId": kwargs.get("agent_id", "a" + "1" * 16),
        "agentType": "opus-judge",
        "resolvedModel": kwargs.get("resolved_model", "claude-opus-5-5"),
        "content": [{"type": "text", "text": text}],
    }
    if kwargs.get("async_launched"):
        post["tool_response"] = {
            "status": "async_launched",
            "isAsync": True,
            "agentId": kwargs.get("agent_id", "a" + "1" * 16),
            "resolvedModel": "claude-opus-5-5",
        }
    return post


def packet_from_reason(reason):
    start = reason.find("<<<PACKET\n")
    end = reason.find("\nPACKET>>>")
    if start < 0 or end < start:
        return None
    return reason[start + len("<<<PACKET\n"):end]


def request_id_from_text(text):
    if not isinstance(text, str):
        return ""
    marker = "request_id="
    start = text.find(marker)
    if start < 0:
        return ""
    rest = text[start + len(marker):]
    end = 0
    while end < len(rest) and (rest[end].isalnum() or rest[end] in "-_"):
        end += 1
    return rest[:end]


def stored_packet(request_id):
    shown = arb.show_request(request_id) if request_id else None
    if not isinstance(shown, dict):
        return None
    packet = shown.get("packet_text")
    return packet if isinstance(packet, str) and packet else None


def pending_from_deny(reason):
    request_id = request_id_from_text(reason)
    return request_id, stored_packet(request_id)


def hook_body(proc):
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError, AttributeError):
        return None
    hook = data.get("hookSpecificOutput") if isinstance(data, dict) else None
    return hook if isinstance(hook, dict) else None


def packet_object(packet):
    if not isinstance(packet, str):
        return None
    lines = packet.split("\n")
    if len(lines) < 2:
        return None
    try:
        obj = json.loads(lines[1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def judge_payload(world, packet, session_id, tool_use_id, model="opus", wrapped=True):
    prompt = "<<<PACKET\n%s\nPACKET>>>" % packet if wrapped else packet
    return agent_payload(
        world,
        session_id=session_id,
        subagent_type="opus-judge",
        model=model,
        description="Judge",
        prompt=prompt,
        tool_use_id=tool_use_id,
        transcript=world.opus_transcript,
        run_in_background=False,
    )


def escalate(world, payload):
    """Deny, run the judge, ingest ESCALATE. Returns (payload, request_id, result)."""
    denied = run_gate(world, payload)
    kind, reason = verdict(denied)
    if kind != "deny" or "ARBITRATION_REQUIRED" not in reason:
        raise AssertionError("expected arbitration deny, got %s %s" % (kind, reason[:240]))
    request_id, packet = pending_from_deny(reason)
    if not request_id or not packet:
        raise AssertionError("missing stored packet %s %s" % (request_id, reason[:240]))
    launched = run_gate(
        world,
        judge_payload(world, packet, payload["session_id"], "judge-" + request_id),
    )
    launch_kind, launch_reason = verdict(launched)
    if launch_kind != "allow":
        raise AssertionError("judge launch %s %s" % (launch_kind, launch_reason[:240]))
    result = arb.ingest_judge_result(judge_result(
        world, request_id, "judge-" + request_id, "ESCALATE", session_id=payload["session_id"],
    ))
    return payload, request_id, result


def test_resolution(suite):
    world = World()
    try:
        cases = (
            ("U01", "general-purpose sonnet env allows", {"subagent_type": "general-purpose"}, {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"}, world.opus_transcript, "allow"),
            ("U02", "file-reader haiku allows", {"subagent_type": "file-reader"}, None, world.opus_transcript, "allow"),
            ("U03", "file-writer sonnet allows", {"subagent_type": "file-writer"}, None, world.opus_transcript, "allow"),
            ("U04", "explicit opus denies", {"model": "opus", "description": "deep sk-abcdefghijklmnopqrstuvwxyz"}, None, world.sonnet_transcript, "deny"),
            ("U05", "inherit with opus main denies", {"model": "inherit", "subagent_type": "Plan"}, None, world.opus_transcript, "deny"),
            ("U05", "inherit with sonnet main allows", {"model": "inherit", "subagent_type": "Plan", "session_id": "sess-inh-s"}, None, world.sonnet_transcript, "allow"),
            ("U06", "Plan follows opus main", {"subagent_type": "Plan", "session_id": "sess-plan-o"}, None, world.opus_transcript, "deny"),
            ("U06", "Plan follows sonnet main", {"subagent_type": "Plan", "session_id": "sess-plan-s"}, None, world.sonnet_transcript, "allow"),
            ("U07", "plugin opus denies", {"subagent_type": "opusplug:worker", "session_id": "sess-plug-o"}, None, world.sonnet_transcript, "deny"),
            ("U08", "plugin sonnet allows", {"subagent_type": "sonnetplug:worker"}, None, world.opus_transcript, "allow"),
            ("U08", "plugin missing denies", {"subagent_type": "missingplug:worker", "session_id": "sess-plug-x"}, None, world.sonnet_transcript, "deny"),
        )
        for number, name, fields, extra, transcript, expect in cases:
            payload = agent_payload(world, transcript=transcript, tool_use_id="tool-" + name, **fields)
            kind, reason = verdict(run_gate(world, payload, extra))
            suite.check(number, name, kind == expect, "kind=%s reason=%s" % (kind, reason[:180]))
        denied = [row for row in rows(world, "requests") if row["reason_code"] == "UNAPPROVED_OPUS"]
        secret = "sk-abcdefghijklmnopqrstuvwxyz"
        packet = ""
        if denied:
            promoted = arb.promote_pending(denied[0]["request_id"]) if denied[0]["state"] == "CANDIDATE" else denied[0]
            packet = arb.build_judge_packet(arb.show_request(denied[0]["request_id"]) or {})
        events = ""
        event_path = world.home / ".claude" / "logs" / "opus-arbitration" / "events.jsonl"
        if event_path.exists():
            events = event_path.read_text(encoding="utf-8")
        suite.check(
            "U04",
            "opus deny redacts secrets and creates one candidate",
            len(denied) >= 1 and secret not in packet and "[REDACTED]" in packet and secret not in events,
            "requests=%s" % len(denied),
        )
        ledger = world.home / ".claude" / "logs" / "opus-arbitration"
        db = ledger / "ledger.db"
        suite.check(
            "U04",
            "ledger dir is 0700 and db is 0600",
            ledger.is_dir() and stat.S_IMODE(ledger.stat().st_mode) == 0o700 and stat.S_IMODE(db.stat().st_mode) == 0o600,
            "dir=%o file=%o" % (stat.S_IMODE(ledger.stat().st_mode), stat.S_IMODE(db.stat().st_mode)),
        )
    finally:
        world.close()


def test_gate_edges(suite):
    world = World()
    try:
        payload = agent_payload(world, model="opus", session_id="sess-edge", description="edges")
        kind, reason = verdict(run_gate(world, {**payload, "tool_input": {**payload["tool_input"], "run_in_background": True}}))
        suite.check("U04", "background opus denies", kind == "deny" and reason == "OPUS_BACKGROUND_DENIED", reason)
        nested = agent_payload(world, model="opus", session_id="sess-nest", description="nested", agent_id="a" + "e" * 16)
        kind, reason = verdict(run_gate(world, nested))
        suite.check("U04", "nested opus denies", kind == "deny" and reason == "OPUS_NESTED_DENIED", reason)
        kind, reason = verdict(run_gate(world, agent_payload(
            world, subagent_type="opus-judge", model="opus", session_id="sess-j", description="no pending",
        )))
        suite.check("U09", "judge without a pending request denies", kind == "deny" and reason == "JUDGE_NOT_PENDING", reason)
        pending = agent_payload(world, model="opus", session_id="sess-jm", description="mismatch", prompt="needs opus")
        kind, reason = verdict(run_gate(world, pending))
        packet = packet_from_reason(reason)
        bad = judge_payload(world, packet, "sess-jm", "judge-bad", model="sonnet")
        kind, reason = verdict(run_gate(world, bad))
        suite.check("U10", "judge overridden to sonnet denies", kind == "deny" and reason == "JUDGE_MODEL_MISMATCH", reason)
        strict = agent_payload(world, model="opus", session_id="sess-strict", description="strict")
        kind, reason = verdict(run_gate(world, strict, {"CLAUDE_ROUTING_MODE": "strict"}))
        suite.check("U36", "strict mode denies opus", kind == "deny" and reason == "OPUS_DENIED mode=strict; use sonnet/haiku", reason)
        suite.check("U36", "strict mode creates no request", not any(row["session_id"] == "sess-strict" for row in rows(world, "requests")), "")
        kind, reason = verdict(run_gate(world, agent_payload(
            world, model="opus", session_id="sess-legacy", description="legacy",
        ), {"CLAUDE_ROUTING_ALLOW_OPUS_SUBAGENT": "1"}))
        suite.check("U24", "legacy opus override still denies", kind == "deny" and "ARBITRATION_REQUIRED" in reason, reason[:160])
        kind, reason = verdict(run_gate(world, agent_payload(
            world, model="opus", session_id="sess-off", description="off",
        ), {"CLAUDE_ROUTING": "off"}))
        suite.check("U25", "CLAUDE_ROUTING=off still denies opus", kind == "deny" and "ARBITRATION_REQUIRED" in reason, reason[:160])
        event_text = (world.home / ".claude" / "logs" / "opus-arbitration" / "events.jsonl").read_text(encoding="utf-8")
        suite.check("U24", "legacy override is logged and ignored", "legacy_override_ignored" in event_text, "")
        proc = run_gate(world, None)
        # run_gate json.dumps None is invalid for the hook only if we pass raw. Send "{".
        proc = subprocess.run(
            [sys.executable, str(GATE)],
            input="{",
            text=True,
            capture_output=True,
            env=world.env(),
            timeout=15,
            check=False,
        )
        GATE_STDOUTS.append(proc.stdout)
        suite.check(
            "U39",
            "invalid JSON exits 2",
            proc.returncode == 2 and "invalid JSON" in proc.stderr and proc.stdout == "",
            "exit=%s stderr=%r" % (proc.returncode, proc.stderr),
        )
    finally:
        world.close()


def test_crash_and_ledger(suite):
    world = World()
    try:
        crash = world.home / ".claude" / "agents" / "crash-agent.md"
        crash.write_bytes(b"---\nmodel: bad\x00value\n---\n")
        payload = agent_payload(world, subagent_type="crash-agent", session_id="sess-crash", description="crash")
        proc = run_gate(world, payload)
        kind, reason = verdict(proc)
        suite.check(
            "U22",
            "corrupt definition denies",
            proc.returncode == 0 and kind == "deny" and reason == "OPUS_GATE_ERROR",
            "exit=%s kind=%s reason=%s stderr=%r" % (proc.returncode, kind, reason, proc.stderr[:200]),
        )
        logs = world.home / ".claude" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        os.chmod(logs, 0o500)
        try:
            sonnet = run_gate(world, agent_payload(world, subagent_type="file-reader", description="safe"))
            skind, sreason = verdict(sonnet)
            opus = run_gate(world, agent_payload(world, model="opus", session_id="sess-locked", description="locked"))
            okind, oreason = verdict(opus)
            suite.check("U23", "unwritable ledger still allows sonnet", skind == "allow", "kind=%s reason=%s" % (skind, sreason))
            suite.check(
                "U23",
                "unwritable ledger denies opus",
                opus.returncode == 0 and okind == "deny",
                "exit=%s kind=%s reason=%s" % (opus.returncode, okind, oreason[:160]),
            )
        finally:
            os.chmod(logs, 0o700)
    finally:
        world.close()


def test_judge_and_grant(suite):
    world = World()
    try:
        base = agent_payload(
            world, model="opus", session_id="sess-grant", description="approved work", prompt="do the approved thing",
        )
        payload, request_id, result = escalate(world, base)
        digest = arb.invocation_digest("sess-grant", payload["tool_input"], "opus")
        grants = rows(world, "grants")
        suite.check(
            "U14",
            "ESCALATE mints one grant bound to the pending digest",
            result.get("decision") == "ESCALATE"
            and result.get("worker_invocation") is None
            and len(grants) == 1
            and grants[0]["invocation_digest"] == digest
            and grants[0]["uses_left"] == 1
            and grants[0]["invocation_json"] is None,
            "result=%s grants=%s" % (result, grants),
        )
        mutations = (
            ("prompt", {**payload, "tool_input": {**payload["tool_input"], "prompt": "different"}, "tool_use_id": "mut-prompt"}),
            ("model", {**payload, "tool_input": {**payload["tool_input"], "model": "claude-opus-5-5"}, "tool_use_id": "mut-model"}),
            ("subagent_type", {**payload, "tool_input": {**payload["tool_input"], "subagent_type": "file-writer"}, "tool_use_id": "mut-type"}),
            ("session", {**payload, "session_id": "sess-other", "tool_use_id": "mut-session"}),
            ("description", {**payload, "tool_input": {**payload["tool_input"], "description": "other work"}, "tool_use_id": "mut-desc", "session_id": "sess-desc"}),
            ("background", {**payload, "tool_input": {**payload["tool_input"], "run_in_background": True}, "tool_use_id": "mut-bg"}),
        )
        mutation_ok = True
        mutation_detail = ""
        for label, mutated in mutations:
            kind, reason = verdict(run_gate(world, mutated))
            if kind != "deny":
                mutation_ok = False
                mutation_detail = "%s -> %s %s" % (label, kind, reason[:120])
                break
        suite.check("U20", "each invocation mutation denies", mutation_ok, mutation_detail)
        allowed = run_gate(world, {**payload, "tool_use_id": "consume-1"})
        kind, reason = verdict(allowed)
        suite.check("U14", "matching call consumes the grant", kind == "allow" and allowed.stdout == "", "kind=%s stdout=%r reason=%s" % (kind, allowed.stdout, reason))
        again = run_gate(world, {**payload, "tool_use_id": "consume-2"})
        kind, reason = verdict(again)
        suite.check("U18", "second distinct use denies", kind == "deny", "kind=%s reason=%s" % (kind, reason[:160]))
        replay = run_gate(world, {**payload, "tool_use_id": "consume-1"})
        kind, reason = verdict(replay)
        suite.check("U18", "replay of the consumed tool use allows", kind == "allow", "kind=%s reason=%s" % (kind, reason[:160]))
        expired_payload = agent_payload(
            world, model="opus", session_id="sess-expire", description="expires", prompt="expire me",
        )
        expire_call, _request, expire_result = escalate(world, expired_payload)
        suite.check("U14", "second session can escalate", expire_result.get("decision") == "ESCALATE", str(expire_result))
        import sqlite3
        db = world.home / ".claude" / "logs" / "opus-arbitration" / "ledger.db"
        conn = sqlite3.connect(db)
        conn.execute("UPDATE grants SET expires=1 WHERE session_id='sess-expire'")
        conn.execute("UPDATE requests SET expires=1 WHERE session_id='sess-expire' AND state='GRANT_READY'")
        conn.commit()
        conn.close()
        kind, reason = verdict(run_gate(world, {**expire_call, "tool_use_id": "expire-use"}))
        states = [row["state"] for row in rows(world, "requests") if row["session_id"] == "sess-expire"]
        suite.check("U21", "expired grant denies and is EXPIRED", kind == "deny" and "EXPIRED" in states, "kind=%s states=%s reason=%s" % (kind, states, reason[:120]))
    finally:
        world.close()


def test_judge_rejections(suite):
    world = World()
    try:
        outcomes = (
            ("U11", "CONTINUE", "CONTINUE", False),
            ("U12", "RETRY", "RETRY", False),
            ("U13", "STOP", "STOP", False),
        )
        for number, decision, label, _grant in outcomes:
            payload = agent_payload(
                world, model="opus", session_id="sess-" + decision, description=label, prompt="work " + label,
            )
            denied = run_gate(world, payload)
            kind, reason = verdict(denied)
            request_id, packet = pending_from_deny(reason)
            launched = run_gate(world, judge_payload(world, packet, payload["session_id"], "judge-" + decision, wrapped=False))
            launch_kind, launch_reason = verdict(launched)
            result = arb.ingest_judge_result(judge_result(
                world, request_id, "judge-" + decision, decision, session_id=payload["session_id"],
            ))
            grants = [row for row in rows(world, "grants") if row["session_id"] == payload["session_id"]]
            suite.check(
                number,
                "%s issues no grant" % label,
                launch_kind == "allow" and result.get("decision") == decision and result.get("worker_invocation") is None and grants == [],
                "launch=%s result=%s grants=%s reason=%s" % (launch_kind, result, grants, launch_reason[:120]),
            )
        bad_cases = (
            ("U15", "malformed", {"text": "not json"}),
            ("U15", "fenced", {"fence": True}),
            ("U15", "extra key", {"extra_key": True}),
            ("U16", "wrong request id", {"request_id_override": "esc-wrongwrongwrong"}),
            ("U17", "async launched", {"async_launched": True}),
            ("U10", "wrong resolved model", {"resolved_model": "claude-sonnet-5-5"}),
            ("U16", "uncorrelated tool use", {"tool_use": "toolu_other"}),
        )
        for number, label, options in bad_cases:
            payload = agent_payload(
                world, model="opus", session_id="sess-bad-" + label.replace(" ", "-"), description=label, prompt="bad " + label,
            )
            kind, reason = verdict(run_gate(world, payload))
            request_id, packet = pending_from_deny(reason)
            tool_use = "judge-bad-" + label.replace(" ", "-")
            run_gate(world, judge_payload(world, packet, payload["session_id"], tool_use))
            ingest_id = options.pop("tool_use", tool_use) if "tool_use" in options else tool_use
            before = len(rows(world, "grants"))
            result = arb.ingest_judge_result(judge_result(
                world, request_id, ingest_id, "ESCALATE", session_id=payload["session_id"], **options,
            ))
            after = len(rows(world, "grants"))
            suite.check(
                number,
                "judge %s grants nothing" % label,
                result.get("worker_invocation") is None and after == before and result.get("decision") != "ESCALATE",
                "result=%s grants %s->%s" % (result, before, after),
            )
        payload = agent_payload(world, model="opus", session_id="sess-transcript", description="transcript", prompt="check model")
        kind, reason = verdict(run_gate(world, payload))
        request_id, packet = pending_from_deny(reason)
        agent_id = "a" + "9" * 16
        run_gate(world, judge_payload(world, packet, payload["session_id"], "judge-transcript"))
        transcript = world.sonnet_transcript.parent / payload["session_id"] / "subagents" / ("agent-%s.jsonl" % agent_id)
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "model": "claude-sonnet-5-5"},
        }) + "\n", encoding="utf-8")
        before = len(rows(world, "grants"))
        result = arb.ingest_judge_result(judge_result(
            world, request_id, "judge-transcript", "ESCALATE",
            session_id=payload["session_id"], agent_id=agent_id, transcript=world.sonnet_transcript,
        ))
        suite.check(
            "U10",
            "judge transcript model mismatch grants nothing",
            len(rows(world, "grants")) == before and result.get("decision") != "ESCALATE",
            str(result),
        )
    finally:
        world.close()


def test_race(suite):
    world = World()
    try:
        payload = agent_payload(world, model="opus", session_id="sess-race", description="race", prompt="race the grant")
        payload, _request, result = escalate(world, payload)
        if result.get("decision") != "ESCALATE":
            suite.check("U19", "race setup escalates", False, str(result))
            return

        def once(index):
            call = dict(payload)
            call["tool_use_id"] = "race-%s" % index
            call["tool_input"] = dict(payload["tool_input"])
            return verdict(run_gate(world, call))[0]

        with ThreadPoolExecutor(max_workers=8) as pool:
            kinds = list(pool.map(once, range(8)))
        suite.check("U19", "eight consumers allow exactly one", kinds.count("allow") == 1, "kinds=%s" % kinds)
        suite.check("U38", "parallel consumers do not double-grant", kinds.count("allow") == 1 and kinds.count("deny") == 7, "kinds=%s" % kinds)

        def same_call(index):
            call = agent_payload(
                world, model="opus", session_id="sess-dedup-race", description="same", prompt="same", tool_use_id="dedup-%s" % index,
            )
            return verdict(run_gate(world, call))[0]

        with ThreadPoolExecutor(max_workers=4) as pool:
            dedup_kinds = list(pool.map(same_call, range(4)))
        created = [row for row in rows(world, "requests") if row["session_id"] == "sess-dedup-race"]
        suite.check(
            "U38",
            "parallel identical opus calls share one request",
            dedup_kinds.count("deny") == 4 and len(created) == 1,
            "kinds=%s rows=%s" % (dedup_kinds, len(created)),
        )
    finally:
        world.close()


def test_send_message(suite):
    world = World()
    try:
        sonnet_id = "a" + "b" * 16
        opus_id = "a" + "c" * 16
        judge_id = "a" + "d" * 16
        unknown_id = "a" + "e" * 16
        transcript_sonnet = "a" + "f" * 16
        transcript_opus = "a" + "a" * 16
        arb.record_agent_result({
            "session_id": "sess-1",
            "tool_use_id": "tu-sonnet",
            "tool_response": {"status": "completed", "agentId": sonnet_id, "agentType": "file-reader", "resolvedModel": "claude-sonnet-5-5"},
        })
        arb.record_agent_result({
            "session_id": "sess-1",
            "tool_use_id": "tu-opus",
            "tool_response": {"status": "completed", "agentId": opus_id, "agentType": "general-purpose", "resolvedModel": "claude-opus-5-5"},
        })
        arb.record_agent_result({
            "session_id": "sess-1",
            "tool_use_id": "tu-judge",
            "tool_response": {"status": "completed", "agentId": judge_id, "agentType": "opus-judge", "resolvedModel": "claude-sonnet-5-5"},
        })
        for agent_id, model in ((transcript_sonnet, "claude-sonnet-5-5"), (transcript_opus, "claude-opus-5-5")):
            path = world.sonnet_transcript.parent / "sess-1" / "subagents" / ("agent-%s.jsonl" % agent_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"type": "assistant", "message": {"model": model}}) + "\n", encoding="utf-8")
        checks = (
            ("recorded opus denies", opus_id, "deny"),
            ("recorded judge denies", judge_id, "deny"),
            ("recorded sonnet allows", sonnet_id, "allow"),
            ("unknown agent id denies", unknown_id, "deny"),
            ("name main allows", "main", "allow"),
            ("transcript sonnet allows", transcript_sonnet, "allow"),
            ("transcript opus denies", transcript_opus, "deny"),
        )
        for name, target, expect in checks:
            kind, reason = verdict(run_gate(world, send_payload(world, target)))
            suite.check("U26", name, kind == expect, "kind=%s reason=%s" % (kind, reason[:160]))
        suite.check(
            "U27",
            "resume of an opus agent denies",
            verdict(run_gate(world, send_payload(world, opus_id)))[0] == "deny",
            "",
        )
    finally:
        world.close()


def test_quotas_and_machine(suite):
    world = World()
    try:
        first = None
        created = 0
        blocked = None
        for index in range(4):
            result = arb.register_candidate(
                session_id="sess-q",
                task_id="task-%s" % index,
                trigger="T04",
                reason_code="REPEATED_DISTINCT_FAILURE",
                scope_digest="scope-%s" % index,
            )
            if result["ok"]:
                created += 1
                first = first or result
            else:
                blocked = result
        again = arb.register_candidate(
            session_id="sess-q",
            task_id="task-0",
            trigger="T04",
            reason_code="REPEATED_DISTINCT_FAILURE",
            scope_digest="scope-0",
        )
        suite.check(
            "U28",
            "same candidate twice is one request",
            first and again["ok"] and again["created"] is False and again["request"]["request_id"] == first["request"]["request_id"],
            "again=%s" % again,
        )
        suite.check(
            "U37",
            "fourth judgment in a session is quota",
            created == 3 and blocked and blocked["reason"] == "QUOTA_EXHAUSTED" and blocked["request"] is None,
            "created=%s blocked=%s" % (created, blocked),
        )
        os.environ["CLAUDE_ROUTING_UNATTENDED"] = "1"
        try:
            unattended = []
            for index in range(3):
                unattended.append(arb.register_candidate(
                    session_id="sess-u",
                    task_id="u-%s" % index,
                    trigger="T01",
                    reason_code="MANDATORY_REVIEW",
                    scope_digest="u-%s" % index,
                ))
            suite.check(
                "U37",
                "unattended env tightens the session quota",
                unattended[0]["ok"] and unattended[1]["ok"] and not unattended[2]["ok"] and unattended[2]["reason"] == "QUOTA_EXHAUSTED",
                str([(item["ok"], item["reason"]) for item in unattended]),
            )
        finally:
            os.environ.pop("CLAUDE_ROUTING_UNATTENDED", None)
        loose = json.loads(POLICY.read_text(encoding="utf-8"))
        loose["unattended"]["max_judgments_per_session"] = 100
        policy_path = world.home / ".claude" / "hooks" / "data" / "opus-arbitration-policy.json"
        policy_path.write_text(json.dumps(loose), encoding="utf-8")
        os.environ["CLAUDE_ROUTING_UNATTENDED"] = "1"
        try:
            loosened = []
            for index in range(4):
                loosened.append(arb.register_candidate(
                    session_id="sess-loose",
                    task_id="loose-%s" % index,
                    trigger="T01",
                    reason_code="MANDATORY_REVIEW",
                    scope_digest="loose-%s" % index,
                ))
            suite.check(
                "U37",
                "unattended env cannot loosen the quota",
                all(item["ok"] for item in loosened[:3]) and not loosened[3]["ok"],
                str([(item["ok"], item["reason"]) for item in loosened]),
            )
        finally:
            os.environ.pop("CLAUDE_ROUTING_UNATTENDED", None)
            policy_path.write_text(POLICY.read_text(encoding="utf-8"), encoding="utf-8")
        raised = False
        try:
            arb.transition_to(first["request"]["request_id"], "GRANT_CONSUMED")
        except arb.TransitionError:
            raised = True
        state = arb.show_request(first["request"]["request_id"])["state"]
        suite.check("U28", "illegal transition becomes FAILED_CLOSED", raised and state == "FAILED_CLOSED", "raised=%s state=%s" % (raised, state))
    finally:
        os.environ.pop("CLAUDE_ROUTING_UNATTENDED", None)
        world.close()


def test_triggers(suite):
    world = World()
    try:
        same = [
            {"task_id": "cursor:t", "strategy_digest": "aaa", "outcome": "failed"},
            {"task_id": "cursor:t", "strategy_digest": "aaa", "outcome": "failed"},
        ]
        distinct = same + [{"task_id": "cursor:t", "strategy_digest": "bbb", "outcome": "failed"}]
        suite.check("U28", "identical failures are not a candidate", triggers.distinct_failures_candidate("sess-t", "cursor:t", same) is None, "")
        created = triggers.distinct_failures_candidate("sess-t", "cursor:t", distinct)
        again = triggers.distinct_failures_candidate("sess-t", "cursor:t", distinct)
        suite.check(
            "U29",
            "two distinct failures are one candidate",
            created and created["ok"] and again["ok"] and again["created"] is False,
            "created=%s again=%s" % (created, again),
        )
        timeout = triggers.failure_candidate("timeout", "")
        permission = triggers.failure_candidate("failed", "permission denied while opening the file")
        budget = triggers.failure_candidate("failed", "read budget exhausted")
        suite.check(
            "U31",
            "timeout and permission do not escalate",
            timeout["request"] is None and timeout["class"] == "environment"
            and permission["request"] is None and permission["class"] == "environment",
            "timeout=%s permission=%s" % (timeout, permission),
        )
        suite.check("U32", "budget exhaustion does not escalate", budget["request"] is None and budget["class"] == "budget", str(budget))
        footer = {
            "schema_version": 1,
            "task_id": "cursor:checked",
            "status": "COMPLETED",
            "reason_code": "DONE",
            "evidence_refs": [],
            "attempts": [],
        }
        failed_run = [{"task_id": "cursor:checked", "strategy_digest": "ccc", "outcome": "failed"}]
        validated = triggers.validator_failure_candidate("sess-v", footer, failed_run)
        suite.check("U30", "COMPLETED footer plus a failed run is a candidate", validated and validated["ok"] and validated["request"]["trigger"] == "VALIDATOR_FAILURE", str(validated)[:240])
        text = "notes\n```json\n%s\n```\n" % json.dumps({
            "schema_version": 1,
            "task_id": "task-need",
            "status": "NEEDS_JUDGMENT",
            "reason_code": "UNRESOLVED",
            "unresolved": ["which index"],
            "evidence_refs": ["a.md"],
            "attempts": [{"approach": "A", "validator": "t", "result": "failed"}],
            "suggested_scope": "pick one",
        })
        parsed = triggers.parse_footer(text)
        requested = triggers.footer_candidate("sess-f", text)
        suite.check("T03", "NEEDS_JUDGMENT footer is a worker request", parsed and parsed["status"] == "NEEDS_JUDGMENT" and requested and requested["request"]["trigger"] == "WORKER_REQUEST", str(requested)[:240])
        conflict = triggers.footer_candidate("sess-c", json.dumps({
            "schema_version": 1,
            "task_id": "task-conflict",
            "status": "NEEDS_JUDGMENT",
            "reason_code": "CONFLICTING_REQUIREMENTS",
            "evidence_refs": ["spec.md#1"],
            "attempts": [],
        }))
        suite.check("T06", "conflicting requirements are a candidate", conflict and conflict["request"]["reason_code"] == "CONFLICTING_REQUIREMENTS", str(conflict)[:240])
        blocked = triggers.footer_candidate("sess-b", json.dumps({
            "schema_version": 1,
            "task_id": "task-block",
            "status": "BLOCKED",
            "reason_code": "permission denied",
            "unresolved": ["permission denied"],
            "attempts": [{"approach": "A"}],
        }))
        suite.check("T08", "blocked on permission is not a candidate", blocked and blocked["request"] is None and blocked["class"] == "environment", str(blocked))
        suite.check("T01", "empty mandatory list admits nothing", triggers.admission_candidate("security-review", "sess-a", "task-a") is None, "")
        trivial = json.dumps({
            "schema_version": 1,
            "task_id": "task-trivial",
            "status": "NEEDS_JUDGMENT",
            "reason_code": "UNRESOLVED",
            "unresolved": [],
            "attempts": [],
        })
        owned = triggers.footer_candidate("sess-owned", trivial, "file-reader", resolved_model="claude-haiku-4-5")
        ignored = triggers.footer_candidate("sess-gp-empty", trivial, "general-purpose", resolved_model="claude-haiku-4-5")
        suite.check(
            "T03",
            "owned empty footer is a candidate and general-purpose empty footer is not",
            owned and owned.get("ok") and owned["request"]["trigger"] == "WORKER_REQUEST"
            and ignored is None
            and packet_object(owned.get("packet")).get("current_model_observed") == "claude-haiku-4-5",
            "owned=%s ignored=%s" % (str(owned)[:180], ignored),
        )
        long_attempt = "approach-" + ("x" * 240)
        rich = json.dumps({
            "schema_version": 1,
            "task_id": "task-rich",
            "status": "NEEDS_JUDGMENT",
            "reason_code": "UNRESOLVED",
            "unresolved": ["u%s" % index for index in range(7)],
            "evidence_refs": ["ref-%s" % index for index in range(6)],
            "attempts": [{"approach": long_attempt, "result": "failed"} for _index in range(6)],
        })
        rich_result = triggers.footer_candidate("sess-gp-rich", rich, "general-purpose", resolved_model="claude-sonnet-5-5")
        rich_body = packet_object(rich_result.get("packet") if isinstance(rich_result, dict) else None) or {}
        unresolved = [item.get("unresolved") for item in rich_body.get("objective_failure_evidence") or [] if "unresolved" in item]
        refs = [item.get("evidence_ref") for item in rich_body.get("objective_failure_evidence") or [] if "evidence_ref" in item]
        approaches = [item.get("approach") for item in rich_body.get("prior_attempts") or []]
        suite.check(
            "T03",
            "general-purpose footer with unresolved is capped evidence",
            rich_result and rich_result.get("ok")
            and len(unresolved) == 5 and len(refs) == 5 and len(approaches) == 5
            and all(len(item) <= 200 for item in approaches)
            and rich_body.get("current_model_observed") == "claude-sonnet-5-5",
            "unresolved=%s refs=%s attempts=%s" % (len(unresolved), len(refs), len(approaches)),
        )
        t04_body = packet_object(created.get("packet") if isinstance(created, dict) else None) or {}
        digests = [item.get("strategy_digest") for item in t04_body.get("prior_attempts") or []]
        statuses = [item.get("status") for item in t04_body.get("objective_failure_evidence") or []]
        suite.check(
            "T04",
            "distinct-failure packet keeps short digests and failing statuses",
            digests == ["aaa", "bbb"] and statuses == ["failed", "failed"],
            "body=%s" % t04_body,
        )
        validated_body = packet_object(validated.get("packet") if isinstance(validated, dict) else None) or {}
        suite.check(
            "T05",
            "validator packet records the cursor-run attempt",
            validated_body.get("prior_attempts") == [{"outcome": "failed", "strategy_digest": "ccc"}]
            and validated_body.get("objective_failure_evidence") == [{"source": "cursor-run", "status": "failed"}],
            "body=%s" % validated_body,
        )
        spec = world.project / "job.md"
        spec.write_text("do the thing\n", encoding="utf-8")
        command = str(world.home / ".claude" / "hooks" / "bin" / "cursor-run") + " " + str(spec)
        recorded = arb.record_cursor_run_result({
            "tool_name": "Bash",
            "session_id": "sess-run",
            "tool_input": {"command": command},
            "tool_response": {"stdout": "noise\n" + json.dumps({"status": "failed"}) + "\n"},
        })
        skipped = arb.record_cursor_run_result({
            "tool_name": "Bash",
            "session_id": "sess-run",
            "tool_input": {"command": command.split()[0] + " new job"},
            "tool_response": {"stdout": json.dumps({"status": "ok"}) + "\n"},
        })
        suite.check(
            "T04",
            "cursor-run records the spec digest and ignores new",
            recorded and recorded["outcome"] == "failed" and recorded["task_id"] == "cursor:job" and skipped is None,
            "recorded=%s skipped=%s" % (recorded, skipped),
        )
    finally:
        world.close()


def test_cli(suite):
    world = World()
    try:
        payload = agent_payload(world, model="opus", session_id="sess-cli", description="cli", prompt="hold")
        kind, reason = verdict(run_gate(world, payload))
        request_id = request_id_from_text(reason)
        status = subprocess.run([sys.executable, str(CLI), "status"], capture_output=True, text=True, env=world.env(), timeout=15, check=False)
        suite.check(
            "CLI",
            "status lists the open request without the prompt",
            status.returncode == 0 and request_id in status.stdout and "JUDGE_PENDING" in status.stdout and "hold" not in status.stdout,
            status.stdout[:300],
        )
        disabled = subprocess.run([sys.executable, str(CLI), "disable"], capture_output=True, text=True, env=world.env(), timeout=15, check=False)
        shown = arb.show_request(request_id)
        mode = (world.home / ".claude" / "opus-arbitration-mode").read_text(encoding="utf-8")
        kind, reason = verdict(run_gate(world, agent_payload(world, model="opus", session_id="sess-cli2", description="after", prompt="x")))
        suite.check(
            "CLI",
            "disable cancels pending work and denies opus",
            disabled.returncode == 0 and mode == "disabled\n" and shown["state"] == "CANCELLED" and kind == "deny" and reason.startswith("OPUS_DENIED mode=disabled"),
            "exit=%s mode=%r state=%s kind=%s reason=%s" % (disabled.returncode, mode, shown and shown["state"], kind, reason),
        )
        enabled = subprocess.run([sys.executable, str(CLI), "enable"], input="", capture_output=True, text=True, env=world.env(), timeout=15, check=False)
        suite.check("CLI", "non-tty enable is refused", enabled.returncode == 2, "exit=%s stderr=%r" % (enabled.returncode, enabled.stderr))
        arb.write_switch("active")
        suite.check(
            "CLI",
            "enable does not revive a cancelled request",
            arb.show_request(request_id)["state"] == "CANCELLED",
            arb.show_request(request_id)["state"],
        )
    finally:
        world.close()


def test_packet_and_cli(suite):
    world = World()
    try:
        prompt = "PROMPT-TOKEN-XYZ " + ("p" * 1600)
        payload = agent_payload(
            world,
            model="opus",
            session_id="sess-packet",
            description="deep sk-abcdefghijklmnopqrstuvwxyz",
            prompt=prompt,
            transcript=world.sonnet_transcript,
        )
        kind, reason = verdict(run_gate(world, payload))
        request_id, packet = pending_from_deny(reason)
        packet = packet or ""
        body = packet_object(packet) or {}
        again = arb.build_judge_packet(arb.show_request(request_id) or {})
        event_text = (world.home / ".claude" / "logs" / "opus-arbitration" / "events.jsonl").read_text(encoding="utf-8")
        pending = None
        for line in event_text.splitlines():
            if '"event":"judge_pending"' in line and request_id in line:
                pending = json.loads(line)
        excerpt = body.get("task_excerpt") or ""
        suite.check(
            "T02",
            "packet observes the main-session model and the prompt excerpt",
            kind == "deny"
            and "PROMPT-TOKEN-XYZ" not in reason
            and "<<<PACKET" not in reason
            and body.get("request_id") == request_id
            and body.get("current_model_observed") == "claude-sonnet-5-5"
            and body.get("current_model_observed") != "opus"
            and excerpt.startswith("PROMPT-TOKEN-XYZ ")
            and excerpt.endswith("…[truncated]")
            and len(excerpt) == 1500
            and "[REDACTED]" in (body.get("description") or "")
            and "sk-abcdefghijklmnopqrstuvwxyz" not in packet
            and again == packet
            and arb.packet_digest(again) == arb.packet_digest(packet)
            and isinstance(pending, dict)
            and pending.get("task_excerpt_chars") == 1500
            and pending.get("description_chars") == len(body.get("description") or "")
            and "PROMPT-TOKEN-XYZ" not in event_text,
            "model=%s excerpt=%s event=%s" % (
                body.get("current_model_observed"), len(excerpt), pending,
            ),
        )
        defined = agent_payload(
            world, model="opus", subagent_type="file-writer", session_id="sess-def",
            description="write", prompt="write the doc",
        )
        kind, reason = verdict(run_gate(world, defined))
        defined_body = packet_object(stored_packet(request_id_from_text(reason))) or {}
        suite.check(
            "T02",
            "definition baseline is the agent model, not the requested opus",
            kind == "deny" and defined_body.get("current_model_observed") == "sonnet",
            "model=%s" % defined_body.get("current_model_observed"),
        )
        env_payload = agent_payload(
            world, model="opus", session_id="sess-env", description="env", prompt="env prompt",
            transcript=world.opus_transcript,
        )
        kind, reason = verdict(run_gate(
            world, env_payload, {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"},
        ))
        env_body = packet_object(stored_packet(request_id_from_text(reason))) or {}
        suite.check(
            "T02",
            "general-purpose baseline is the env default",
            kind == "deny" and env_body.get("current_model_observed") == "sonnet",
            "model=%s" % env_body.get("current_model_observed"),
        )
        denied_lines = [
            json.loads(line) for line in event_text.splitlines()
            if '"event":"gate_denied"' in line
        ]
        # The definition and env denies append more lines. Re-read.
        event_text = (world.home / ".claude" / "logs" / "opus-arbitration" / "events.jsonl").read_text(encoding="utf-8")
        denied_lines = [json.loads(line) for line in event_text.splitlines() if '"event":"gate_denied"' in line]
        suite.check(
            "T02",
            "gate denial records the tool use and not the prompt",
            any(item.get("tool_use_id") == payload["tool_use_id"] and item.get("reason_code") == "ARBITRATION_REQUIRED" for item in denied_lines)
            and "PROMPT-TOKEN-XYZ" not in event_text
            and all("prompt" not in item for item in denied_lines),
            "events=%s" % denied_lines[:3],
        )

        shown_payload = agent_payload(
            world, model="opus", session_id="sess-show", description="visible-desc",
            prompt="PROMPT-NOT-FOR-CLI", tool_use_id="tool-show",
        )
        _call, shown_id, result = escalate(world, shown_payload)
        consumed_kind, _consumed_reason = verdict(run_gate(world, shown_payload))
        agent_id = "a" + "9" * 16
        arb.record_agent_result({
            "session_id": "sess-show",
            "cwd": str(world.project),
            "tool_use_id": "tool-show",
            "tool_response": {
                "status": "completed",
                "agentId": agent_id,
                "agentType": "general-purpose",
                "resolvedModel": "claude-opus-5-5",
            },
        })
        status = subprocess.run(
            [sys.executable, str(CLI), "status"], capture_output=True, text=True, env=world.env(), timeout=15, check=False,
        )
        show = subprocess.run(
            [sys.executable, str(CLI), "show", shown_id], capture_output=True, text=True, env=world.env(), timeout=15, check=False,
        )
        view = json.loads(show.stdout) if show.returncode == 0 and show.stdout.strip() else {}
        recent = status.stdout.split("recent:", 1)[-1]
        suite.check(
            "CLI",
            "status and show include the decision without the prompt",
            status.returncode == 0
            and shown_id in recent
            and "ESCALATE" in recent
            and "WORKER_COMPLETED" in recent
            and consumed_kind == "allow"
            and "claude-opus-5-5" in status.stdout
            and "PROMPT-NOT-FOR-CLI" not in status.stdout
            and "PROMPT-NOT-FOR-CLI" not in show.stdout
            and view.get("description") == "visible-desc"
            and view.get("judge_model") == "claude-opus-5-5"
            and view.get("judge_agent_id") == "a" + "1" * 16
            and view.get("judge_tool_use_id")
            and view.get("worker", {}).get("agent_id") == agent_id
            and view.get("worker", {}).get("kind") == "escalated"
            and "task_excerpt" not in view
            and result.get("decision") == "ESCALATE",
            "status=%s show=%s" % (status.stdout[:240], show.stdout[:400]),
        )
    finally:
        world.close()


def test_fuzz_and_source(suite):
    rng = random.Random(4)
    alphabet = [
        "a", "Z", " ", "\t", "\r", "\n", "\r\n",
        "e\u0301", "\u00e9", "a\u0308", "\u00e4",
        "🙂", "你", "\u200d", "line  ", "x\n y \n",
    ]
    samples = []
    for _index in range(500):
        chunks = [rng.choice(alphabet) for _ in range(rng.randint(1, 6))]
        samples.append("".join(chunks) + (" " * rng.randint(0, 3)) + rng.choice(["", "\n", "\r\n", "\t"]))
    groups = {}
    stable = True
    for text in samples:
        norm = arb.normalize_text(text)
        if arb.normalize_text(norm) != norm:
            stable = False
            break
        digest = arb.invocation_digest("s", {"prompt": text, "description": "d", "subagent_type": "general-purpose"}, "opus")
        groups.setdefault(norm, set()).add(digest)
    digests = [next(iter(value)) for value in groups.values()]
    suite.check(
        "FUZZ",
        "prompt normalization is stable and digest-equal only for equivalent text",
        stable and all(len(value) == 1 for value in groups.values()) and len(digests) == len(set(digests))
        and arb.normalize_text("a \r\nb  ") == arb.normalize_text("a\nb")
        and arb.normalize_text("e\u0301") == arb.normalize_text("\u00e9"),
        "groups=%s" % len(groups),
    )
    opus_ok = True
    for _index in range(500):
        token = "".join(rng.choice("abcXYZ-_") for _ in range(6)) + rng.choice(["opus", "OPUS", "Opus"]) + "".join(rng.choice("xyz") for _ in range(4))
        if _agent_model._model_family(token) != "opus":
            opus_ok = False
            break
    suite.check("FUZZ", "a string containing opus is never a lower family", opus_ok, "")
    source = GATE.read_text(encoding="utf-8")
    banned = ("OPUS_LEDGER", "OPUS_POLICY", "ARBITRATION_DB", "LEDGER_PATH", "POLICY_PATH")
    suite.check(
        "FUZZ",
        "gate has no home or ledger path seam",
        'os.environ.get("HOME"' not in source and "os.environ.get('HOME'" not in source and all(name not in source for name in banned),
        "",
    )
    composed = arb.compose_worker_invocation(
        {"request_id": "esc-1", "task_id": "task-1", "requester_agent_type": "slack-sync", "task_summary": "look"},
        {"execution_scope": "read", "evidence_used": ["a.md"]},
    )
    suite.check(
        "U14",
        "composed worker stays inside a normal role",
        composed["subagent_type"] == "general-purpose" and composed["model"] == "opus" and "cursor-run" in composed["prompt"],
        str(composed)[:200],
    )


def test_part2_hardening(suite):
    world = World()
    saved_parse = arb._parse_decision_text
    try:
        payload = agent_payload(
            world, model="opus", session_id="sess-replay", description="replay digest", prompt="same prompt",
        )
        call, _request, result = escalate(world, payload)
        suite.check("A1", "replay setup escalates", result.get("decision") == "ESCALATE", str(result))
        kind, reason = verdict(run_gate(world, {**call, "tool_use_id": "replay-ok"}))
        suite.check("A1", "first consume allows", kind == "allow", reason)
        mutated = {
            **call,
            "tool_use_id": "replay-ok",
            "tool_input": {**call["tool_input"], "prompt": "mutated prompt"},
        }
        kind, reason = verdict(run_gate(world, mutated))
        suite.check("A1", "replay with a different digest denies", kind == "deny", "kind=%s reason=%s" % (kind, reason[:160]))
        other = {**call, "tool_use_id": "replay-ok", "session_id": "sess-replay-other"}
        kind, reason = verdict(run_gate(world, other))
        suite.check("A1", "replay in another session denies", kind == "deny", "kind=%s reason=%s" % (kind, reason[:160]))
        kind, reason = verdict(run_gate(world, {**call, "tool_use_id": "replay-ok"}))
        suite.check("A1", "matching replay still allows", kind == "allow", "kind=%s reason=%s" % (kind, reason[:80]))

        boom_payload = agent_payload(
            world, model="opus", session_id="sess-boom", description="boom", prompt="raise in ingest",
        )
        denied = run_gate(world, boom_payload)
        request_id, packet = pending_from_deny(verdict(denied)[1])
        run_gate(world, judge_payload(world, packet, boom_payload["session_id"], "judge-boom"))

        def explode(_text):
            raise RuntimeError("ingest exploded")

        arb._parse_decision_text = explode
        try:
            crashed = arb.ingest_judge_result(judge_result(
                world, request_id, "judge-boom", "ESCALATE", session_id="sess-boom",
            ))
        finally:
            arb._parse_decision_text = saved_parse
        state = arb.show_request(request_id)["state"]
        events = (world.home / ".claude" / "logs" / "opus-arbitration" / "events.jsonl").read_text(encoding="utf-8")
        suite.check(
            "A2",
            "ingest exception fail-closes with judge_ingest_error",
            crashed.get("failed_closed") is True and state == "FAILED_CLOSED" and "judge_ingest_error" in events
            and crashed.get("request_id") == request_id,
            "state=%s result=%s" % (state, crashed),
        )

        trans_payload = agent_payload(
            world, model="opus", session_id="sess-trans", description="transition", prompt="transition error",
        )
        denied = run_gate(world, trans_payload)
        trans_id, packet = pending_from_deny(verdict(denied)[1])
        run_gate(world, judge_payload(world, packet, trans_payload["session_id"], "judge-trans"))
        real_transition = arb._transition

        def boom_transition(conn, request_id, new_state, **fields):
            if isinstance(new_state, str) and new_state.startswith("JUDGED_"):
                raise arb.TransitionError("forced")
            return real_transition(conn, request_id, new_state, **fields)

        arb._transition = boom_transition
        try:
            forced = arb.ingest_judge_result(judge_result(
                world, trans_id, "judge-trans", "CONTINUE", session_id="sess-trans",
            ))
        finally:
            arb._transition = real_transition
        trans_state = arb.show_request(trans_id)["state"]
        trans_events = (world.home / ".claude" / "logs" / "opus-arbitration" / "events.jsonl").read_text(encoding="utf-8")
        import importlib.util
        post_spec = importlib.util.spec_from_file_location(
            "opus_post_decision_text", ROOT / "opus-arbitration-post.py",
        )
        post_mod = importlib.util.module_from_spec(post_spec)
        post_spec.loader.exec_module(post_mod)
        forced_text = post_mod._decision_text(forced)
        suite.check(
            "A2",
            "TransitionError ingest returns ARBITRATION_FAILED",
            forced.get("failed_closed") is True
            and forced.get("reason") == "judge_ingest_error"
            and forced.get("request_id") == trans_id
            and trans_state == "FAILED_CLOSED"
            and "judge_ingest_error" in trans_events
            and "ARBITRATION_FAILED" in forced_text
            and trans_id in forced_text
            and "Do not relaunch the judge" in forced_text,
            "state=%s result=%s text=%r" % (trans_state, forced, forced_text[:240]),
        )

        sonnet_id = "a" + "1" * 16
        opus_id = "a" + "2" * 16
        arb.record_agent_result({
            "session_id": "sess-multi",
            "cwd": str(world.project),
            "tool_use_id": "tu-s",
            "tool_response": {"status": "completed", "agentId": sonnet_id, "agentType": "file-reader", "resolvedModel": "claude-sonnet-5-5"},
        })
        arb.record_agent_result({
            "session_id": "sess-multi",
            "cwd": str(world.project),
            "tool_use_id": "tu-o",
            "tool_response": {"status": "completed", "agentId": opus_id, "agentType": "general-purpose", "resolvedModel": "claude-opus-5-5"},
        })
        mixed = send_payload(world, sonnet_id, session_id="sess-multi")
        mixed["tool_input"]["recipient"] = sonnet_id
        mixed["tool_input"]["to"] = opus_id
        kind, reason = verdict(run_gate(world, mixed))
        suite.check("A3", "any blocked SendMessage target denies", kind == "deny" and "OPUS_RESUME_DENIED" in reason, reason[:160])
        listed = send_payload(world, "main", session_id="sess-multi")
        listed["tool_input"]["recipient"] = "main"
        listed["tool_input"]["to"] = "main"
        listed["tool_input"]["recipients"] = ["main", opus_id]
        kind, reason = verdict(run_gate(world, listed))
        suite.check("A3", "recipients list is checked", kind == "deny", reason[:160])

        outside = world.home / "outside-plugin"
        _agent_md(outside / "agents" / "worker.md", "sonnet")
        linked = world.home / ".claude" / "plugins" / "linkplug"
        linked.mkdir()
        (linked / "agents").symlink_to(outside / "agents")
        catalog_path = world.home / ".claude" / "plugins" / "installed_plugins.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog["plugins"]["linkplug@test"] = [{"scope": "user", "installPath": str(linked), "version": "1"}]
        catalog["plugins"]["outplug@test"] = [{"scope": "user", "installPath": str(outside), "version": "1"}]
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        suite.check(
            "A4",
            "symlinked plugin agents directory fails closed",
            _agent_model.resolve_plugin_definition("linkplug:worker") == ("fail", None),
            str(_agent_model.resolve_plugin_definition("linkplug:worker")),
        )
        suite.check(
            "A4",
            "plugin install outside the plugins root fails closed",
            _agent_model.resolve_plugin_definition("outplug:worker") == ("fail", None),
            str(_agent_model.resolve_plugin_definition("outplug:worker")),
        )

        missing_id = agent_payload(world, model="opus", session_id="sess-noid", description="no id", prompt="no tool use")
        missing_id.pop("tool_use_id")
        kind, reason = verdict(run_gate(world, missing_id))
        suite.check("A5", "opus without tool_use_id denies", kind == "deny" and reason == "OPUS_NO_TOOL_USE_ID", reason)
        judge_missing = agent_payload(
            world, subagent_type="opus-judge", model="opus", session_id="sess-noid-j", description="judge no id",
            transcript=world.opus_transcript,
        )
        judge_missing["tool_use_id"] = ""
        kind, reason = verdict(run_gate(world, judge_missing))
        suite.check("A5", "judge without tool_use_id denies", kind == "deny" and reason == "JUDGE_NO_TOOL_USE_ID", reason)

        for label, flag in (("string true", "true"), ("integer one", 1)):
            flagged = agent_payload(
                world, model="opus", session_id="sess-bg-" + label.replace(" ", "-"), description=label, prompt=label,
            )
            flagged["tool_input"]["run_in_background"] = flag
            kind, reason = verdict(run_gate(world, flagged))
            suite.check("A6", "background %s denies" % label, kind == "deny" and reason == "OPUS_BACKGROUND_DENIED", reason)

        wide = world.home / ".claude" / "projects" / "demo" / "wide.jsonl"
        head = json.dumps({"type": "assistant", "message": {"model": "claude-sonnet-5-5"}}) + "\n"
        wide.write_text(head + ("x" * 300000) + "\n", encoding="utf-8")
        seen = _agent_model.main_session_model({"transcript_path": str(wide)})
        suite.check("A7", "main model reads past 256 KiB", seen == "sonnet", seen)
        huge = world.home / ".claude" / "projects" / "demo" / "cut.jsonl"
        marker = json.dumps({"type": "assistant", "message": {"model": "claude-opus-5-5"}})
        pad = b"z" * (2 * 1024 * 1024)
        huge.write_bytes(marker.encode("utf-8") + b"\n" + pad)
        # The only assistant line starts before the 2 MiB window.
        cut = _agent_model.main_session_model({"transcript_path": str(huge)})
        suite.check("A7", "incomplete assistant line is unknown", cut == "unknown", cut)

        arb.write_switch("disabled")
        kind, reason = verdict(run_gate(world, agent_payload(
            world, model="opus", session_id="sess-off-exact", description="off", prompt="off",
        )))
        suite.check("A8", "disabled switch reason is exact", kind == "deny" and reason == "OPUS_DENIED mode=disabled", reason)
        arb.write_switch("active")
        decided = agent_payload(
            world, model="opus", session_id="sess-decided", description="once", prompt="once only",
        )
        first = run_gate(world, decided)
        kind, reason = verdict(first)
        request_id, packet = pending_from_deny(reason)
        run_gate(world, judge_payload(world, packet, "sess-decided", "judge-decided"))
        arb.ingest_judge_result(judge_result(world, request_id, "judge-decided", "CONTINUE", session_id="sess-decided"))
        kind, reason = verdict(run_gate(world, {**decided, "tool_use_id": "decided-again"}))
        suite.check(
            "A8",
            "terminal identical request is already decided",
            kind == "deny" and reason == "ARBITRATION_ALREADY_DECIDED decision=CONTINUE; do not retry Opus for this task",
            reason,
        )
        quota_reasons = []
        for index in range(4):
            kind, reason = verdict(run_gate(world, agent_payload(
                world, model="opus", session_id="sess-quota-gate", description="q%s" % index, prompt="body %s" % index,
                tool_use_id="quota-%s" % index,
            )))
            quota_reasons.append(reason)
        suite.check(
            "A8",
            "true quota uses ARBITRATION_QUOTA_EXHAUSTED",
            quota_reasons[3] == "ARBITRATION_QUOTA_EXHAUSTED",
            str(quota_reasons),
        )

        import shutil
        copy = world.project / "gate-copy.py"
        shutil.copy(GATE, copy)
        env = world.env()
        env.pop("PYTHONPATH", None)
        imported = subprocess.run(
            [sys.executable, str(copy)],
            input="{}",
            text=True,
            capture_output=True,
            env=env,
            timeout=15,
            check=False,
        )
        GATE_STDOUTS.append(imported.stdout)
        imported_kind, imported_reason = verdict(imported)
        suite.check(
            "A8",
            "import error denies and exits 0",
            imported.returncode == 0 and imported_kind == "deny" and imported_reason == "OPUS_GATE_ERROR import",
            "exit=%s reason=%s stderr=%r" % (imported.returncode, imported_reason, imported.stderr[:200]),
        )

        logged = agent_payload(world, model="opus", session_id="sess-project", description="project", prompt="where")
        run_gate(world, logged)
        stored = [row for row in rows(world, "requests") if row["session_id"] == "sess-project"]
        event_text = (world.home / ".claude" / "logs" / "opus-arbitration" / "events.jsonl").read_text(encoding="utf-8")
        project = os.path.realpath(world.project)
        suite.check(
            "A10",
            "request and event carry the project realpath",
            len(stored) == 1 and stored[0]["project"] == project and '"project":"%s"' % project in event_text,
            "project=%r stored=%s" % (stored[0]["project"] if stored else None, project),
        )

        import importlib.util
        location = importlib.util.spec_from_file_location("route_tools_gap", ROOT / "route-tools.py")
        mod = importlib.util.module_from_spec(location)
        location.loader.exec_module(mod)
        mod.effective_agent_model = None
        log_path = world.project / "routing.jsonl"
        previous = os.environ.get("ROUTING_LOG")
        os.environ["ROUTING_LOG"] = str(log_path)
        try:
            code = mod.handle_agent(
                "main",
                {"session_id": "sess-gap"},
                {"subagent_type": "general-purpose", "description": "d", "prompt": "p"},
                "strict",
            )
        finally:
            if previous is None:
                os.environ.pop("ROUTING_LOG", None)
            else:
                os.environ["ROUTING_LOG"] = previous
        logged_lines = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
        suite.check(
            "A8",
            "missing agent model logs once and does not raise",
            code == 0 and len(logged_lines) == 1 and "import-failed" in logged_lines[0],
            "code=%s lines=%s" % (code, logged_lines[:1]),
        )
    finally:
        arb._parse_decision_text = saved_parse
        world.close()


def _request_state(request_id):
    shown = arb.show_request(request_id)
    if not isinstance(shown, dict):
        return None
    return shown.get("state")


def _grant_row(world, request_id):
    for row in rows(world, "grants"):
        if row.get("request_id") == request_id:
            return row
    return None


def test_references(suite):
    world = World()
    try:
        payload = agent_payload(
            world, model="opus", session_id="sess-ref", description="ref judge",
            prompt="SECRET-PROMPT-REF do the hard part", tool_use_id="ref-opus",
        )
        kind, reason = verdict(run_gate(world, payload))
        request_id, packet = pending_from_deny(reason)
        suite.check(
            "REF",
            "required message names the short packet and hides the prompt",
            kind == "deny"
            and request_id
            and 'prompt exactly "ARBITRATION-PACKET request_id=%s"' % request_id in reason
            and "SECRET-PROMPT-REF" not in reason
            and "<<<PACKET" not in reason
            and isinstance(packet, str)
            and packet.startswith("ARBITRATION-PACKET request_id=%s\n" % request_id),
            reason[:240],
        )
        before = len(rows(world, "requests"))
        unknown = agent_payload(
            world, subagent_type="opus-judge", model="opus", session_id="sess-ref",
            description="Judge missing", prompt="ARBITRATION-PACKET request_id=esc-unknownunknown",
            tool_use_id="ref-unknown", transcript=world.opus_transcript,
        )
        kind, reason = verdict(run_gate(world, unknown))
        suite.check(
            "REF",
            "unknown judge reference denies",
            kind == "deny" and reason == "JUDGE_NOT_PENDING" and len(rows(world, "requests")) == before
            and _request_state(request_id) == "JUDGE_PENDING",
            reason,
        )
        other = agent_payload(
            world, subagent_type="opus-judge", model="opus", session_id="sess-other-ref",
            description="Judge other", prompt="ARBITRATION-PACKET request_id=%s" % request_id,
            tool_use_id="ref-other", transcript=world.opus_transcript,
        )
        kind, reason = verdict(run_gate(world, other))
        suite.check(
            "REF",
            "other session judge reference denies",
            kind == "deny" and reason == "JUDGE_NOT_PENDING" and _request_state(request_id) == "JUDGE_PENDING",
            reason,
        )
        extra = agent_payload(
            world, subagent_type="opus-judge", model="opus", session_id="sess-ref",
            description="Judge extra",
            prompt="ARBITRATION-PACKET request_id=%s\nextra" % request_id,
            tool_use_id="ref-extra", transcript=world.opus_transcript,
        )
        kind, reason = verdict(run_gate(world, extra))
        suite.check(
            "REF",
            "reference plus extra text is an exact-copy mismatch",
            kind == "deny" and reason == "JUDGE_NOT_PENDING" and _request_state(request_id) == "JUDGE_PENDING",
            reason,
        )
        exact_payload = agent_payload(
            world, model="opus", session_id="sess-exact", description="exact copy",
            prompt="exact body", tool_use_id="exact-opus",
        )
        exact_kind, exact_reason = verdict(run_gate(world, exact_payload))
        exact_id, exact_packet = pending_from_deny(exact_reason)
        exact_launch = run_gate(world, judge_payload(
            world, exact_packet, "sess-exact", "judge-exact", wrapped=False,
        ))
        exact_launch_kind, exact_launch_reason = verdict(exact_launch)
        suite.check(
            "REF",
            "exact judge copy still proceeds without updatedInput",
            exact_kind == "deny" and exact_launch_kind == "allow" and exact_launch.stdout == ""
            and _request_state(exact_id) == "JUDGE_RUNNING",
            "launch=%s %s stdout=%r" % (exact_launch_kind, exact_launch_reason[:120], exact_launch.stdout[:160]),
        )
        arb.ingest_judge_result(judge_result(
            world, exact_id, "judge-exact", "CONTINUE", session_id="sess-exact",
        ))
        stale = agent_payload(
            world, subagent_type="opus-judge", model="opus", session_id="sess-exact",
            description="Judge stale", prompt="ARBITRATION-PACKET request_id=%s" % exact_id,
            tool_use_id="ref-stale", transcript=world.opus_transcript,
        )
        kind, reason = verdict(run_gate(world, stale))
        suite.check(
            "REF",
            "non-pending judge reference denies",
            kind == "deny" and reason == "JUDGE_NOT_PENDING" and _request_state(exact_id) == "JUDGED_CONTINUE",
            "state=%s reason=%s" % (_request_state(exact_id), reason),
        )
        good = agent_payload(
            world, subagent_type="opus-judge", model="opus", session_id="sess-ref",
            description="Judge %s" % request_id,
            prompt="ARBITRATION-PACKET request_id=%s \n" % request_id,
            tool_use_id="ref-good", transcript=world.opus_transcript,
        )
        proc = run_gate(world, good)
        hook = hook_body(proc)
        updated = hook.get("updatedInput") if isinstance(hook, dict) else None
        suite.check(
            "REF",
            "judge reference rewrites the prompt and marks the judge running",
            proc.returncode == 0
            and isinstance(hook, dict)
            and hook.get("hookEventName") == "PreToolUse"
            and "permissionDecision" not in hook
            and isinstance(updated, dict)
            and updated.get("prompt") == packet
            and updated.get("description") == good["tool_input"]["description"]
            and updated.get("subagent_type") == "opus-judge"
            and updated.get("model") == "opus"
            and updated.get("run_in_background") is False
            and _request_state(request_id) == "JUDGE_RUNNING",
            "state=%s stdout=%s" % (_request_state(request_id), proc.stdout[:300]),
        )
        again = agent_payload(
            world, subagent_type="opus-judge", model="opus", session_id="sess-ref",
            description="Judge again", prompt="ARBITRATION-PACKET request_id=%s" % request_id,
            tool_use_id="ref-again", transcript=world.opus_transcript,
        )
        kind, reason = verdict(run_gate(world, again))
        suite.check(
            "REF",
            "second judge reference on a running request denies",
            kind == "deny" and reason == "JUDGE_NOT_PENDING" and _request_state(request_id) == "JUDGE_RUNNING",
            reason,
        )
        marked_opus = agent_payload(
            world, model="opus", session_id="sess-mark", description="marked",
            prompt="marked body", tool_use_id="mark-opus",
        )
        mark_kind, mark_reason = verdict(run_gate(world, marked_opus))
        mark_id, mark_packet = pending_from_deny(mark_reason)
        wrapped = "<<<PACKET\nARBITRATION-PACKET request_id=%s\nPACKET>>>" % mark_id
        marked = run_gate(world, judge_payload(world, wrapped, "sess-mark", "judge-mark", wrapped=False))
        marked_hook = hook_body(marked)
        marked_input = marked_hook.get("updatedInput") if isinstance(marked_hook, dict) else None
        suite.check(
            "REF",
            "markers around the packet reference are stripped",
            mark_kind == "deny"
            and isinstance(marked_input, dict)
            and marked_input.get("prompt") == mark_packet
            and _request_state(mark_id) == "JUDGE_RUNNING",
            marked.stdout[:240],
        )

        registered = arb.register_candidate(
            session_id="sess-grant-ref",
            task_id="task-compose",
            trigger="T03",
            reason_code="CONFLICTING_REQUIREMENTS",
            requester_agent_type="file-reader",
            summary="resolve",
            task_summary="resolve the conflict",
            project=str(world.project),
        )
        request = arb.promote_pending(registered["request"]["request_id"]) if registered.get("ok") else None
        grant_request = request["request_id"] if isinstance(request, dict) else ""
        launch = run_gate(world, judge_payload(
            world, request.get("packet_text") if isinstance(request, dict) else "",
            "sess-grant-ref", "judge-grant-ref", wrapped=False,
        ))
        launch_kind, launch_reason = verdict(launch)
        judged = arb.ingest_judge_result(judge_result(
            world, grant_request, "judge-grant-ref", "ESCALATE", session_id="sess-grant-ref",
        ))
        grant = _grant_row(world, grant_request)
        stored = None
        if grant and isinstance(grant.get("invocation_json"), str):
            stored = json.loads(grant["invocation_json"])
        suite.check(
            "REF",
            "composed escalate stores invocation_json",
            launch_kind == "allow" and judged.get("decision") == "ESCALATE" and isinstance(stored, dict)
            and stored.get("model") == "opus",
            "launch=%s %s result=%s" % (launch_kind, launch_reason[:80], judged),
        )
        if isinstance(stored, dict):
            def grant_call(prompt, tool_use, **overrides):
                fields = {
                    "session_id": "sess-grant-ref",
                    "subagent_type": stored["subagent_type"],
                    "model": stored["model"],
                    "description": stored["description"],
                    "prompt": prompt,
                    "tool_use_id": tool_use,
                    "run_in_background": False,
                }
                fields.update(overrides)
                return agent_payload(world, **fields)

            mismatches = (
                ("description", {"description": "other work"}),
                ("model", {"model": "sonnet"}),
                ("subagent_type", {"subagent_type": "general-purpose"}),
                ("background", {"run_in_background": True}),
                ("extra key", {}),
            )
            mismatch_ok = True
            mismatch_detail = ""
            for label, overrides in mismatches:
                call = grant_call(
                    "ARBITRATION-GRANT request_id=%s" % grant_request,
                    "mis-" + label.replace(" ", "-"),
                    **overrides,
                )
                if label == "extra key":
                    call["tool_input"]["team"] = "x"
                kind, reason = verdict(run_gate(world, call))
                fresh = _grant_row(world, grant_request)
                state = _request_state(grant_request)
                uses = fresh.get("uses_left") if isinstance(fresh, dict) else None
                if kind != "deny" or reason != "GRANT_MISMATCH" or uses != 1 or state != "GRANT_READY":
                    mismatch_ok = False
                    mismatch_detail = "%s kind=%s reason=%s uses=%s state=%s" % (label, kind, reason, uses, state)
                    break
            suite.check("REF", "grant reference mismatches deny and do not consume", mismatch_ok, mismatch_detail)
            good_grant = grant_call("ARBITRATION-GRANT request_id=%s\n" % grant_request, "grant-ref-1")
            proc = run_gate(world, good_grant)
            hook = hook_body(proc)
            updated = hook.get("updatedInput") if isinstance(hook, dict) else None
            fresh = _grant_row(world, grant_request)
            suite.check(
                "REF",
                "grant reference rewrites to the stored invocation",
                proc.returncode == 0
                and isinstance(hook, dict)
                and "permissionDecision" not in hook
                and updated == stored
                and isinstance(fresh, dict)
                and fresh.get("uses_left") == 0
                and _request_state(grant_request) == "GRANT_CONSUMED",
                "state=%s stdout=%s" % (_request_state(grant_request), proc.stdout[:300]),
            )
            second = grant_call("ARBITRATION-GRANT request_id=%s" % grant_request, "grant-ref-2")
            kind, reason = verdict(run_gate(world, second))
            suite.check(
                "REF",
                "second grant reference denies",
                kind == "deny" and _grant_row(world, grant_request).get("uses_left") == 0,
                reason,
            )

        t02 = agent_payload(
            world, model="opus", session_id="sess-t02-ref", description="original",
            prompt="original prompt", tool_use_id="t02-opus",
        )
        t02_call, t02_id, t02_result = escalate(world, t02)
        t02_grant = _grant_row(world, t02_id)
        ref = agent_payload(
            world, model="opus", session_id="sess-t02-ref", description="original",
            prompt="ARBITRATION-GRANT request_id=%s" % t02_id, tool_use_id="t02-ref",
        )
        kind, reason = verdict(run_gate(world, ref))
        after_ref = _grant_row(world, t02_id)
        original = run_gate(world, {**t02_call, "tool_use_id": "t02-original"})
        original_kind, original_reason = verdict(original)
        suite.check(
            "REF",
            "T02 grant reference denies and the original call still consumes",
            t02_result.get("decision") == "ESCALATE"
            and isinstance(t02_grant, dict)
            and t02_grant.get("invocation_json") is None
            and kind == "deny" and reason == "GRANT_MISMATCH"
            and isinstance(after_ref, dict)
            and after_ref.get("uses_left") == 1
            and original_kind == "allow" and original.stdout == "",
            "ref=%s %s original=%s %s" % (kind, reason, original_kind, original_reason[:120]),
        )
    finally:
        world.close()


def _call(world, tool_name, tool_input, transcript, session_id, tool_use_id):
    data = agent_payload(
        world,
        transcript=transcript,
        session_id=session_id,
        tool_use_id=tool_use_id,
        prompt="unused",
        description="unused",
    )
    data["tool_name"] = tool_name
    data["tool_input"] = tool_input
    return data


def _write_skill(world, name, front):
    path = world.home / ".claude" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n%s---\nbody\n" % front, encoding="utf-8")


def test_v2_hardening(suite):
    world = World()
    try:
        env_cases = (
            ("env claude-opus-5-5 denies", {"CLAUDE_CODE_SUBAGENT_MODEL": "claude-opus-5-5"}, world.sonnet_transcript, "deny"),
            ("env best denies", {"CLAUDE_CODE_SUBAGENT_MODEL": "best"}, world.sonnet_transcript, "deny"),
            ("env sonnet allows", {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"}, world.opus_transcript, "allow"),
        )
        for name, extra, transcript, expect in env_cases:
            payload = agent_payload(
                world,
                subagent_type="general-purpose",
                transcript=transcript,
                session_id="sess-" + name.split()[1],
                tool_use_id="env-" + name.split()[1],
            )
            kind, reason = verdict(run_gate(world, payload, extra))
            suite.check("H2", name, kind == expect, "kind=%s reason=%s" % (kind, reason[:160]))
        bare = world.home / ".claude" / "agents" / "bare-agent.md"
        bare.write_text("---\nname: bare-agent\n---\n", encoding="utf-8")
        kind, reason = verdict(run_gate(world, agent_payload(
            world, subagent_type="bare-agent", transcript=world.sonnet_transcript,
            session_id="sess-bare-best", tool_use_id="bare-best",
        ), {"CLAUDE_CODE_SUBAGENT_MODEL": "best"}))
        suite.check("H2", "definition without a model and env best denies", kind == "deny", reason[:160])
        kind, reason = verdict(run_gate(world, agent_payload(
            world, subagent_type="bare-agent", transcript=world.opus_transcript,
            session_id="sess-bare-sonnet", tool_use_id="bare-sonnet",
        ), {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"}))
        suite.check("H2", "definition without a model and env sonnet allows", kind == "allow", reason[:160])

        agents = world.home / ".claude" / "agents"
        (agents / "indent-agent.md").write_text("---\nname: indent-agent\n  model: haiku\n---\n", encoding="utf-8")
        indented = agent_payload(
            world, subagent_type="indent-agent", transcript=world.opus_transcript,
            session_id="sess-indent", tool_use_id="indent-1",
        )
        saved_env = os.environ.pop("CLAUDE_CODE_SUBAGENT_MODEL", None)
        try:
            family = _agent_model.resolve_for_gate(indented, indented["tool_input"]).get("family")
        finally:
            if saved_env is not None:
                os.environ["CLAUDE_CODE_SUBAGENT_MODEL"] = saved_env
        kind, reason = verdict(run_gate(world, indented))
        suite.check(
            "H3",
            "indented model key is unknown",
            family == "unknown" and kind == "deny",
            "family=%s kind=%s reason=%s" % (family, kind, reason[:120]),
        )
        (agents / "dup-agent.md").write_text("---\nmodel: haiku\nmodel: sonnet\n---\n", encoding="utf-8")
        duplicated = agent_payload(
            world, subagent_type="dup-agent", transcript=world.sonnet_transcript,
            session_id="sess-dup", tool_use_id="dup-1",
        )
        family = _agent_model.resolve_for_gate(duplicated, duplicated["tool_input"]).get("family")
        kind, _reason = verdict(run_gate(world, duplicated))
        suite.check("H3", "duplicate model keys are unknown", family == "unknown" and kind == "deny", "family=%s kind=%s" % (family, kind))
        (agents / "quoted-agent.md").write_text('---\nmodel: "opus"\n---\n', encoding="utf-8")
        quoted = agent_payload(
            world, subagent_type="quoted-agent", transcript=world.sonnet_transcript,
            session_id="sess-quoted", tool_use_id="quoted-1",
        )
        family = _agent_model.resolve_for_gate(quoted, quoted["tool_input"]).get("family")
        kind, _reason = verdict(run_gate(world, quoted))
        suite.check("H3", "quoted model opus resolves to opus", family == "opus" and kind == "deny", "family=%s kind=%s" % (family, kind))

        for label, value in (("number", 1), ("list", ["haiku"]), ("bool", True)):
            payload = agent_payload(
                world, subagent_type="file-reader", transcript=world.sonnet_transcript,
                session_id="sess-ns-" + label, tool_use_id="ns-" + label,
            )
            payload["tool_input"]["model"] = value
            family = _agent_model.resolve_for_gate(payload, payload["tool_input"]).get("family")
            kind, _reason = verdict(run_gate(world, payload))
            suite.check("H6", "non-string model %s is unknown" % label, family == "unknown" and kind == "deny", "family=%s kind=%s" % (family, kind))
        bad_type = agent_payload(
            world, subagent_type="bad type", transcript=world.sonnet_transcript,
            session_id="sess-bad-type", tool_use_id="bad-type",
        )
        family = _agent_model.resolve_for_gate(bad_type, bad_type["tool_input"]).get("family")
        kind, _reason = verdict(run_gate(world, bad_type))
        suite.check("H6", "illegal subagent_type is unknown", family == "unknown" and kind == "deny", "family=%s kind=%s" % (family, kind))
        missing = agent_payload(
            world, subagent_type="not-builtin", transcript=world.sonnet_transcript,
            session_id="sess-missing-type", tool_use_id="missing-type",
        )
        family = _agent_model.resolve_for_gate(missing, missing["tool_input"]).get("family")
        kind, _reason = verdict(run_gate(world, missing))
        suite.check("H6", "unknown agent type is unknown", family == "unknown" and kind == "deny", "family=%s kind=%s" % (family, kind))
        explore = agent_payload(
            world, subagent_type="Explore", transcript=world.sonnet_transcript,
            session_id="sess-explore", tool_use_id="explore-1",
        )
        kind, reason = verdict(run_gate(world, explore))
        suite.check("H6", "known builtin Explore follows sonnet main", kind == "allow", reason[:120])

        project_judge = world.project / ".claude" / "agents" / "opus-judge.md"
        project_judge.parent.mkdir(parents=True, exist_ok=True)
        project_judge.write_text("---\nmodel: opus\ntools: Read\n---\n", encoding="utf-8")
        kind, reason = verdict(run_gate(world, agent_payload(
            world, subagent_type="opus-judge", model="opus", transcript=world.opus_transcript,
            session_id="sess-shadow", tool_use_id="judge-shadow", description="Judge",
        )))
        suite.check("H4", "project opus-judge file is shadowed", kind == "deny" and reason == "JUDGE_SHADOWED", reason)
        project_judge.unlink()
        user_judge = world.home / ".claude" / "agents" / "opus-judge.md"
        saved = user_judge.read_text(encoding="utf-8")
        link_target = world.home / ".claude" / "agents" / "judge-real.md"
        link_target.write_text(saved, encoding="utf-8")
        user_judge.unlink()
        user_judge.symlink_to(link_target)
        kind, reason = verdict(run_gate(world, agent_payload(
            world, subagent_type="opus-judge", model="opus", transcript=world.opus_transcript,
            session_id="sess-link", tool_use_id="judge-link", description="Judge",
        )))
        suite.check("H4", "symlinked judge definition is invalid", kind == "deny" and reason == "JUDGE_DEFINITION_INVALID", reason)
        user_judge.unlink()
        user_judge.write_text("---\nmodel: haiku\ntools: Read\n---\n", encoding="utf-8")
        kind, reason = verdict(run_gate(world, agent_payload(
            world, subagent_type="opus-judge", model="opus", transcript=world.opus_transcript,
            session_id="sess-haiku-judge", tool_use_id="judge-haiku", description="Judge",
        )))
        suite.check("H4", "judge model other than opus is invalid", kind == "deny" and reason == "JUDGE_DEFINITION_INVALID", reason)
        for label, body in (
            ("[]", "tools: []\n"),
            ("Read, Bash", "tools: Read, Bash\n"),
            ("star", "tools: '*'\n"),
            ("disallowedTools", "tools: Read\ndisallowedTools: Bash\n"),
        ):
            user_judge.write_text("---\nmodel: opus\n%s---\n" % body, encoding="utf-8")
            kind, reason = verdict(run_gate(world, agent_payload(
                world, subagent_type="opus-judge", model="opus", transcript=world.opus_transcript,
                session_id="sess-tools-" + label, tool_use_id="judge-tools-" + label, description="Judge",
            )))
            suite.check(
                "H4",
                "judge tools %s are invalid" % label,
                kind == "deny" and reason == "JUDGE_DEFINITION_INVALID",
                reason,
            )
        user_judge.write_text("---\nmodel: opus\n---\n", encoding="utf-8")
        kind, reason = verdict(run_gate(world, agent_payload(
            world, subagent_type="opus-judge", model="opus", transcript=world.opus_transcript,
            session_id="sess-notools", tool_use_id="judge-notools", description="Judge",
        )))
        suite.check("H4", "judge without tools is invalid", kind == "deny" and reason == "JUDGE_DEFINITION_INVALID", reason)
        user_judge.write_text("---\nmodel: opus\ntools: [Read]\n---\n", encoding="utf-8")
        kind, reason = verdict(run_gate(world, agent_payload(
            world, subagent_type="opus-judge", model="opus", transcript=world.opus_transcript,
            session_id="sess-tools-list", tool_use_id="judge-tools-list", description="Judge",
        )))
        suite.check("H4", "judge tools [Read] still needs a pending request", kind == "deny" and reason == "JUDGE_NOT_PENDING", reason)
        user_judge.write_text(saved, encoding="utf-8")
        kind, reason = verdict(run_gate(world, agent_payload(
            world, subagent_type="opus-judge", model="opus", transcript=world.opus_transcript,
            session_id="sess-judge-ok", tool_use_id="judge-ok", description="Judge",
        )))
        suite.check("H4", "valid judge still needs a pending request", kind == "deny" and reason == "JUDGE_NOT_PENDING", reason)

        workflow_main = (
            "OPUS_WORKFLOW_DENIED; workflow agents would inherit the Opus main model. "
            "Run workflows from a sonnet/haiku main session, or run the steps as normal Agent calls (they will be arbitrated)."
        )
        workflow_field = (
            "OPUS_WORKFLOW_DENIED; name, resumeFromRunId, an unreadable scriptPath, "
            "or script text containing opus is denied"
        )
        bare_script = 'agent(prompt="go")\n'
        safe_script = "agent(model: 'sonnet')\nagent(model: \"claude-haiku-4-5\")\n"
        opus_script = "agent(model: 'sonnet')\n# uses opus\n"
        cases = (
            ("any workflow under opus main denies", bare_script, world.opus_transcript, "deny", WORKFLOW_ENV),
            ("pinned-looking workflow under opus main denies", safe_script, world.opus_transcript, "deny", WORKFLOW_ENV),
            ("workflow text containing opus denies", opus_script, world.sonnet_transcript, "deny", workflow_field),
        )
        for index, (name, script, transcript, expect, expect_reason) in enumerate(cases):
            payload = _call(
                world, "Workflow", {"script": script}, transcript,
                "sess-wf-h5-%s" % index, "wf-h5-%s" % index,
            )
            kind, reason = verdict(run_gate(world, payload))
            ok = kind == expect and (expect != "deny" or reason == expect_reason)
            suite.check("H5", name, ok, "kind=%s reason=%s" % (kind, reason[:180]))
        for label, transcript, expect_reason in (
            ("opus", world.opus_transcript, workflow_field),
            ("sonnet", world.sonnet_transcript, workflow_field),
        ):
            payload = _call(
                world, "Workflow", {"name": "weekly"}, transcript,
                "sess-named-wf-" + label, "wf-named-" + label,
            )
            kind, reason = verdict(run_gate(world, payload))
            suite.check(
                "H5",
                "named workflow under %s main denies" % label,
                kind == "deny" and reason == expect_reason,
                reason[:180],
            )
        outside = world.root / "outside-workflow.js"
        outside.write_text(safe_script, encoding="utf-8")
        payload = _call(
            world, "Workflow", {"scriptPath": str(outside)}, world.sonnet_transcript,
            "sess-outside-wf", "wf-outside",
        )
        kind, reason = verdict(run_gate(world, payload))
        suite.check("H5", "script outside home is unknown and denies", kind == "deny" and reason == workflow_field, reason[:180])
        inside = world.home / ".claude" / "inside-workflow.js"
        inside.write_text(safe_script, encoding="utf-8")
        payload = _call(
            world, "Workflow", {"scriptPath": str(inside)}, world.opus_transcript,
            "sess-inside-wf", "wf-inside",
        )
        kind, reason = verdict(run_gate(world, payload))
        suite.check(
            "H5",
            "scriptPath under home still denies when the main model is opus",
            kind == "deny" and reason == WORKFLOW_ENV,
            reason[:180],
        )

        _write_skill(world, "fork-opus", "name: fork-opus\ncontext: fork\nmodel: opus\n")
        _write_skill(world, "fork-plain", "name: fork-plain\ncontext: fork\n")
        _write_skill(world, "inline-opus", "name: inline-opus\nmodel: opus\n")
        kind, reason = verdict(run_gate(world, _call(
            world, "Skill", {"skill": "fork-opus"}, world.sonnet_transcript, "sess-fork-opus", "skill-opus",
        )))
        suite.check("H5", "fork skill with model opus denies", kind == "deny" and reason == "OPUS_SKILL_FORK_DENIED", reason)
        kind, reason = verdict(run_gate(world, _call(
            world, "Skill", {"skill": "/fork-plain"}, world.sonnet_transcript, "sess-fork-plain", "skill-plain",
        )))
        suite.check("H5", "fork skill without a model under sonnet main allows", kind == "allow", reason[:160])
        kind, reason = verdict(run_gate(world, _call(
            world, "Skill", {"skill": "inline-opus"}, world.opus_transcript, "sess-inline", "skill-inline",
        )))
        suite.check("H5", "inline skill allows", kind == "allow", reason[:160])
        kind, reason = verdict(run_gate(world, _call(
            world, "Skill", {"skill": "missing-skill"}, world.opus_transcript, "sess-missing-skill", "skill-missing",
        )))
        events = (world.home / ".claude" / "logs" / "opus-arbitration" / "events.jsonl").read_text(encoding="utf-8")
        suite.check(
            "H5",
            "unresolvable skill allows and records skill_unresolved",
            kind == "allow" and "skill_unresolved" in events,
            reason[:160],
        )

        judge_id = "a" + "9" * 16
        esc_id = "a" + "8" * 16
        arb.record_agent_result({
            "session_id": "sess-named",
            "cwd": str(world.project),
            "tool_use_id": "tu-named-judge",
            "tool_input": {"name": "judge-bob"},
            "tool_response": {
                "status": "completed",
                "agentId": judge_id,
                "agentType": "opus-judge",
                "resolvedModel": "claude-opus-5-5",
            },
        })
        arb.record_agent_result({
            "session_id": "sess-named",
            "cwd": str(world.project),
            "tool_use_id": "tu-named-esc",
            "tool_input": {"name": "esc-bob"},
            "tool_response": {
                "status": "completed",
                "agentId": esc_id,
                "agentType": "file-reader",
                "resolvedModel": "claude-sonnet-5-5",
            },
        })
        import sqlite3
        db = world.home / ".claude" / "logs" / "opus-arbitration" / "ledger.db"
        conn = sqlite3.connect(db)
        conn.execute("UPDATE agent_runs SET kind='escalated' WHERE name=?", ("esc-bob",))
        now = time.time()
        conn.execute(
            "INSERT INTO requests (request_id, session_id, state, created, updated) VALUES (?,?,?,?,?)",
            ("stuck-old", "sess-stuck", "JUDGE_RUNNING", now - 5000, now - 5000),
        )
        conn.execute(
            "INSERT INTO requests (request_id, session_id, state, created, updated) VALUES (?,?,?,?,?)",
            ("stuck-fresh", "sess-stuck", "JUDGE_RUNNING", now, now),
        )
        conn.commit()
        conn.close()
        arb.record_event("sweep-stuck", session_id="sess-stuck")
        stored = {row["request_id"]: row for row in rows(world, "requests")}
        suite.check(
            "H7",
            "stuck JUDGE_RUNNING fails closed",
            stored["stuck-old"]["state"] == "FAILED_CLOSED" and stored["stuck-old"]["decision"] == "judge_timeout"
            and stored["stuck-fresh"]["state"] == "JUDGE_RUNNING",
            "old=%s fresh=%s" % (stored["stuck-old"]["state"], stored["stuck-fresh"]["state"]),
        )
        named = [row for row in rows(world, "agent_runs") if row.get("name") == "judge-bob"]
        kind, reason = verdict(run_gate(world, send_payload(world, "judge-bob", session_id="sess-named")))
        esc_kind, esc_reason = verdict(run_gate(world, send_payload(world, "esc-bob", session_id="sess-named")))
        unknown_kind, unknown_reason = verdict(run_gate(world, send_payload(world, "nobody", session_id="sess-named")))
        other_kind, other_reason = verdict(run_gate(world, send_payload(world, "judge-bob", session_id="sess-other")))
        suite.check(
            "H9",
            "named judge and escalated agent deny; unknown name allows",
            len(named) == 1
            and kind == "deny" and "OPUS_RESUME_DENIED" in reason
            and esc_kind == "deny" and "OPUS_RESUME_DENIED" in esc_reason
            and unknown_kind == "allow"
            and other_kind == "allow",
            "judge=%s esc=%s unknown=%s other=%s" % (kind, esc_kind, unknown_kind, other_kind),
        )
    finally:
        world.close()

    fifo = World()
    try:
        switch = fifo.home / ".claude" / "opus-arbitration-mode"
        os.mkfifo(switch)
        payload = agent_payload(
            fifo, model="opus", transcript=fifo.opus_transcript,
            session_id="sess-fifo", tool_use_id="fifo-opus",
        )
        started = time.monotonic()
        kind, reason = verdict(run_gate(fifo, payload))
        elapsed = time.monotonic() - started
        suite.check(
            "H1",
            "fifo switch denies opus within 2s",
            elapsed < 2 and kind == "deny",
            "elapsed=%.2f kind=%s reason=%s" % (elapsed, kind, reason[:160]),
        )
    finally:
        fifo.close()

    locked = World()
    holder = None
    try:
        arb.record_event("lock-setup", session_id="sess-lock")
        db = locked.home / ".claude" / "logs" / "opus-arbitration" / "ledger.db"
        holder = subprocess.Popen(
            [
                sys.executable, "-c",
                "import sqlite3,sys,time\n"
                "conn=sqlite3.connect(sys.argv[1], isolation_level=None, timeout=60)\n"
                "conn.execute('BEGIN IMMEDIATE')\n"
                "print('locked', flush=True)\n"
                "time.sleep(30)\n",
                str(db),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        ready = holder.stdout.readline()
        payload = agent_payload(
            locked, model="opus", transcript=locked.sonnet_transcript,
            session_id="sess-lock-opus", tool_use_id="lock-opus",
        )
        started = time.monotonic()
        kind, reason = verdict(run_gate(locked, payload))
        elapsed = time.monotonic() - started
        suite.check(
            "H1",
            "locked db denies opus within 3s",
            ready.strip() == "locked" and elapsed < 3 and kind == "deny" and reason == "OPUS_GATE_BUSY",
            "elapsed=%.2f kind=%s reason=%s" % (elapsed, kind, reason[:160]),
        )
        sonnet = agent_payload(
            locked, model="sonnet", transcript=locked.sonnet_transcript,
            session_id="sess-lock-sonnet", tool_use_id="lock-sonnet",
        )
        started = time.monotonic()
        sonnet_kind, sonnet_reason = verdict(run_gate(locked, sonnet))
        sonnet_elapsed = time.monotonic() - started
        suite.check(
            "H1",
            "locked db still allows a sonnet call",
            sonnet_kind == "allow" and sonnet_elapsed < 1,
            "elapsed=%.2f kind=%s reason=%s" % (sonnet_elapsed, sonnet_kind, sonnet_reason[:120]),
        )
    finally:
        if holder is not None:
            holder.kill()
            holder.wait(timeout=5)
        locked.close()


def _front_family(world, name, body, transcript):
    path = world.home / ".claude" / "agents" / ("%s.md" % name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n%s\n---\n" % body, encoding="utf-8")
    payload = agent_payload(
        world,
        subagent_type=name,
        transcript=transcript,
        session_id="sess-" + name,
        tool_use_id="tu-" + name,
    )
    saved = os.environ.pop("CLAUDE_CODE_SUBAGENT_MODEL", None)
    try:
        family = _agent_model.resolve_for_gate(payload, payload["tool_input"]).get("family")
    finally:
        if saved is not None:
            os.environ["CLAUDE_CODE_SUBAGENT_MODEL"] = saved
    kind, reason = verdict(run_gate(world, payload))
    return family, kind, reason


def test_hardening2(suite):
    world = World()
    try:
        cases = (
            ("spaced-model", "model : opus", "opus"),
            ("quoted-key-model", '"model": opus', "opus"),
            ("case-key-model", "'Model': opus", "opus"),
            ("empty-dup-model", "model:\nmodel: haiku", "unknown"),
            ("indent-key-model", "name: indent-key-model\n  model: haiku", "unknown"),
        )
        for name, body, expect in cases:
            family, kind, reason = _front_family(world, name, body, world.sonnet_transcript)
            suite.check(
                "K1",
                "%s resolves to %s" % (name, expect),
                family == expect and kind == "deny",
                "family=%s kind=%s reason=%s" % (family, kind, reason[:160]),
            )
        user_judge = world.home / ".claude" / "agents" / "opus-judge.md"
        saved = user_judge.read_text(encoding="utf-8")
        user_judge.write_text("---\nmodel: opus\ntools : Read\n---\n", encoding="utf-8")
        kind, reason = verdict(run_gate(world, agent_payload(
            world, subagent_type="opus-judge", model="opus", transcript=world.opus_transcript,
            session_id="sess-tools-space", tool_use_id="judge-tools-space", description="Judge",
        )))
        suite.check(
            "K1",
            "judge tools Read with odd key spacing stays valid",
            kind == "deny" and reason == "JUDGE_NOT_PENDING",
            reason[:160],
        )
        user_judge.write_text("---\nmodel: opus\ntools : []\n---\n", encoding="utf-8")
        kind, reason = verdict(run_gate(world, agent_payload(
            world, subagent_type="opus-judge", model="opus", transcript=world.opus_transcript,
            session_id="sess-tools-empty", tool_use_id="judge-tools-empty", description="Judge",
        )))
        suite.check(
            "K1",
            "judge tools [] are invalid",
            kind == "deny" and reason == "JUDGE_DEFINITION_INVALID",
            reason[:160],
        )
        user_judge.write_text("---\nmodel: opus\ntools : [ ]\n---\n", encoding="utf-8")
        kind, reason = verdict(run_gate(world, agent_payload(
            world, subagent_type="opus-judge", model="opus", transcript=world.opus_transcript,
            session_id="sess-tools-gap", tool_use_id="judge-tools-gap", description="Judge",
        )))
        suite.check(
            "K1",
            "a spaced empty tools list is invalid",
            kind == "deny" and reason == "JUDGE_DEFINITION_INVALID",
            reason[:160],
        )
        user_judge.write_text(saved, encoding="utf-8")

        workflow_main = (
            "OPUS_WORKFLOW_DENIED; workflow agents would inherit the Opus main model. "
            "Run workflows from a sonnet/haiku main session, or run the steps as normal Agent calls (they will be arbitrated)."
        )
        workflow_field = (
            "OPUS_WORKFLOW_DENIED; name, resumeFromRunId, an unreadable scriptPath, "
            "or script text containing opus is denied"
        )
        plain = "echo hi\n"
        payload = _call(
            world, "Workflow", {"script": plain}, world.opus_transcript,
            "sess-wf-opus-plain", "wf-opus-plain",
        )
        kind, reason = verdict(run_gate(world, payload))
        suite.check(
            "K2",
            "opus main denies a workflow that never mentions an agent",
            kind == "deny" and reason == WORKFLOW_ENV,
            reason[:180],
        )
        payload = _call(
            world, "Workflow", {"script": plain}, world.sonnet_transcript,
            "sess-wf-sonnet-open", "wf-sonnet-open",
        )
        _plant_guard_attest(world, "sess-wf-sonnet-open", "wf-sonnet-open")
        kind, reason = verdict(run_gate(world, payload))
        suite.check("K2", "sonnet main allows a script that does not contain opus", kind == "allow", reason[:180])
        named = _call(
            world, "Workflow", {"script": plain, "name": "weekly"}, world.sonnet_transcript,
            "sess-wf-named-safe", "wf-named-safe",
        )
        kind, reason = verdict(run_gate(world, named))
        suite.check(
            "K2",
            "benign script plus name denies",
            kind == "deny" and reason == workflow_field,
            reason[:180],
        )
        resumed = _call(
            world, "Workflow", {"script": plain, "resumeFromRunId": "run-1"}, world.sonnet_transcript,
            "sess-wf-resume", "wf-resume",
        )
        kind, reason = verdict(run_gate(world, resumed))
        suite.check(
            "K2",
            "resumeFromRunId denies",
            kind == "deny" and reason == workflow_field,
            reason[:180],
        )
        missing = world.home / ".claude" / "missing-workflow.js"
        both = _call(
            world, "Workflow", {"script": plain, "scriptPath": str(missing)}, world.sonnet_transcript,
            "sess-wf-missing", "wf-missing",
        )
        kind, reason = verdict(run_gate(world, both))
        suite.check(
            "K2",
            "unreadable scriptPath denies",
            kind == "deny" and reason == workflow_field,
            reason[:180],
        )

        _write_skill(world, "quoted-fork", "'context': fork\nmodel: haiku\n")
        _write_skill(world, "bad-fork", "name: bad-fork\ncontext: fork\n  model: haiku\n")
        _write_skill(world, "bad-inline", "name: bad-inline\nmodel: |\n  haiku\n")
        kind, reason = verdict(run_gate(world, _call(
            world, "Skill", {"skill": "quoted-fork"}, world.opus_transcript, "sess-quoted-fork", "skill-quoted",
        )))
        suite.check("K5", "quoted context fork with haiku allows", kind == "allow", reason[:160])
        kind, reason = verdict(run_gate(world, _call(
            world, "Skill", {"skill": "bad-fork"}, world.sonnet_transcript, "sess-bad-fork", "skill-bad-fork",
        )))
        suite.check(
            "K5",
            "malformed fork frontmatter denies",
            kind == "deny" and reason == "OPUS_SKILL_FORK_DENIED",
            reason[:160],
        )
        bad_inline_payload = _call(
            world, "Skill", {"skill": "bad-inline"}, world.opus_transcript, "sess-bad-inline", "skill-bad-inline",
        )
        kind, reason = verdict(run_gate(world, bad_inline_payload))
        bad_inline_found = _agent_model.resolve_skill_for_gate(
            bad_inline_payload, bad_inline_payload["tool_input"],
        )
        events = (world.home / ".claude" / "logs" / "opus-arbitration" / "events.jsonl").read_text(encoding="utf-8")
        suite.check(
            "K5",
            "malformed skill with no context key is inline",
            kind == "allow"
            and bad_inline_found.get("status") == "inline"
            and "bad-inline" not in events,
            "kind=%s found=%s reason=%s" % (kind, bad_inline_found, reason[:80]),
        )

        for label, value in (("empty", ""), ("blank", "   "), ("number", 3), ("null", None)):
            payload = agent_payload(
                world, subagent_type="Explore", model="haiku", transcript=world.sonnet_transcript,
                session_id="sess-type-" + label, tool_use_id="type-" + label,
            )
            payload["tool_input"]["subagent_type"] = value
            saved = os.environ.pop("CLAUDE_CODE_SUBAGENT_MODEL", None)
            try:
                family = _agent_model.resolve_for_gate(payload, payload["tool_input"]).get("family")
            finally:
                if saved is not None:
                    os.environ["CLAUDE_CODE_SUBAGENT_MODEL"] = saved
            kind, _reason = verdict(run_gate(world, payload))
            suite.check(
                "K7",
                "subagent_type %s is unknown" % label,
                family == "unknown" and kind == "deny",
                "family=%s kind=%s" % (family, kind),
            )

        import sqlite3
        arb.record_event("purge-seed", session_id="sess-purge")
        db = world.home / ".claude" / "logs" / "opus-arbitration" / "ledger.db"
        now = time.time()
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM meta WHERE key='last_purge'")
        conn.execute(
            "INSERT INTO requests (request_id, session_id, state, created, updated) VALUES (?,?,?,?,?)",
            ("old-1", "sess-purge", "EXPIRED", now - 8 * 86400, now - 8 * 86400),
        )
        conn.execute(
            "INSERT INTO requests (request_id, session_id, state, created, updated) VALUES (?,?,?,?,?)",
            ("keep-1", "sess-purge", "EXPIRED", now, now),
        )
        conn.commit()
        conn.close()
        arb.record_event("purge-run", session_id="sess-purge")
        first = {row["request_id"] for row in rows(world, "requests")}
        meta = {row["key"]: row["value"] for row in rows(world, "meta")}
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO requests (request_id, session_id, state, created, updated) VALUES (?,?,?,?,?)",
            ("old-2", "sess-purge", "EXPIRED", now - 8 * 86400, now - 8 * 86400),
        )
        conn.commit()
        conn.close()
        arb.record_event("purge-skip", session_id="sess-purge")
        second = {row["request_id"] for row in rows(world, "requests")}
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE meta SET value=? WHERE key='last_purge'",
            (str(time.time() - 3601),),
        )
        conn.commit()
        conn.close()
        arb.record_event("purge-again", session_id="sess-purge")
        third = {row["request_id"] for row in rows(world, "requests")}
        suite.check(
            "K8",
            "retention purge runs at most once an hour",
            "old-1" not in first and "keep-1" in first and "last_purge" in meta
            and "old-2" in second and "old-2" not in third and "keep-1" in third,
            "first=%s second=%s third=%s" % (sorted(first), sorted(second), sorted(third)),
        )

        bad_opens = []
        for path in (
            ROOT / "opus-arbitration-gate.py",
            ROOT / "opus-arbitration-post.py",
            ROOT / "_opus_arbitration.py",
            ROOT / "_opus_triggers.py",
            ROOT / "_agent_model.py",
        ):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                code = line.split("#", 1)[0]
                if "os.open(" in code and "O_NONBLOCK" not in code:
                    bad_opens.append("%s:%s" % (path.name, number))
        suite.check("K4", "every os.open uses O_NONBLOCK", not bad_opens, ", ".join(bad_opens))
    finally:
        world.close()


def test_strict_rules(suite):
    world = World()
    try:
        workflow_main = (
            "OPUS_WORKFLOW_DENIED; workflow agents would inherit the Opus main model. "
            "Run workflows from a sonnet/haiku main session, or run the steps as normal Agent calls (they will be arbitrated)."
        )
        workflow_field = (
            "OPUS_WORKFLOW_DENIED; name, resumeFromRunId, an unreadable scriptPath, "
            "or script text containing opus is denied"
        )
        haiku = world.home / ".claude" / "projects" / "demo" / "haiku-main.jsonl"
        haiku.parent.mkdir(parents=True, exist_ok=True)
        haiku.write_text(
            json.dumps({"type": "assistant", "message": {"model": "claude-haiku-4-5"}}) + "\n",
            encoding="utf-8",
        )
        _plant_guard_attest(world, "sess-wf-haiku", "wf-haiku")
        kind, reason = verdict(run_gate(world, _call(
            world, "Workflow", {"script": "echo hi\n"}, haiku, "sess-wf-haiku", "wf-haiku",
        )))
        suite.check("S1", "haiku main allows a script without opus", kind == "allow", reason[:160])
        kind, reason = verdict(run_gate(world, _call(
            world, "Workflow", {"script": "echo hi\n"}, world.home / "missing-transcript.jsonl",
            "sess-wf-unknown", "wf-unknown",
        )))
        suite.check(
            "S1",
            "unknown main denies every workflow",
            kind == "deny" and reason == WORKFLOW_ENV,
            reason[:200],
        )
        kind, reason = verdict(run_gate(world, _call(
            world, "Workflow", {"script": "print('Opus')\n"}, world.sonnet_transcript,
            "sess-wf-opus-word", "wf-opus-word",
        )))
        suite.check(
            "S1",
            "sonnet main denies script text containing opus",
            kind == "deny" and reason == workflow_field,
            reason[:200],
        )
        safe_path = world.home / ".claude" / "safe-workflow.js"
        safe_path.write_text("echo hi\n", encoding="utf-8")
        _plant_guard_attest(world, "sess-wf-path-ok", "wf-path-ok")
        kind, reason = verdict(run_gate(world, _call(
            world, "Workflow", {"scriptPath": str(safe_path)}, world.sonnet_transcript,
            "sess-wf-path-ok", "wf-path-ok",
        )))
        suite.check("S1", "readable scriptPath without opus allows under sonnet", kind == "allow", reason[:160])

        bom = world.home / ".claude" / "agents" / "bom-agent.md"
        bom.write_bytes(b"\xef\xbb\xbf\n\n--- \nmodel: \"opus\" # pinned\n---\n")
        bom_payload = agent_payload(
            world, subagent_type="bom-agent", transcript=world.sonnet_transcript,
            session_id="sess-bom-agent", tool_use_id="bom-agent",
        )
        family = _agent_model.resolve_for_gate(bom_payload, bom_payload["tool_input"]).get("family")
        kind, _reason = verdict(run_gate(world, bom_payload))
        suite.check(
            "S2",
            "BOM, blank lines, and a commented quoted model resolve to opus",
            family == "opus" and kind == "deny",
            "family=%s kind=%s" % (family, kind),
        )
        family, kind, reason = _front_family(world, "comment-haiku", "model: haiku # fast", world.opus_transcript)
        suite.check(
            "S2",
            "comment after a model token is haiku",
            family == "haiku" and kind == "allow",
            "family=%s kind=%s %s" % (family, kind, reason[:80]),
        )
        family, kind, reason = _front_family(world, "extra-haiku", "model: haiku extra", world.sonnet_transcript)
        suite.check(
            "S2",
            "trailing model text is unknown",
            family == "unknown" and kind == "deny",
            "family=%s kind=%s %s" % (family, kind, reason[:80]),
        )
        user_judge = world.home / ".claude" / "agents" / "opus-judge.md"
        saved_judge = user_judge.read_text(encoding="utf-8")
        user_judge.write_text("---\nmodel: opus # pinned\ntools: Read # only\n---\n", encoding="utf-8")
        kind, reason = verdict(run_gate(world, agent_payload(
            world, subagent_type="opus-judge", model="opus", transcript=world.opus_transcript,
            session_id="sess-judge-comment", tool_use_id="judge-comment", description="Judge",
        )))
        suite.check(
            "S2",
            "commented judge frontmatter stays valid",
            kind == "deny" and reason == "JUDGE_NOT_PENDING",
            reason[:160],
        )
        user_judge.write_text(saved_judge, encoding="utf-8")

        _write_skill(world, "comment-fork", "context: fork # isolated\nmodel: opus\n")
        kind, reason = verdict(run_gate(world, _call(
            world, "Skill", {"skill": "comment-fork"}, world.sonnet_transcript,
            "sess-comment-fork", "skill-comment-fork",
        )))
        suite.check(
            "S3",
            "context fork comment with model opus denies",
            kind == "deny" and reason == "OPUS_SKILL_FORK_DENIED",
            reason,
        )
        bom_skill = world.home / ".claude" / "skills" / "bom-fork" / "SKILL.md"
        bom_skill.parent.mkdir(parents=True, exist_ok=True)
        bom_skill.write_bytes(b"\xef\xbb\xbf\n\n---\ncontext: fork\n---\n")
        kind, reason = verdict(run_gate(world, _call(
            world, "Skill", {"skill": "bom-fork"}, world.opus_transcript, "sess-bom-fork", "skill-bom-fork",
        )))
        suite.check(
            "S3",
            "BOM fork with no model under opus main denies",
            kind == "deny" and reason == "OPUS_SKILL_FORK_DENIED",
            reason,
        )
        target = world.home / ".claude" / "skill-targets" / "inline.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "---\nname: link-inline\ncontext: inline\ndescription: mentions a fork of the old steps\n---\n",
            encoding="utf-8",
        )
        link = world.home / ".claude" / "skills" / "link-inline"
        link.mkdir(parents=True, exist_ok=True)
        (link / "SKILL.md").symlink_to(target)
        kind, reason = verdict(run_gate(world, _call(
            world, "Skill", {"skill": "link-inline"}, world.opus_transcript,
            "sess-link-inline", "skill-link-inline",
        )))
        suite.check("S3", "symlinked inline skill allows", kind == "allow", reason[:160])
        _write_skill(world, "upper-fork", "context: FORK\nmodel: haiku\n")
        upper_payload = _call(
            world, "Skill", {"skill": "upper-fork"}, world.opus_transcript, "sess-upper-fork", "skill-upper-fork",
        )
        found = _agent_model.resolve_skill_for_gate(upper_payload, upper_payload["tool_input"])
        kind, reason = verdict(run_gate(world, upper_payload))
        suite.check(
            "S3",
            "context FORK is a fork and uses its model",
            kind == "allow" and found.get("status") == "fork" and found.get("family") == "haiku",
            "kind=%s found=%s reason=%s" % (kind, found, reason[:80]),
        )
        _write_skill(world, "desc-fork", "name: desc-fork\ndescription: this is a fork of the old skill\n")
        desc_payload = _call(
            world, "Skill", {"skill": "desc-fork"}, world.opus_transcript, "sess-desc-fork", "skill-desc-fork",
        )
        desc_found = _agent_model.resolve_skill_for_gate(desc_payload, desc_payload["tool_input"])
        kind, reason = verdict(run_gate(world, desc_payload))
        suite.check(
            "S3",
            "description that says fork allows under opus without a context key",
            kind == "allow" and desc_found.get("status") == "inline",
            "kind=%s found=%s reason=%s" % (kind, desc_found, reason[:80]),
        )
        _write_skill(
            world,
            "desc-quoted-fork",
            'name: desc-quoted-fork\ndescription: "a separate skill, a `context: fork` skill, or inline"\n',
        )
        quoted_payload = _call(
            world, "Skill", {"skill": "desc-quoted-fork"}, world.opus_transcript,
            "sess-desc-quoted", "skill-desc-quoted",
        )
        quoted_found = _agent_model.resolve_skill_for_gate(quoted_payload, quoted_payload["tool_input"])
        kind, reason = verdict(run_gate(world, quoted_payload))
        suite.check(
            "S3",
            "description line quoting context fork allows",
            kind == "allow" and quoted_found.get("status") == "inline",
            "kind=%s found=%s reason=%s" % (kind, quoted_found, reason[:80]),
        )
        _write_skill(world, "fork-sonnet", "name: fork-sonnet\ncontext: fork\nmodel: sonnet\n")
        sonnet_payload = _call(
            world, "Skill", {"skill": "fork-sonnet"}, world.opus_transcript,
            "sess-fork-sonnet", "skill-fork-sonnet",
        )
        sonnet_found = _agent_model.resolve_skill_for_gate(sonnet_payload, sonnet_payload["tool_input"])
        kind, reason = verdict(run_gate(world, sonnet_payload))
        suite.check(
            "S3",
            "context fork with model sonnet allows",
            kind == "allow" and sonnet_found.get("status") == "fork" and sonnet_found.get("family") == "sonnet",
            "kind=%s found=%s reason=%s" % (kind, sonnet_found, reason[:80]),
        )
        _write_skill(world, "quoted-upper-fork", 'Context : "FORK" # x\n')
        upper_quoted = _call(
            world, "Skill", {"skill": "quoted-upper-fork"}, world.opus_transcript,
            "sess-quoted-upper", "skill-quoted-upper",
        )
        kind, reason = verdict(run_gate(world, upper_quoted))
        suite.check(
            "S3",
            "quoted Context FORK with no model under opus main denies",
            kind == "deny" and reason == "OPUS_SKILL_FORK_DENIED",
            reason[:160],
        )
        _write_skill(world, "plain-inline", "name: plain-inline\ncontext: inline\n")
        plain_payload = _call(
            world, "Skill", {"skill": "plain-inline"}, world.opus_transcript,
            "sess-plain-inline", "skill-plain-inline",
        )
        plain_found = _agent_model.resolve_skill_for_gate(plain_payload, plain_payload["tool_input"])
        kind, reason = verdict(run_gate(world, plain_payload))
        suite.check(
            "S3",
            "context inline allows",
            kind == "allow" and plain_found.get("status") == "inline",
            "kind=%s found=%s reason=%s" % (kind, plain_found, reason[:80]),
        )
        _write_skill(world, "desc-inline", "name: desc-inline\ncontext: inline\ndescription: this is a fork of the old skill\n")
        kind, reason = verdict(run_gate(world, _call(
            world, "Skill", {"skill": "desc-inline"}, world.opus_transcript, "sess-desc-inline", "skill-desc-inline",
        )))
        suite.check("S3", "context inline allows a description that says fork", kind == "allow", reason[:160])
        _write_skill(world, "side-context", "context: side\nmodel: haiku\n")
        kind, reason = verdict(run_gate(world, _call(
            world, "Skill", {"skill": "side-context"}, world.sonnet_transcript, "sess-side", "skill-side",
        )))
        suite.check(
            "S3",
            "context other than fork or inline denies",
            kind == "deny" and reason == "OPUS_SKILL_FORK_DENIED",
            reason[:160],
        )
        huge = world.home / ".claude" / "skills" / "huge-skill" / "SKILL.md"
        huge.parent.mkdir(parents=True, exist_ok=True)
        huge.write_bytes(b"---\ncontext: inline\n---\n" + (b"x" * (256 * 1024)))
        kind, reason = verdict(run_gate(world, _call(
            world, "Skill", {"skill": "huge-skill"}, world.opus_transcript, "sess-huge-opus", "skill-huge-opus",
        )))
        suite.check("S3", "unreadable skill under opus denies", kind == "deny" and reason == "OPUS_SKILL_FORK_DENIED", reason)
        kind, reason = verdict(run_gate(world, _call(
            world, "Skill", {"skill": "huge-skill"}, world.sonnet_transcript, "sess-huge-sonnet", "skill-huge-sonnet",
        )))
        events = (world.home / ".claude" / "logs" / "opus-arbitration" / "events.jsonl").read_text(encoding="utf-8")
        suite.check(
            "S3",
            "unreadable skill under sonnet allows and logs skill_unresolved",
            kind == "allow" and "skill_unresolved" in events and "huge-skill" in events,
            reason[:160],
        )
        loose = world.home / ".claude" / "skills" / "nofm-fork" / "SKILL.md"
        loose.parent.mkdir(parents=True, exist_ok=True)
        loose.write_text("This helper will fork the work.\ncontext: fork\n", encoding="utf-8")
        nofm_payload = _call(
            world, "Skill", {"skill": "nofm-fork"}, world.opus_transcript, "sess-nofm", "skill-nofm",
        )
        nofm_found = _agent_model.resolve_skill_for_gate(nofm_payload, nofm_payload["tool_input"])
        kind, reason = verdict(run_gate(world, nofm_payload))
        suite.check(
            "S3",
            "no frontmatter is inline even when the body says context fork",
            kind == "allow" and nofm_found.get("status") == "inline",
            "kind=%s found=%s reason=%s" % (kind, nofm_found, reason[:80]),
        )

        named_id = "a" + "b" * 16
        arb.record_agent_result({
            "session_id": "sess-case",
            "cwd": str(world.project),
            "tool_use_id": "tu-case-name",
            "tool_input": {"name": "AlphaJudge"},
            "tool_response": {
                "status": "completed",
                "agentId": named_id,
                "agentType": "opus-judge",
                "resolvedModel": "claude-opus-5-5",
            },
        })
        stored = [row for row in rows(world, "agent_runs") if row.get("tool_use_id") == "tu-case-name"]
        kind, reason = verdict(run_gate(world, send_payload(world, "ALPHAJUDGE", session_id="sess-case")))
        suite.check(
            "S4",
            "agent name is stored and matched case-insensitively",
            len(stored) == 1
            and stored[0].get("name") == "alphajudge"
            and kind == "deny"
            and "OPUS_RESUME_DENIED" in reason,
            "name=%s kind=%s reason=%s" % (stored and stored[0].get("name"), kind, reason[:120]),
        )
        nested_id = "a" + "c" * 16
        arb.record_agent_result({
            "session_id": "sess-nested",
            "cwd": str(world.project),
            "tool_use_id": "tu-nested",
            "tool_response": {
                "status": "completed",
                "agentId": nested_id,
                "agentType": "general-purpose",
                "resolvedModel": "claude-opus-5-5",
            },
        })
        nested = send_payload(world, "main", session_id="sess-nested")
        nested["tool_input"] = {"to": "main", "message": {"parts": ["hello", {"id": nested_id}]}}
        kind, reason = verdict(run_gate(world, nested))
        suite.check("S4", "nested agent id denies", kind == "deny" and "OPUS_RESUME_DENIED" in reason, reason[:160])

        denied = run_gate(world, agent_payload(
            world, model="opus", transcript=world.opus_transcript,
            session_id="sess-pre", tool_use_id="pre-opus", description="needs a judge", prompt="do the hard part",
        ))
        deny_kind, deny_reason = verdict(denied)
        request_id, packet = pending_from_deny(deny_reason)
        judge = judge_payload(
            world, "ARBITRATION-PACKET request_id=%s" % request_id,
            "sess-pre", "judge-pre-ref", wrapped=False,
        )
        judge["tool_input"]["name"] = "CaseJudge"
        launched = run_gate(world, judge)
        hook = hook_body(launched)
        updated = hook.get("updatedInput") if isinstance(hook, dict) else None
        runs = [row for row in rows(world, "agent_runs") if row.get("tool_use_id") == "judge-pre-ref"]
        send_kind, send_reason = verdict(run_gate(world, send_payload(world, "CaseJudge", session_id="sess-pre")))
        id_kind, id_reason = verdict(run_gate(world, send_payload(world, "judge-pre-ref", session_id="sess-pre")))
        suite.check(
            "S5",
            "allowed judge with no post hook blocks its name and tool use id",
            deny_kind == "deny" and bool(request_id) and bool(packet)
            and launched.returncode == 0
            and isinstance(hook, dict)
            and "permissionDecision" not in hook
            and isinstance(updated, dict)
            and updated.get("prompt") == packet
            and len(runs) == 1
            and runs[0]["kind"] == "judge"
            and runs[0]["status"] == "launched"
            and runs[0]["name"] == "casejudge"
            and runs[0]["agent_id"] == "launched:judge-pre-ref"
            and send_kind == "deny" and "OPUS_RESUME_DENIED" in send_reason
            and id_kind == "deny" and "OPUS_RESUME_DENIED" in id_reason,
            "hook=%s runs=%s send=%s %s id=%s %s" % (
                hook, runs, send_kind, send_reason[:60], id_kind, id_reason[:60],
            ),
        )
        filled_id = "a" + "d" * 16
        arb.record_agent_result({
            "session_id": "sess-pre",
            "cwd": str(world.project),
            "tool_use_id": "judge-pre-ref",
            "tool_input": {"name": "CaseJudge", "subagent_type": "opus-judge"},
            "tool_response": {
                "status": "completed",
                "agentId": filled_id,
                "agentType": "opus-judge",
                "resolvedModel": "claude-opus-5-5",
            },
        })
        filled = [row for row in rows(world, "agent_runs") if row.get("tool_use_id") == "judge-pre-ref"]
        suite.check(
            "S5",
            "post hook fills agent id and final status on the launched row",
            len(filled) == 1
            and filled[0]["agent_id"] == filled_id
            and filled[0]["status"] == "completed"
            and filled[0]["resolved_model"] == "claude-opus-5-5"
            and filled[0]["kind"] == "judge"
            and filled[0]["name"] == "casejudge",
            str(filled)[:400],
        )

        denied = run_gate(world, agent_payload(
            world, model="opus", transcript=world.opus_transcript,
            session_id="sess-exact", tool_use_id="exact-opus", description="exact judge", prompt="exact body",
        ))
        _exact_kind, exact_reason = verdict(denied)
        exact_id, exact_packet = pending_from_deny(exact_reason)
        exact = judge_payload(world, exact_packet, "sess-exact", "judge-exact", wrapped=False)
        exact["tool_input"]["name"] = "ExactJudge"
        exact_launch, exact_launch_reason = verdict(run_gate(world, exact))
        exact_runs = [row for row in rows(world, "agent_runs") if row.get("tool_use_id") == "judge-exact"]
        suite.check(
            "S5",
            "exact judge packet is pre-recorded",
            bool(exact_id) and exact_launch == "allow"
            and len(exact_runs) == 1
            and exact_runs[0]["status"] == "launched"
            and exact_runs[0]["kind"] == "judge"
            and exact_runs[0]["name"] == "exactjudge",
            "launch=%s %s runs=%s" % (exact_launch, exact_launch_reason[:80], exact_runs),
        )

        worker = agent_payload(
            world, model="opus", transcript=world.sonnet_transcript,
            session_id="sess-worker-pre", tool_use_id="worker-pre-deny",
            description="approved worker", prompt="do the granted work",
        )
        worker["tool_input"]["name"] = "WorkerBob"
        _worker_payload, _worker_request, worker_result = escalate(world, worker)
        consume_kind, consume_reason = verdict(run_gate(world, {**worker, "tool_use_id": "worker-pre-use"}))
        worker_runs = [row for row in rows(world, "agent_runs") if row.get("tool_use_id") == "worker-pre-use"]
        worker_send, worker_send_reason = verdict(run_gate(
            world, send_payload(world, "workerbob", session_id="sess-worker-pre"),
        ))
        suite.check(
            "S5",
            "consumed grant is pre-recorded and its name denies before post",
            worker_result.get("decision") == "ESCALATE"
            and consume_kind == "allow"
            and len(worker_runs) == 1
            and worker_runs[0]["kind"] == "escalated"
            and worker_runs[0]["status"] == "launched"
            and worker_runs[0]["name"] == "workerbob"
            and worker_send == "deny" and "OPUS_RESUME_DENIED" in worker_send_reason,
            "consume=%s %s runs=%s send=%s %s" % (
                consume_kind, consume_reason[:80], worker_runs, worker_send, worker_send_reason[:60],
            ),
        )
    finally:
        world.close()


def _policy_mode(world, mode):
    path = world.home / ".claude" / "hooks" / "data" / "opus-arbitration-policy.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["workflow"]["opus_main_mode"] = mode
    path.write_text(json.dumps(data), encoding="utf-8")


def _workflow_call(world, script, transcript, session_id, tool_use_id, **fields):
    payload = _call(world, "Workflow", {"script": script}, transcript, session_id, tool_use_id)
    payload.update(fields)
    return payload


def _plant_guard_attest(world, session_id, tool_use_id):
    directory = world.home / ".claude" / "logs" / "opus-arbitration" / "guard"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("attest-%s-%s" % (session_id, tool_use_id))
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)


def _plant_workflow_runtime(world, session_id, tool_use_id):
    """Guard attestation, mod heartbeat, and mod call attestation for one Workflow call."""
    _plant_guard_attest(world, session_id, tool_use_id)
    policy = world.home / ".claude" / "hooks" / "data" / "opus-arbitration-policy.json"
    digest = hashlib.sha256(policy.read_bytes()).hexdigest()
    directory = world.home / ".claude" / "logs" / "opus-arbitration" / "mod"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ("%s.json" % session_id)).write_text(json.dumps({
        "session_id": session_id,
        "mod_version": "1.0.0",
        "policy_sha256": digest,
        "started_at": "2026-11-05T00:00:00Z",
        "pid_marker": "test",
    }), encoding="utf-8")
    (directory / ("attest-%s-%s.json" % (session_id, tool_use_id))).write_text(
        json.dumps({
            "session_id": session_id,
            "tool_use_id": tool_use_id,
            "mod_version": "1.0.0",
            "policy_sha256": digest,
            "ts": "2026-11-05T00:00:00Z",
        }),
        encoding="utf-8",
    )


def test_workflow_and_auto(suite):
    world = World()
    try:
        pinned = {
            "CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL": "sonnet",
            "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS": "4",
        }
        fixture = json.loads(
            (FIXTURES / "workflow" / "20261105T043808.278093Z-PreToolUse-Workflow.json").read_text(encoding="utf-8")
        )
        fixture["cwd"] = str(world.project)
        fixture["transcript_path"] = str(world.opus_transcript)
        fixture["session_id"] = "sess-wf-pin"
        fixture["tool_use_id"] = "wf-pin-1"
        _plant_workflow_runtime(world, "sess-wf-pin", "wf-pin-1")
        kind, reason = verdict(run_gate(world, fixture, pinned))
        recorded = [row for row in rows(world, "workflows") if row.get("tool_use_id") == "wf-pin-1"]
        suite.check(
            "WF",
            "opus main with pinned env allows and records the workflow",
            kind == "allow" and len(recorded) == 1
            and recorded[0]["status"] == "launched"
            and recorded[0]["expected_family"] == "sonnet"
            and recorded[0]["prompt_id"] == fixture["prompt_id"],
            "kind=%s reason=%s rows=%s" % (kind, reason[:180], recorded),
        )
        posted = subprocess.run(
            [sys.executable, str(ROOT / "opus-arbitration-post.py")],
            input=json.dumps({
                "hook_event_name": "PostToolUse",
                "tool_name": "Workflow",
                "session_id": "sess-wf-pin",
                "cwd": str(world.project),
                "tool_use_id": "wf-pin-1",
                "tool_response": {
                    "status": "async_launched",
                    "taskId": "wv0rif4ua",
                    "runId": "wf_e05f19e2-4af",
                    "workflowName": "wf-model-probe",
                },
            }),
            text=True,
            capture_output=True,
            env=world.env(),
            timeout=30,
            check=False,
        )
        filled = [row for row in rows(world, "workflows") if row.get("tool_use_id") == "wf-pin-1"]
        suite.check(
            "WF",
            "workflow post fills runId taskId and name",
            posted.returncode == 0 and posted.stdout.strip() == ""
            and filled and filled[0]["run_id"] == "wf_e05f19e2-4af"
            and filled[0]["task_id"] == "wv0rif4ua"
            and filled[0]["workflow_name"] == "wf-model-probe",
            "stdout=%r row=%s" % (posted.stdout[:160], filled),
        )
        plain = "echo hi\n"
        cases = (
            ("missing env", {}, "reason=env"),
            ("env opus", {"CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL": "opus", "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS": "4"}, "reason=env"),
            ("env best", {"CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL": "best", "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS": "4"}, "reason=env"),
            ("concurrency missing", {"CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL": "claude-haiku-5-5"}, "reason=concurrency"),
            ("concurrency 8", {"CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL": "sonnet", "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS": "8"}, "reason=concurrency"),
        )
        for name, extra, token in cases:
            payload = _workflow_call(world, plain, world.opus_transcript, "sess-" + name.replace(" ", "-"), "tu-" + name.replace(" ", "-"))
            kind, reason = verdict(run_gate(world, payload, extra))
            suite.check(
                "WF",
                "%s denies" % name,
                kind == "deny" and token in reason and reason.startswith("OPUS_WORKFLOW_DENIED"),
                reason[:220],
            )
        env_reason = verdict(run_gate(
            world,
            _workflow_call(world, plain, world.opus_transcript, "sess-env-lines", "tu-env-lines"),
        ))[1]
        suite.check(
            "WF",
            "env denial names the required lines",
            "CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL=sonnet" in env_reason
            and "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS=4" in env_reason,
            env_reason[:220],
        )
        for index in range(3):
            _plant_workflow_runtime(world, "sess-cap", "tu-cap-%s" % index)
            kind, reason = verdict(run_gate(
                world,
                _workflow_call(world, plain, world.opus_transcript, "sess-cap", "tu-cap-%s" % index),
                pinned,
            ))
            if kind != "allow":
                break
        else:
            _plant_workflow_runtime(world, "sess-cap", "tu-cap-3")
            kind, reason = verdict(run_gate(
                world,
                _workflow_call(world, plain, world.opus_transcript, "sess-cap", "tu-cap-3"),
                pinned,
            ))
        suite.check(
            "WF",
            "fourth workflow in a session denies",
            kind == "deny" and "reason=session" in reason,
            reason[:220],
        )
        many = "\n".join("agent (%s)" % index for index in range(13))
        kind, reason = verdict(run_gate(
            world,
            _workflow_call(world, many, world.opus_transcript, "sess-agents", "tu-agents"),
            pinned,
        ))
        suite.check(
            "WF",
            "thirteen agent() calls deny",
            kind == "deny" and "reason=agents" in reason,
            reason[:220],
        )
        missing_guard = _workflow_call(
            world, plain, world.opus_transcript, "sess-no-guard", "tu-no-guard",
        )
        kind, reason = verdict(run_gate(world, missing_guard, pinned))
        suite.check(
            "WF",
            "missing guard attestation denies",
            kind == "deny" and reason == "WORKFLOW_GUARD_NOT_ACTIVE",
            reason[:200],
        )
        _plant_guard_attest(world, "sess-no-heart", "tu-no-heart")
        kind, reason = verdict(run_gate(
            world,
            _workflow_call(world, plain, world.opus_transcript, "sess-no-heart", "tu-no-heart"),
            pinned,
        ))
        suite.check(
            "WF",
            "missing mod heartbeat denies",
            kind == "deny" and reason == "WORKFLOW_SPAWN_MOD_NOT_ACTIVE",
            reason[:200],
        )
        _plant_workflow_runtime(world, "sess-stale-mod", "tu-stale-mod")
        heartbeat = world.home / ".claude" / "logs" / "opus-arbitration" / "mod" / "sess-stale-mod.json"
        stale = json.loads(heartbeat.read_text(encoding="utf-8"))
        stale["mod_version"] = "0.9.0"
        heartbeat.write_text(json.dumps(stale), encoding="utf-8")
        kind, reason = verdict(run_gate(
            world,
            _workflow_call(world, plain, world.opus_transcript, "sess-stale-mod", "tu-stale-mod"),
            pinned,
        ))
        suite.check(
            "WF",
            "stale mod heartbeat denies",
            kind == "deny" and reason == "WORKFLOW_SPAWN_MOD_NOT_ACTIVE",
            reason[:200],
        )
        _plant_workflow_runtime(world, "sess-hash-mod", "tu-hash-mod")
        hashed = world.home / ".claude" / "logs" / "opus-arbitration" / "mod" / "sess-hash-mod.json"
        mismatched = json.loads(hashed.read_text(encoding="utf-8"))
        mismatched["policy_sha256"] = "0" * 64
        hashed.write_text(json.dumps(mismatched), encoding="utf-8")
        kind, reason = verdict(run_gate(
            world,
            _workflow_call(world, plain, world.opus_transcript, "sess-hash-mod", "tu-hash-mod"),
            pinned,
        ))
        suite.check(
            "WF",
            "policy hash mismatch denies",
            kind == "deny" and reason == "WORKFLOW_SPAWN_MOD_NOT_ACTIVE",
            reason[:200],
        )
        _plant_workflow_runtime(world, "sess-no-call", "tu-no-call")
        call_file = world.home / ".claude" / "logs" / "opus-arbitration" / "mod" / "attest-sess-no-call-tu-no-call.json"
        call_file.unlink()
        kind, reason = verdict(run_gate(
            world,
            _workflow_call(world, plain, world.opus_transcript, "sess-no-call", "tu-no-call"),
            pinned,
        ))
        suite.check(
            "WF",
            "missing mod call attestation denies",
            kind == "deny" and reason == "WORKFLOW_SPAWN_MOD_NOT_ACTIVE call",
            reason[:200],
        )
        _plant_guard_attest(world, "sess-sonnet-noguard", "tu-sonnet-kept")
        (world.home / ".claude" / "logs" / "opus-arbitration" / "guard" / "attest-sess-sonnet-noguard-tu-sonnet-kept").unlink()
        kind, reason = verdict(run_gate(
            world,
            _workflow_call(world, plain, world.sonnet_transcript, "sess-sonnet-noguard", "tu-sonnet-kept"),
        ))
        suite.check(
            "WF",
            "sonnet main without guard attestation denies",
            kind == "deny" and reason == "WORKFLOW_GUARD_NOT_ACTIVE",
            reason[:200],
        )

        _policy_mode(world, "deny")
        kind, reason = verdict(run_gate(
            world,
            _workflow_call(world, plain, world.opus_transcript, "sess-mode-deny", "tu-mode-deny"),
            pinned,
        ))
        suite.check(
            "WF",
            "deny mode keeps the opus-main text",
            kind == "deny" and reason == WORKFLOW_MAIN,
            reason[:220],
        )
        _policy_mode(world, "pinned_env")
        for label, tool_input in (
            ("name", {"script": plain, "name": "weekly"}),
            ("resume", {"script": plain, "resumeFromRunId": "run-1"}),
        ):
            payload = _call(
                world, "Workflow", tool_input, world.opus_transcript,
                "sess-struct-" + label, "tu-struct-" + label,
            )
            kind, reason = verdict(run_gate(world, payload, pinned))
            suite.check(
                "WF",
                "%s denies before the pinned env" % label,
                kind == "deny" and "name, resumeFromRunId" in reason,
                reason[:180],
            )
        kind, reason = verdict(run_gate(
            world,
            _workflow_call(world, "print('opus')\n", world.opus_transcript, "sess-opus-word", "tu-opus-word"),
            pinned,
        ))
        suite.check(
            "WF",
            "script text containing opus denies",
            kind == "deny" and "script text containing opus" in reason,
            reason[:180],
        )
        for tool_name in ("Agent", "Task", "SendMessage", "Workflow"):
            payload = _call(
                world, tool_name, {"script": plain, "prompt": "go", "description": "go"},
                world.sonnet_transcript, "sess-worker-gate", "tu-worker-" + tool_name,
            )
            payload["agent_type"] = "workflow-subagent"
            payload["agent_id"] = "a" + "b" * 16
            kind, reason = verdict(run_gate(world, payload, pinned))
            suite.check(
                "WF",
                "workflow worker %s is denied by the gate" % tool_name,
                kind == "deny" and reason == arb.WORKER_NO_DELEGATION,
                reason[:180],
            )

        secret = agent_payload(
            world, model="opus", session_id="sess-auto-req", description="auto judge",
            prompt="SECRET-AUTO-PACKET do the hard part", tool_use_id="auto-opus",
        )
        secret["permission_mode"] = "auto"
        kind, reason = verdict(run_gate(world, secret))
        request_id, packet = pending_from_deny(reason)
        wrapped = packet_from_reason(reason)
        suite.check(
            "AUTO",
            "auto mode required text contains the full packet",
            kind == "deny" and request_id and wrapped == packet
            and "copy this prompt exactly" in reason
            and "SECRET-AUTO-PACKET" not in reason.split("<<<PACKET", 1)[0]
            and _request_state(request_id) == "JUDGE_PENDING",
            reason[:240],
        )
        reference = agent_payload(
            world, subagent_type="opus-judge", model="opus", session_id="sess-auto-req",
            description="Judge auto", prompt="ARBITRATION-PACKET request_id=%s" % request_id,
            tool_use_id="auto-ref", transcript=world.opus_transcript,
        )
        reference["permission_mode"] = "auto"
        ref_proc = run_gate(world, reference)
        ref_kind, ref_reason = verdict(ref_proc)
        suite.check(
            "AUTO",
            "auto mode judge reference denies and stays pending",
            ref_kind == "deny" and ref_reason.startswith("JUDGE_REFERENCE_UNSUPPORTED_IN_AUTO")
            and packet_from_reason(ref_reason) == packet
            and "updatedInput" not in ref_proc.stdout
            and _request_state(request_id) == "JUDGE_PENDING",
            "state=%s reason=%s" % (_request_state(request_id), ref_reason[:180]),
        )
        registered = arb.register_candidate(
            session_id="sess-auto-grant",
            task_id="task-auto-grant",
            trigger="T03",
            reason_code="CONFLICTING_REQUIREMENTS",
            requester_agent_type="file-reader",
            summary="resolve",
            task_summary="resolve the conflict",
            project=str(world.project),
        )
        promoted = arb.promote_pending(registered["request"]["request_id"]) if registered.get("ok") else None
        grant_id = promoted["request_id"] if isinstance(promoted, dict) else ""
        launch_kind, _launch_reason = verdict(run_gate(world, judge_payload(
            world, promoted.get("packet_text") if isinstance(promoted, dict) else "",
            "sess-auto-grant", "judge-auto-grant", wrapped=False,
        )))
        judged = arb.ingest_judge_result(judge_result(
            world, grant_id, "judge-auto-grant", "ESCALATE", session_id="sess-auto-grant",
        ))
        grant = _grant_row(world, grant_id)
        stored = json.loads(grant["invocation_json"]) if grant and isinstance(grant.get("invocation_json"), str) else None
        grant_call = agent_payload(
            world,
            session_id="sess-auto-grant",
            subagent_type=stored["subagent_type"] if isinstance(stored, dict) else "file-reader",
            model="opus",
            description=stored["description"] if isinstance(stored, dict) else "x",
            prompt="ARBITRATION-GRANT request_id=%s" % grant_id,
            tool_use_id="auto-grant",
            transcript=world.opus_transcript,
        )
        if isinstance(stored, dict):
            grant_call["tool_input"]["description"] = stored["description"]
            grant_call["tool_input"]["subagent_type"] = stored["subagent_type"]
        grant_call["permission_mode"] = "auto"
        before_uses = grant.get("uses_left") if isinstance(grant, dict) else None
        grant_kind, grant_reason = verdict(run_gate(world, grant_call))
        after = _grant_row(world, grant_id)
        suite.check(
            "AUTO",
            "auto mode grant reference denies and does not consume",
            launch_kind == "allow" and judged.get("decision") == "ESCALATE"
            and isinstance(stored, dict)
            and grant_kind == "deny"
            and grant_reason.startswith("GRANT_REFERENCE_UNSUPPORTED_IN_AUTO")
            and stored and json.dumps(stored, sort_keys=True, ensure_ascii=False, separators=(",", ":")) in grant_reason
            and before_uses == 1 and after and after.get("uses_left") == 1
            and _request_state(grant_id) == "GRANT_READY",
            "launch=%s grant=%s uses=%s reason=%s" % (
                launch_kind, grant_kind, None if after is None else after.get("uses_left"), grant_reason[:180],
            ),
        )
        stuck_payload = agent_payload(
            world, model="opus", session_id="sess-stuck-retry", description="stuck",
            prompt="stuck body", tool_use_id="stuck-opus",
        )
        stuck_kind, stuck_reason = verdict(run_gate(world, stuck_payload))
        stuck_id, stuck_packet = pending_from_deny(stuck_reason)
        first_id = "judge-stuck-1"
        first = run_gate(world, judge_payload(world, "ARBITRATION-PACKET request_id=%s" % stuck_id, "sess-stuck-retry", first_id, wrapped=False))
        import sqlite3
        db = world.home / ".claude" / "logs" / "opus-arbitration" / "ledger.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE requests SET updated=? WHERE request_id=?",
            (time.time() - 200, stuck_id),
        )
        conn.commit()
        conn.close()
        second_id = "judge-stuck-2"
        second = run_gate(world, judge_payload(
            world, "ARBITRATION-PACKET request_id=%s" % stuck_id, "sess-stuck-retry", second_id, wrapped=False,
        ))
        second_hook = hook_body(second)
        shown = arb.show_request(stuck_id) or {}
        suite.check(
            "AUTO",
            "stuck judge retry after 120s is allowed",
            stuck_kind == "deny" and stuck_packet
            and verdict(first)[0] is None
            and isinstance(second_hook, dict)
            and second_hook.get("permissionDecision") is None
            and isinstance(second_hook.get("updatedInput"), dict)
            and shown.get("state") == "JUDGE_RUNNING"
            and shown.get("judge_tool_use_id") == second_id,
            "first=%s second=%s state=%s tool=%s" % (
                verdict(first), verdict(second)[0], shown.get("state"), shown.get("judge_tool_use_id"),
            ),
        )
    finally:
        world.close()


def test_plan_ledger(suite):
    world = World()
    try:
        import sqlite3

        ledger = world.home / ".claude" / "logs" / "opus-arbitration"
        ledger.mkdir(parents=True)
        db = ledger / "ledger.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE requests (
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
                context_json TEXT
            );
            CREATE TABLE grants (
                grant_id TEXT PRIMARY KEY,
                request_id TEXT,
                session_id TEXT,
                invocation_digest TEXT,
                invocation_json TEXT,
                expires REAL,
                uses_left INTEGER,
                consumed_tool_use_id TEXT,
                consumed_at REAL
            );
            CREATE TABLE agent_runs (
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
            CREATE TABLE workflows (
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
            CREATE TABLE attempts (
                session_id TEXT,
                task_id TEXT,
                strategy_digest TEXT,
                outcome TEXT,
                created REAL
            );
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                session_id TEXT,
                event TEXT,
                request_id TEXT,
                detail_json TEXT
            );
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        conn.execute(
            """INSERT INTO requests (
                   request_id, session_id, task_id, trigger, reason_code, state, created, updated
               ) VALUES (?,?,?,?,?,?,?,?)""",
            ("keep-me", "sess-old", "old-task", "T02", "UNAPPROVED_OPUS", "JUDGED_CONTINUE", 10, 10),
        )
        conn.execute(
            "INSERT INTO grants (grant_id, request_id, uses_left) VALUES ('g-old', 'keep-me', 1)"
        )
        conn.commit()
        conn.close()
        arb.active_plan_version("none")
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        req_cols = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
        grant_cols = {row[1] for row in conn.execute("PRAGMA table_info(grants)")}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        kept = dict(conn.execute("SELECT * FROM requests WHERE request_id='keep-me'").fetchone())
        grant = dict(conn.execute("SELECT * FROM grants WHERE grant_id='g-old'").fetchone())
        conn.close()
        suite.check(
            "P1",
            "old ledger gains plan columns and keeps its row",
            "candidate_type" in req_cols
            and "plan_task_id" in req_cols
            and "purpose" in grant_cols
            and "plans" in tables
            and "stages" in tables
            and kept["request_id"] == "keep-me"
            and kept["state"] == "JUDGED_CONTINUE"
            and kept["candidate_type"] == "EXECUTOR_ESCALATION"
            and kept["plan_task_id"] is None
            and grant["purpose"] == "EXECUTOR"
            and grant["uses_left"] == 1,
            "row=%s grant=%s" % (kept, grant),
        )

        def register(session_id, task_id):
            return arb.register_candidate(
                session_id=session_id,
                task_id=task_id,
                trigger="T02",
                reason_code="UNAPPROVED_OPUS",
                scope_digest=task_id,
                summary="follow through",
            )

        created = register("sess-ft", "ft-task")
        request_id = created["request"]["request_id"]
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE requests SET expires=? WHERE request_id=?",
            (time.time() - 5, request_id),
        )
        conn.commit()
        conn.close()
        arb.record_event("tick-ft", session_id="sess-ft")
        shown = arb.show_request(request_id)
        follow = [
            row for row in rows(world, "events")
            if row["event"] == "no_follow_through" and row["request_id"] == request_id
        ]
        suite.check(
            "P1",
            "candidate past its ttl expires with no follow-through",
            created["ok"]
            and shown["state"] == "EXPIRED"
            and shown["decision"] == "NO_FOLLOW_THROUGH"
            and follow
            and '"from_state":"CANDIDATE"' in (follow[-1]["detail_json"] or ""),
            "state=%s decision=%s events=%s" % (
                shown and shown.get("state"), shown and shown.get("decision"), follow,
            ),
        )
        legacy_created = time.time() - 4000
        conn = sqlite3.connect(db)
        conn.execute(
            """INSERT INTO requests (
                   request_id, session_id, task_id, trigger, reason_code, state, created, updated, expires
               ) VALUES (?,?,?,?,?,?,?,?,NULL)""",
            (
                "legacy-1", "sess-legacy", "legacy-task", "T02", "UNAPPROVED_OPUS",
                "CANDIDATE", legacy_created, legacy_created,
            ),
        )
        conn.commit()
        conn.close()
        arb.record_event("tick-legacy", session_id="sess-legacy")
        legacy = arb.show_request("legacy-1")
        legacy_events = [
            row for row in rows(world, "events")
            if row["event"] == "no_follow_through" and row["request_id"] == "legacy-1"
        ]
        suite.check(
            "P1",
            "legacy candidate with null expires is swept",
            legacy["state"] == "EXPIRED"
            and legacy["decision"] == "NO_FOLLOW_THROUGH"
            and legacy_events
            and '"from_state":"CANDIDATE"' in (legacy_events[-1]["detail_json"] or ""),
            "state=%s decision=%s" % (legacy.get("state"), legacy.get("decision")),
        )
        pending = register("sess-judge", "judge-task")
        promoted = arb.promote_pending(pending["request"]["request_id"]) if pending.get("ok") else None
        pending_id = promoted["request_id"] if isinstance(promoted, dict) else ""
        conn = sqlite3.connect(db)
        conn.execute("UPDATE requests SET expires=? WHERE request_id=?", (time.time() - 5, pending_id))
        conn.commit()
        conn.close()
        arb.record_event("tick-judge", session_id="sess-judge")
        judged = arb.show_request(pending_id) or {}
        suite.check(
            "P1",
            "judge pending expiry is no follow-through",
            promoted and promoted.get("state") == "JUDGE_PENDING"
            and judged.get("state") == "EXPIRED"
            and judged.get("decision") == "NO_FOLLOW_THROUGH",
            "promoted=%s judged=%s" % (
                None if promoted is None else promoted.get("state"), judged.get("decision"),
            ),
        )
        now = time.time()
        conn = sqlite3.connect(db)
        conn.execute(
            """INSERT INTO requests (
                   request_id, session_id, trigger, reason_code, state, decision, created, updated, expires
               ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                "grant-old", "sess-grant", "T02", "UNAPPROVED_OPUS", "GRANT_READY",
                "ESCALATE", now, now, now - 5,
            ),
        )
        conn.execute(
            """INSERT INTO grants (grant_id, request_id, expires, uses_left)
               VALUES ('g-ready', 'grant-old', ?, 1)""",
            (now - 5,),
        )
        conn.commit()
        conn.close()
        arb.record_event("tick-grant", session_id="sess-grant")
        ready = arb.show_request("grant-old") or {}
        ready_grant = [row for row in rows(world, "grants") if row["grant_id"] == "g-ready"]
        ready_event = [
            row for row in rows(world, "events")
            if row["event"] == "no_follow_through" and row["request_id"] == "grant-old"
        ]
        suite.check(
            "P1",
            "grant ready expiry keeps its decision and logs follow-through",
            ready.get("state") == "EXPIRED"
            and ready.get("decision") == "ESCALATE"
            and ready_grant and ready_grant[0]["uses_left"] == 0
            and ready_event
            and '"from_state":"GRANT_READY"' in (ready_event[-1]["detail_json"] or ""),
            "state=%s decision=%s grant=%s" % (
                ready.get("state"), ready.get("decision"), ready_grant,
            ),
        )
        replan = register("sess-replan", "replan-task")
        replan_id = replan["request"]["request_id"]
        arb.transition_to(replan_id, "REPLAN_PENDING")
        resolved = arb.resolve_replan(replan_id, "because")
        again = arb.resolve_replan(replan_id, "again")
        replan_row = arb.show_request(replan_id) or {}
        other = register("sess-other", "other-task")
        other_id = other["request"]["request_id"]
        refused = arb.resolve_replan(other_id, "no")
        other_row = arb.show_request(other_id) or {}
        suite.check(
            "P1",
            "resolve replan only moves REPLAN_PENDING",
            replan["ok"] and resolved is True and again is False
            and replan_row.get("state") == "REPLAN_RESOLVED"
            and replan_row.get("decision") == "because"
            and refused is False and other_row.get("state") == "CANDIDATE",
            "resolved=%s again=%s state=%s other=%s" % (
                resolved, again, replan_row.get("state"), other_row.get("state"),
            ),
        )
        path = world.home / ".claude" / "hooks" / "data" / "opus-arbitration-policy.json"
        original = path.read_text(encoding="utf-8")
        data = json.loads(original)
        data.pop("planning", None)
        path.write_text(json.dumps(data), encoding="utf-8")
        loaded = arb.load_policy()
        plain = arb.planning_config(loaded)
        data["planning"] = "nope"
        path.write_text(json.dumps(data), encoding="utf-8")
        raised = False
        try:
            malformed = arb.planning_config(arb.load_policy())
        except Exception as exc:
            raised = True
            malformed = {"error": str(exc)}
        data["planning"] = {"enabled": "yes", "max_steps": 4}
        path.write_text(json.dumps(data), encoding="utf-8")
        typed = arb.planning_config(arb.load_policy())
        path.write_text(original, encoding="utf-8")
        suite.check(
            "P1",
            "missing or malformed planning stays disabled",
            loaded is not None and loaded.get("enabled") is True
            and "planning" not in loaded
            and plain["enabled"] is False and plain["max_steps"] == 12
            and raised is False and malformed["enabled"] is False
            and typed["enabled"] is False and typed["max_plan_versions_per_task"] == 3,
            "plain=%s malformed=%s typed=%s" % (plain, malformed, typed),
        )
        import _router_plans as plans
        body = {
            "schema_version": 1,
            "task_id": "status-task",
            "base_plan_version": 0,
            "objective": "Show the new status sections",
            "assumptions": [],
            "invariants": [],
            "steps": [{
                "id": "draft",
                "depends_on": [],
                "executor_role": "main",
                "allowed_model": "main",
                "instructions": "Record the stage",
                "acceptance_checks": ["The stage is open"],
                "requires_user_approval": False,
            }],
            "risks": [],
            "handoff_notes": "",
        }
        record = plans.promote("status-task", body)
        arb.record_plan_version(
            "sess-status", "status-task", record["version"],
            record["content_sha256"], record["scope_digest"],
        )
        started, _code, _attempt = arb.stage_start("sess-status", "status-task", record["version"], "draft")
        status = subprocess.run(
            [sys.executable, str(CLI), "status"],
            capture_output=True, text=True, env=world.env(), timeout=15, check=False,
        )
        suite.check(
            "P1",
            "status shows unresolved follow-through and open stages",
            started and status.returncode == 0
            and "unresolved (no follow-through, 7d):" in status.stdout
            and "stages open:" in status.stdout
            and request_id in status.stdout
            and "from_state=CANDIDATE" in status.stdout
            and "stage=draft" in status.stdout
            and "plan_version=1" in status.stdout,
            status.stdout[-500:],
        )
        cancel = register("sess-cancel", "cancel-task")
        cancel_id = cancel["request"]["request_id"]
        arb.transition_to(cancel_id, "REPLAN_PENDING")
        arb.disable_arbitration()
        cancelled = arb.show_request(cancel_id) or {}
        suite.check(
            "P1",
            "disable cancels REPLAN_PENDING",
            cancel["ok"] and cancelled.get("state") == "CANCELLED",
            cancelled.get("state"),
        )
    finally:
        world.close()


def _enable_planning(world):
    path = world.home / ".claude" / "hooks" / "data" / "opus-arbitration-policy.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["planning"]["enabled"] = True
    path.write_text(json.dumps(data), encoding="utf-8")


def _planner_file(world, model="opus", tools="tools: Read\n"):
    _agent_md(world.home / ".claude" / "agents" / "opus-planner.md", model, tools)


def _planner_call(world, **kwargs):
    kwargs.setdefault("subagent_type", "opus-planner")
    kwargs.setdefault("model", "opus")
    kwargs.setdefault("description", "Plan the work")
    kwargs.setdefault("prompt", "ROUTER-TASK plan-1\nPlan the store")
    return agent_payload(world, **kwargs)


def _grant_uses(world, request_id):
    for row in rows(world, "grants"):
        if row.get("request_id") == request_id:
            return row.get("uses_left")
    return None


def test_planner(suite):
    judge_text = (ROOT.parent / "agents" / "opus-judge.md").read_text(encoding="utf-8")
    suite.check(
        "P11",
        "judge instructions keep the nine keys and the planner sentence",
        all(key in judge_text for key in (
            '"schema_version":1', '"request_id":', '"decision":', '"recommended_model":',
            '"reason_code":', '"rationale":', '"evidence_used":', '"execution_scope":',
            '"suggested_next_step":',
        ))
        and "For PLAN_REQUEST and REPLAN_REQUEST, ESCALATE authorizes only one analysis-only planner call; choose CONTINUE when Sonnet-level planning is enough." in judge_text,
        "",
    )
    world = World()
    try:
        before = rows(world, "requests")
        _set_planning(world, False)
        kind, reason = verdict(run_gate(world, _planner_call(
            world, session_id="sess-p1", tool_use_id="p1",
        )))
        suite.check(
            "P1",
            "disabled planning denies and creates no request",
            kind == "deny" and reason == "OPUS_PLANNER_DISABLED" and rows(world, "requests") == before,
            reason[:200],
        )
        _enable_planning(world)
        _planner_file(world)
        kind, reason = verdict(run_gate(world, _planner_call(
            world, session_id="sess-p2", tool_use_id="p2", transcript=world.opus_transcript,
        )))
        suite.check(
            "P2",
            "an Opus main session is told to plan itself",
            kind == "deny"
            and reason.startswith("OPUS_PLANNER_NOT_NEEDED ")
            and "write the plan yourself" in reason
            and not any(row["session_id"] == "sess-p2" for row in rows(world, "requests")),
            reason[:240],
        )
        kind, reason = verdict(run_gate(world, _planner_call(
            world, session_id="sess-p2b", tool_use_id="p2b", model="inherit",
            transcript=world.opus_transcript,
        )))
        suite.check(
            "P2b",
            "inherit on an Opus main does not need the planner",
            kind == "deny" and reason.startswith("OPUS_PLANNER_NOT_NEEDED ")
            and not any(row["session_id"] == "sess-p2b" for row in rows(world, "requests")),
            reason[:240],
        )
        early = world.home / ".claude" / "projects" / "demo" / "early-opus.jsonl"
        early.write_text(
            json.dumps({
                "type": "attachment",
                "attachment": {
                    "type": "model",
                    "identity": {"modelId": "claude-opus-5-5", "marketingName": "Opus"},
                },
            }) + "\n",
            encoding="utf-8",
        )
        early_family = _agent_model.main_session_model({"transcript_path": str(early)})
        kind, reason = verdict(run_gate(world, _planner_call(
            world, session_id="sess-early", tool_use_id="p-early", transcript=early,
        )))
        suite.check(
            "P2",
            "a session-start model attachment is enough for NOT_NEEDED",
            early_family == "opus"
            and kind == "deny" and reason.startswith("OPUS_PLANNER_NOT_NEEDED ")
            and not any(row["session_id"] == "sess-early" for row in rows(world, "requests")),
            "family=%s reason=%s" % (early_family, reason[:200]),
        )
        quoted = world.home / ".claude" / "projects" / "demo" / "quoted-model.jsonl"
        quoted.write_text(
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "The exact model ID is claude-opus-5-5"},
            }) + "\n",
            encoding="utf-8",
        )
        empty = world.home / ".claude" / "projects" / "demo" / "empty-main.jsonl"
        empty.write_text("{}\n", encoding="utf-8")
        quoted_family = _agent_model.main_session_model({"transcript_path": str(quoted)})
        empty_family = _agent_model.main_session_model({"transcript_path": str(empty)})
        kind, reason = verdict(run_gate(world, _planner_call(
            world, session_id="sess-unknown", tool_use_id="p-unknown", transcript=empty,
        )))
        suite.check(
            "P2",
            "unknown stays unknown and still asks for arbitration",
            quoted_family == "unknown" and empty_family == "unknown"
            and kind == "deny" and "ARBITRATION_REQUIRED" in reason
            and "OPUS_PLANNER_NOT_NEEDED" not in reason,
            "quoted=%s empty=%s reason=%s" % (quoted_family, empty_family, reason[:200]),
        )
        mixed = world.home / ".claude" / "projects" / "demo" / "mixed-model.jsonl"
        mixed.write_text(
            json.dumps({
                "type": "attachment",
                "attachment": {"type": "model", "identity": {"modelId": "claude-opus-5-5"}},
            }) + "\n"
            + json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "model": "claude-sonnet-5-5", "content": [{"type": "text", "text": "ok"}]},
            }) + "\n",
            encoding="utf-8",
        )
        suite.check(
            "P2",
            "a later assistant model wins over the session-start identity",
            _agent_model.main_session_model({"transcript_path": str(mixed)}) == "sonnet",
            _agent_model.main_session_model({"transcript_path": str(mixed)}),
        )
        kind, reason = verdict(run_gate(world, _planner_call(
            world, session_id="sess-p3", tool_use_id="p3", prompt="ROUTER-TASK plan-1\nPlan the store",
        )))
        request_id, packet = pending_from_deny(reason)
        stored = [row for row in rows(world, "requests") if row.get("request_id") == request_id]
        suite.check(
            "P3",
            "a Sonnet main session opens a plan request",
            kind == "deny" and "ARBITRATION_REQUIRED" in reason
            and stored and stored[0].get("candidate_type") == "PLAN_REQUEST"
            and stored[0].get("plan_task_id") == "plan-1",
            "kind=%s type=%s task=%s" % (
                kind,
                None if not stored else stored[0].get("candidate_type"),
                None if not stored else stored[0].get("plan_task_id"),
            ),
        )
        suite.check(
            "P11",
            "a plan packet names the candidate and the capability",
            isinstance(packet, str)
            and "candidate_type: PLAN_REQUEST" in packet
            and "requested_capability: PLAN_ONLY" in packet
            and isinstance(packet_object(packet), dict),
            (packet or "")[:300],
        )
        counted = len(rows(world, "requests"))
        kind, reason = verdict(run_gate(world, _planner_call(
            world, session_id="sess-sonnet", tool_use_id="p3-sonnet", model="sonnet",
        )))
        suite.check(
            "P3",
            "a per-call sonnet planner is not Opus",
            kind == "allow" and len(rows(world, "requests")) == counted,
            "kind=%s reason=%s" % (kind, reason[:160]),
        )
        exec_kind, exec_reason = verdict(run_gate(world, agent_payload(
            world, model="opus", session_id="sess-exec", tool_use_id="p-exec",
            description="Do the work", prompt="do the work",
        )))
        _exec_id, exec_packet = pending_from_deny(exec_reason)
        suite.check(
            "P11",
            "an executor packet stays an executor escalation",
            exec_kind == "deny" and isinstance(exec_packet, str)
            and "candidate_type: EXECUTOR_ESCALATION" in exec_packet
            and "requested_capability: EXECUTOR" in exec_packet,
            (exec_packet or exec_reason)[:300],
        )
        for decision in ("CONTINUE", "RETRY", "STOP"):
            payload = _planner_call(
                world, session_id="sess-" + decision.lower(), tool_use_id="open-" + decision,
                prompt="ROUTER-TASK " + decision.lower() + "\nPlan " + decision,
                description="Plan " + decision,
            )
            denied = run_gate(world, payload)
            _kind, denied_reason = verdict(denied)
            decided_id, decided_packet = pending_from_deny(denied_reason)
            run_gate(world, judge_payload(world, decided_packet, payload["session_id"], "judge-" + decision))
            arb.ingest_judge_result(judge_result(
                world, decided_id, "judge-" + decision, decision, session_id=payload["session_id"],
            ))
            again_kind, again_reason = verdict(run_gate(world, {**payload, "tool_use_id": "again-" + decision}))
            suite.check(
                "P4",
                "%s leaves no grant and blocks a retry" % decision,
                again_kind == "deny" and again_reason.startswith("ARBITRATION_ALREADY_DECIDED")
                and _grant_uses(world, decided_id) is None,
                again_reason[:200],
            )
        granted = _planner_call(
            world, session_id="sess-grant", tool_use_id="grant-open",
            prompt="ROUTER-TASK granted\nPlan the granted work", description="Plan the granted work",
        )
        granted, grant_id, grant_result = escalate(world, granted)
        grant_rows = [row for row in rows(world, "grants") if row.get("request_id") == grant_id]
        mismatch = agent_payload(
            world, model="opus", session_id="sess-grant", tool_use_id="grant-mismatch",
            subagent_type="general-purpose", description="Plan the granted work",
            prompt="ROUTER-TASK granted\nPlan the granted work",
        )
        mismatch_kind, mismatch_reason = verdict(run_gate(world, mismatch))
        suite.check(
            "P6",
            "a planner grant cannot run general-purpose",
            grant_result.get("decision") == "ESCALATE"
            and grant_rows and grant_rows[0].get("purpose") == "PLAN_ONLY"
            and mismatch_kind == "deny" and mismatch_reason == "GRANT_PURPOSE_MISMATCH"
            and _grant_uses(world, grant_id) == 1,
            "decision=%s reason=%s uses=%s" % (grant_result.get("decision"), mismatch_reason, _grant_uses(world, grant_id)),
        )
        reference = agent_payload(
            world, model="opus", session_id="sess-grant", tool_use_id="grant-ref-purpose",
            subagent_type="general-purpose", description="Plan the granted work",
            prompt="ARBITRATION-GRANT request_id=%s" % grant_id,
        )
        ref_kind, ref_reason = verdict(run_gate(world, reference))
        suite.check(
            "P6b",
            "a grant reference from a non-planner does not consume a plan grant",
            ref_kind == "deny" and ref_reason == "GRANT_PURPOSE_MISMATCH"
            and _grant_uses(world, grant_id) == 1,
            "reason=%s uses=%s" % (ref_reason, _grant_uses(world, grant_id)),
        )
        used = run_gate(world, {**granted, "tool_use_id": "grant-use"})
        used_kind, used_reason = verdict(used)
        second_kind, second_reason = verdict(run_gate(world, {**granted, "tool_use_id": "grant-use-2"}))
        suite.check(
            "P5",
            "the planner grant allows one identical call",
            used_kind == "allow" and used.stdout == ""
            and second_kind == "deny" and _grant_uses(world, grant_id) == 0,
            "used=%s %s second=%s %s" % (used_kind, used_reason[:80], second_kind, second_reason[:80]),
        )
        fresh = _planner_call(
            world, session_id="sess-mutate", tool_use_id="mutate-open",
            prompt="ROUTER-TASK mutate\nPlan the mutable work", description="Plan the mutable work",
        )
        fresh, fresh_id, _fresh_result = escalate(world, fresh)
        prompt_kind, prompt_reason = verdict(run_gate(world, {
            **fresh, "tool_use_id": "mutate-prompt",
            "tool_input": {**fresh["tool_input"], "prompt": "ROUTER-TASK mutate\nA different plan"},
        }))
        desc_kind, desc_reason = verdict(run_gate(world, {
            **fresh, "tool_use_id": "mutate-desc",
            "tool_input": {**fresh["tool_input"], "description": "A different description"},
        }))
        bg_kind, bg_reason = verdict(run_gate(world, {
            **fresh, "tool_use_id": "mutate-bg",
            "tool_input": {**fresh["tool_input"], "run_in_background": True},
        }))
        suite.check(
            "P7",
            "a changed planner call does not consume the grant",
            prompt_kind == "deny" and desc_kind == "deny"
            and bg_kind == "deny" and bg_reason == "OPUS_BACKGROUND_DENIED"
            and _grant_uses(world, fresh_id) == 1,
            "prompt=%s desc=%s bg=%s uses=%s" % (prompt_reason[:80], desc_reason[:80], bg_reason, _grant_uses(world, fresh_id)),
        )
        strict_kind, strict_reason = verdict(run_gate(
            world,
            _planner_call(world, session_id="sess-strict", tool_use_id="strict-1"),
            {"CLAUDE_ROUTING_MODE": "strict"},
        ))
        suite.check(
            "P8",
            "strict mode denies the planner before planning",
            strict_kind == "deny" and strict_reason == "OPUS_DENIED mode=strict; use sonnet/haiku"
            and not any(row["session_id"] == "sess-strict" for row in rows(world, "requests")),
            strict_reason,
        )
        pending = _planner_call(world, session_id="sess-disable", tool_use_id="disable-1")
        pending_kind, pending_reason = verdict(run_gate(world, pending))
        pending_id = request_id_from_text(pending_reason)
        arb.disable_arbitration()
        shown = arb.show_request(pending_id)
        off_kind, off_reason = verdict(run_gate(world, {**pending, "tool_use_id": "disable-2"}))
        suite.check(
            "P8",
            "disable cancels a pending plan request",
            pending_kind == "deny" and "ARBITRATION_REQUIRED" in pending_reason
            and isinstance(shown, dict) and shown.get("state") == "CANCELLED"
            and off_kind == "deny" and off_reason == "OPUS_DENIED mode=disabled",
            "state=%s off=%s" % (None if not isinstance(shown, dict) else shown.get("state"), off_reason),
        )
    finally:
        world.close()

    broken = World()
    try:
        _enable_planning(broken)
        for label, tools in (
            ("[]", "tools: []\n"),
            ("omitted", ""),
            ("Read, Bash", "tools: Read, Bash\n"),
            ("disallowedTools", "tools: [Read, Bash]\ndisallowedTools: [Bash]\n"),
        ):
            _planner_file(broken, tools=tools)
            kind, reason = verdict(run_gate(broken, _planner_call(
                broken, session_id="sess-tools-" + label, tool_use_id="bad-tools-" + label,
            )))
            suite.check(
                "P9",
                "planner tools %s are invalid" % label,
                kind == "deny" and reason == "PLANNER_DEFINITION_INVALID",
                reason,
            )
        _planner_file(broken, tools="tools: [Read]\n")
        kind, reason = verdict(run_gate(broken, _planner_call(
            broken, session_id="sess-tools-list", tool_use_id="tools-list",
        )))
        suite.check(
            "P9",
            "planner tools [Read] pass the definition check",
            kind == "deny" and "ARBITRATION_REQUIRED" in reason,
            reason[:200],
        )
        _planner_file(broken, model="haiku")
        kind, reason = verdict(run_gate(broken, _planner_call(
            broken, session_id="sess-model", tool_use_id="bad-model",
        )))
        suite.check("P9", "a non-opus planner model is invalid", kind == "deny" and reason == "PLANNER_DEFINITION_INVALID", reason)
        _planner_file(broken)
        project_agent = broken.project / ".claude" / "agents" / "opus-planner.md"
        _agent_md(project_agent, "opus", "tools: Read\n")
        kind, reason = verdict(run_gate(broken, _planner_call(
            broken, session_id="sess-shadow", tool_use_id="shadow-project",
        )))
        suite.check("P9", "a project planner shadows the user file", kind == "deny" and reason == "PLANNER_SHADOWED", reason)
        project_agent.unlink()
        plugin_agent = broken.home / ".claude" / "plugins" / "opusplug" / "agents" / "opus-planner.md"
        _agent_md(plugin_agent, "opus", "tools: Read\n")
        kind, reason = verdict(run_gate(broken, _planner_call(
            broken, session_id="sess-plugin", tool_use_id="shadow-plugin",
        )))
        suite.check("P9", "a plugin planner shadows the user file", kind == "deny" and reason == "PLANNER_SHADOWED", reason)
    finally:
        broken.close()


def _plan_body(task_id, base_version=0, steps=None):
    if steps is None:
        steps = ("draft", "review")
    body_steps = []
    for index, step_id in enumerate(steps):
        body_steps.append({
            "id": step_id,
            "depends_on": [] if index == 0 else [steps[index - 1]],
            "executor_role": "main",
            "allowed_model": "main",
            "instructions": "Carry the stage",
            "acceptance_checks": ["The stage is recorded"],
            "requires_user_approval": False,
        })
    return {
        "schema_version": 1,
        "task_id": task_id,
        "base_plan_version": base_version,
        "objective": "Revise the stage when the checkpoint is wrong",
        "assumptions": ["The ledger is local"],
        "invariants": ["Plans are data"],
        "steps": body_steps,
        "risks": [],
        "handoff_notes": "",
    }


def _publish_plan(session_id, task_id, base_version=0, steps=None):
    import _router_plans as plans

    record = plans.promote(task_id, _plan_body(task_id, base_version, steps))
    arb.record_plan_version(
        session_id, task_id, record["version"], record["content_sha256"], record["scope_digest"],
    )
    return record


def _reserve_worker(world, session_id, task_id, version, stage_id, tool_use_id, prompt_id, agent_id):
    accepted = arb.accept_workflow({
        "tool_use_id": tool_use_id,
        "session_id": session_id,
        "prompt_id": prompt_id,
        "cwd": str(world.project),
    }, "sonnet")
    ok, code, _attempt = arb.stage_start(
        session_id, task_id, version, stage_id, workflow_tool_use_id=tool_use_id,
    )
    recorded = arb.record_workflow_worker({
        "agent_id": agent_id,
        "session_id": session_id,
        "prompt_id": prompt_id,
        "cwd": str(world.project),
    })
    return accepted and ok and code == "OK" and isinstance(recorded, dict)


def _replan_footer(task_id, stage_id, version, **extra):
    body = {
        "schema_version": 2,
        "task_id": task_id,
        "status": "NEEDS_JUDGMENT",
        "reason_code": "PLAN_REVISION_REQUIRED",
        "evidence_refs": ["notes/evidence.md"],
        "stage_id": stage_id,
        "plan_version": version,
        "unresolved_question": "Which file owns the stage?",
        "requested_action": "REPLAN",
        "failed_requirement_ids": ["req-1"],
        "completed_stage_ids": [],
    }
    body.update(extra)
    return body


def _run_guard(world, payload):
    return subprocess.run(
        [sys.executable, str(ROOT / "opus-workflow-guard.py")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=world.env(),
        cwd=str(world.project),
        timeout=30,
        check=False,
    )


def _worker_stop(world, session_id, agent_id, footer, transcript):
    stop = json.loads(
        (FIXTURES / "workflow" / "20261105T043812.562702Z-SubagentStop-none.json").read_text(encoding="utf-8")
    )
    stop["session_id"] = session_id
    stop["cwd"] = str(world.project)
    stop["transcript_path"] = str(transcript)
    stop["agent_id"] = agent_id
    stop["agent_type"] = "workflow-subagent"
    stop["last_assistant_message"] = json.dumps(footer)
    stop.pop("agent_transcript_path", None)
    return _run_guard(world, stop)


def _main_pretool(world, session_id, tool_use_id):
    return _run_guard(world, {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "session_id": session_id,
        "cwd": str(world.project),
        "tool_use_id": tool_use_id,
        "tool_input": {"file_path": "/tmp/readme"},
    })


def _stage_rows(world, task_id, stage_id=None):
    found = [row for row in rows(world, "stages") if row.get("task_id") == task_id]
    if stage_id is None:
        return found
    return [row for row in found if row.get("stage_id") == stage_id]


def _set_planning(world, enabled=True, replans=None, judgments=None):
    path = world.home / ".claude" / "hooks" / "data" / "opus-arbitration-policy.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["planning"]["enabled"] = enabled
    if replans is not None:
        data["planning"]["max_replans_per_task"] = replans
    if judgments is not None:
        data["limits"]["max_judgments_per_session"] = judgments
        data["limits"]["max_judgments_per_task"] = judgments
    path.write_text(json.dumps(data), encoding="utf-8")


def test_replan(suite):
    v1 = {
        "schema_version": 1,
        "task_id": "footer-task",
        "status": "NEEDS_JUDGMENT",
        "reason_code": "WORKER_REQUEST",
        "unresolved": ["one"],
        "evidence_refs": ["a.md"],
    }
    parsed_v1 = triggers.parse_footer("noise\n" + json.dumps(v1))
    v2 = _replan_footer("footer-task", "draft", 1, plan_id="plan-1", workflow_run_id="run-1")
    parsed_v2 = triggers.parse_footer(json.dumps(v2))
    unknown = dict(v2)
    unknown["extra"] = "no"
    boolean = dict(v2)
    boolean["plan_version"] = True
    suite.check(
        "R1",
        "v1 stays identical, v2 parses, and a bad v2 is rejected",
        parsed_v1 == v1
        and parsed_v2 == v2
        and triggers.parse_footer(json.dumps(unknown)) is None
        and triggers.parse_footer(json.dumps(boolean)) is None,
        "v1=%s v2=%s" % (parsed_v1, None if parsed_v2 is None else parsed_v2.get("schema_version")),
    )

    world = World()
    try:
        _set_planning(world, True, judgments=1)
        _planner_file(world)
        task_id = "replan-task"
        session_id = "sess-replan"
        record = _publish_plan(session_id, task_id)
        version = record["version"]
        agent_id = "a" + "b" * 16
        ready = _reserve_worker(
            world, session_id, task_id, version, "draft", "wf-replan", "prompt-replan", agent_id,
        )
        stopped = _worker_stop(
            world, session_id, agent_id,
            _replan_footer(task_id, "draft", version),
            world.opus_transcript,
        )
        replans = [
            row for row in rows(world, "requests")
            if row.get("candidate_type") == "REPLAN_REQUEST" and row.get("plan_task_id") == task_id
        ]
        stage = _stage_rows(world, task_id, "draft")
        events = (world.home / ".claude" / "logs" / "opus-arbitration" / "events.jsonl").read_text(encoding="utf-8")
        first = _main_pretool(world, session_id, "main-r2")
        text = (hook_body(first) or {}).get("additionalContext") or ""
        second = _main_pretool(world, session_id, "main-r2b")
        later = arb.register_candidate(
            session_id=session_id, task_id="other-quota", trigger="T02", reason_code="UNAPPROVED_OPUS",
        )
        suite.check(
            "R2",
            "an Opus main replan is pending without a judge and surfaces once",
            ready and stopped.returncode == 0
            and len(replans) == 1
            and replans[0]["state"] == "REPLAN_PENDING"
            and replans[0]["candidate_type"] == "REPLAN_REQUEST"
            and not replans[0].get("packet_text")
            and "judge_pending" not in events
            and stage and stage[0]["status"] == "needs_judgment"
            and text.startswith("REPLAN_REQUIRED request_id=%s" % replans[0]["request_id"])
            and "task=%s" % task_id in text
            and "plan_version=%s" % version in text
            and "stage=draft" in text
            and second.stdout == ""
            and later.get("ok") is True,
            "text=%s later=%s state=%s" % (
                text[:240], later.get("reason"), None if not replans else replans[0]["state"],
            ),
        )

        other_agent = "a" + "c" * 16
        linked = arb.record_workflow_worker({
            "agent_id": other_agent,
            "session_id": session_id,
            "prompt_id": "prompt-replan",
            "cwd": str(world.project),
        })
        again = _worker_stop(
            world, session_id, other_agent,
            _replan_footer(task_id, "draft", version),
            world.opus_transcript,
        )
        same = [
            row for row in rows(world, "requests")
            if row.get("candidate_type") == "REPLAN_REQUEST" and row.get("plan_task_id") == task_id
        ]
        deduped = (world.home / ".claude" / "logs" / "opus-arbitration" / "events.jsonl").read_text(encoding="utf-8")
        suite.check(
            "R3",
            "two workers in one stage share one replan",
            isinstance(linked, dict) and again.returncode == 0 and len(same) == 1
            and "candidate_deduped" in deduped,
            "rows=%s" % len(same),
        )

        _set_planning(world, True, judgments=3)
        bare = "a" + "d" * 16
        arb.record_workflow_worker({
            "agent_id": bare,
            "session_id": session_id,
            "prompt_id": "missing-prompt",
            "cwd": str(world.project),
        })
        mismatch_footer = _replan_footer(task_id, "review", version, unresolved=["the mismatch"])
        _worker_stop(world, session_id, agent_id, mismatch_footer, world.opus_transcript)
        _worker_stop(
            world, session_id, bare,
            _replan_footer(task_id, "draft", version, unresolved=["no stage"]),
            world.opus_transcript,
        )
        kinds = [
            row.get("candidate_type") for row in rows(world, "requests")
            if row.get("session_id") == session_id
        ]
        uncorrelated = (world.home / ".claude" / "logs" / "opus-arbitration" / "events.jsonl").read_text(encoding="utf-8")
        suite.check(
            "R4",
            "an unmatched footer stays on the generic path",
            uncorrelated.count("replan_uncorrelated") >= 2
            and kinds.count("REPLAN_REQUEST") == 1
            and "EXECUTOR_ESCALATION" in kinds,
            "kinds=%s" % kinds,
        )
    finally:
        world.close()

    limited = World()
    try:
        _set_planning(limited, False)
        task_id = "limit-task"
        session_id = "sess-limit"
        record = _publish_plan(session_id, task_id, steps=("draft", "review", "notes"))
        version = record["version"]
        agent_id = "a" + "e" * 16
        _reserve_worker(limited, session_id, task_id, version, "draft", "wf-off", "prompt-off", agent_id)
        _worker_stop(
            limited, session_id, agent_id,
            _replan_footer(task_id, "draft", version),
            limited.opus_transcript,
        )
        blocked = _main_pretool(limited, session_id, "main-off")
        blocked_text = (hook_body(blocked) or {}).get("additionalContext") or ""
        suite.check(
            "R5",
            "disabled planning surfaces REPLAN_BLOCKED and creates nothing",
            not any(row.get("candidate_type") == "REPLAN_REQUEST" for row in rows(limited, "requests"))
            and "REPLAN_BLOCKED task=%s stage=draft reason=planning_disabled" % task_id in blocked_text
            and _stage_rows(limited, task_id, "draft")[0]["status"] == "needs_judgment",
            blocked_text[:240],
        )
        _set_planning(limited, True, replans=1)
        review_agent = "a" + "f" * 16
        review_ready = _reserve_worker(
            limited, session_id, task_id, version, "review", "wf-cap", "prompt-cap", review_agent,
        )
        _worker_stop(
            limited, session_id, review_agent,
            _replan_footer(task_id, "review", version),
            limited.opus_transcript,
        )
        notes_agent = "a" + "9" * 16
        notes_ready = _reserve_worker(
            limited, session_id, task_id, version, "notes", "wf-notes", "prompt-notes", notes_agent,
        )
        _worker_stop(
            limited, session_id, notes_agent,
            _replan_footer(task_id, "notes", version),
            limited.opus_transcript,
        )
        capped = [
            row for row in rows(limited, "requests")
            if row.get("candidate_type") == "REPLAN_REQUEST" and row.get("plan_task_id") == task_id
        ]
        cap_text = (hook_body(_main_pretool(limited, session_id, "main-cap")) or {}).get("additionalContext") or ""
        suite.check(
            "R5",
            "the replan cap surfaces REPLAN_BLOCKED and creates no further request",
            review_ready and notes_ready and len(capped) == 1
            and "REPLAN_BLOCKED task=%s stage=notes reason=replan_limit" % task_id in cap_text
            and _stage_rows(limited, task_id, "notes")[0]["status"] == "needs_judgment",
            "rows=%s text=%s" % (len(capped), cap_text[:240]),
        )
    finally:
        limited.close()

    sonnet = World()
    try:
        _set_planning(sonnet, True)
        _planner_file(sonnet)
        session_id = "sess-sonnet"
        esc_task = "sonnet-esc"
        cont_task = "sonnet-cont"
        esc_version = _publish_plan(session_id, esc_task)["version"]
        cont_version = _publish_plan(session_id, cont_task)["version"]
        esc_agent = "a" + "1" * 16
        cont_agent = "a" + "2" * 16
        _reserve_worker(sonnet, session_id, esc_task, esc_version, "draft", "wf-esc", "prompt-esc", esc_agent)
        _reserve_worker(sonnet, session_id, cont_task, cont_version, "draft", "wf-cont", "prompt-cont", cont_agent)
        _worker_stop(sonnet, session_id, esc_agent, _replan_footer(esc_task, "draft", esc_version), sonnet.sonnet_transcript)
        _worker_stop(sonnet, session_id, cont_agent, _replan_footer(cont_task, "draft", cont_version), sonnet.sonnet_transcript)
        esc_row = next(
            row for row in rows(sonnet, "requests")
            if row.get("plan_task_id") == esc_task and row.get("candidate_type") == "REPLAN_REQUEST"
        )
        cont_row = next(
            row for row in rows(sonnet, "requests")
            if row.get("plan_task_id") == cont_task and row.get("candidate_type") == "REPLAN_REQUEST"
        )
        esc_packet = stored_packet(esc_row["request_id"])
        launched = run_gate(sonnet, judge_payload(sonnet, esc_packet, session_id, "judge-" + esc_row["request_id"]))
        launch_kind, launch_reason = verdict(launched)
        escalated = arb.ingest_judge_result(judge_result(
            sonnet, esc_row["request_id"], "judge-" + esc_row["request_id"], "ESCALATE", session_id=session_id,
        ))
        worker = escalated.get("worker_invocation") or {}
        grant_rows = [row for row in rows(sonnet, "grants") if row.get("request_id") == esc_row["request_id"]]
        reference = agent_payload(
            sonnet, model="opus", session_id=session_id, tool_use_id="replan-ref",
            subagent_type="general-purpose", description=worker.get("description"),
            prompt="ARBITRATION-GRANT request_id=%s" % esc_row["request_id"],
            transcript=sonnet.sonnet_transcript,
        )
        ref_kind, ref_reason = verdict(run_gate(sonnet, reference))
        exact = agent_payload(
            sonnet, session_id=session_id, tool_use_id="replan-use",
            subagent_type=worker.get("subagent_type"), model=worker.get("model"),
            description=worker.get("description"), prompt=worker.get("prompt"),
            run_in_background=False, transcript=sonnet.sonnet_transcript,
        )
        used_kind, used_reason = verdict(run_gate(sonnet, exact))
        again_kind, again_reason = verdict(run_gate(sonnet, {**exact, "tool_use_id": "replan-use-2"}))
        cont_packet = stored_packet(cont_row["request_id"])
        run_gate(sonnet, judge_payload(sonnet, cont_packet, session_id, "judge-" + cont_row["request_id"]))
        continued = arb.ingest_judge_result(judge_result(
            sonnet, cont_row["request_id"], "judge-" + cont_row["request_id"], "CONTINUE", session_id=session_id,
        ))
        suite.check(
            "R6",
            "a Sonnet main replan is judged and one planner call consumes the grant",
            esc_row["state"] == "JUDGE_PENDING"
            and "candidate_type: REPLAN_REQUEST" in (esc_packet or "")
            and launch_kind == "allow"
            and escalated.get("decision") == "ESCALATE"
            and worker.get("subagent_type") == "opus-planner"
            and worker.get("model") == "opus"
            and isinstance(worker.get("prompt"), str)
            and worker["prompt"].startswith("ROUTER-TASK %s" % esc_task)
            and grant_rows and grant_rows[0].get("purpose") == "PLAN_ONLY"
            and ref_kind == "deny" and ref_reason == "GRANT_PURPOSE_MISMATCH"
            and used_kind == "allow"
            and again_kind == "deny"
            and continued.get("decision") == "CONTINUE"
            and _grant_uses(sonnet, cont_row["request_id"]) is None,
            "launch=%s %s ref=%s used=%s %s again=%s %s worker=%s" % (
                launch_kind, launch_reason[:120], ref_reason, used_kind, used_reason[:120],
                again_kind, again_reason[:80], worker.get("subagent_type"),
            ),
        )
    finally:
        sonnet.close()

    strict_world = World()
    try:
        _set_planning(strict_world, True)
        _planner_file(strict_world)
        session_id = "sess-strict-replan"
        task_id = "strict-task"
        version = _publish_plan(session_id, task_id)["version"]
        agent_id = "a" + "7" * 16
        _reserve_worker(strict_world, session_id, task_id, version, "draft", "wf-strict", "prompt-strict", agent_id)
        _worker_stop(
            strict_world, session_id, agent_id,
            _replan_footer(task_id, "draft", version),
            strict_world.sonnet_transcript,
        )
        row = next(item for item in rows(strict_world, "requests") if item.get("plan_task_id") == task_id)
        packet = stored_packet(row["request_id"])
        run_gate(strict_world, judge_payload(strict_world, packet, session_id, "judge-" + row["request_id"]))
        result = arb.ingest_judge_result(judge_result(
            strict_world, row["request_id"], "judge-" + row["request_id"], "ESCALATE", session_id=session_id,
        ))
        worker = result.get("worker_invocation") or {}
        denied_kind, denied_reason = verdict(run_gate(
            strict_world,
            agent_payload(
                strict_world, session_id=session_id, tool_use_id="strict-planner",
                subagent_type=worker.get("subagent_type"), model=worker.get("model"),
                description=worker.get("description"), prompt=worker.get("prompt"),
                transcript=strict_world.sonnet_transcript,
            ),
            {"CLAUDE_ROUTING_MODE": "strict"},
        ))
        suite.check(
            "R12",
            "strict mode keeps the replan and denies the planner",
            row["candidate_type"] == "REPLAN_REQUEST"
            and row["state"] == "JUDGE_PENDING"
            and result.get("decision") == "ESCALATE"
            and denied_kind == "deny"
            and denied_reason == "OPUS_DENIED mode=strict; use sonnet/haiku"
            and _grant_uses(strict_world, row["request_id"]) == 1,
            "reason=%s uses=%s" % (denied_reason, _grant_uses(strict_world, row["request_id"])),
        )
    finally:
        strict_world.close()

    promoted = World()
    try:
        _set_planning(promoted, True)
        task_id = "promote-task"
        session_id = "sess-promote"
        version = _publish_plan(session_id, task_id)["version"]
        first_agent = "a" + "a" * 16
        second_agent = "a" + "b" * 16
        _reserve_worker(promoted, session_id, task_id, version, "draft", "wf-p1", "prompt-p1", first_agent)
        _reserve_worker(promoted, session_id, task_id, version, "review", "wf-p2", "prompt-p2", second_agent)
        _worker_stop(promoted, session_id, first_agent, _replan_footer(task_id, "draft", version), promoted.opus_transcript)
        _worker_stop(promoted, session_id, second_agent, _replan_footer(task_id, "review", version), promoted.opus_transcript)
        open_rows = [
            row for row in rows(promoted, "requests")
            if row.get("plan_task_id") == task_id and row.get("state") == "REPLAN_PENDING"
        ]
        named = next(row for row in open_rows if "draft" in (row.get("context_json") or ""))
        other = next(row for row in open_rows if row["request_id"] != named["request_id"])
        drafts = promoted.home / ".claude" / "plans" / "router" / "drafts"
        drafts.mkdir(parents=True, exist_ok=True)
        draft_path = drafts / ("%s.json" % task_id)
        draft_path.write_text(json.dumps(_plan_body(task_id, version, ("fresh",))), encoding="utf-8")
        cli = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "router-plan"), "promote", task_id, str(draft_path), "--resolves", named["request_id"]],
            text=True, capture_output=True, env=promoted.env(), cwd=str(promoted.project), timeout=30, check=False,
        )
        after = {row["request_id"]: row for row in rows(promoted, "requests") if row.get("plan_task_id") == task_id}
        dup_ok, dup_code, _dup = arb.stage_start(session_id, task_id, version, "draft")
        pinned = {
            "CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL": "sonnet",
            "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS": "4",
        }
        fresh_id = "wf-fresh"
        _plant_workflow_runtime(promoted, session_id, fresh_id)
        fresh = _workflow_call(promoted, "log('fresh')", promoted.opus_transcript, session_id, fresh_id)
        fresh["tool_input"]["args"] = {"router_stage": {"task_id": task_id, "plan_version": version + 1, "stage_id": "fresh"}}
        fresh_kind, fresh_reason = verdict(run_gate(promoted, fresh, pinned))
        fresh_rows = _stage_rows(promoted, task_id, "fresh")
        old_rows = _stage_rows(promoted, task_id, "draft")
        resume_id = "wf-resume"
        _plant_workflow_runtime(promoted, session_id, resume_id)
        resume = _workflow_call(promoted, "log('fresh')", promoted.opus_transcript, session_id, resume_id)
        resume["tool_input"]["resumeFromRunId"] = "run-old"
        resume["tool_input"]["args"] = {
            "router_stage": {"task_id": task_id, "plan_version": version + 1, "stage_id": "other"},
        }
        resume_kind, resume_reason = verdict(run_gate(promoted, resume, pinned))
        suite.check(
            "R7",
            "promote resolves the named replan and supersedes the other",
            cli.returncode == 0 and len(open_rows) == 2
            and after[named["request_id"]]["state"] == "REPLAN_RESOLVED"
            and after[named["request_id"]]["decision"] == "PLAN_PROMOTED"
            and after[other["request_id"]]["state"] == "REPLAN_RESOLVED"
            and after[other["request_id"]]["decision"] == "superseded_by_v%s" % (version + 1),
            "code=%s stdout=%s stderr=%s" % (cli.returncode, cli.stdout[-300:], cli.stderr[-200:]),
        )
        suite.check(
            "R9",
            "a promoted plan cancels the yielded stage and the new version can start",
            dup_ok is False and dup_code == "ROUTER_PLAN_STALE"
            and old_rows and old_rows[0]["status"] == "cancelled"
            and fresh_kind == "allow"
            and fresh_rows and fresh_rows[0]["status"] == "started"
            and fresh_rows[0]["plan_version"] == version + 1
            and resume_kind == "deny" and "resumeFromRunId" in resume_reason
            and not _stage_rows(promoted, task_id, "other"),
            "dup=%s fresh=%s %s resume=%s %s" % (dup_code, fresh_kind, fresh_reason[:160], resume_kind, resume_reason[:160]),
        )
    finally:
        promoted.close()

    gated = World()
    try:
        task_id = "gate-task"
        session_id = "sess-gate"
        version = _publish_plan(session_id, task_id)["version"]
        script = "log('stage')"

        def workflow(tool_id, stage=None, **fields):
            payload = _workflow_call(gated, script, gated.sonnet_transcript, session_id, tool_id)
            if stage is not None:
                payload["tool_input"]["args"] = {"router_stage": stage}
            payload["tool_input"].update(fields)
            _plant_guard_attest(gated, session_id, tool_id)
            return payload

        stage = {"task_id": task_id, "plan_version": version, "stage_id": "draft"}
        first_kind, first_reason = verdict(run_gate(gated, workflow("wf-ok", stage)))
        started = _stage_rows(gated, task_id, "draft")
        dup_kind, dup_reason = verdict(run_gate(gated, workflow("wf-dup", stage)))
        stale = dict(stage)
        stale["plan_version"] = 99
        stale_kind, stale_reason = verdict(run_gate(gated, workflow("wf-stale", stale)))
        invalid = dict(stage)
        invalid["extra"] = "no"
        invalid_kind, invalid_reason = verdict(run_gate(gated, workflow("wf-bad", invalid)))
        opus_args = {"router_stage": {"task_id": task_id, "plan_version": version, "stage_id": "review"}, "note": "opus"}
        opus_payload = workflow("wf-opus")
        opus_payload["tool_input"]["args"] = opus_args
        opus_kind, opus_reason = verdict(run_gate(gated, opus_payload))
        plain_kind, plain_reason = verdict(run_gate(gated, workflow("wf-plain")))
        resume_payload = workflow("wf-resume", {"task_id": task_id, "plan_version": version, "stage_id": "notes"})
        resume_payload["tool_input"]["resumeFromRunId"] = "run-old"
        resume_kind, resume_reason = verdict(run_gate(gated, resume_payload))
        opus_session = "sess-gate-opus"
        missing_id = "wf-missing-mod"
        _plant_guard_attest(gated, opus_session, missing_id)
        missing = _workflow_call(gated, script, gated.opus_transcript, opus_session, missing_id)
        missing["tool_input"]["args"] = {"router_stage": stage}
        missing_kind, missing_reason = verdict(run_gate(gated, missing, {
            "CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL": "sonnet",
            "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS": "4",
        }))
        stage_ids = [row.get("workflow_tool_use_id") for row in rows(gated, "stages")]
        suite.check(
            "R8",
            "router_stage reserves only after the workflow is allowed",
            first_kind == "allow" and started and started[0]["status"] == "started"
            and dup_kind == "deny" and dup_reason.startswith("ROUTER_STAGE_DUPLICATE; run router-plan show %s" % task_id)
            and stale_kind == "deny" and stale_reason.startswith("ROUTER_PLAN_STALE; run router-plan show %s" % task_id)
            and invalid_kind == "deny" and invalid_reason == "ROUTER_STAGE_INVALID"
            and opus_kind == "deny" and opus_reason == "OPUS_WORKFLOW_DENIED reason=args"
            and plain_kind == "allow"
            and resume_kind == "deny" and "resumeFromRunId" in resume_reason
            and missing_kind == "deny"
            and stage_ids == ["wf-ok"],
            "first=%s %s dup=%s stale=%s invalid=%s opus=%s plain=%s %s resume=%s missing=%s ids=%s" % (
                first_kind, first_reason[:80], dup_reason[:80], stale_reason[:80], invalid_reason,
                opus_reason, plain_kind, plain_reason[:80], resume_reason[:80], missing_reason[:80], stage_ids,
            ),
        )
    finally:
        gated.close()

    expired = World()
    try:
        _set_planning(expired, True)
        task_id = "expire-task"
        session_id = "sess-expire-replan"
        version = _publish_plan(session_id, task_id)["version"]
        agent_id = "a" + "e" * 16
        _reserve_worker(expired, session_id, task_id, version, "draft", "wf-exp", "prompt-exp", agent_id)
        _worker_stop(expired, session_id, agent_id, _replan_footer(task_id, "draft", version), expired.opus_transcript)
        request_id = next(
            row["request_id"] for row in rows(expired, "requests") if row.get("plan_task_id") == task_id
        )
        import sqlite3
        db = expired.home / ".claude" / "logs" / "opus-arbitration" / "ledger.db"
        conn = sqlite3.connect(db)
        conn.execute("UPDATE requests SET expires=? WHERE request_id=?", (time.time() - 5, request_id))
        conn.execute(
            "UPDATE stages SET created=? WHERE task_id=? AND stage_id='draft'",
            (time.time() - 4000, task_id),
        )
        conn.commit()
        conn.close()
        arb.record_event("tick-replan", session_id=session_id)
        shown = arb.show_request(request_id) or {}
        status = subprocess.run(
            [sys.executable, str(CLI), "status"],
            text=True, capture_output=True, env=expired.env(), cwd=str(expired.project), timeout=30, check=False,
        )
        suite.check(
            "R11",
            "an expired replan is unresolved follow-through",
            shown.get("state") == "EXPIRED" and shown.get("decision") == "NO_FOLLOW_THROUGH"
            and request_id in status.stdout and "from_state=REPLAN_PENDING" in status.stdout,
            "state=%s decision=%s status=%s" % (shown.get("state"), shown.get("decision"), status.stdout[-400:]),
        )
        suite.check(
            "R13",
            "a stage open for more than an hour is marked stale",
            " stale" in status.stdout and "stage=draft" in status.stdout,
            status.stdout[-400:],
        )
    finally:
        expired.close()


class _Proc:
    def __init__(self, code, stdout, stderr):
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


def _load_gate():
    import importlib.util

    spec = importlib.util.spec_from_file_location("opus_gate_wp3b", ROOT / "opus-arbitration-gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_gate_inprocess(gate, world, payload, extra=None):
    import io

    saved = dict(os.environ)
    env = world.env(extra)
    old_in, old_out, old_err = sys.stdin, sys.stdout, sys.stderr
    sys.stdin = io.StringIO(json.dumps(payload))
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()
    os.environ.clear()
    os.environ.update(env)
    try:
        code = gate.main()
        return _Proc(code, sys.stdout.getvalue(), sys.stderr.getvalue())
    finally:
        sys.stdin, sys.stdout, sys.stderr = old_in, old_out, old_err
        os.environ.clear()
        os.environ.update(saved)


def _opus_replan(session_id, task_id, stage):
    return arb.register_candidate(
        session_id=session_id,
        task_id=task_id,
        trigger="WORKFLOW_WORKER_REQUEST",
        reason_code="PLAN_REVISION_REQUIRED",
        candidate_type="REPLAN_REQUEST",
        plan_task_id=task_id,
        key_override=arb.replan_candidate_key(task_id, 1, stage, "PLAN_REVISION_REQUIRED"),
        skip_judgment_quota=True,
        follow_state="REPLAN_PENDING",
        scope_digest=stage,
    )


def test_wp3b(suite):
    pinned = {
        "CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL": "sonnet",
        "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS": "4",
    }
    world = World()
    saved_start = arb.stage_start
    try:
        task_id = "cap-task"
        session_id = "sess-cap-stage"
        version = _publish_plan(
            session_id, task_id, steps=("draft", "review", "notes", "extra"),
        )["version"]
        script = "log('stage')"

        def launch(tool_id, stage_id):
            _plant_workflow_runtime(world, session_id, tool_id)
            payload = _workflow_call(world, script, world.opus_transcript, session_id, tool_id)
            payload["tool_input"]["args"] = {
                "router_stage": {"task_id": task_id, "plan_version": version, "stage_id": stage_id},
            }
            return payload

        first_kind, first_reason = verdict(run_gate(world, launch("wf-cap-a", "draft"), pinned))
        second_kind, second_reason = verdict(run_gate(world, launch("wf-cap-b", "review"), pinned))
        dup_kind, dup_reason = verdict(run_gate(world, launch("wf-cap-dup", "draft"), pinned))
        race_payload = launch("wf-cap-race", "race")
        gate = _load_gate()

        def deny_start(*_args, **_kwargs):
            return (False, "ROUTER_STAGE_DUPLICATE", None)

        arb.stage_start = deny_start
        try:
            race_kind, race_reason = verdict(_run_gate_inprocess(gate, world, race_payload, pinned))
        finally:
            arb.stage_start = saved_start
        race_row = next(
            (row for row in rows(world, "workflows") if row.get("tool_use_id") == "wf-cap-race"),
            None,
        )
        third_kind, third_reason = verdict(run_gate(world, launch("wf-cap-c", "notes"), pinned))
        fourth_kind, fourth_reason = verdict(run_gate(world, launch("wf-cap-d", "extra"), pinned))
        launched = [
            row for row in rows(world, "workflows")
            if row.get("session_id") == session_id and row.get("status") == "launched"
        ]
        dup_rows = [row for row in rows(world, "workflows") if row.get("tool_use_id") == "wf-cap-dup"]
        suite.check(
            "W3",
            "a stage-denied workflow does not spend a session slot",
            first_kind == "allow" and second_kind == "allow"
            and dup_kind == "deny" and dup_reason.startswith("ROUTER_STAGE_DUPLICATE")
            and not dup_rows
            and race_kind == "deny" and race_reason.startswith("ROUTER_STAGE_DUPLICATE")
            and isinstance(race_row, dict) and race_row.get("status") == "denied_stage"
            and not _stage_rows(world, task_id, "race")
            and third_kind == "allow"
            and fourth_kind == "deny" and "reason=session" in fourth_reason
            and len(launched) == 3,
            "1=%s 2=%s dup=%s %s race=%s %s third=%s %s fourth=%s %s launched=%s row=%s" % (
                first_kind, second_kind, dup_kind, dup_reason[:60],
                race_kind, race_reason[:60], third_kind, third_reason[:60],
                fourth_kind, fourth_reason[:60], len(launched),
                None if race_row is None else race_row.get("status"),
            ),
        )

        busy_id = "wf-busy"
        plain_id = "wf-plain-busy"
        busy_session = "sess-busy"
        _plant_guard_attest(world, busy_session, busy_id)
        _plant_guard_attest(world, busy_session, plain_id)
        busy_payload = _workflow_call(world, script, world.sonnet_transcript, busy_session, busy_id)
        busy_payload["tool_input"]["args"] = {
            "router_stage": {"task_id": task_id, "plan_version": version, "stage_id": "extra"},
        }
        plain_payload = _workflow_call(world, script, world.sonnet_transcript, busy_session, plain_id)

        def boom(*_args, **_kwargs):
            raise arb.GateBusy()

        arb.stage_start = boom
        try:
            busy_kind, busy_reason = verdict(_run_gate_inprocess(gate, world, busy_payload))
            plain_kind, plain_reason = verdict(_run_gate_inprocess(gate, world, plain_payload))
        finally:
            arb.stage_start = saved_start
        suite.check(
            "W3",
            "a stage reservation error denies and leaves no stage row",
            busy_kind == "deny" and busy_reason == "ROUTER_STAGE_BUSY"
            and not _stage_rows(world, task_id, "extra")
            and plain_kind == "allow",
            "busy=%s %s plain=%s %s" % (busy_kind, busy_reason[:120], plain_kind, plain_reason[:120]),
        )
    finally:
        arb.stage_start = saved_start
        world.close()

    quota = World()
    try:
        _set_planning(quota, True, judgments=2)
        session_id = "sess-quota-replan"
        task_id = "quota-task"
        made = [_opus_replan(session_id, task_id, stage) for stage in ("draft", "review")]
        resolved = arb.resolve_open_replans(task_id, "PLAN_PROMOTED")
        replans = [
            row for row in rows(quota, "requests")
            if row.get("candidate_type") == "REPLAN_REQUEST" and row.get("plan_task_id") == task_id
        ]
        later = arb.register_candidate(
            session_id=session_id,
            task_id=task_id,
            trigger="T02",
            reason_code="UNAPPROVED_OPUS",
            scope_digest="after-replans",
        )
        promoted = None
        if later.get("ok") and isinstance(later.get("request"), dict):
            promoted = arb.promote_pending(later["request"]["request_id"])
        suite.check(
            "W3",
            "two resolved Opus replans still leave a judgment",
            all(item.get("ok") for item in made) and resolved == 2
            and len(replans) == 2
            and all(
                row.get("state") == "REPLAN_RESOLVED"
                and row.get("packet_digest") is None
                and row.get("judge_tool_use_id") is None
                for row in replans
            )
            and later.get("ok") is True
            and isinstance(promoted, dict) and promoted.get("state") == "JUDGE_PENDING",
            "made=%s resolved=%s later=%s promoted=%s" % (
                [(item.get("ok"), item.get("reason")) for item in made],
                resolved,
                later.get("reason"),
                None if not isinstance(promoted, dict) else promoted.get("state"),
            ),
        )
    finally:
        quota.close()

    counted = World()
    try:
        _set_planning(counted, True, judgments=1)
        session_id = "sess-sonnet-quota"
        task_id = "sonnet-quota"
        made = arb.register_candidate(
            session_id=session_id,
            task_id=task_id,
            trigger="WORKFLOW_WORKER_REQUEST",
            reason_code="PLAN_REVISION_REQUIRED",
            candidate_type="REPLAN_REQUEST",
            plan_task_id=task_id,
            key_override=arb.replan_candidate_key(task_id, 1, "draft", "PLAN_REVISION_REQUIRED"),
            scope_digest="sonnet-draft",
        )
        promoted = None
        if made.get("ok") and isinstance(made.get("request"), dict):
            promoted = arb.promote_pending(made["request"]["request_id"])
        blocked = arb.register_candidate(
            session_id=session_id,
            task_id=task_id,
            trigger="T02",
            reason_code="UNAPPROVED_OPUS",
            scope_digest="after-sonnet-replan",
        )
        suite.check(
            "W3",
            "a Sonnet replan that reached the judge still counts",
            made.get("ok") is True
            and isinstance(promoted, dict)
            and promoted.get("state") == "JUDGE_PENDING"
            and promoted.get("packet_digest")
            and blocked.get("ok") is False
            and blocked.get("reason") == "QUOTA_EXHAUSTED",
            "made=%s promoted=%s blocked=%s" % (
                made.get("reason"),
                None if not isinstance(promoted, dict) else promoted.get("state"),
                blocked.get("reason"),
            ),
        )
    finally:
        counted.close()

    linked = World()
    try:
        _set_planning(linked, True)
        session_id = "sess-corr"
        done = "done-task"
        old = "old-task"
        done_version = _publish_plan(session_id, done)["version"]
        old_version = _publish_plan(session_id, old)["version"]
        done_agent = "a" + "d" * 16
        old_agent = "a" + "e" * 16
        done_ready = _reserve_worker(
            linked, session_id, done, done_version, "draft", "wf-done", "prompt-done", done_agent,
        )
        old_ready = _reserve_worker(
            linked, session_id, old, old_version, "draft", "wf-old", "prompt-old", old_agent,
        )
        finished = arb.stage_finish(done, done_version, "draft", "completed")
        superseded = _publish_plan(session_id, old, base_version=old_version)
        done_stop = _worker_stop(
            linked, session_id, done_agent,
            _replan_footer(done, "draft", done_version, unresolved=["finished stage"]),
            linked.opus_transcript,
        )
        old_stop = _worker_stop(
            linked, session_id, old_agent,
            _replan_footer(old, "draft", old_version, unresolved=["superseded stage"]),
            linked.opus_transcript,
        )
        events = (
            linked.home / ".claude" / "logs" / "opus-arbitration" / "events.jsonl"
        ).read_text(encoding="utf-8")
        requests = rows(linked, "requests")

        def kinds_for(task):
            return [
                row.get("candidate_type") for row in requests
                if row.get("task_id") == task or row.get("plan_task_id") == task
            ]

        done_stage = _stage_rows(linked, done, "draft")
        old_stage = _stage_rows(linked, old, "draft")
        suite.check(
            "W3",
            "a finished stage does not correlate",
            done_ready and finished is True and done_stop.returncode == 0
            and "replan_uncorrelated" in events
            and "REPLAN_REQUEST" not in kinds_for(done)
            and "EXECUTOR_ESCALATION" in kinds_for(done)
            and done_stage and done_stage[0]["status"] == "completed",
            "kinds=%s status=%s" % (
                kinds_for(done),
                None if not done_stage else done_stage[0].get("status"),
            ),
        )
        suite.check(
            "W3",
            "a superseded stage does not correlate",
            old_ready and superseded["version"] == old_version + 1
            and old_stop.returncode == 0
            and events.count("replan_uncorrelated") >= 2
            and "REPLAN_REQUEST" not in kinds_for(old)
            and "EXECUTOR_ESCALATION" in kinds_for(old)
            and old_stage and old_stage[0]["status"] == "cancelled"
            and old_stage[0]["plan_version"] == old_version,
            "kinds=%s status=%s version=%s" % (
                kinds_for(old),
                None if not old_stage else old_stage[0].get("status"),
                None if not old_stage else old_stage[0].get("plan_version"),
            ),
        )
    finally:
        linked.close()


def _plant_open(session_id, task_id, state):
    """A live request in this state. Later plants must not see it as expired."""
    made = arb.register_candidate(
        session_id=session_id,
        task_id=task_id,
        trigger="T02",
        reason_code="UNAPPROVED_OPUS",
        scope_digest=task_id,
        summary="stale",
    )
    request_id = made["request"]["request_id"] if made.get("ok") else ""
    if state == "JUDGE_PENDING" and request_id:
        arb.promote_pending(request_id)
    elif state == "REPLAN_PENDING" and request_id:
        arb.transition_to(request_id, "REPLAN_PENDING")
    return made.get("ok") is True, request_id


def _expire_requests(request_ids):
    """Move expires into the past without running the sweep."""
    import sqlite3

    db = os.environ["HOME"] + "/.claude/logs/opus-arbitration/ledger.db"
    conn = sqlite3.connect(db)
    for request_id in request_ids:
        conn.execute(
            "UPDATE requests SET expires=? WHERE request_id=?",
            (time.time() - 30, request_id),
        )
    conn.commit()
    conn.close()


def _follow_event(world, request_id, from_state):
    found = [
        row for row in rows(world, "events")
        if row.get("event") == "no_follow_through" and row.get("request_id") == request_id
    ]
    if not found:
        return False
    detail = found[-1].get("detail_json") or ""
    return '"from_state":"%s"' % from_state in detail


def test_read_sweep(suite):
    world = World()
    try:
        shown_ok, shown_id = _plant_open("sess-show", "show-task", "JUDGE_PENDING")
        _expire_requests([shown_id])
        shown = subprocess.run(
            [sys.executable, str(CLI), "show", shown_id],
            capture_output=True, text=True, env=world.env(), timeout=15, check=False,
        )
        try:
            body = json.loads(shown.stdout)
        except json.JSONDecodeError:
            body = {}
        suite.check(
            "T24",
            "show expires one JUDGE_PENDING row past its ttl",
            shown_ok and shown.returncode == 0
            and body.get("state") == "EXPIRED"
            and body.get("decision") == "NO_FOLLOW_THROUGH"
            and _follow_event(world, shown_id, "JUDGE_PENDING"),
            "exit=%s body=%s" % (shown.returncode, shown.stdout[:300]),
        )
        planted = []
        for state, session_id, task_id in (
            ("JUDGE_PENDING", "sess-st-judge", "st-judge"),
            ("CANDIDATE", "sess-st-cand", "st-cand"),
            ("REPLAN_PENDING", "sess-st-replan", "st-replan"),
        ):
            ok, request_id = _plant_open(session_id, task_id, state)
            planted.append((ok, state, request_id))
        _expire_requests([request_id for _ok, _state, request_id in planted])
        before = {row["request_id"]: row["state"] for row in rows(world, "requests")}
        status = subprocess.run(
            [sys.executable, str(CLI), "status"],
            capture_output=True, text=True, env=world.env(), timeout=15, check=False,
        )
        after = {row["request_id"]: row for row in rows(world, "requests")}
        expired_ok = True
        detail = []
        for ok, state, request_id in planted:
            row = after.get(request_id) or {}
            line_ok = (
                request_id in status.stdout
                and "state=EXPIRED" in status.stdout
                and "decision=NO_FOLLOW_THROUGH" in status.stdout
            )
            # The request's own recent line carries both fields.
            own = any(
                request_id in line and "state=EXPIRED" in line and "decision=NO_FOLLOW_THROUGH" in line
                for line in status.stdout.splitlines()
            )
            good = (
                ok and before.get(request_id) == state
                and row.get("state") == "EXPIRED"
                and row.get("decision") == "NO_FOLLOW_THROUGH"
                and own and line_ok
                and _follow_event(world, request_id, state)
                and ("from_state=%s" % state) in status.stdout
            )
            if not good:
                expired_ok = False
            detail.append("%s ok=%s before=%s after=%s own=%s" % (
                state, ok, before.get(request_id), row.get("state"), own,
            ))
        suite.check(
            "T24",
            "one status call expires JUDGE_PENDING, CANDIDATE, and REPLAN_PENDING",
            status.returncode == 0 and expired_ok,
            "exit=%s %s" % (status.returncode, "; ".join(detail)),
        )
        plan_ok, plan_id = _plant_open("sess-plan-show", "plan-show", "CANDIDATE")
        _expire_requests([plan_id])
        plan_show = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "router-plan"), "show", "missing-task"],
            capture_output=True, text=True, env=world.env(), timeout=15, check=False,
        )
        plan_row = next((row for row in rows(world, "requests") if row["request_id"] == plan_id), {})
        suite.check(
            "T24",
            "router-plan show sweeps before it reads",
            plan_ok and plan_row.get("state") == "EXPIRED"
            and plan_row.get("decision") == "NO_FOLLOW_THROUGH"
            and _follow_event(world, plan_id, "CANDIDATE")
            and "sweep skipped" not in plan_show.stdout,
            "exit=%s state=%s" % (plan_show.returncode, plan_row.get("state")),
        )
    finally:
        world.close()

    locked = World()
    holder = None
    status_proc = None
    try:
        ok, request_id = _plant_open("sess-busy", "busy-task", "JUDGE_PENDING")
        _expire_requests([request_id])
        db = locked.home / ".claude" / "logs" / "opus-arbitration" / "ledger.db"
        holder = subprocess.Popen(
            [
                sys.executable, "-c",
                "import sqlite3,sys\n"
                "conn=sqlite3.connect(sys.argv[1], isolation_level=None, timeout=60)\n"
                "conn.execute('BEGIN IMMEDIATE')\n"
                "print('locked', flush=True)\n"
                "sys.stdin.readline()\n"
                "conn.execute('ROLLBACK')\n",
                str(db),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        ready = holder.stdout.readline()
        status_proc = subprocess.Popen(
            [sys.executable, str(CLI), "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=locked.env(),
            cwd=str(locked.project),
        )
        chunks = []

        def _read_status():
            while True:
                line = status_proc.stdout.readline()
                if line == "":
                    break
                chunks.append(line)
                if "sweep skipped: ledger busy" in line:
                    try:
                        holder.stdin.write("\n")
                        holder.stdin.flush()
                    except Exception:
                        pass

        import threading
        reader = threading.Thread(target=_read_status)
        reader.start()
        reader.join(timeout=20)
        if status_proc.poll() is None:
            try:
                holder.stdin.write("\n")
                holder.stdin.flush()
            except Exception:
                pass
            try:
                status_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                status_proc.kill()
                status_proc.wait(timeout=5)
        else:
            status_proc.wait(timeout=5)
        reader.join(timeout=5)
        stdout = "".join(chunks)
        stderr = ""
        if status_proc.stderr is not None:
            stderr = status_proc.stderr.read()
        kept = next((row for row in rows(locked, "requests") if row["request_id"] == request_id), {})
        suite.check(
            "T24",
            "a busy ledger prints the skip line and status exits 0",
            ok and ready.strip() == "locked"
            and status_proc.returncode == 0
            and "sweep skipped: ledger busy" in stdout
            and kept.get("state") == "JUDGE_PENDING",
            "exit=%s state=%s stdout=%s stderr=%s" % (
                None if status_proc is None else status_proc.returncode,
                kept.get("state"), stdout[:300], stderr[:200],
            ),
        )
    finally:
        if holder is not None and holder.poll() is None:
            try:
                holder.stdin.write("\n")
                holder.stdin.flush()
            except Exception:
                pass
            holder.kill()
            holder.wait(timeout=5)
        if status_proc is not None and status_proc.poll() is None:
            status_proc.kill()
            status_proc.wait(timeout=5)
        locked.close()


def main():
    suite = Suite()
    test_resolution(suite)
    test_gate_edges(suite)
    test_crash_and_ledger(suite)
    test_judge_and_grant(suite)
    test_judge_rejections(suite)
    test_race(suite)
    test_send_message(suite)
    test_quotas_and_machine(suite)
    test_triggers(suite)
    test_cli(suite)
    test_packet_and_cli(suite)
    test_fuzz_and_source(suite)
    test_part2_hardening(suite)
    test_references(suite)
    test_v2_hardening(suite)
    test_hardening2(suite)
    test_strict_rules(suite)
    test_workflow_and_auto(suite)
    test_plan_ledger(suite)
    test_planner(suite)
    test_replan(suite)
    test_wp3b(suite)
    test_read_sweep(suite)
    suite.check(
        "REF",
        "gate output never contains permissionDecision allow",
        not any(_records_allow(text) for text in GATE_STDOUTS),
        "outputs=%s" % len(GATE_STDOUTS),
    )
    print("Tally: %s passed, %s failed" % (suite.passed, suite.failed))
    return 0 if suite.failed == 0 and suite.passed > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
