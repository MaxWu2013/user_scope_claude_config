#!/usr/bin/env python3
"""Subprocess tests for route-tools.py. The hook is not registered."""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

HOOK_DIR = Path(__file__).resolve().parent
HOOK = HOOK_DIR / "route-tools.py"
CODE_HOOK = HOOK_DIR / "require-cursor-agent.py"
HOME = Path(os.path.expanduser("~"))
SAFE = "~/.claude/hooks/bin/safe-read"
SAFE_ABS = f"{HOME}/.claude/hooks/bin/safe-read"
CURSOR_RUN_CMD = "~/.claude/hooks/bin/cursor-run"

READER_BASH_DENY = "file-reader may only run ~/.claude/hooks/bin/safe-read."
READER_WRITE_DENY = "file-reader is read-only."
WRITER_BASH_DENY = "file-writer has no shell. Report what you could not do."
STATE_DENY = "Specs, memory and plans belong to the orchestrator. Report back instead."
OTHER_DOC_DENY = (
    "Not allowed here: document edits from this subagent. "
    "Give the file-writer subagent (Agent tool) the exact files and changes."
)
ORCH_DOC_DENY = (
    "Not allowed here: editing documents on the main thread. "
    "Give the file-writer subagent (Agent tool) the exact files and changes."
)
ORCH_BASH_WRITE_DENY = (
    "Not allowed here: writing documents through Bash. "
    "Give the file-writer subagent (Agent tool) the exact files and changes."
)
CURSOR_AGENT_DENY = "Start cursor-agent only through ~/.claude/hooks/bin/cursor-run <spec>."
WRITER_READ_DENY = "file-writer may only Read documents."
DELETE_ASK = "Unclassified Obsidian operation 'delete_file'. Approve manually."


def read_deny(subject):
    return (
        f"Not allowed here: {subject}. "
        "Ask the file-reader subagent (Agent tool); batch your questions."
    )


def payload(tool, tool_input, *, agent_id=None, agent_type=None, session_id=None, cwd=None):
    body = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
    }
    if agent_id is not None:
        body["agent_id"] = agent_id
    if agent_type is not None:
        body["agent_type"] = agent_type
    if session_id is not None:
        body["session_id"] = session_id
    if cwd is not None:
        body["cwd"] = cwd
    return body


def reader(tool, tool_input):
    return payload(tool, tool_input, agent_id="reader-1", agent_type="file-reader")


def writer(tool, tool_input):
    return payload(tool, tool_input, agent_id="writer-1", agent_type="file-writer")


def other(tool, tool_input, agent_type="worker"):
    return payload(tool, tool_input, agent_id="other-1", agent_type=agent_type)


def orchestrator(tool, tool_input):
    return payload(tool, tool_input)


def bash(command):
    return {"command": command}


def read_input(path):
    return {"file_path": path}


def write_input(path):
    return {"file_path": path}


def obsidian(op, filepath="note.md", **extra):
    tool = f"mcp__mcp-obsidian__obsidian_{op}"
    body = {"filepath": filepath}
    body.update(extra)
    return tool, body


class Suite:
    def __init__(self, project, log_path):
        self.project = project
        self.log_path = log_path
        self.passed = 0
        self.failed = 0
        self.failures = []

    def run(self, body=None, *, env_extra=None, stdin=None, hook=None, cwd=None, keep_pythonpath=True):
        env = os.environ.copy()
        for key in (
            "CLAUDE_ROUTING",
            "CLAUDE_ROUTING_MODE",
            "CLAUDE_ROUTING_OTHER",
            "CLAUDE_ROUTING_ALLOW_OPUS_SUBAGENT",
            "CLAUDE_CODE_SUBAGENT_MODEL",
            "CURSOR_RUN_PATH",
            "CURSOR_AGENT",
            "SUB_AGENTS_DIR",
        ):
            env.pop(key, None)
        env["CLAUDE_PROJECT_DIR"] = str(self.project)
        env["CLAUDE_ROUTING_MODE"] = "strict"
        env["ROUTING_LOG"] = str(self.log_path)
        if not keep_pythonpath:
            env.pop("PYTHONPATH", None)
        if env_extra:
            env.update(env_extra)
        if stdin is None:
            stdin = json.dumps(body)
        completed = subprocess.run(
            [sys.executable, str(hook or HOOK)],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(cwd or self.project),
            check=False,
        )
        return completed

    def check(self, number, name, ok, detail=""):
        status = "PASS" if ok else "FAIL"
        print(f"{str(number):>4}  {name:<78} {status}")
        if ok:
            self.passed += 1
        else:
            self.failed += 1
            self.failures.append(f"{number} {name}: {detail}")
            if detail:
                print(f"      {detail}")

    def expect(self, number, name, body, kind, reason=None, **kwargs):
        completed = self.run(body, **kwargs)
        detail = (
            f"exit={completed.returncode} stdout={completed.stdout!r} "
            f"stderr={completed.stderr[:400]!r}"
        )
        if completed.returncode != 0 or completed.stderr != "":
            self.check(number, name, False, detail)
            return completed
        text = completed.stdout
        if kind == "silent":
            self.check(number, name, text == "", detail)
            return completed
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            self.check(number, name, False, detail)
            return completed
        if kind == "cursor":
            self.check(number, name, data == {"permission": "allow"} and text.strip() == text.strip(), detail)
            return completed
        hook_out = data.get("hookSpecificOutput")
        expected = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": kind,
                "permissionDecisionReason": reason,
            }
        }
        self.check(number, name, data == expected, detail + f" parsed={data!r}")
        return completed


ALLOW_LOG_FIELDS = {
    "ts", "role", "agent_type", "tool", "path", "decision",
    "session_id", "project", "mode",
}


def context_ok(rec, project, mode="strict", session_id=None):
    return (
        "session_id" in rec
        and rec.get("session_id") == session_id
        and rec.get("project") == os.path.realpath(str(project))
        and rec.get("mode") == mode
    )


def log_records(path):
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def fresh_log(prefix):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix=prefix)))
    return root, root / "logs" / "routing.jsonl"


def main():
    raw = tempfile.mkdtemp(prefix="phase5b-project-")
    project = Path(os.path.realpath(raw))
    log_root = Path(os.path.realpath(tempfile.mkdtemp(prefix="phase5b-log-")))
    log_path = log_root / "routing.jsonl"
    suite = Suite(project, log_path)
    os.environ["CLAUDE_PROJECT_DIR"] = str(project)
    extra_dirs = [log_root]
    if Path(raw) != project:
        extra_dirs.append(Path(raw))
    try:
        case_1(suite)
        case_2(suite)
        case_3(suite)
        case_4(suite)
        case_5(suite)
        case_6(suite)
        case_7(suite)
        case_8(suite)
        case_9(suite)
        case_10(suite)
        case_11(suite)
        case_12(suite, extra_dirs)
        case_13(suite)
        case_14(suite, extra_dirs)
        case_15(suite)
        case_16(suite)
        case_17(suite)
        case_18(suite)
        case_19(suite, extra_dirs)
        case_20(suite)
        case_21(suite, extra_dirs)
        case_22(suite, extra_dirs)
        case_23(suite, extra_dirs)
        case_24(suite)
        case_25(suite, extra_dirs)
        case_26(suite)
        default_records = log_records(log_path)
        allow_logs = [rec for rec in default_records if rec.get("decision") == "allow-log"]
        suite.check(
            "14",
            "default routing log has no allow-log records",
            allow_logs == [],
            f"records={allow_logs!r}",
        )
    finally:
        shutil.rmtree(project, ignore_errors=True)
        for directory in extra_dirs:
            shutil.rmtree(directory, ignore_errors=True)

    print(f"Tally: {suite.passed} passed, {suite.failed} failed")
    return 0 if suite.failed == 0 and suite.passed > 0 else 1


def case_1(suite):
    p = suite.project
    for command, label in (
        ("ls", "ls"),
        ("cat x", "cat x"),
        ("git diff", "git diff"),
    ):
        suite.expect(
            "1",
            f"reader Bash {label} -> deny",
            reader("Bash", bash(command)),
            "deny",
            READER_BASH_DENY,
        )
    suite.expect(
        "1",
        "reader Bash absolute cat -> deny",
        reader("Bash", bash(f"cat {p / 'src' / 'a.py'}")),
        "deny",
        READER_BASH_DENY,
    )


def case_2(suite):
    suite.expect(
        "2",
        "reader Bash ~/safe-read ls . -> silent",
        reader("Bash", bash(f"{SAFE} ls .")),
        "silent",
    )
    suite.expect(
        "2",
        "reader Bash $HOME/safe-read git-diff --stat HEAD~1 -> silent",
        reader("Bash", bash(f"{SAFE_ABS} git-diff --stat HEAD~1")),
        "silent",
    )


def case_3(suite):
    samples = (
        (f"{SAFE} rg x $(id)", "command substitution"),
        (f"{SAFE} rg x > out", "redirect"),
        (f"{SAFE} rg x; ls", "semicolon"),
        (f"{SAFE} rg x | sh", "pipe"),
        (f"{SAFE} rg x `id`", "backticks"),
        (f"{SAFE} rg x \n ls", "newline then ls"),
        ("safe-read ls .", "bare safe-read"),
    )
    for command, label in samples:
        suite.expect(
            "3",
            f"reader Bash {label} -> deny",
            reader("Bash", bash(command)),
            "deny",
            READER_BASH_DENY,
        )


