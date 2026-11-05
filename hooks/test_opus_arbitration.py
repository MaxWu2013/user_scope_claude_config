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
        _agent_md(agents / "opus-judge.md", "opus", "tools: []\n")
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
        project_judge.write_text("---\nmodel: opus\ntools: []\n---\n", encoding="utf-8")
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
        user_judge.write_text("---\nmodel: haiku\ntools: []\n---\n", encoding="utf-8")
        kind, reason = verdict(run_gate(world, agent_payload(
            world, subagent_type="opus-judge", model="opus", transcript=world.opus_transcript,
            session_id="sess-haiku-judge", tool_use_id="judge-haiku", description="Judge",
        )))
        suite.check("H4", "judge model other than opus is invalid", kind == "deny" and reason == "JUDGE_DEFINITION_INVALID", reason)
        user_judge.write_text("---\nmodel: opus\ntools: [Read]\n---\n", encoding="utf-8")
        kind, reason = verdict(run_gate(world, agent_payload(
            world, subagent_type="opus-judge", model="opus", transcript=world.opus_transcript,
            session_id="sess-tools", tool_use_id="judge-tools", description="Judge",
        )))
        suite.check("H4", "judge tools other than [] are invalid", kind == "deny" and reason == "JUDGE_DEFINITION_INVALID", reason)
        user_judge.write_text("---\nmodel: opus\n---\n", encoding="utf-8")
        kind, reason = verdict(run_gate(world, agent_payload(
            world, subagent_type="opus-judge", model="opus", transcript=world.opus_transcript,
            session_id="sess-notools", tool_use_id="judge-notools", description="Judge",
        )))
        suite.check("H4", "judge without tools is invalid", kind == "deny" and reason == "JUDGE_DEFINITION_INVALID", reason)
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
        user_judge.write_text("---\nmodel: opus\ntools : []\n---\n", encoding="utf-8")
        kind, reason = verdict(run_gate(world, agent_payload(
            world, subagent_type="opus-judge", model="opus", transcript=world.opus_transcript,
            session_id="sess-tools-space", tool_use_id="judge-tools-space", description="Judge",
        )))
        suite.check(
            "K1",
            "judge tools with odd spacing is an empty list",
            kind == "deny" and reason == "JUDGE_NOT_PENDING",
            reason[:160],
        )
        user_judge.write_text("---\nmodel: opus\ntools : [ ]\n---\n", encoding="utf-8")
        kind, reason = verdict(run_gate(world, agent_payload(
            world, subagent_type="opus-judge", model="opus", transcript=world.opus_transcript,
            session_id="sess-tools-gap", tool_use_id="judge-tools-gap", description="Judge",
        )))
        suite.check(
            "K1",
            "judge tools that are not exactly [] are invalid",
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
        user_judge.write_text("---\nmodel: opus # pinned\ntools: [] # none\n---\n", encoding="utf-8")
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
