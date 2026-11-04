#!/usr/bin/env python3
"""session-cost: dedupe, agent tree, pins, advisor split, ccusage join, CLI guards."""

import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent
BIN = ROOT / "bin" / "session-cost"
SID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def load_module():
    loader = importlib.machinery.SourceFileLoader("session_cost_under_test", str(BIN))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


MOD = load_module()


class Tally:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, name, ok, detail=""):
        status = "PASS" if ok else "FAIL"
        print(f"{name:<78} {status}")
        if ok:
            self.passed += 1
        else:
            self.failed += 1
            if detail:
                print(f"      {detail}")

    def finish(self):
        print(f"Tally: {self.passed} passed, {self.failed} failed")
        return 0 if self.failed == 0 and self.passed > 0 else 1


def write_jsonl(path: Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    chunks = []
    for record in records:
        if isinstance(record, str):
            chunks.append(record if record.endswith("\n") else record + "\n")
        else:
            chunks.append(json.dumps(record) + "\n")
    path.write_text("".join(chunks), encoding="utf-8")


def agent_md(path: Path, model: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {path.stem}\nmodel: {model}\n---\nbody\n", encoding="utf-8")


def usage(inp, out, iterations=None):
    body = {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    if iterations is not None:
        body["iterations"] = iterations
    return body


def iteration(kind, inp, out, model=None):
    item = {
        "type": kind,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    if model:
        item["model"] = model
    return item


def assistant(message_id, model, timestamp, usage_body, content, request_id, cwd=None, advisor_model=None):
    record = {
        "type": "assistant",
        "timestamp": timestamp,
        "requestId": request_id,
        "message": {
            "id": message_id,
            "model": model,
            "role": "assistant",
            "content": content,
            "usage": usage_body,
        },
    }
    if cwd:
        record["cwd"] = cwd
    if advisor_model:
        record["advisorModel"] = advisor_model
    return record


def stub_script(path: Path) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, time\n"
        "marker = os.environ.get('CCUSAGE_STUB_MARKER')\n"
        "if marker:\n"
        "    open(marker, 'w', encoding='utf-8').write('ran')\n"
        "code = os.environ.get('CCUSAGE_STUB_EXIT')\n"
        "if code:\n"
        "    sys.stderr.write('stub failed\\n')\n"
        "    sys.exit(int(code))\n"
        "delay = os.environ.get('CCUSAGE_STUB_SLEEP')\n"
        "if delay:\n"
        "    time.sleep(float(delay))\n"
        "sys.stdout.write(open(os.environ['CCUSAGE_STUB_JSON'], encoding='utf-8').read())\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def pricing_file(path: Path) -> None:
    models = {}
    for name in ("claude-opus-5-5", "claude-haiku-5-5", "claude-sonnet-5-5"):
        models[name] = {
            "input_cost_per_token": 1,
            "output_cost_per_token": 1,
            "cache_creation_input_token_cost": 1,
            "cache_creation_input_token_cost_above_1hr": 2,
            "cache_read_input_token_cost": 1,
        }
    path.write_text(json.dumps(models), encoding="utf-8")


def ccusage_payload(path: Path, entries, total_cost, total_tokens) -> None:
    path.write_text(
        json.dumps({
            "sessionId": SID,
            "totalCost": total_cost,
            "totalTokens": total_tokens,
            "entries": entries,
        }),
        encoding="utf-8",
    )


def entry(timestamp, model, inp, out, cost):
    return {
        "timestamp": timestamp,
        "model": model,
        "inputTokens": inp,
        "outputTokens": out,
        "cacheCreationTokens": 0,
        "cacheReadTokens": 0,
        "costUSD": cost,
    }


def run(args, config=None, env=None):
    """Call main() in-process. config injects paths; env is only for the ccusage stub."""
    saved = {}
    if env:
        for key, value in env.items():
            saved[key] = os.environ.get(key)
            os.environ[key] = value
    out = io.StringIO()
    err = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        try:
            code = MOD.main(["session-cost", *args], config)
        except SystemExit as exc:
            code = exc.code
            if code is None:
                code = 0
            elif not isinstance(code, int):
                code = 1
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        for key, previous in saved.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
    return SimpleNamespace(returncode=code, stdout=out.getvalue(), stderr=err.getvalue())


def files_under(root: Path):
    found = []
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            found.append(os.path.join(dirpath, name))
    return sorted(found)


def agent_by_id(report, agent_id):
    for agent in report["agents"]:
        if agent["id"] == agent_id:
            return agent
    return None


def build_core(root: Path, home: Path, project: Path):
    agent_md(home / ".claude" / "agents" / "pinned-reader.md", "haiku")
    agent_md(home / ".claude" / "agents" / "general-purpose.md", "haiku")
    agent_md(home / ".claude" / "agents" / "mismatch-agent.md", "sonnet")
    agent_md(project / ".claude" / "agents" / "mismatch-agent.md", "opus")
    session = root / "proj-a" / SID
    sub = session / "subagents"
    main_usage = usage(10, 1, [iteration("message", 10, 1)])
    main_content_tools = [
        {
            "type": "tool_use",
            "id": "toolu_child",
            "name": "Agent",
            "input": {"subagent_type": "pinned-reader", "model": "haiku", "description": "read"},
        },
        {
            "type": "tool_use",
            "id": "toolu_bi",
            "name": "Agent",
            "input": {"subagent_type": "general-purpose", "description": "built in"},
        },
        {
            "type": "tool_use",
            "id": "toolu_missing",
            "name": "Agent",
            "input": {"subagent_type": "file-writer", "description": "gone"},
        },
    ]
    write_jsonl(
        root / "proj-a" / f"{SID}.jsonl",
        [
            {
                "type": "attachment",
                "attachment": {
                    "type": "model",
                    "identity": {"modelId": "claude-opus-5-5", "marketingName": "Opus 5.5"},
                },
            },
            assistant(
                "msg-main",
                "claude-opus-5-5",
                "2026-01-01T00:00:00.000Z",
                main_usage,
                [{"type": "thinking"}],
                "req-main",
            ),
            assistant(
                "msg-main",
                "claude-opus-5-5",
                "2026-01-01T00:00:01.000Z",
                main_usage,
                [{"type": "text", "text": "same call"}],
                "req-main",
            ),
            assistant(
                "msg-main",
                "claude-opus-5-5",
                "2026-01-01T00:00:02.000Z",
                main_usage,
                main_content_tools,
                "req-main",
            ),
            "{not json",
            assistant(
                "msg-empty",
                "claude-opus-5-5",
                "2026-01-01T00:00:09.000Z",
                None,
                [{"type": "text", "text": "no usage"}],
                "req-empty",
            ),
        ],
    )
    # The helper always inserts usage. Rewrite the empty one by patching the file
    # is unnecessary if usage None is omitted. assistant() sets usage to None.
    child_usage = usage(
        5,
        6,
        [
            iteration("message", 5, 6),
            iteration("advisor_message", 7, 8, "claude-opus-5-5"),
        ],
    )
    child_content = [
        {"type": "server_tool_use", "id": "srv_1", "name": "advisor", "input": {}},
        {
            "type": "tool_use",
            "id": "toolu_leaf",
            "name": "Agent",
            "input": {"subagent_type": "mismatch-agent", "description": "leaf"},
        },
    ]
    write_jsonl(
        sub / "agent-child.jsonl",
        [
            {
                "type": "api-request-shape",
                "shape": {"tools": [{"type": "advisor_20260301", "name": "advisor", "model": "claude-opus-5-5"}]},
            },
            assistant(
                "msg-child",
                "claude-haiku-5-5",
                "2026-01-01T00:01:00.000Z",
                child_usage,
                child_content,
                "req-child",
                advisor_model="claude-opus-5-5",
            ),
            assistant(
                "msg-child",
                "claude-haiku-5-5",
                "2026-01-01T00:01:01.000Z",
                child_usage,
                [{"type": "text", "text": "chunk"}],
                "req-child",
                advisor_model="claude-opus-5-5",
            ),
            {
                "type": "user",
                "timestamp": "2026-01-01T00:01:02.000Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "advisor_tool_result", "tool_use_id": "srv_1", "content": "ok"}],
                },
            },
        ],
    )
    (sub / "agent-child.meta.json").write_text(
        json.dumps({
            "agentType": "pinned-reader",
            "description": "child reader",
            "toolUseId": "toolu_child",
            "spawnDepth": 1,
            "isBuiltIn": False,
            "launchedWithDefaults": False,
            "pickedEffortModel": "claude-opus-5-5",
        }),
        encoding="utf-8",
    )
    write_jsonl(
        sub / "agent-leaf.jsonl",
        [
            assistant(
                "msg-leaf",
                "claude-sonnet-5-5",
                "2026-01-01T00:02:00.000Z",
                usage(3, 3, [iteration("message", 3, 3)]),
                [{"type": "text", "text": "leaf"}],
                "req-leaf",
                cwd=str(project),
            ),
        ],
    )
    (sub / "agent-leaf.meta.json").write_text(
        json.dumps({
            "agentType": "mismatch-agent",
            "description": "depth two",
            "toolUseId": "toolu_leaf",
            "spawnDepth": 2,
            "isBuiltIn": False,
            "pickedEffortModel": "claude-haiku-5-5",
        }),
        encoding="utf-8",
    )
    write_jsonl(
        sub / "agent-builtin.jsonl",
        [
            assistant(
                "msg-bi",
                "claude-opus-5-5",
                "2026-01-01T00:03:00.000Z",
                usage(4, 4, [iteration("message", 4, 4)]),
                [{"type": "text", "text": "built in"}],
                "req-bi",
            ),
        ],
    )
    (sub / "agent-builtin.meta.json").write_text(
        json.dumps({
            "agentType": "general-purpose",
            "description": "built in",
            "toolUseId": "toolu_bi",
            "spawnDepth": 1,
            "isBuiltIn": True,
            "launchedWithDefaults": True,
            "pickedEffortModel": "claude-opus-5-5",
        }),
        encoding="utf-8",
    )
    (sub / "agent-orphan.meta.json").write_text(
        json.dumps({"agentType": "pinned-reader", "toolUseId": "toolu_orphan", "spawnDepth": 1}),
        encoding="utf-8",
    )
    write_jsonl(
        sub / "agent-ghost.jsonl",
        [{"type": "assistant", "timestamp": "2026-01-01T00:04:00.000Z", "message": {"id": "msg-ghost", "model": "x", "content": []}}],
    )


def base_config(root: Path, home: Path, stub: Path, prices: Path, timeout=None):
    config = {
        "projects_root": str(root),
        "home": str(home),
        "ccusage": str(stub),
        "pricing_cache": str(prices),
    }
    if timeout is not None:
        config["ccusage_timeout"] = timeout
    return config


def stub_env(payload: Path, marker: Path | None = None):
    env = {"CCUSAGE_STUB_JSON": str(payload)}
    if marker is not None:
        env["CCUSAGE_STUB_MARKER"] = str(marker)
    return env


def test_core(tally: Tally):
    root = Path(tempfile.mkdtemp(prefix="session-cost-core-"))
    home = root / "home"
    project = root / "leaf-project"
    stub = root / "ccusage"
    payload = root / "payload.json"
    marker = root / "marker"
    prices = root / "prices.json"
    stub_script(stub)
    pricing_file(prices)
    build_core(root / "projects", home, project)
    # assistant() with usage None still writes "usage": null. The scanner
    # treats a non-dict usage as missing. null is not a dict. Good.
    ccusage_payload(
        payload,
        [
            entry("2026-01-01T00:00:00.000Z", "claude-opus-5-5", 10, 1, 0.5),
            entry("2026-01-01T00:01:00.000Z", "claude-haiku-5-5", 5, 6, 1.25),
            entry("2026-01-01T00:02:00.000Z", "claude-sonnet-5-5", 3, 3, 0.25),
            entry("2026-01-01T00:03:00.000Z", "claude-opus-5-5", 4, 4, 0.1),
        ],
        2.1,
        36,
    )
    config = base_config(root / "projects", home, stub, prices)
    env = stub_env(payload, marker)
    before = files_under(root / "projects") + files_under(home)
    completed = run([SID, "--json"], config, env)
    after = files_under(root / "projects") + files_under(home)
    tally.check("core exits 0", completed.returncode == 0 and completed.stderr == "", completed.stderr + completed.stdout[:400])
    if completed.returncode != 0:
        return
    report = json.loads(completed.stdout)
    tally.check("core json is one document", isinstance(report, dict) and report["sessionId"] == SID)
    tally.check(
        "deduped calls are 4, main tokens not tripled",
        report["coverage"]["calls"] == 4
        and agent_by_id(report, "main")["executor"]["tokens"]["input"] == 10
        and agent_by_id(report, "main")["executor"]["calls"] == 1,
        str(report["coverage"]),
    )
    child = agent_by_id(report, "child")
    leaf = agent_by_id(report, "leaf")
    builtin = agent_by_id(report, "builtin")
    tally.check(
        "depth-2 child linked by toolUseId",
        child["parent"] == "main"
        and child["spawnDepth"] == 1
        and leaf["parent"] == "child"
        and leaf["spawnDepth"] == 2
        and leaf["toolUseId"] == "toolu_leaf"
        and child["spawn"]["subagent_type"] == "pinned-reader"
        and child["spawn"]["model"] == "haiku",
        f"child={child and child['parent']} leaf={leaf and (leaf['parent'], leaf['spawnDepth'])}",
    )
    tally.check(
        "pin_held, MISMATCH, unpinned",
        child["pin"] == "pin_held"
        and child["definition_model"] == "haiku"
        and leaf["pin"] == "MISMATCH"
        and leaf["definition_model"] == "opus"
        and builtin["pin"] == "unpinned"
        and builtin["isBuiltIn"] is True
        and agent_by_id(report, "main")["pin"] == "unpinned",
        f"child={child['pin']} leaf={leaf['pin']}/{leaf['definition_model']} bi={builtin['pin']}",
    )
    tally.check(
        "pickedEffortModel is recorded and is not the pin source",
        child["pickedEffortModel"] == "claude-opus-5-5" and child["pin"] == "pin_held",
    )
    events = {(event["type"], event["name"]) for event in child["advisor"]["events"]}
    tally.check(
        "advisor server_tool_use and advisor_tool_result",
        ("server_tool_use", "advisor") in events and ("advisor_tool_result", None) in events,
        str(events),
    )
    tally.check(
        "iterations: top-level equals executor, advisor tokens separate",
        child["executor"]["tokens"]["input"] == 5
        and child["executor"]["tokens"]["output"] == 6
        and child["advisor"]["tokens"]["input"] == 7
        and child["advisor"]["tokens"]["output"] == 8
        and report["empirical"]["top_equals_executor"] == 4
        and report["empirical"]["top_equals_all"] == 3
        and report["empirical"]["advisor_iterations"] == 1,
        str(child["advisor"]["tokens"]),
    )
    tally.check(
        "advisor cost from pricing cache and combined adds it",
        child["advisor"]["cost"] == 15
        and report["totals"]["advisor_cost"] == 15
        and report["totals"]["executor_cost"] == 2.1
        and report["totals"]["combined_cost"] == 17.1
        and child["executor"]["cost"] == 1.25
        and child["executor"]["cost_label"] == "ccusage"
        and report["totals"]["executor_cost_scale"] is None
        and report["totals"]["ccusage_includes_advisor_tokens"] is False,
        str(report["totals"]),
    )
    warning_text = "\n".join(report["warnings"])
    tally.check("malformed line warned", "malformed line" in warning_text, warning_text)
    tally.check("assistant without usage warned", "without usage" in warning_text, warning_text)
    tally.check("missing sub-agent transcript warned", "toolu_missing" in warning_text and "no sub-agent transcript" in warning_text, warning_text)
    tally.check("meta without jsonl warned", "meta.json without jsonl: orphan" in warning_text, warning_text)
    tally.check("jsonl without meta warned", "jsonl without meta.json: ghost" in warning_text, warning_text)
    tally.check("tool does not write", before == after, f"added={set(after) - set(before)}")
    human = run([SID], config, env)
    tally.check(
        "human report names pins, coverage, and stays within 400 lines",
        human.returncode == 0
        and "pin=pin_held" in human.stdout
        and "pin=MISMATCH" in human.stdout
        and "pin=unpinned" in human.stdout
        and "Coverage" in human.stdout
        and "pickedEffortModel" in human.stdout
        and len(human.stdout.splitlines()) <= 400,
        f"lines={len(human.stdout.splitlines())}",
    )


def test_top_equals_all(tally: Tally):
    root = Path(tempfile.mkdtemp(prefix="session-cost-all-"))
    home = root / "home"
    home.mkdir()
    projects = root / "projects"
    stub = root / "ccusage"
    payload = root / "payload.json"
    prices = root / "prices.json"
    stub_script(stub)
    pricing_file(prices)
    top = usage(
        3,
        0,
        [iteration("message", 1, 0), iteration("advisor_message", 2, 0, "claude-opus-5-5")],
    )
    write_jsonl(
        projects / "proj" / f"{SID}.jsonl",
        [
            assistant(
                "msg-1",
                "claude-opus-5-5",
                "2026-01-02T00:00:00.000Z",
                top,
                [{"type": "server_tool_use", "id": "srv", "name": "advisor"}],
                "req-1",
                advisor_model="claude-opus-5-5",
            )
        ],
    )
    ccusage_payload(payload, [entry("2026-01-02T00:00:00.000Z", "claude-opus-5-5", 3, 0, 9)], 9, 3)
    config = base_config(projects, home, stub, prices)
    env = stub_env(payload, root / "marker")
    completed = run([SID, "--json"], config, env)
    tally.check("top==all exits 0", completed.returncode == 0, completed.stderr)
    if completed.returncode != 0:
        return
    report = json.loads(completed.stdout)
    main = agent_by_id(report, "main")
    text = "\n".join(report["warnings"])
    tally.check(
        "top-level includes advisor iterations and is not double counted",
        main["executor"]["tokens"]["input"] == 1
        and main["advisor"]["tokens"]["input"] == 2
        and report["empirical"]["top_equals_all"] == 1
        and report["empirical"]["top_equals_executor"] == 0
        and report["totals"]["ccusage_includes_advisor_tokens"] is True
        and report["totals"]["combined_cost"] == 9
        and report["totals"]["advisor_cost"] == 2
        and "not added on top" in text
        and "does not equal the non-advisor" in text
        and "differ from ccusage totalTokens" in text,
        text + " " + str(report["totals"]),
    )


def test_no_iterations(tally: Tally):
    root = Path(tempfile.mkdtemp(prefix="session-cost-none-"))
    projects = root / "projects"
    write_jsonl(
        projects / "proj" / f"{SID}.jsonl",
        [
            {
                "type": "api-request-shape",
                "shape": {"tools": [{"type": "advisor_20260301", "name": "advisor"}]},
            },
            assistant(
                "msg-1",
                "claude-haiku-5-5",
                "2026-01-03T00:00:00.000Z",
                usage(1, 1),
                [{"type": "text", "text": "hi"}],
                "req-1",
            ),
        ],
    )
    (root / "home").mkdir()
    config = {
        "projects_root": str(projects),
        "home": str(root / "home"),
        "pricing_cache": str(root / "missing-prices.json"),
    }
    completed = run([SID, "--no-ccusage", "--json"], config)
    tally.check("no-iterations exits 0", completed.returncode == 0, completed.stderr)
    if completed.returncode != 0:
        return
    report = json.loads(completed.stdout)
    text = "\n".join(report["warnings"])
    tally.check(
        "says iterations are absent when advisor is configured",
        report["empirical"]["calls_with_iterations"] == 0
        and "iterations is absent" in text
        and "skipped with --no-ccusage" in text,
        text,
    )
    human = run([SID, "--no-ccusage"], config)
    tally.check(
        "human report says no call has iterations",
        "No API call in this session has message.usage.iterations." in human.stdout,
        human.stdout,
    )


def test_advisor_direction_warnings(tally: Tally):
    root = Path(tempfile.mkdtemp(prefix="session-cost-dir-"))
    projects = root / "projects"
    write_jsonl(
        projects / "calls" / f"{SID}.jsonl",
        [
            assistant(
                "msg-call",
                "claude-haiku-5-5",
                "2026-01-04T00:00:00.000Z",
                usage(1, 1, [iteration("message", 1, 1)]),
                [{"type": "server_tool_use", "id": "srv", "name": "advisor"}],
                "req-call",
            )
        ],
    )
    (root / "home").mkdir()
    config = {
        "projects_root": str(projects),
        "home": str(root / "home"),
        "pricing_cache": str(root / "missing-prices.json"),
    }
    called = json.loads(run([SID, "--no-ccusage", "--json"], config).stdout)
    tally.check(
        "advisor call without advisor_message warns",
        any("advisor calls seen with no advisor_message" in item for item in called["warnings"]),
        "\n".join(called["warnings"]),
    )
    write_jsonl(
        projects / "usage" / f"{OTHER}.jsonl",
        [
            assistant(
                "msg-rev",
                "claude-opus-5-5",
                "2026-01-04T00:00:00.000Z",
                usage(1, 0, [iteration("message", 1, 0), iteration("advisor_message", 4, 0, "claude-opus-5-5")]),
                [{"type": "text", "text": "no call block"}],
                "req-rev",
            )
        ],
    )
    reverse = json.loads(run([OTHER, "--no-ccusage", "--json"], config).stdout)
    tally.check(
        "advisor_message without a call warns",
        any("advisor_message usage seen with no advisor call" in item for item in reverse["warnings"]),
        "\n".join(reverse["warnings"]),
    )


def test_ccusage_failures(tally: Tally):
    root = Path(tempfile.mkdtemp(prefix="session-cost-cc-"))
    projects = root / "projects"
    home = root / "home"
    home.mkdir()
    write_jsonl(
        projects / "proj" / f"{SID}.jsonl",
        [assistant("msg-1", "claude-haiku-5-5", "2026-01-05T00:00:00.000Z", usage(2, 2, [iteration("message", 2, 2)]), [{"type": "text", "text": "x"}], "req")],
    )
    stub = root / "ccusage"
    stub_script(stub)
    payload = root / "payload.json"
    ccusage_payload(payload, [], 0, 0)
    config = {
        "projects_root": str(projects),
        "home": str(home),
        "pricing_cache": str(root / "missing-prices.json"),
        "ccusage": str(root / "no-such-ccusage"),
    }
    env = {"CCUSAGE_STUB_JSON": str(payload)}
    missing = json.loads(run([SID, "--json"], config, env).stdout)
    tally.check(
        "missing ccusage warns and keeps scanner calls",
        missing["ccusage"] is None
        and missing["coverage"]["calls"] == 1
        and any("ccusage unavailable" in item for item in missing["warnings"]),
        "\n".join(missing["warnings"]),
    )
    config["ccusage"] = str(stub)
    env["CCUSAGE_STUB_EXIT"] = "1"
    failed = json.loads(run([SID, "--json"], config, env).stdout)
    tally.check(
        "ccusage failure warns",
        any("failed" in item for item in failed["warnings"]),
        "\n".join(failed["warnings"]),
    )
    env.pop("CCUSAGE_STUB_EXIT")
    env["CCUSAGE_STUB_SLEEP"] = "30"
    config["ccusage_timeout"] = 1
    timed = run([SID, "--json"], config, env)
    tally.check(
        "ccusage timeout warns",
        timed.returncode == 0 and "timed out" in timed.stdout,
        timed.stderr[:200] + timed.stdout[:300],
    )
    marker = root / "marker"
    env.pop("CCUSAGE_STUB_SLEEP")
    env["CCUSAGE_STUB_MARKER"] = str(marker)
    skipped = run([SID, "--no-ccusage", "--json"], config, env)
    tally.check(
        "--no-ccusage does not run ccusage",
        skipped.returncode == 0 and not marker.exists() and "skipped with --no-ccusage" in skipped.stdout,
    )


def test_cli(tally: Tally):
    root = Path(tempfile.mkdtemp(prefix="session-cost-cli-"))
    projects = root / "projects"
    projects.mkdir()
    outside = root / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    link_dir = projects / "linked"
    link_dir.mkdir()
    os.symlink(outside, link_dir / f"{SID}.jsonl")
    plain = projects / "one"
    plain.mkdir()
    (plain / f"{SID}.jsonl").write_text("{}\n", encoding="utf-8")
    other = projects / "two"
    other.mkdir()
    (other / f"{SID}.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "home").mkdir()
    config = {
        "projects_root": str(projects),
        "home": str(root / "home"),
        "pricing_cache": str(root / "missing-prices.json"),
    }
    cases = (
        ([], "missing args"),
        (["not-a-uuid"], "non-uuid"),
        (["../etc/passwd"], "path traversal"),
        ([SID, "extra"], "extra arg"),
        ([SID, "--json", "--json"], "duplicate flag"),
        (["--bogus", SID], "unknown flag"),
    )
    for args, label in cases:
        completed = run(args, config)
        tally.check(f"CLI rejects {label}", completed.returncode == 2 and "usage:" in completed.stderr, completed.stderr)
    # Ambiguous and symlink fixtures share SID, so move the valid copies aside
    # before the symlink-only check.
    (plain / f"{SID}.jsonl").rename(plain / "aside.jsonl")
    (other / f"{SID}.jsonl").rename(other / "aside.jsonl")
    escaped = run([SID], config)
    tally.check(
        "symlink outside projects root is refused",
        escaped.returncode == 2 and "outside" in escaped.stderr,
        escaped.stderr,
    )
    os.remove(link_dir / f"{SID}.jsonl")
    (plain / "aside.jsonl").rename(plain / f"{SID}.jsonl")
    (other / "aside.jsonl").rename(other / f"{SID}.jsonl")
    ambiguous = run([SID], config)
    tally.check("ambiguous session is rejected", ambiguous.returncode == 2 and "ambiguous" in ambiguous.stderr, ambiguous.stderr)
    missing = run([OTHER], config)
    tally.check("missing session is rejected", missing.returncode == 2 and "not found" in missing.stderr, missing.stderr)
    default = run(["00000000-0000-4000-8000-000000000000"], None)
    tally.check(
        "default projects root rejects an unknown uuid",
        default.returncode == 2 and "not found" in default.stderr,
        default.stderr,
    )


def test_env_has_no_effect(tally: Tally):
    source = BIN.read_text(encoding="utf-8")
    tally.check(
        "production source has no SESSION_COST_ seam",
        "SESSION_COST_" not in source and "os.environ" not in source,
    )
    root = Path(tempfile.mkdtemp(prefix="session-cost-env-"))
    poisoned_projects = root / "projects"
    proj = poisoned_projects / "only-here"
    proj.mkdir(parents=True)
    sid = "33333333-3333-4333-8333-333333333333"
    (proj / f"{sid}.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
    fake_home = root / "home"
    fake_home.mkdir()
    fake_cache = root / "cache" / "ccusage" / "http-cache"
    fake_cache.mkdir(parents=True)
    (fake_cache / "litellm-evil.cache").write_text("{}", encoding="utf-8")
    prices = root / "prices.json"
    pricing_file(prices)
    stub = root / "ccusage"
    stub_script(stub)
    marker = root / "marker"
    payload = root / "payload.json"
    ccusage_payload(payload, [], 0, 0)
    poison = {
        "HOME": str(fake_home),
        "XDG_CACHE_HOME": str(root / "cache"),
        "SESSION_COST_PROJECTS_ROOT": str(poisoned_projects),
        "SESSION_COST_CCUSAGE": str(stub),
        "SESSION_COST_PRICING_CACHE": str(prices),
        "SESSION_COST_CCUSAGE_TIMEOUT": "0.01",
        "CCUSAGE_STUB_MARKER": str(marker),
        "CCUSAGE_STUB_JSON": str(payload),
    }
    saved = {}
    for key, value in poison.items():
        saved[key] = os.environ.get(key)
        os.environ[key] = value
    try:
        expected_root = os.path.realpath(os.path.join(MOD.real_home(), ".claude", "projects"))
        expected_cache = os.path.realpath(
            os.path.join(MOD.real_home(), ".cache", "ccusage", "http-cache")
        )
        price = MOD.find_pricing_file(None)
        price_ok = price is not None and (
            os.path.realpath(price) == expected_cache
            or os.path.realpath(price).startswith(expected_cache + os.sep)
        )
        tally.check(
            "old env vars do not change root, ccusage, timeout, or prices",
            MOD.projects_root(None) == expected_root
            and MOD.ccusage_binary(None) == "/opt/homebrew/bin/ccusage"
            and MOD.ccusage_timeout(None) == 120.0
            and price_ok
            and str(root) not in (price or ""),
            f"root={MOD.projects_root(None)} timeout={MOD.ccusage_timeout(None)} price={price}",
        )
    finally:
        for key, previous in saved.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
    completed = subprocess.run(
        [sys.executable, "-I", str(BIN), sid, "--json"],
        capture_output=True,
        text=True,
        env={**os.environ, **poison},
        check=False,
    )
    tally.check(
        "a normal run ignores the old env vars",
        completed.returncode == 2
        and "not found" in completed.stderr
        and "not a directory" not in completed.stderr
        and not marker.exists(),
        completed.stderr + completed.stdout[:300],
    )


def test_empirical_conclusions(tally: Tally):
    root = Path(tempfile.mkdtemp(prefix="session-cost-emp-"))
    projects = root / "projects"
    home = root / "home"
    home.mkdir()
    config = {
        "projects_root": str(projects),
        "home": str(home),
        "pricing_cache": str(root / "missing-prices.json"),
    }
    write_jsonl(
        projects / "plain" / f"{SID}.jsonl",
        [
            assistant(
                "msg-1",
                "claude-haiku-5-5",
                "2026-02-01T00:00:00.000Z",
                usage(2, 1, [iteration("message", 2, 1)]),
                [{"type": "text", "text": "x"}],
                "req-1",
            )
        ],
    )
    plain = json.loads(run([SID, "--no-ccusage", "--json"], config).stdout)
    human = run([SID, "--no-ccusage"], config)
    tally.check(
        "no advisor tokens leaves the fold question undetermined",
        "undetermined for this session (no advisor iterations)" in plain["empirical"]["conclusion"]
        and "undetermined for this session (no advisor iterations)" in human.stdout
        and "not folded" not in human.stdout
        and "not folded" not in plain["empirical"]["conclusion"],
        plain["empirical"]["conclusion"],
    )
    zero_id = "44444444-4444-4444-8444-444444444444"
    write_jsonl(
        projects / "zero" / f"{zero_id}.jsonl",
        [
            assistant(
                "msg-z",
                "claude-haiku-5-5",
                "2026-02-01T00:00:00.000Z",
                usage(
                    2,
                    1,
                    [iteration("message", 2, 1), iteration("advisor_message", 0, 0, "claude-haiku-5-5")],
                ),
                [{"type": "text", "text": "x"}],
                "req-z",
            )
        ],
    )
    zero = json.loads(run([zero_id, "--no-ccusage", "--json"], config).stdout)
    tally.check(
        "zero-token advisor_message is still undetermined",
        zero["empirical"]["advisor_iterations"] == 1
        and zero["empirical"]["nonzero_advisor_iterations"] == 0
        and "undetermined for this session (no advisor iterations)" in zero["empirical"]["conclusion"],
        zero["empirical"]["conclusion"],
    )
    live_id = "55555555-5555-4555-8555-555555555555"
    write_jsonl(
        projects / "live" / f"{live_id}.jsonl",
        [
            assistant(
                "msg-l",
                "claude-opus-5-5",
                "2026-02-02T00:00:00.000Z",
                usage(
                    5,
                    1,
                    [
                        iteration("message", 5, 1),
                        iteration("advisor_message", 3, 4, "claude-opus-5-5"),
                    ],
                ),
                [{"type": "server_tool_use", "id": "srv", "name": "advisor"}],
                "req-l",
                advisor_model="claude-opus-5-5",
            )
        ],
    )
    live = json.loads(run([live_id, "--no-ccusage", "--json"], config).stdout)
    live_human = run([live_id, "--no-ccusage"], config)
    tally.check(
        "non-zero advisor_message states a fold conclusion",
        live["empirical"]["nonzero_advisor_iterations"] == 1
        and "outside top-level usage" in live["empirical"]["conclusion"]
        and "undetermined" not in live["empirical"]["conclusion"]
        and "outside top-level usage" in live_human.stdout
        and "not folded" not in live["empirical"]["conclusion"],
        live["empirical"]["conclusion"],
    )


def test_growing_chunks(tally: Tally):
    root = Path(tempfile.mkdtemp(prefix="session-cost-chunks-"))
    projects = root / "projects"
    home = root / "home"
    home.mkdir()
    config = {
        "projects_root": str(projects),
        "home": str(home),
        "pricing_cache": str(root / "missing-prices.json"),
    }
    write_jsonl(
        projects / "grow" / f"{SID}.jsonl",
        [
            assistant(
                "msg-g",
                "claude-haiku-5-5",
                "2026-03-01T00:00:00.000Z",
                usage(10, 1),
                [{"type": "text", "text": "a"}],
                "req-g",
            ),
            assistant(
                "msg-g",
                "claude-haiku-5-5",
                "2026-03-01T00:00:01.000Z",
                usage(10, 4),
                [{"type": "text", "text": "ab"}],
                "req-g",
            ),
            assistant(
                "msg-g",
                "claude-haiku-5-5",
                "2026-03-01T00:00:02.000Z",
                usage(11, 9),
                [{"type": "text", "text": "abc"}],
                "req-g",
            ),
        ],
    )
    grown = json.loads(run([SID, "--no-ccusage", "--json"], config).stdout)
    main = agent_by_id(grown, "main")
    text = "\n".join(grown["warnings"])
    tally.check(
        "growing chunks keep the largest output_tokens",
        main["executor"]["tokens"]["output"] == 9
        and main["executor"]["tokens"]["input"] == 11
        and main["executor"]["calls"] == 1
        and "conflicting usage" in text
        and "largest output_tokens" in text,
        text + " " + str(main["executor"]["tokens"]),
    )
    shrink_id = "66666666-6666-4666-8666-666666666666"
    write_jsonl(
        projects / "shrink" / f"{shrink_id}.jsonl",
        [
            assistant(
                "msg-s",
                "claude-haiku-5-5",
                "2026-03-02T00:00:00.000Z",
                usage(1, 2),
                [{"type": "text", "text": "a"}],
                "req-s",
            ),
            assistant(
                "msg-s",
                "claude-haiku-5-5",
                "2026-03-02T00:00:01.000Z",
                usage(8, 9),
                [{"type": "text", "text": "peak"}],
                "req-s",
            ),
            assistant(
                "msg-s",
                "claude-haiku-5-5",
                "2026-03-02T00:00:02.000Z",
                usage(3, 4),
                [{"type": "text", "text": "later smaller"}],
                "req-s",
            ),
            assistant(
                "msg-t",
                "claude-haiku-5-5",
                "2026-03-02T00:00:03.000Z",
                usage(5, 5),
                [{"type": "text", "text": "tie-first"}],
                "req-t",
            ),
            assistant(
                "msg-t",
                "claude-haiku-5-5",
                "2026-03-02T00:00:04.000Z",
                usage(7, 5),
                [{"type": "text", "text": "tie-last"}],
                "req-t",
            ),
        ],
    )
    shrunk = json.loads(run([shrink_id, "--no-ccusage", "--json"], config).stdout)
    main = agent_by_id(shrunk, "main")
    tally.check(
        "a later smaller chunk loses and a tie keeps the last",
        main["executor"]["calls"] == 2
        and main["executor"]["tokens"]["output"] == 14
        and main["executor"]["tokens"]["input"] == 15,
        str(main["executor"]),
    )


def test_inherit_pin(tally: Tally):
    root = Path(tempfile.mkdtemp(prefix="session-cost-pin-"))
    home = root / "home"
    projects = root / "projects"
    agent_md(home / ".claude" / "agents" / "inherits.md", "inherit")
    bare_path = home / ".claude" / "agents" / "bare.md"
    bare_path.parent.mkdir(parents=True, exist_ok=True)
    bare_path.write_text("---\nname: bare\n---\nbody\n", encoding="utf-8")
    sub = projects / "proj" / SID / "subagents"
    write_jsonl(
        projects / "proj" / f"{SID}.jsonl",
        [
            assistant(
                "msg-m",
                "claude-opus-5-5",
                "2026-04-01T00:00:00.000Z",
                usage(1, 1, [iteration("message", 1, 1)]),
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_inh",
                        "name": "Agent",
                        "input": {"subagent_type": "inherits", "description": "inh"},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_bare",
                        "name": "Agent",
                        "input": {"subagent_type": "bare", "description": "bare"},
                    },
                ],
                "req-m",
            )
        ],
    )
    write_jsonl(
        sub / "agent-inh.jsonl",
        [
            assistant(
                "msg-i",
                "claude-opus-5-5",
                "2026-04-01T00:01:00.000Z",
                usage(2, 2, [iteration("message", 2, 2)]),
                [{"type": "text", "text": "i"}],
                "req-i",
            )
        ],
    )
    (sub / "agent-inh.meta.json").write_text(
        json.dumps({
            "agentType": "inherits",
            "toolUseId": "toolu_inh",
            "spawnDepth": 1,
            "isBuiltIn": False,
        }),
        encoding="utf-8",
    )
    write_jsonl(
        sub / "agent-bare.jsonl",
        [
            assistant(
                "msg-b",
                "claude-sonnet-5-5",
                "2026-04-01T00:02:00.000Z",
                usage(2, 2, [iteration("message", 2, 2)]),
                [{"type": "text", "text": "b"}],
                "req-b",
            )
        ],
    )
    (sub / "agent-bare.meta.json").write_text(
        json.dumps({
            "agentType": "bare",
            "toolUseId": "toolu_bare",
            "spawnDepth": 1,
            "isBuiltIn": False,
        }),
        encoding="utf-8",
    )
    config = {
        "projects_root": str(projects),
        "home": str(home),
        "pricing_cache": str(root / "missing-prices.json"),
    }
    report = json.loads(run([SID, "--no-ccusage", "--json"], config).stdout)
    inherited = agent_by_id(report, "inh")
    bare = agent_by_id(report, "bare")
    tally.check(
        "model inherit and a missing model are unpinned",
        inherited["pin"] == "unpinned"
        and inherited["definition_model"] == "inherit"
        and bare["pin"] == "unpinned"
        and bare["definition_model"] is None
        and bare["definition_path"] is not None,
        f"inh={inherited and inherited['pin']}/{inherited and inherited['definition_model']} "
        f"bare={bare and bare['pin']}/{bare and bare['definition_model']}",
    )


def test_scaled_cost(tally: Tally):
    root = Path(tempfile.mkdtemp(prefix="session-cost-scale-"))
    home = root / "home"
    home.mkdir()
    projects = root / "projects"
    prices = root / "prices.json"
    pricing_file(prices)
    stub = root / "ccusage"
    stub_script(stub)
    payload = root / "payload.json"
    sub = projects / "proj" / SID / "subagents"
    write_jsonl(
        projects / "proj" / f"{SID}.jsonl",
        [
            assistant(
                "msg-m",
                "claude-haiku-5-5",
                "2026-05-01T00:00:00.000Z",
                usage(4, 0, [iteration("message", 4, 0)]),
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_c",
                        "name": "Agent",
                        "input": {"subagent_type": "pinned-reader", "description": "c"},
                    }
                ],
                "req-m",
            )
        ],
    )
    write_jsonl(
        sub / "agent-child.jsonl",
        [
            assistant(
                "msg-c",
                "claude-sonnet-5-5",
                "2026-05-01T00:01:00.000Z",
                usage(6, 0, [iteration("message", 6, 0)]),
                [{"type": "text", "text": "c"}],
                "req-c",
            )
        ],
    )
    (sub / "agent-child.meta.json").write_text(
        json.dumps({
            "agentType": "pinned-reader",
            "toolUseId": "toolu_c",
            "spawnDepth": 1,
            "isBuiltIn": False,
        }),
        encoding="utf-8",
    )
    agent_md(home / ".claude" / "agents" / "pinned-reader.md", "haiku")
    ccusage_payload(
        payload,
        [
            entry("2026-05-01T00:00:00.000Z", "claude-haiku-5-5", 4, 0, 0),
            entry("2026-05-01T00:01:00.000Z", "claude-sonnet-5-5", 6, 0, 0),
        ],
        25,
        10,
    )
    config = base_config(projects, home, stub, prices)
    env = stub_env(payload)
    report = json.loads(run([SID, "--json"], config, env).stdout)
    main = agent_by_id(report, "main")
    child = agent_by_id(report, "child")
    label = "estimated (LiteLLM cache, scaled to ccusage total)"
    human = run([SID], config, env)
    tally.check(
        "per-agent costs scale to ccusage totalCost",
        report["totals"]["executor_cost"] == 25
        and report["totals"]["executor_cost_scale"] == 2.5
        and main["executor"]["cost"] + child["executor"]["cost"] == 25
        and main["executor"]["cost"] == 10
        and child["executor"]["cost"] == 15
        and main["executor"]["cost_label"] == label
        and child["executor"]["cost_label"] == label
        and label in human.stdout
        and "scale factor: 2.5" in human.stdout,
        str(report["totals"]) + " " + str(main["executor"]) + " " + str(child["executor"]) + "\n" + human.stdout,
    )


def main():
    tally = Tally()
    test_core(tally)
    test_top_equals_all(tally)
    test_no_iterations(tally)
    test_advisor_direction_warnings(tally)
    test_ccusage_failures(tally)
    test_cli(tally)
    test_env_has_no_effect(tally)
    test_empirical_conclusions(tally)
    test_growing_chunks(tally)
    test_inherit_pin(tally)
    test_scaled_cost(tally)
    return tally.finish()


if __name__ == "__main__":
    sys.exit(main())