def case_4(suite):
    p = suite.project
    cases = (
        ("Edit", write_input(str(p / "notes.md")), "Edit notes.md"),
        ("Write", write_input(str(p / "src" / "a.py")), "Write src/a.py"),
        ("Write", write_input("~/.claude/settings.json"), "Write ~/.claude/settings.json"),
        ("MultiEdit", write_input(str(p / "notes.md")), "MultiEdit notes.md"),
        ("NotebookEdit", {"notebook_path": str(p / "notes.ipynb")}, "NotebookEdit"),
    )
    for tool, tool_input, label in cases:
        suite.expect("4", f"reader {label} -> deny", reader(tool, tool_input), "deny", READER_WRITE_DENY)
    tool, tool_input = obsidian("put_content", "note.md")
    suite.expect(
        "4",
        "reader obsidian put_content note.md -> deny",
        reader(tool, tool_input),
        "deny",
        READER_WRITE_DENY,
    )


def case_5(suite):
    p = suite.project
    suite.expect("5", "reader Read src/a.py -> silent", reader("Read", read_input(str(p / "src" / "a.py"))), "silent")
    suite.expect("5", "reader Grep -> silent", reader("Grep", {"pattern": "x", "path": str(p)}), "silent")
    suite.expect("5", "reader Glob -> silent", reader("Glob", {"pattern": "**/*", "path": str(p)}), "silent")
    tool, tool_input = obsidian("simple_search", "")
    tool_input = {"query": "x"}
    suite.expect("5", "reader obsidian simple_search -> silent", reader(tool, tool_input), "silent")


def case_6(suite):
    p = suite.project
    suite.expect(
        "6",
        "orchestrator Read src/a.py -> deny",
        orchestrator("Read", read_input(str(p / "src" / "a.py"))),
        "deny",
        read_deny("Read on the main thread"),
    )
    suite.expect(
        "6",
        "orchestrator Read docs/x.md -> deny",
        orchestrator("Read", read_input(str(p / "docs" / "x.md"))),
        "deny",
        read_deny("Read on the main thread"),
    )
    suite.expect(
        "6",
        "orchestrator Grep no path -> deny",
        orchestrator("Grep", {"pattern": "TODO"}),
        "deny",
        read_deny("Grep on the main thread"),
    )
    suite.expect(
        "6",
        "orchestrator Glob path=P -> deny",
        orchestrator("Glob", {"pattern": "**/*", "path": str(p)}),
        "deny",
        read_deny("Glob on the main thread"),
    )
    tool, _ = obsidian("simple_search")
    suite.expect(
        "6",
        "orchestrator obsidian simple_search -> deny",
        orchestrator(tool, {"query": "x"}),
        "deny",
        read_deny("reading the vault from the main thread"),
    )


def case_7(suite):
    p = suite.project
    samples = (
        (str(p / "CLAUDE.md"), "P/CLAUDE.md"),
        ("~/.claude/CLAUDE.md", "~/.claude/CLAUDE.md"),
        (str(p / ".claude" / "specs" / "t.md"), "P/.claude/specs/t.md"),
        ("~/.claude/plans/p.md", "~/.claude/plans/p.md"),
        ("~/.claude/skills/x/SKILL.md", "~/.claude/skills/x/SKILL.md"),
        ("~/.claude/settings.json", "~/.claude/settings.json"),
        ("/tmp/outside.txt", "/tmp/outside.txt"),
    )
    for path, label in samples:
        suite.expect(
            "7",
            f"orchestrator Read {label} -> silent",
            orchestrator("Read", read_input(path)),
            "silent",
        )


def case_8(suite):
    p = suite.project
    suite.expect(
        "8",
        "orchestrator Edit docs/guide.md -> deny",
        orchestrator("Edit", write_input(str(p / "docs" / "guide.md"))),
        "deny",
        ORCH_DOC_DENY,
    )
    suite.expect(
        "8",
        "orchestrator Write notes.md -> deny",
        orchestrator("Write", write_input(str(p / "notes.md"))),
        "deny",
        ORCH_DOC_DENY,
    )
    for filepath, label in (("note.md", "note.md"), ("board.canvas", "board.canvas")):
        tool, tool_input = obsidian("put_content", filepath)
        suite.expect(
            "8",
            f"orchestrator obsidian put_content {label} -> deny",
            orchestrator(tool, tool_input),
            "deny",
            ORCH_DOC_DENY,
        )


def case_9(suite):
    p = suite.project
    samples = (
        (str(p / ".claude" / "specs" / "t.diff"), "P/.claude/specs/t.diff"),
        ("~/.claude/plans/p.md", "~/.claude/plans/p.md"),
        ("~/.claude/projects/x/memory/m.md", "~/.claude/projects/x/memory/m.md"),
    )
    for path, label in samples:
        suite.expect(
            "9",
            f"orchestrator Write {label} -> silent",
            orchestrator("Write", write_input(path)),
            "silent",
        )


def case_10(suite):
    p = suite.project
    suite.expect(
        "10",
        "orchestrator Edit src/a.ts -> silent",
        orchestrator("Edit", write_input(str(p / "src" / "a.ts"))),
        "silent",
    )
    tool, tool_input = obsidian("put_content", "data.json")
    suite.expect("10", "orchestrator obsidian put_content data.json -> silent", orchestrator(tool, tool_input), "silent")
    tool, tool_input = obsidian("put_content", "img.png")
    suite.expect("10", "orchestrator obsidian put_content img.png -> silent", orchestrator(tool, tool_input), "silent")
    suite.expect(
        "10",
        "orchestrator Edit ~/.claude/settings.json -> silent",
        orchestrator("Edit", write_input("~/.claude/settings.json")),
        "silent",
    )
    suite.expect(
        "10",
        "orchestrator Edit ~/.claude/skills/x/SKILL.md -> silent",
        orchestrator("Edit", write_input("~/.claude/skills/x/SKILL.md")),
        "silent",
    )


def case_11(suite):
    p = suite.project
    silent_cmds = (
        "git status",
        "git diff --stat",
        "git diff --name-only",
        "git diff --name-status",
        "git diff --shortstat",
        "python3 -m pytest",
        f"echo x > {p / 'a.py'}",
        f"{SAFE} git diff",
        "ls",
    )
    for command in silent_cmds:
        suite.expect(
            "11",
            f"orchestrator Bash {command!r} -> silent",
            orchestrator("Bash", bash(command)),
            "silent",
        )
    for command in ("git diff", "cat a.py", "rg foo", "git log -p", "git show"):
        suite.expect(
            "11",
            f"orchestrator Bash {command!r} -> deny",
            orchestrator("Bash", bash(command)),
            "deny",
            read_deny("reading files or full diffs through Bash"),
        )
    suite.expect(
        "11",
        "orchestrator Bash echo > notes.md -> deny",
        orchestrator("Bash", bash(f"echo x > {p / 'notes.md'}")),
        "deny",
        ORCH_BASH_WRITE_DENY,
    )


def case_12(suite, extra_dirs):
    absent = {"CURSOR_RUN_PATH": "/nonexistent"}
    suite.expect(
        "12",
        "orchestrator Bash cursor-agent (absent) -> silent",
        orchestrator("Bash", bash("cursor-agent -p hi")),
        "silent",
        env_extra=absent,
    )
    suite.expect(
        "12",
        "orchestrator Bash cursor-run git diff while absent -> deny",
        orchestrator("Bash", bash(f"{CURSOR_RUN_CMD} git diff")),
        "deny",
        read_deny("reading files or full diffs through Bash"),
        env_extra=absent,
    )

    home_root = Path(os.path.realpath(tempfile.mkdtemp(prefix="phase5b-home-")))
    extra_dirs.append(home_root)
    binary = home_root / ".claude" / "hooks" / "bin" / "cursor-run"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    home_env = {"HOME": str(home_root)}
    suite.expect(
        "12",
        "temp HOME cursor-agent -> deny",
        orchestrator("Bash", bash("cursor-agent -p hi")),
        "deny",
        CURSOR_AGENT_DENY,
        env_extra=home_env,
    )
    suite.expect(
        "12",
        "temp HOME cursor-run git diff -> silent",
        orchestrator("Bash", bash(f"{binary} git diff")),
        "silent",
        env_extra=home_env,
    )
    suite.expect(
        "12",
        "temp HOME tilde cursor-run spec -> silent",
        orchestrator("Bash", bash(f"{CURSOR_RUN_CMD} spec.md")),
        "silent",
        env_extra=home_env,
    )
    suite.expect(
        "12",
        "temp HOME cursor-run with semicolon still denies cat",
        orchestrator("Bash", bash(f"{CURSOR_RUN_CMD} foo; cat x")),
        "deny",
        read_deny("reading files or full diffs through Bash"),
        env_extra=home_env,
    )

    seam_root = Path(os.path.realpath(tempfile.mkdtemp(prefix="phase5b-cursor-run-")))
    extra_dirs.append(seam_root)
    seam = seam_root / "cursor-run"
    seam.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    seam.chmod(0o755)
    seam_env = {"CURSOR_RUN_PATH": str(seam)}
    suite.expect(
        "12",
        "CURSOR_RUN_PATH present: cursor-agent -> deny",
        orchestrator("Bash", bash("cursor-agent -p hi")),
        "deny",
        CURSOR_AGENT_DENY,
        env_extra=seam_env,
    )
    suite.expect(
        "12",
        "CURSOR_RUN_PATH present: canonical cursor-run git diff -> silent",
        orchestrator("Bash", bash(f"{CURSOR_RUN_CMD} git diff")),
        "silent",
        env_extra=seam_env,
    )
    plain = seam_root / "not-executable"
    plain.write_text("nope", encoding="utf-8")
    plain.chmod(0o644)
    suite.expect(
        "12",
        "CURSOR_RUN_PATH not executable: cursor-agent -> silent",
        orchestrator("Bash", bash("cursor-agent -p hi")),
        "silent",
        env_extra={"CURSOR_RUN_PATH": str(plain)},
    )


def case_13(suite):
    p = suite.project
    suite.expect(
        "13",
        "writer Edit docs/x.md -> silent",
        writer("Edit", write_input(str(p / "docs" / "x.md"))),
        "silent",
    )
    suite.expect(
        "13",
        "writer Write specs/t.md -> deny",
        writer("Write", write_input(str(p / ".claude" / "specs" / "t.md"))),
        "deny",
        STATE_DENY,
    )
    suite.expect(
        "13",
        "writer Write memory/m.md -> deny",
        writer("Write", write_input("~/.claude/projects/x/memory/m.md")),
        "deny",
        STATE_DENY,
    )
    suite.expect(
        "13",
        "writer Bash ls -> deny",
        writer("Bash", bash("ls")),
        "deny",
        WRITER_BASH_DENY,
    )
    suite.expect(
        "13",
        "writer Read src/a.py -> deny",
        writer("Read", read_input(str(p / "src" / "a.py"))),
        "deny",
        WRITER_READ_DENY,
    )
    tool, tool_input = obsidian("put_content", "note.md")
    suite.expect("13", "writer obsidian put_content note.md -> silent", writer(tool, tool_input), "silent")


def case_14(suite, extra_dirs):
    p = suite.project
    root, log_path = fresh_log("phase5b-other-")
    extra_dirs.append(root)
    completed = suite.expect(
        "14",
        "other Write notes.md -> silent",
        other("Write", write_input(str(p / "notes.md"))),
        "silent",
        env_extra={"ROUTING_LOG": str(log_path)},
    )
    records = log_records(log_path)
    ok = (
        completed.returncode == 0
        and len(records) == 1
        and records[0].get("role") == "other"
        and records[0].get("agent_type") == "worker"
        and records[0].get("tool") == "Write"
        and records[0].get("decision") == "allow-log"
        and records[0].get("path") == str(p / "notes.md")
        and isinstance(records[0].get("ts"), str)
        and records[0]["ts"]
        and set(records[0]) == ALLOW_LOG_FIELDS
        and context_ok(records[0], p)
    )
    suite.check("14", "other Write notes.md appended one allow-log line", ok, f"records={records!r}")
    if log_path.exists():
        file_mode = stat.S_IMODE(log_path.stat().st_mode)
        dir_mode = stat.S_IMODE(log_path.parent.stat().st_mode)
        suite.check(
            "14",
            "routing log file 0600 and dir 0700",
            file_mode == 0o600 and dir_mode == 0o700,
            f"file={oct(file_mode)} dir={oct(dir_mode)}",
        )
    else:
        suite.check("14", "routing log file 0600 and dir 0700", False, "log missing")

    strict_root, strict_log = fresh_log("phase5b-strict-")
    extra_dirs.append(strict_root)
    suite.expect(
        "14",
        "other Write notes.md strict -> deny",
        other("Write", write_input(str(p / "notes.md"))),
        "deny",
        OTHER_DOC_DENY,
        env_extra={"CLAUDE_ROUTING_OTHER": "strict", "ROUTING_LOG": str(strict_log)},
    )
    strict_records = log_records(strict_log)
    suite.check(
        "14",
        "strict other write logged one deny",
        len(strict_records) == 1
        and strict_records[0].get("decision") == "deny"
        and strict_records[0].get("role") == "other"
        and strict_records[0].get("agent_type") == "worker"
        and strict_records[0].get("tool") == "Write"
        and strict_records[0].get("path") == str(p / "notes.md")
        and strict_records[0].get("reason") == OTHER_DOC_DENY[:120]
        and "command" not in strict_records[0]
        and isinstance(strict_records[0].get("ts"), str)
        and strict_records[0]["ts"],
        f"records={strict_records!r}",
    )
    suite.expect(
        "14",
        "other Write plans/p.md -> deny",
        other("Write", write_input("~/.claude/plans/p.md")),
        "deny",
        STATE_DENY,
    )
    suite.expect(
        "14",
        "other Bash cat x -> silent",
        other("Bash", bash("cat x")),
        "silent",
    )
    plugin_root, plugin_log = fresh_log("phase5b-plugin-")
    extra_dirs.append(plugin_root)
    suite.expect(
        "14",
        "plugin:foo Write notes.md -> silent",
        other("Write", write_input(str(p / "notes.md")), agent_type="plugin:foo"),
        "silent",
        env_extra={"ROUTING_LOG": str(plugin_log)},
    )
    plugin_records = log_records(plugin_log)
    suite.check(
        "14",
        "plugin:foo logged as other",
        len(plugin_records) == 1
        and plugin_records[0].get("role") == "other"
        and plugin_records[0].get("agent_type") == "plugin:foo"
        and plugin_records[0].get("decision") == "allow-log",
        f"records={plugin_records!r}",
    )


def case_15(suite):
    body = payload(
        "Edit",
        write_input(str(suite.project / "docs" / "x.md")),
        agent_type="file-writer",
    )
    suite.expect(
        "15",
        "--agent file-writer (no agent_id) Edit docs/x.md -> deny",
        body,
        "deny",
        ORCH_DOC_DENY,
    )


def case_16(suite):
    tool, tool_input = obsidian("delete_file", "note.md")
    for label, body in (
        ("orchestrator", orchestrator(tool, tool_input)),
        ("writer", writer(tool, tool_input)),
        ("reader", reader(tool, tool_input)),
        ("other", other(tool, tool_input)),
    ):
        suite.expect("16", f"{label} obsidian_delete_file -> ask", body, "ask", DELETE_ASK)


def case_17(suite):
    p = suite.project
    suite.expect(
        "17",
        "CLAUDE_ROUTING=off orchestrator Edit docs/x.md -> silent",
        orchestrator("Edit", write_input(str(p / "docs" / "x.md"))),
        "silent",
        env_extra={"CLAUDE_ROUTING": "off"},
    )
    completed = suite.run(
        orchestrator("Edit", write_input(str(p / "src" / "a.py"))),
        env_extra={"CLAUDE_ROUTING": "off"},
        hook=CODE_HOOK,
    )
    suite.check(
        "17",
        "code hook Edit src/a.py with CLAUDE_ROUTING=off -> exit 2",
        completed.returncode == 2,
        f"exit={completed.returncode} stdout={completed.stdout!r} stderr={completed.stderr[:300]!r}",
    )


def case_18(suite):
    for label, stdin in (
        ("malformed JSON", "{"),
        ("empty stdin", ""),
        ("not json", "not-json"),
    ):
        completed = suite.run(stdin=stdin)
        suite.check(
            "18",
            f"{label} -> exit 0 silent",
            completed.returncode == 0 and completed.stdout == "" and completed.stderr == "",
            f"exit={completed.returncode} stdout={completed.stdout!r} stderr={completed.stderr[:300]!r}",
        )
    completed = suite.run({"hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": ["not", "a", "dict"]})
    suite.check(
        "18",
        "tool_input list -> exit 0 silent",
        completed.returncode == 0 and completed.stdout == "" and completed.stderr == "",
        f"exit={completed.returncode} stdout={completed.stdout!r} stderr={completed.stderr[:300]!r}",
    )


def case_19(suite, extra_dirs):
    source = HOOK.read_text(encoding="utf-8")
    main_body = source.split("def main()", 1)[1]
    flag_at = source.index("sys.dont_write_bytecode")
    import_at = main_body.index("from _paths import")
    cursor_at = main_body.index("is_cursor_runtime(data)")
    routing_at = main_body.index('os.environ.get("CLAUDE_ROUTING")')
    runtime_import = re.search(r"^\s*(?:import|from)\s+_runtime\b", source, re.M)
    order_ok = (
        flag_at < source.index("def main()")
        and cursor_at < routing_at < import_at
        and runtime_import is None
    )
    suite.check(
        "19",
        "startup order: bytecode, cursor, routing off, then _paths; no _runtime import",
        order_ok,
        f"cursor={cursor_at} routing={routing_at} import={import_at} runtime={runtime_import!r}",
    )

    fixture = (HOOK_DIR / "fixtures" / "cursor-pretooluse-write.json").read_text(encoding="utf-8")
    suite.expect("19", "cursor fixture -> allow", None, "cursor", stdin=fixture)

    alone = Path(os.path.realpath(tempfile.mkdtemp(prefix="phase5b-alone-")))
    extra_dirs.append(alone)
    shutil.copy(HOOK, alone / "route-tools.py")
    alone_hook = alone / "route-tools.py"
    suite.expect(
        "19",
        "route-tools.py alone: cursor fixture -> allow",
        None,
        "cursor",
        stdin=fixture,
        hook=alone_hook,
        cwd=alone,
        keep_pythonpath=False,
    )
    suite.expect(
        "19",
        "route-tools.py alone: Claude Edit docs/x.md -> silent",
        orchestrator("Edit", write_input("docs/x.md")),
        "silent",
        hook=alone_hook,
        cwd=alone,
        keep_pythonpath=False,
    )

    broken = Path(os.path.realpath(tempfile.mkdtemp(prefix="phase5b-broken-paths-")))
    extra_dirs.append(broken)
    shutil.copy(HOOK, broken / "route-tools.py")
    (broken / "_paths.py").write_text('raise RuntimeError("phase5b import boom")\n', encoding="utf-8")
    broken_hook = broken / "route-tools.py"
    suite.expect(
        "19",
        "raising _paths.py: cursor fixture -> allow",
        None,
        "cursor",
        stdin=fixture,
        hook=broken_hook,
        cwd=broken,
        keep_pythonpath=False,
    )
    suite.expect(
        "19",
        "raising _paths.py: Claude Edit docs/x.md -> silent",
        orchestrator("Edit", write_input("docs/x.md")),
        "silent",
        hook=broken_hook,
        cwd=broken,
        keep_pythonpath=False,
    )


def case_20(suite):
    spec = suite.project / ".claude" / "specs"
    spec.mkdir(parents=True)
    target = spec / "t.md"
    target.write_text("spec\n", encoding="utf-8")
    link = suite.project / "link.md"
    link.symlink_to(target)

    sys.path.insert(0, str(HOOK_DIR))
    os.environ["CLAUDE_PROJECT_DIR"] = str(suite.project)
    from _paths import STRICTNESS, _classify, resolve_target

    literal, real = resolve_target("Write", {"file_path": str(link)})
    literal_cls = _classify(literal)
    real_cls = _classify(real)
    winner = min((literal_cls, real_cls), key=STRICTNESS.index)
    print(
        f"      symlink STRICTNESS winner={winner} "
        f"(literal {literal_cls} {literal}, real {real_cls} {real}); "
        f"order={STRICTNESS}"
    )
    if winner in ("document", "vault_structured"):
        suite.expect(
            "20",
            f"orchestrator Write link.md stricter={winner} -> deny",
            orchestrator("Write", write_input(str(link))),
            "deny",
            ORCH_DOC_DENY,
        )
    else:
        suite.expect(
            "20",
            f"orchestrator Write link.md stricter={winner} -> silent",
            orchestrator("Write", write_input(str(link))),
            "silent",
        )


def check_audit(
    suite, number, name, log_path, *, role, agent_type, tool, path, decision, reason, command=None, bash=False,
):
    records = log_records(log_path)
    if len(records) != 1:
        suite.check(number, name, False, f"records={records!r}")
        return
    rec = records[0]
    allowed = {"ts", "role", "agent_type", "tool", "path", "decision", "reason", "session_id", "project", "mode"}
    if bash:
        allowed.add("command")
    problems = []
    if set(rec) != allowed:
        problems.append(f"keys={sorted(rec)}")
    if not context_ok(rec, suite.project):
        problems.append(
            f"context session_id={rec.get('session_id')!r} project={rec.get('project')!r} mode={rec.get('mode')!r}"
        )
    for key, expected in (
        ("role", role),
        ("agent_type", agent_type),
        ("tool", tool),
        ("path", path),
        ("decision", decision),
        ("reason", reason),
    ):
        if rec.get(key) != expected:
            problems.append(f"{key}={rec.get(key)!r} expected={expected!r}")
    if bash and rec.get("command") != command:
        problems.append(f"command={rec.get('command')!r} expected={command!r}")
    if not isinstance(rec.get("ts"), str) or not rec["ts"]:
        problems.append(f"ts={rec.get('ts')!r}")
    suite.check(number, name, not problems, "; ".join(problems) + f" record={rec!r}")


def _fresh_env(extra_dirs, env_extra=None):
    root, log_path = fresh_log("phase5b-audit-")
    extra_dirs.append(root)
    env = {"ROUTING_LOG": str(log_path)}
    if env_extra:
        env.update(env_extra)
    return log_path, env


def run_other_bash(suite, extra_dirs, label, command, kind, reason=None, *, mode=None, log_kind, path=None):
    env_extra = {"CLAUDE_ROUTING_OTHER": mode} if mode is not None else None
    log_path, env = _fresh_env(extra_dirs, env_extra)
    suite.expect("21", label, other("Bash", bash(command)), kind, reason, env_extra=env)
    if log_kind == "absent":
        suite.check(
            "21",
            label + " (not logged)",
            not log_path.exists(),
            f"records={log_records(log_path)!r}",
        )
        return
    if log_kind == "allow":
        records = log_records(log_path)
        ok = (
            len(records) == 1
            and records[0].get("role") == "other"
            and records[0].get("agent_type") == "worker"
            and records[0].get("tool") == "Bash"
            and records[0].get("path") == path
            and records[0].get("decision") == "allow-log"
            and isinstance(records[0].get("ts"), str)
            and records[0]["ts"]
            and set(records[0]) == ALLOW_LOG_FIELDS
            and context_ok(records[0], suite.project)
        )
        suite.check("21", label + " (one allow-log)", ok, f"records={records!r}")
        return
    check_audit(
        suite,
        "21",
        label + " (one deny log)",
        log_path,
        role="other",
        agent_type="worker",
        tool="Bash",
        path=path,
        decision="deny",
        reason=(reason or "")[:120],
        command=command,
        bash=True,
    )


def case_21(suite, extra_dirs):
    writes = (
        ("printf 'x\\n' > docs/printf.md", "docs/printf.md", "printf redirect"),
        ("echo x > docs/echo.md", "docs/echo.md", "echo redirect"),
        ("tee docs/tee.md", "docs/tee.md", "tee"),
        ("cp docs/cp.md docs/cp2.md", "docs/cp.md", "cp"),
        ("mv docs/mv.md docs/mv2.md", "docs/mv.md", "mv"),
        ("sed -i 's/a/b/' docs/sed.md", "docs/sed.md", "sed -i"),
        (
            "echo x > ~/workplace/project/Obsidian_doc/board.canvas",
            "~/workplace/project/Obsidian_doc/board.canvas",
            "vault canvas redirect",
        ),
    )
    for command, path, label in writes:
        run_other_bash(
            suite, extra_dirs, f"other strict Bash {label} -> deny",
            command, "deny", OTHER_DOC_DENY, mode="strict", log_kind="deny", path=path,
        )
        run_other_bash(
            suite, extra_dirs, f"other log Bash {label} -> silent",
            command, "silent", mode="log", log_kind="allow", path=path,
        )

    for mode in ("strict", "log"):
        run_other_bash(
            suite, extra_dirs, f"other {mode} Bash ls docs -> silent",
            "ls docs", "silent", mode=mode, log_kind="absent",
        )
        run_other_bash(
            suite, extra_dirs, f"other {mode} Bash echo > /tmp/x.txt -> silent",
            "echo x > /tmp/x.txt", "silent", mode=mode, log_kind="absent",
        )
        run_other_bash(
            suite, extra_dirs, f"other {mode} Bash echo > build/out.js -> silent",
            "echo x > build/out.js", "silent", mode=mode, log_kind="absent",
        )

    state_cmds = (
        ("echo x > .claude/specs/t.md", ".claude/specs/t.md", "specs"),
        ("echo x > ~/.claude/plans/p.md", "~/.claude/plans/p.md", "plans"),
        (
            "echo x > ~/.claude/projects/x/memory/m.md",
            "~/.claude/projects/x/memory/m.md",
            "memory",
        ),
    )
    for command, path, label in state_cmds:
        for mode in ("strict", "log"):
            run_other_bash(
                suite, extra_dirs, f"other {mode} Bash {label} -> deny",
                command, "deny", STATE_DENY, mode=mode, log_kind="deny", path=path,
            )
    run_other_bash(
        suite, extra_dirs, "other log Bash spec and doc -> state deny",
        "echo x > .claude/specs/t.md > docs/a.md",
        "deny",
        STATE_DENY,
        mode="log",
        log_kind="deny",
        path=".claude/specs/t.md",
    )

    suite.expect(
        "21",
        "reader Bash printf > docs/printf.md -> deny",
        reader("Bash", bash("printf 'x\\n' > docs/printf.md")),
        "deny",
        READER_BASH_DENY,
    )
    suite.expect(
        "21",
        "writer Bash printf > docs/printf.md -> deny",
        writer("Bash", bash("printf 'x\\n' > docs/printf.md")),
        "deny",
        WRITER_BASH_DENY,
    )
    suite.expect(
        "21",
        "orchestrator Bash printf > docs/printf.md -> deny",
        orchestrator("Bash", bash("printf 'x\\n' > docs/printf.md")),
        "deny",
        ORCH_BASH_WRITE_DENY,
    )


def case_22(suite, extra_dirs):
    p = suite.project
    notes = os.path.normpath(str(p / "notes.md"))
    code = os.path.normpath(str(p / "src" / "a.py"))
    doc = os.path.normpath(str(p / "docs" / "guide.md"))
    spec = os.path.normpath(str(p / ".claude" / "specs" / "t.md"))
    vault_note = os.path.normpath(
        os.path.join(os.path.expanduser("~"), "workplace/project/Obsidian_doc", "note.md")
    )
    raw = "\n" + ("A" * 250)
    expected_command = "\\n" + ("A" * 199)

    samples = (
        (
            "reader bash deny logged",
            reader("Bash", bash(raw)),
            "deny",
            READER_BASH_DENY,
            "file-reader",
            "file-reader",
            "Bash",
            None,
            expected_command,
            True,
            None,
        ),
        (
            "reader write deny logged",
            reader("Write", write_input(notes)),
            "deny",
            READER_WRITE_DENY,
            "file-reader",
            "file-reader",
            "Write",
            notes,
            None,
            False,
            None,
        ),
        (
            "writer bash deny logged",
            writer("Bash", bash("ls")),
            "deny",
            WRITER_BASH_DENY,
            "file-writer",
            "file-writer",
            "Bash",
            None,
            "ls",
            True,
            None,
        ),
        (
            "state deny logged",
            writer("Write", write_input(spec)),
            "deny",
            STATE_DENY,
            "file-writer",
            "file-writer",
            "Write",
            spec,
            None,
            False,
            None,
        ),
        (
            "orchestrator read deny logged",
            orchestrator("Read", read_input(code)),
            "deny",
            read_deny("Read on the main thread"),
            "orchestrator",
            "",
            "Read",
            code,
            None,
            False,
            None,
        ),
        (
            "orchestrator doc write deny logged",
            orchestrator("Edit", write_input(doc)),
            "deny",
            ORCH_DOC_DENY,
            "orchestrator",
            "",
            "Edit",
            doc,
            None,
            False,
            None,
        ),
        (
            "other strict deny logged",
            other("Write", write_input(notes)),
            "deny",
            OTHER_DOC_DENY,
            "other",
            "worker",
            "Write",
            notes,
            None,
            False,
            {"CLAUDE_ROUTING_OTHER": "strict"},
        ),
    )
    first_log = None
    for label, body, kind, reason, role, agent_type, tool, path, command, is_bash, env_extra in samples:
        log_path, env = _fresh_env(extra_dirs, env_extra)
        if first_log is None:
            first_log = log_path
        suite.expect("22", label, body, kind, reason, env_extra=env)
        check_audit(
            suite,
            "22",
            label + " record",
            log_path,
            role=role,
            agent_type=agent_type,
            tool=tool,
            path=path,
            decision="deny",
            reason=reason[:120],
            command=command,
            bash=is_bash,
        )

    if first_log is not None and first_log.exists():
        file_mode = stat.S_IMODE(first_log.stat().st_mode)
        dir_mode = stat.S_IMODE(first_log.parent.stat().st_mode)
        suite.check(
            "22",
            "deny log file 0600 and dir 0700",
            file_mode == 0o600 and dir_mode == 0o700,
            f"file={oct(file_mode)} dir={oct(dir_mode)}",
        )
    else:
        suite.check("22", "deny log file 0600 and dir 0700", False, "log missing")

    cursor_log, cursor_env = _fresh_env(extra_dirs)
    binary = cursor_log.parent.parent / "cursor-run"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    cursor_env["CURSOR_RUN_PATH"] = str(binary)
    cursor_cmd = "cursor-agent -p hi"
    suite.expect(
        "22",
        "cursor-agent deny logged",
        orchestrator("Bash", bash(cursor_cmd)),
        "deny",
        CURSOR_AGENT_DENY,
        env_extra=cursor_env,
    )
    check_audit(
        suite,
        "22",
        "cursor-agent deny record",
        cursor_log,
        role="orchestrator",
        agent_type="",
        tool="Bash",
        path=None,
        decision="deny",
        reason=CURSOR_AGENT_DENY[:120],
        command=cursor_cmd,
        bash=True,
    )

    read_tool, read_input_body = obsidian("get_file_contents", "note.md")
    obs_log, obs_env = _fresh_env(extra_dirs)
    suite.expect(
        "22",
        "obsidian read deny logged",
        orchestrator(read_tool, read_input_body),
        "deny",
        read_deny("reading the vault from the main thread"),
        env_extra=obs_env,
    )
    check_audit(
        suite,
        "22",
        "obsidian read deny record",
        obs_log,
        role="orchestrator",
        agent_type="",
        tool=read_tool,
        path=vault_note,
        decision="deny",
        reason=read_deny("reading the vault from the main thread")[:120],
        bash=False,
    )

    ask_tool, ask_input = obsidian("delete_file", "note.md")
    ask_log, ask_env = _fresh_env(extra_dirs)
    suite.expect(
        "22",
        "obsidian ask logged",
        orchestrator(ask_tool, ask_input),
        "ask",
        DELETE_ASK,
        env_extra=ask_env,
    )
    check_audit(
        suite,
        "22",
        "obsidian ask record",
        ask_log,
        role="orchestrator",
        agent_type="",
        tool=ask_tool,
        path=vault_note,
        decision="ask",
        reason=DELETE_ASK[:120],
        bash=False,
    )

    fail_root, good_log = fresh_log("phase5b-logfail-")
    extra_dirs.append(fail_root)
    blocker = fail_root / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    bad_log = blocker / "routing.jsonl"
    body = reader("Bash", bash("ls"))
    good = suite.run(body, env_extra={"ROUTING_LOG": str(good_log)})
    bad = suite.run(body, env_extra={"ROUTING_LOG": str(bad_log)})
    same = False
    detail = f"good={good.returncode} {good.stdout!r} bad={bad.returncode} {bad.stdout!r}"
    try:
        parsed = json.loads(good.stdout)
        same = (
            good.returncode == 0
            and bad.returncode == 0
            and good.stderr == ""
            and bad.stderr == ""
            and good.stdout == bad.stdout
            and parsed["hookSpecificOutput"]["permissionDecision"] == "deny"
            and parsed["hookSpecificOutput"]["permissionDecisionReason"] == READER_BASH_DENY
            and not bad_log.exists()
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        detail = f"{detail} err={exc}"
    suite.check("22", "unwritable routing log does not change hook output", same, detail)


def case_23(suite, extra_dirs):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="phase5b-cursor-quote-")))
    extra_dirs.append(root)
    binary = root / "cursor-run"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    env = {"CURSOR_RUN_PATH": str(binary)}
    suite.expect(
        "23",
        'orchestrator Bash git commit -m "note about cursor-agent" -> silent',
        orchestrator("Bash", bash('git commit -m "note about cursor-agent"')),
        "silent",
        env_extra=env,
    )
    suite.expect(
        "23",
        "orchestrator Bash git commit -m 'cursor-agent' -> silent",
        orchestrator("Bash", bash("git commit -m 'cursor-agent'")),
        "silent",
        env_extra=env,
    )
    suite.expect(
        "23",
        "orchestrator Bash cursor-agent -p hello -> deny",
        orchestrator("Bash", bash("cursor-agent -p hello")),
        "deny",
        CURSOR_AGENT_DENY,
        env_extra=env,
    )


def case_24(suite):
    """Piped stdin filters are not file reads. A file operand, -r/-f, or || still is."""
    bash_read = read_deny("reading files or full diffs through Bash")
    allowed = (
        "git log | grep fix",
        "ls | head",
        "git log --oneline | grep \"a b\"",
        "ls | head -n 5",
        "ls | tail",
        "ls | tail -n 1",
        "ls | less",
        "ls | more",
        "ls | egrep fix",
        "ls | rg pat",
        "ls | sed -n '1p'",
        "ls | awk '{print $1}'",
        "cmd | grep -e x",
        "cmd | grep --regexp x",
        "git diff --stat | grep x",
        "git show --name-only | grep x",
    )
    denied = (
        "cat file | grep x",
        "grep x file",
        "git diff | grep x",
        "git show | grep x",
        "git log -p | head",
        "find .",
        "fd .",
        "tree",
        "cmd | grep -r x",
        "cmd | grep -R x",
        "cmd | rg --recursive x",
        "cmd | grep x file",
        "cmd | grep \"a b\" \"file\"",
        "cmd | grep -e x file",
        "cmd | grep -f pats",
        "cat f | grep x",
        "cmd || grep x f",
        "ls | head file",
        "ls | tail file",
        "ls | rg pat dir",
        "ls | sed -n '1p' file",
        "ls | awk '{print $1}' file",
        'ls | grep "unterminated',
    )
    for command in allowed:
        suite.expect(
            "24",
            f"orchestrator Bash {command!r} -> silent",
            orchestrator("Bash", bash(command)),
            "silent",
        )
    for command in denied:
        suite.expect(
            "24",
            f"orchestrator Bash {command!r} -> deny",
            orchestrator("Bash", bash(command)),
            "deny",
            bash_read,
        )


EXPLORE_AGENT_DENY = "Explore is off in strict mode; use file-reader."


def effective_deny(effective, source):
    return (
        'effective model %s (%s); pass model: "sonnet" (or "haiku"), '
        "or use a pinned agent."
        % (effective, source)
    )
READ_LINE_EXTRA = " Read a smaller range with offset and limit, or use file-reader for large files."


def check_spawn(
    suite, name, log_path, *, role, subagent_type, model, description, mode, decision,
    effective_model, effective_source, reason=None, agent_type="",
):
    records = log_records(log_path)
    if len(records) != 1:
        suite.check("25", name, False, f"records={records!r}")
        return
    rec = records[0]
    allowed = set(ALLOW_LOG_FIELDS) | {
        "subagent_type", "model", "description", "effective_model", "effective_source",
    }
    if decision in ("deny", "ask"):
        allowed.add("reason")
    problems = []
    if set(rec) != allowed:
        problems.append(f"keys={sorted(rec)}")
    if rec.get("role") != role or rec.get("agent_type") != agent_type or rec.get("tool") != "Agent":
        problems.append(
            f"role={rec.get('role')!r} agent_type={rec.get('agent_type')!r} tool={rec.get('tool')!r}"
        )
    if rec.get("subagent_type") != subagent_type:
        problems.append(f"subagent_type={rec.get('subagent_type')!r}")
    if rec.get("model") != model:
        problems.append(f"model={rec.get('model')!r}")
    if rec.get("description") != description:
        problems.append(f"description={rec.get('description')!r}")
    if rec.get("effective_model") != effective_model:
        problems.append(f"effective_model={rec.get('effective_model')!r}")
    if rec.get("effective_source") != effective_source:
        problems.append(f"effective_source={rec.get('effective_source')!r}")
    if rec.get("decision") != decision:
        problems.append(f"decision={rec.get('decision')!r}")
    if not context_ok(rec, suite.project, mode):
        problems.append(
            f"context session_id={rec.get('session_id')!r} project={rec.get('project')!r} mode={rec.get('mode')!r}"
        )
    if decision in ("deny", "ask") and rec.get("reason") != (reason or "")[:120]:
        problems.append(f"reason={rec.get('reason')!r}")
    suite.check("25", name, not problems, "; ".join(problems) + f" record={rec!r}")


def make_temp(extra_dirs, prefix):
    path = Path(os.path.realpath(tempfile.mkdtemp(prefix=prefix)))
    extra_dirs.append(path)
    return path


def write_agent_md(root, agent_type, model):
    path = Path(root) / ".claude" / "agents" / f"{agent_type}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if model is None:
        text = f"---\nname: {agent_type}\ndescription: test\n---\nbody\n"
    else:
        text = f"---\nname: {agent_type}\nmodel: {model}\n---\nbody\n"
    path.write_text(text, encoding="utf-8")
    return path


def run_spawn(
    suite, extra_dirs, name, body, *, mode, decision, effective_model, effective_source,
    subagent_type, model, description, reason=None, env=None,
):
    merged = {"CLAUDE_ROUTING_MODE": mode}
    if env:
        merged.update(env)
    log_path, env_extra = _fresh_env(extra_dirs, merged)
    kind = "deny" if decision == "deny" else "silent"
    suite.expect(
        "25",
        name,
        body,
        kind,
        reason if decision == "deny" else None,
        env_extra=env_extra,
    )
    check_spawn(
        suite,
        name + " logged",
        log_path,
        role="orchestrator",
        subagent_type=subagent_type,
        model=model,
        description=description,
        mode=mode,
        decision="deny" if decision == "deny" else "allow-log",
        effective_model=effective_model,
        effective_source=effective_source,
        reason=reason,
    )


def _spawn_both(suite, extra_dirs, label, body, *, strict, effective_model, effective_source,
                subagent_type, model, description, env):
    for mode in ("strict", "task-aware"):
        decision = strict if mode == "strict" else "allow"
        reason = None
        if decision == "deny":
            reason = effective_deny(effective_model, effective_source)
        run_spawn(
            suite,
            extra_dirs,
            f"{mode} {label}",
            body,
            mode=mode,
            decision=decision,
            effective_model=effective_model,
            effective_source=effective_source,
            subagent_type=subagent_type,
            model=model,
            description=description,
            reason=reason,
            env=env,
        )


def _effective_model_cases(suite, extra_dirs, empty_home):
    """Strict effective-model matrix, repeated in task-aware (allow + log)."""
    home = str(empty_home)
    base = {"HOME": home}
    sonnet_env = {"HOME": home, "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"}
    _spawn_both(
        suite, extra_dirs, "general-purpose inherit",
        payload("Agent", {"subagent_type": "general-purpose", "model": "inherit", "description": "deep", "prompt": "x"}),
        strict="deny", effective_model="inherit-main", effective_source="per-call",
        subagent_type="general-purpose", model="inherit", description="deep", env=base,
    )
    _spawn_both(
        suite, extra_dirs, "Plan no model",
        payload("Agent", {"subagent_type": "Plan", "description": "plan", "prompt": "x"}),
        strict="deny", effective_model="inherit-main", effective_source="builtin-inherit",
        subagent_type="Plan", model="default", description="plan", env=base,
    )
    _spawn_both(
        suite, extra_dirs, "claude no model",
        payload("Agent", {"subagent_type": "claude", "description": "built-in", "prompt": "x"}),
        strict="deny", effective_model="inherit-main", effective_source="builtin-inherit",
        subagent_type="claude", model="default", description="built-in", env=base,
    )
    _spawn_both(
        suite, extra_dirs, "Plan model sonnet",
        payload("Agent", {"subagent_type": "Plan", "model": "sonnet", "description": "plan", "prompt": "x"}),
        strict="allow", effective_model="sonnet", effective_source="per-call",
        subagent_type="Plan", model="sonnet", description="plan", env=base,
    )
    _spawn_both(
        suite, extra_dirs, "general-purpose no model env sonnet",
        payload("Agent", {"description": "env", "prompt": "x"}),
        strict="allow", effective_model="sonnet", effective_source="env-default",
        subagent_type="general-purpose", model="default", description="env", env=sonnet_env,
    )
    _spawn_both(
        suite, extra_dirs, "slack-sync opus",
        payload("Agent", {"subagent_type": "slack-sync", "model": "opus", "description": "sync", "prompt": "x"}),
        strict="deny", effective_model="opus", effective_source="per-call",
        subagent_type="slack-sync", model="opus", description="sync", env=base,
    )
    _spawn_both(
        suite, extra_dirs, "file-writer opus",
        payload("Agent", {"subagent_type": "file-writer", "model": "opus", "description": "write", "prompt": "x"}),
        strict="deny", effective_model="opus", effective_source="per-call",
        subagent_type="file-writer", model="opus", description="write", env=base,
    )
    _spawn_both(
        suite, extra_dirs, "unknown model string",
        payload("Agent", {"subagent_type": "general-purpose", "model": "not-a-family", "description": "odd", "prompt": "x"}),
        strict="deny", effective_model="unknown", effective_source="per-call",
        subagent_type="general-purpose", model="not-a-family", description="odd", env=base,
    )
    allow_plan = {"HOME": home, "CLAUDE_ROUTING_ALLOW_OPUS_SUBAGENT": "1"}
    _spawn_both(
        suite, extra_dirs, "override env does not allow Plan",
        payload("Agent", {"subagent_type": "Plan", "description": "plan", "prompt": "x"}),
        strict="deny", effective_model="inherit-main", effective_source="builtin-inherit",
        subagent_type="Plan", model="default", description="plan", env=allow_plan,
    )

    reader_home = make_temp(extra_dirs, "phase5b-reader-home-")
    write_agent_md(reader_home, "file-reader", "haiku")
    _spawn_both(
        suite, extra_dirs, "user def file-reader haiku",
        payload("Agent", {"subagent_type": "file-reader", "description": "read", "prompt": "x"}),
        strict="allow", effective_model="haiku", effective_source="user-def",
        subagent_type="file-reader", model="default", description="read",
        env={"HOME": str(reader_home)},
    )

    inherit_home = make_temp(extra_dirs, "phase5b-inherit-home-")
    write_agent_md(inherit_home, "inherit-agent", "inherit")
    _spawn_both(
        suite, extra_dirs, "def model inherit beats env sonnet",
        payload("Agent", {"subagent_type": "inherit-agent", "description": "inh", "prompt": "x"}),
        strict="deny", effective_model="inherit-main", effective_source="user-def",
        subagent_type="inherit-agent", model="default", description="inh",
        env={"HOME": str(inherit_home), "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"},
    )

    bare_home = make_temp(extra_dirs, "phase5b-bare-home-")
    write_agent_md(bare_home, "bare-agent", None)
    _spawn_both(
        suite, extra_dirs, "def missing model env sonnet",
        payload("Agent", {"subagent_type": "bare-agent", "description": "bare", "prompt": "x"}),
        strict="allow", effective_model="sonnet", effective_source="env-default",
        subagent_type="bare-agent", model="default", description="bare",
        env={"HOME": str(bare_home), "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"},
    )

    broken_home = make_temp(extra_dirs, "phase5b-broken-home-")
    broken = write_agent_md(broken_home, "broken-agent", "haiku")
    os.chmod(broken, 0)
    try:
        _spawn_both(
            suite, extra_dirs, "unreadable def",
            payload("Agent", {"subagent_type": "broken-agent", "description": "bad", "prompt": "x"}),
            strict="deny", effective_model="unknown", effective_source="unknown",
            subagent_type="broken-agent", model="default", description="bad",
            env={"HOME": str(broken_home)},
        )
    finally:
        os.chmod(broken, 0o644)

    override_home = make_temp(extra_dirs, "phase5b-override-home-")
    write_agent_md(override_home, "file-reader", "opus")
    project_reader = write_agent_md(suite.project, "file-reader", "haiku")
    try:
        _spawn_both(
            suite, extra_dirs, "project def overrides user def",
            payload("Agent", {"subagent_type": "file-reader", "description": "read", "prompt": "x"}),
            strict="allow", effective_model="haiku", effective_source="project-def",
            subagent_type="file-reader", model="default", description="read",
            env={"HOME": str(override_home)},
        )
    finally:
        project_reader.unlink()

    cwd_home = make_temp(extra_dirs, "phase5b-cwd-home-")
    cwd_project = make_temp(extra_dirs, "phase5b-cwd-project-")
    write_agent_md(cwd_home, "cwd-agent", "sonnet")
    project_cwd = write_agent_md(suite.project, "cwd-agent", "opus")
    write_agent_md(cwd_project, "cwd-agent", "haiku")
    try:
        _spawn_both(
            suite, extra_dirs, "payload cwd project def wins",
            payload(
                "Agent",
                {"subagent_type": "cwd-agent", "description": "cwd", "prompt": "x"},
                cwd=str(cwd_project),
            ),
            strict="allow", effective_model="haiku", effective_source="project-def",
            subagent_type="cwd-agent", model="default", description="cwd",
            env={"HOME": str(cwd_home)},
        )
    finally:
        project_cwd.unlink()


def case_25(suite, extra_dirs):
    """Explore role, Agent spawn policy, general-purpose read budget, slack-sync writes."""
    p = suite.project
    code = str(p / "src" / "a.py")
    notes = str(p / "notes.md")

    suite.expect(
        "25",
        "Explore Read src/a.py -> silent (file-reader)",
        payload("Read", read_input(code), agent_id="ex-1", agent_type="Explore"),
        "silent",
    )
    explore_log, explore_env = _fresh_env(extra_dirs)
    suite.expect(
        "25",
        "Explore Write notes.md -> deny",
        payload("Write", write_input(notes), agent_id="ex-1", agent_type="Explore"),
        "deny",
        READER_WRITE_DENY,
        env_extra=explore_env,
    )
    check_audit(
        suite,
        "25",
        "Explore Write logged as file-reader",
        explore_log,
        role="file-reader",
        agent_type="Explore",
        tool="Write",
        path=os.path.normpath(notes),
        decision="deny",
        reason=READER_WRITE_DENY,
    )
    suite.expect(
        "25",
        "Explore Bash echo -> deny",
        payload("Bash", bash("echo hi"), agent_id="ex-1", agent_type="Explore"),
        "deny",
        READER_BASH_DENY,
    )

    empty_home = make_temp(extra_dirs, "phase5b-agent-home-")
    aware_log, aware_env = _fresh_env(
        extra_dirs, {"CLAUDE_ROUTING_MODE": "task-aware", "HOME": str(empty_home)},
    )
    suite.expect(
        "25",
        "task-aware Agent with no model -> silent",
        payload("Agent", {"description": "scan channels", "prompt": "look"}),
        "silent",
        env_extra=aware_env,
    )
    check_spawn(
        suite,
        "task-aware Agent logged",
        aware_log,
        role="orchestrator",
        subagent_type="general-purpose",
        model="default",
        description="scan channels",
        mode="task-aware",
        decision="allow-log",
        effective_model="inherit-main",
        effective_source="builtin-inherit",
    )
    aware_opus_log, aware_opus_env = _fresh_env(
        extra_dirs, {"CLAUDE_ROUTING_MODE": "task-aware"},
    )
    suite.expect(
        "25",
        "task-aware Agent opus -> silent",
        payload("Agent", {"subagent_type": "Explore", "model": "opus", "description": "deep", "prompt": "x"}),
        "silent",
        env_extra=aware_opus_env,
    )
    check_spawn(
        suite,
        "task-aware opus Agent logged allow",
        aware_opus_log,
        role="orchestrator",
        subagent_type="Explore",
        model="opus",
        description="deep",
        mode="task-aware",
        decision="allow-log",
        effective_model="opus",
        effective_source="per-call",
    )

    explore_spawn_log, explore_spawn_env = _fresh_env(extra_dirs)
    suite.expect(
        "25",
        "strict Agent Explore -> deny",
        payload("Agent", {"subagent_type": "Explore", "model": "haiku", "description": "look", "prompt": "x"}),
        "deny",
        EXPLORE_AGENT_DENY,
        env_extra=explore_spawn_env,
    )
    check_spawn(
        suite,
        "strict Explore Agent logged deny",
        explore_spawn_log,
        role="orchestrator",
        subagent_type="Explore",
        model="haiku",
        description="look",
        mode="strict",
        decision="deny",
        reason=EXPLORE_AGENT_DENY,
        effective_model="haiku",
        effective_source="per-call",
    )

    opus_log, opus_env = _fresh_env(extra_dirs)
    suite.expect(
        "25",
        "strict Agent opus general-purpose -> deny",
        payload("Agent", {
            "subagent_type": "general-purpose",
            "model": "claude-opus-4-6",
            "description": "deep",
            "prompt": "x",
        }),
        "deny",
        effective_deny("opus", "per-call"),
        env_extra=opus_env,
    )
    check_spawn(
        suite,
        "strict opus Agent logged deny",
        opus_log,
        role="orchestrator",
        subagent_type="general-purpose",
        model="claude-opus-4-6",
        description="deep",
        mode="strict",
        decision="deny",
        reason=effective_deny("opus", "per-call"),
        effective_model="opus",
        effective_source="per-call",
    )
    omitted_log, omitted_env = _fresh_env(extra_dirs)
    suite.expect(
        "25",
        "strict Agent opus with no subagent_type -> deny",
        payload("Agent", {"model": "opus", "description": "deep", "prompt": "x"}),
        "deny",
        effective_deny("opus", "per-call"),
        env_extra=omitted_env,
    )
    check_spawn(
        suite,
        "omitted type logged as general-purpose",
        omitted_log,
        role="orchestrator",
        subagent_type="general-purpose",
        model="opus",
        description="deep",
        mode="strict",
        decision="deny",
        reason=effective_deny("opus", "per-call"),
        effective_model="opus",
        effective_source="per-call",
    )

    allow_log, allow_env = _fresh_env(extra_dirs, {"CLAUDE_ROUTING_ALLOW_OPUS_SUBAGENT": "1"})
    suite.expect(
        "25",
        "strict Agent opus env override -> deny",
        payload("Agent", {"model": "opus", "description": "deep", "prompt": "x"}),
        "deny",
        effective_deny("opus", "per-call"),
        env_extra=allow_env,
    )
    check_spawn(
        suite,
        "opus env override logged deny",
        allow_log,
        role="orchestrator",
        subagent_type="general-purpose",
        model="opus",
        description="deep",
        mode="strict",
        decision="deny",
        reason=effective_deny("opus", "per-call"),
        effective_model="opus",
        effective_source="per-call",
    )

    plain_log, plain_env = _fresh_env(extra_dirs, {"HOME": str(empty_home)})
    suite.expect(
        "25",
        "strict Agent model absent env unset -> deny",
        payload("Agent", {"description": "batch", "prompt": "x"}),
        "deny",
        effective_deny("inherit-main", "builtin-inherit"),
        env_extra=plain_env,
    )
    check_spawn(
        suite,
        "model absent env unset logged deny",
        plain_log,
        role="orchestrator",
        subagent_type="general-purpose",
        model="default",
        description="batch",
        mode="strict",
        decision="deny",
        reason=effective_deny("inherit-main", "builtin-inherit"),
        effective_model="inherit-main",
        effective_source="builtin-inherit",
    )

    haiku_log, haiku_env = _fresh_env(extra_dirs)
    suite.expect(
        "25",
        "strict Agent file-reader haiku -> silent",
        payload("Agent", {"subagent_type": "file-reader", "model": "haiku", "description": "read", "prompt": "x"}),
        "silent",
        env_extra=haiku_env,
    )
    check_spawn(
        suite,
        "file-reader haiku spawn logged allow",
        haiku_log,
        role="orchestrator",
        subagent_type="file-reader",
        model="haiku",
        description="read",
        mode="strict",
        decision="allow-log",
        effective_model="haiku",
        effective_source="per-call",
    )
    _effective_model_cases(suite, extra_dirs, empty_home)
    suite.expect(
        "25",
        "file-reader Read still silent",
        reader("Read", read_input(code)),
        "silent",
    )

    home = Path(os.path.realpath(tempfile.mkdtemp(prefix="phase5b-gp-home-")))
    extra_dirs.append(home)
    docs = p / "docs"
    docs.mkdir(exist_ok=True)
    budget_env = {"HOME": str(home), "CLAUDE_ROUTING_MODE": "task-aware"}
    session = "gp-files"
    first_log = None
    for index in range(10):
        path = docs / f"gp-{index}.md"
        path.write_text("line\n", encoding="utf-8")
        log_path, env = _fresh_env(extra_dirs, budget_env)
        if first_log is None:
            first_log = log_path
        suite.expect(
            "25",
            f"general-purpose Read file {index} under budget -> silent",
            payload(
                "Read",
                {"file_path": str(path), "limit": 5},
                agent_id="gp-a",
                agent_type="general-purpose",
                session_id=session,
            ),
            "silent",
            env_extra=env,
        )
    first_records = log_records(first_log)
    suite.check(
        "25",
        "general-purpose under-budget Read is logged",
        len(first_records) == 1
        and first_records[0].get("decision") == "allow-budget"
        and first_records[0].get("role") == "other"
        and first_records[0].get("agent_type") == "general-purpose"
        and first_records[0].get("tool") == "Read"
        and first_records[0].get("budget_files") == 1
        and first_records[0].get("lines") == 1
        and first_records[0].get("mode") == "task-aware",
        f"records={first_records!r}",
    )
    over = docs / "gp-over.md"
    over.write_text("line\n", encoding="utf-8")
    suite.expect(
        "25",
        "general-purpose 11th file -> deny",
        payload(
            "Read",
            {"file_path": str(over), "limit": 5},
            agent_id="gp-a",
            agent_type="general-purpose",
            session_id=session,
        ),
        "deny",
        read_deny("Read from general-purpose"),
        env_extra=budget_env,
    )
    other_agent = docs / "gp-b.md"
    other_agent.write_text("line\n", encoding="utf-8")
    other_log, other_env = _fresh_env(extra_dirs, budget_env)
    suite.expect(
        "25",
        "second general-purpose agent still under its own budget -> silent",
        payload(
            "Read",
            {"file_path": str(other_agent), "limit": 5},
            agent_id="gp-b",
            agent_type="general-purpose",
            session_id=session,
        ),
        "silent",
        env_extra=other_env,
    )
    other_records = log_records(other_log)
    suite.check(
        "25",
        "per-agent budget starts at 1 file",
        len(other_records) == 1
        and other_records[0].get("decision") == "allow-budget"
        and other_records[0].get("budget_files") == 1,
        f"records={other_records!r}",
    )
    orch_path = docs / "orch-free.md"
    orch_path.write_text("line\n", encoding="utf-8")
    orch_log, orch_env = _fresh_env(extra_dirs, budget_env)
    suite.expect(
        "25",
        "orchestrator budget is separate from general-purpose -> silent",
        payload(
            "Read",
            {"file_path": str(orch_path), "limit": 5},
            session_id=session,
        ),
        "silent",
        env_extra=orch_env,
    )
    orch_records = log_records(orch_log)
    suite.check(
        "25",
        "orchestrator counter stays separate",
        len(orch_records) == 1
        and orch_records[0].get("role") == "orchestrator"
        and orch_records[0].get("decision") == "allow-budget"
        and orch_records[0].get("budget_files") == 1,
        f"records={orch_records!r}",
    )

    wide = docs / "gp-wide.md"
    wide.write_text("".join(f"{i}\n" for i in range(1500)), encoding="utf-8")
    more = docs / "gp-more.md"
    more.write_text("".join(f"{i}\n" for i in range(600)), encoding="utf-8")
    line_env_base = dict(budget_env)
    wide_log, wide_env = _fresh_env(extra_dirs, line_env_base)
    suite.expect(
        "25",
        "general-purpose 1500 lines under line budget -> silent",
        payload(
            "Read",
            {"file_path": str(wide), "limit": 1500},
            agent_id="gp-line",
            agent_type="general-purpose",
            session_id="gp-lines",
        ),
        "silent",
        env_extra=wide_env,
    )
    wide_records = log_records(wide_log)
    suite.check(
        "25",
        "line budget charges 1500",
        len(wide_records) == 1 and wide_records[0].get("budget_lines") == 1500,
        f"records={wide_records!r}",
    )
    suite.expect(
        "25",
        "general-purpose over line budget -> deny",
        payload(
            "Read",
            {"file_path": str(more), "limit": 600},
            agent_id="gp-line",
            agent_type="general-purpose",
            session_id="gp-lines",
        ),
        "deny",
        read_deny("Read from general-purpose") + READ_LINE_EXTRA,
        env_extra=line_env_base,
    )

    suite.expect(
        "25",
        "strict general-purpose Read -> deny",
        payload("Read", read_input(str(docs / "gp-0.md")), agent_id="gp-a", agent_type="general-purpose"),
        "deny",
        read_deny("Read from general-purpose"),
    )
    suite.expect(
        "25",
        "strict general-purpose Grep -> deny",
        payload("Grep", {"pattern": "x", "path": str(docs)}, agent_id="gp-a", agent_type="general-purpose"),
        "deny",
        read_deny("Grep from general-purpose"),
    )
    suite.expect(
        "25",
        "strict general-purpose Glob -> deny",
        payload("Glob", {"pattern": "*.md", "path": str(docs)}, agent_id="gp-a", agent_type="general-purpose"),
        "deny",
        read_deny("Glob from general-purpose"),
    )
    suite.expect(
        "25",
        "task-aware general-purpose Grep -> deny",
        payload("Grep", {"pattern": "x", "path": str(docs)}, agent_id="gp-a", agent_type="general-purpose"),
        "deny",
        read_deny("Grep from general-purpose"),
        env_extra={"CLAUDE_ROUTING_MODE": "task-aware"},
    )
    suite.expect(
        "25",
        "slack-sync Read stays silent",
        payload("Read", read_input(str(docs / "gp-0.md")), agent_id="slack-1", agent_type="slack-sync"),
        "silent",
    )

    channel = p / "data" / "eng-room"
    channel.mkdir(parents=True)
    archive = channel / "消息-20261104.md"
    source = channel / "source.json"
    archive_log, archive_env = _fresh_env(extra_dirs)
    suite.expect(
        "25",
        "slack-sync Write data archive -> silent",
        payload("Write", write_input(str(archive)), agent_id="slack-1", agent_type="slack-sync"),
        "silent",
        env_extra=archive_env,
    )
    archive_records = log_records(archive_log)
    suite.check(
        "25",
        "slack-sync archive write logged allow",
        len(archive_records) == 1
        and archive_records[0].get("role") == "other"
        and archive_records[0].get("agent_type") == "slack-sync"
        and archive_records[0].get("tool") == "Write"
        and archive_records[0].get("decision") == "allow-log"
        and archive_records[0].get("path") == os.path.normpath(str(archive)),
        f"records={archive_records!r}",
    )
    source_log, source_env = _fresh_env(extra_dirs)
    suite.expect(
        "25",
        "slack-sync Write source.json -> silent",
        payload("Write", write_input(str(source)), agent_id="slack-1", agent_type="slack-sync"),
        "silent",
        env_extra=source_env,
    )
    suite.check(
        "25",
        "slack-sync source.json is not a routed write",
        not source_log.exists(),
        f"records={log_records(source_log)!r}",
    )
    suite.expect(
        "25",
        "slack-sync Write plans -> deny",
        payload("Write", write_input("~/.claude/plans/p.md"), agent_id="slack-1", agent_type="slack-sync"),
        "deny",
        STATE_DENY,
    )


SESSION_COST_DENY = "session-cost must be invoked alone with plain arguments."
SESSION_COST = "~/.claude/hooks/bin/session-cost"
SESSION_COST_ABS = f"{HOME}/.claude/hooks/bin/session-cost"
SESSION_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def case_26(suite):
    plain = f"{SESSION_COST} {SESSION_ID}"
    flagged = f"{SESSION_COST_ABS} {SESSION_ID} --json --no-ccusage"
    for body, label in (
        (reader("Bash", bash(plain)), "reader session-cost -> silent"),
        (reader("Bash", bash(flagged)), "reader absolute session-cost flags -> silent"),
        (reader("Bash", bash(f"  {plain} --json")), "reader indented session-cost -> silent"),
        (
            payload("Bash", bash(plain), agent_id="explore-1", agent_type="Explore"),
            "Explore session-cost -> silent",
        ),
        (orchestrator("Bash", bash(plain)), "orchestrator session-cost -> silent"),
        (orchestrator("Bash", bash(flagged)), "orchestrator absolute session-cost -> silent"),
        (orchestrator("Bash", bash("echo hello")), "orchestrator echo still silent"),
    ):
        suite.expect("26", label, body, "silent")
    unsafe = (
        (f"{plain} | cat", "pipe"),
        (f"{plain}; ls", "semicolon"),
        (f"{plain} > /tmp/session-cost-out", "redirect"),
        (f"{plain} $(id)", "command substitution"),
        (f"{plain} `id`", "backticks"),
        (f"bash -c '{plain}'", "bash -c"),
        (f"sh -c '{plain}'", "sh -c"),
        (f"eval {plain}", "eval"),
    )
    for command, label in unsafe:
        suite.expect(
            "26",
            f"reader session-cost {label} -> deny",
            reader("Bash", bash(command)),
            "deny",
            READER_BASH_DENY,
        )
        suite.expect(
            "26",
            f"orchestrator session-cost {label} -> deny",
            orchestrator("Bash", bash(command)),
            "deny",
            SESSION_COST_DENY,
        )
    suite.expect(
        "26",
        "reader bare session-cost -> deny",
        reader("Bash", bash(f"session-cost {SESSION_ID}")),
        "deny",
        READER_BASH_DENY,
    )
    suite.expect(
        "26",
        "file-writer session-cost -> deny",
        writer("Bash", bash(plain)),
        "deny",
        WRITER_BASH_DENY,
    )


if __name__ == "__main__":
    sys.exit(main())
