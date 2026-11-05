#!/usr/bin/env python3
"""Subprocess tests for route-tools.py pilot and all-writes scope. The hook is not registered.

Prints a table and `Tally: N passed, M failed`. Exits non-zero on failure.
HOME and ROUTING_LOG are temp paths on every invocation so the live
~/.claude/logs/routing.jsonl is never written.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

HOOK = Path(__file__).resolve().parent / "route-tools.py"
ROUTING_READS = Path(__file__).resolve().parent / "bin" / "routing-reads"
LIVE_LOG = Path(os.path.expanduser("~")) / ".claude" / "logs" / "routing.jsonl"

READER_BASH_DENY = "file-reader may only run ~/.claude/hooks/bin/safe-read."
READER_WRITE_DENY = "file-reader is read-only."
ORCH_DOC_DENY = (
    "Not allowed here: editing documents on the main thread. "
    "Give the file-writer subagent (Agent tool) the exact files and changes."
)
ORCH_BASH_WRITE_DENY = (
    "Not allowed here: writing documents through Bash. "
    "Give the file-writer subagent (Agent tool) the exact files and changes."
)
WRITER_BASH_DENY = "file-writer has no shell. Report what you could not do."
WRITER_SEARCH_DENY = "file-writer may not search. Ask the file-reader subagent (Agent tool)."
WRITER_READ_DENY = "file-writer may only Read documents."
WRITER_OBSIDIAN_READ_DENY = "file-writer may not read the vault through Obsidian."
STATE_DENY = "Specs, memory and plans belong to the orchestrator. Report back instead."
CURSOR_AGENT_DENY = "Start cursor-agent only through ~/.claude/hooks/bin/cursor-run <spec>."
READ_LINE_EXTRA = " Read a smaller range with offset and limit, or use file-reader for large files."


def read_deny(subject):
    return (
        f"Not allowed here: {subject}. "
        "Ask the file-reader subagent (Agent tool); batch your questions."
    )


def payload(tool, tool_input, *, agent_id=None, agent_type=None, cwd=None, session_id=None):
    body = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
    }
    if agent_id is not None:
        body["agent_id"] = agent_id
    if agent_type is not None:
        body["agent_type"] = agent_type
    if cwd is not None:
        body["cwd"] = cwd
    if session_id is not None:
        body["session_id"] = session_id
    return body


def reader(tool, tool_input, *, cwd=None):
    return payload(tool, tool_input, agent_id="reader-1", agent_type="file-reader", cwd=cwd)


def writer(tool, tool_input):
    return payload(tool, tool_input, agent_id="writer-1", agent_type="file-writer")


def orchestrator(tool, tool_input, *, cwd=None, session_id=None):
    return payload(tool, tool_input, cwd=cwd, session_id=session_id)


def line_block(count):
    return "".join(f"l{index}\n" for index in range(count))


def log_records(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def bash(command):
    return {"command": command}


def log_stat(path):
    if not path.exists():
        return None
    st = path.stat()
    return (st.st_ino, st.st_size, st.st_mtime_ns)


class Suite:
    def __init__(self, home, project, log_path):
        self.home = home
        self.project = project
        self.log_path = log_path
        self.passed = 0
        self.failed = 0
        self.failures = []

    def run(self, body=None, *, env_extra=None, stdin=None, scope="pilots"):
        env = os.environ.copy()
        for key in (
            "CLAUDE_ROUTING",
            "CLAUDE_ROUTING_MODE",
            "CLAUDE_ROUTING_OTHER",
            "CLAUDE_ROUTING_ALLOW_OPUS_SUBAGENT",
            "CLAUDE_ROUTING_SCOPE",
            "CURSOR_RUN_PATH",
            "CURSOR_AGENT",
            "SUB_AGENTS_DIR",
        ):
            env.pop(key, None)
        env["HOME"] = str(self.home)
        env["CLAUDE_PROJECT_DIR"] = str(self.project)
        env["CLAUDE_ROUTING_MODE"] = "strict"
        env["ROUTING_LOG"] = str(self.log_path)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if scope is not None:
            env["CLAUDE_ROUTING_SCOPE"] = scope
        if env_extra:
            env.update(env_extra)
        if stdin is None:
            stdin = json.dumps(body)
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(self.project),
            check=False,
        )

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
            self.check(number, name, data == {"permission": "allow"}, detail + f" parsed={data!r}")
            return completed
        expected = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": kind,
                "permissionDecisionReason": reason,
            }
        }
        self.check(number, name, data == expected, detail + f" parsed={data!r}")
        return completed


def build_tree(home):
    vault = home / "workplace" / "project" / "Obsidian_doc"
    pt = home / "workplace" / "OlaChat" / "H5_repo" / "pt-zh-web-page"
    sibling_dir = home / "workplace" / "project" / "Obsidian_doc-old"
    outside = home / "elsewhere"
    links = home / "links"
    vault.mkdir(parents=True)
    note = vault / "note.md"
    note.write_text("note\n", encoding="utf-8")
    (vault / "lib").mkdir()
    py = vault / "lib" / "app.py"
    py.write_text("x = 1\n", encoding="utf-8")
    skill = vault / ".claude" / "skills" / "x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# skill\n", encoding="utf-8")
    (vault / "daily").mkdir()
    (pt / "src").mkdir(parents=True)
    page = pt / "src" / "main.ts"
    page.write_text("export {}\n", encoding="utf-8")
    readme = pt / "README.md"
    readme.write_text("# pt\n", encoding="utf-8")
    sibling_dir.mkdir(parents=True)
    sibling = sibling_dir / "x.md"
    sibling.write_text("old\n", encoding="utf-8")
    outside.mkdir()
    outside_md = outside / "notes.md"
    outside_md.write_text("out\n", encoding="utf-8")
    links.mkdir()
    link = links / "vault-note.md"
    link.symlink_to(note)
    return {
        "vault": vault,
        "note": note,
        "py": py,
        "skill": skill,
        "pt": pt,
        "page": page,
        "readme": readme,
        "sibling": sibling,
        "outside": outside_md,
        "link": link,
    }


def all_writes_cases(suite, home, other, tree, log_root, read_main, bash_read):
    """CLAUDE_ROUTING_SCOPE=all-writes, project outside the pilots."""
    doc = "docs/a.md"
    other_doc = "docs/b.md"
    memory = "~/.claude/projects/x/memory/m.md"
    project_env = {"CLAUDE_PROJECT_DIR": str(other)}

    def expect_aw(name, body, kind, reason=None, **extra):
        env = dict(project_env)
        env.update(extra)
        return suite.expect("7", name, body, kind, reason, scope="all-writes", env_extra=env)

    for mode in ("task-aware", "strict"):
        expect_aw(
            f"all-writes {mode} orch Write docs/a.md -> deny",
            orchestrator("Write", {"file_path": doc}),
            "deny",
            ORCH_DOC_DENY,
            CLAUDE_ROUTING_MODE=mode,
        )
    expect_aw(
        "all-writes task-aware orch Edit 5 lines -> silent",
        orchestrator(
            "Edit",
            {"file_path": doc, "old_string": "a\n", "new_string": line_block(5)},
            session_id="aw-5-aware",
        ),
        "silent",
        CLAUDE_ROUTING_MODE="task-aware",
    )
    expect_aw(
        "all-writes strict orch Edit 5 lines -> deny",
        orchestrator(
            "Edit",
            {"file_path": doc, "old_string": "a\n", "new_string": line_block(5)},
            session_id="aw-5-strict",
        ),
        "deny",
        ORCH_DOC_DENY,
        CLAUDE_ROUTING_MODE="strict",
    )
    for mode in ("task-aware", "strict"):
        expect_aw(
            f"all-writes {mode} orch Edit 21 lines -> deny",
            orchestrator(
                "Edit",
                {"file_path": doc, "old_string": "a\n", "new_string": line_block(21)},
                session_id=f"aw-21-{mode}",
            ),
            "deny",
            ORCH_DOC_DENY,
            CLAUDE_ROUTING_MODE=mode,
        )
    expect_aw(
        "all-writes task-aware first file -> silent",
        orchestrator(
            "Edit",
            {"file_path": doc, "old_string": "a\n", "new_string": "b\n"},
            session_id="aw-2-aware",
        ),
        "silent",
        CLAUDE_ROUTING_MODE="task-aware",
    )
    expect_aw(
        "all-writes task-aware second file -> deny",
        orchestrator(
            "Edit",
            {"file_path": other_doc, "old_string": "a\n", "new_string": "b\n"},
            session_id="aw-2-aware",
        ),
        "deny",
        ORCH_DOC_DENY,
        CLAUDE_ROUTING_MODE="task-aware",
    )
    expect_aw(
        "all-writes strict second file -> deny",
        orchestrator(
            "Edit",
            {"file_path": other_doc, "old_string": "a\n", "new_string": "b\n"},
            session_id="aw-2-strict",
        ),
        "deny",
        ORCH_DOC_DENY,
        CLAUDE_ROUTING_MODE="strict",
    )
    expect_aw(
        "all-writes file-writer Write docs/a.md -> silent",
        writer("Write", {"file_path": doc}),
        "silent",
    )
    tmp_root = Path(os.path.realpath(tempfile.mkdtemp(prefix="route-scope-run-", dir=str(home))))
    stamp = "20261102-183328-467288"
    run_dir = tmp_root / other.name / stamp
    spec_dir = run_dir / "specs"
    spec_dir.mkdir(parents=True, mode=0o700)
    os.chmod(tmp_root / other.name, 0o700)
    os.chmod(run_dir, 0o700)
    os.chmod(spec_dir, 0o700)
    (run_dir / "run.json").write_text(json.dumps({"project": str(other)}) + "\n", encoding="utf-8")
    os.chmod(run_dir / "run.json", 0o600)
    spec = str(spec_dir / "task.md")
    spec_env = {"CURSOR_RUN_TMP_ROOT": str(tmp_root)}
    expect_aw(
        "all-writes orch Write run spec -> silent",
        orchestrator("Write", {"file_path": spec}),
        "silent",
        **spec_env,
    )
    expect_aw(
        "all-writes file-writer Write run spec -> deny",
        writer("Write", {"file_path": spec}),
        "deny",
        STATE_DENY,
        **spec_env,
    )
    expect_aw(
        "all-writes orch Write memory -> silent",
        orchestrator("Write", {"file_path": memory}),
        "silent",
    )
    expect_aw(
        "all-writes file-writer Write memory -> deny",
        writer("Write", {"file_path": memory}),
        "deny",
        STATE_DENY,
    )
    expect_aw(
        "all-writes orch Write src/a.py -> silent",
        orchestrator("Write", {"file_path": "src/a.py"}),
        "silent",
    )
    expect_aw(
        "all-writes orch Write skill -> silent",
        orchestrator("Write", {"file_path": ".claude/skills/x/SKILL.md"}),
        "silent",
    )
    expect_aw(
        "all-writes orch Write CLAUDE.md -> silent",
        orchestrator("Write", {"file_path": "CLAUDE.md"}),
        "silent",
    )
    expect_aw(
        "all-writes orch Read docs/a.md -> silent",
        orchestrator("Read", {"file_path": doc}),
        "silent",
    )
    expect_aw(
        "all-writes orch Grep docs/a.md -> silent",
        orchestrator("Grep", {"pattern": "x", "path": doc}),
        "silent",
    )
    expect_aw(
        "all-writes orch Bash cat docs/a.md -> silent",
        orchestrator("Bash", bash("cat docs/a.md")),
        "silent",
    )
    expect_aw(
        "all-writes orch Bash echo > docs/a.md -> deny",
        orchestrator("Bash", bash("echo x > docs/a.md")),
        "deny",
        ORCH_BASH_WRITE_DENY,
    )
    cursor_run = home / "bin" / "cursor-run"
    cursor_run.parent.mkdir(parents=True, exist_ok=True)
    cursor_run.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cursor_run.chmod(0o755)
    expect_aw(
        "all-writes orch Bash git commit mentions cursor-agent -> silent",
        orchestrator("Bash", bash('git commit -m "note about cursor-agent"')),
        "silent",
        CURSOR_RUN_PATH=str(cursor_run),
    )
    gp_log = log_root / "all-writes-other.jsonl"
    doc_abs = os.path.normpath(os.path.join(str(other), "docs", "a.md"))
    expect_aw(
        "all-writes general-purpose Write docs/a.md -> silent",
        payload("Write", {"file_path": doc}, agent_id="gp-1", agent_type="general-purpose"),
        "silent",
        ROUTING_LOG=str(gp_log),
    )
    records = log_records(gp_log)
    logged = records[0] if len(records) == 1 else {}
    suite.check(
        "7",
        "all-writes general-purpose Write docs/a.md logged",
        len(records) == 1
        and logged.get("role") == "other"
        and logged.get("agent_type") == "general-purpose"
        and logged.get("tool") == "Write"
        and logged.get("path") == doc_abs
        and logged.get("decision") == "allow-log"
        and isinstance(logged.get("ts"), str)
        and logged["ts"]
        and set(logged) == {
            "ts", "role", "agent_type", "tool", "path", "decision",
            "session_id", "project", "mode",
        },
        f"records={records!r}",
    )
    note = str(tree["note"])
    expect_aw(
        "all-writes pilot Read -> deny",
        orchestrator("Read", {"file_path": note}),
        "deny",
        read_main,
    )
    expect_aw(
        "all-writes pilot Bash cat -> deny",
        orchestrator("Bash", bash(f"cat {note}")),
        "deny",
        bash_read,
    )
    suite.expect(
        "7",
        "all-writes project is a pilot -> silent",
        orchestrator("Write", {"file_path": note}),
        "silent",
        scope="all-writes",
        env_extra={"CLAUDE_PROJECT_DIR": str(tree["vault"])},
    )
    cursor = {
        "hook_event_name": "preToolUse",
        "cursor_version": "2026.10.22-27dcf5f",
        "tool_name": "Write",
        "tool_input": {"file_path": doc},
    }
    expect_aw("all-writes cursor payload -> allow", cursor, "cursor")
    expect_aw(
        "all-writes CLAUDE_ROUTING=off -> silent",
        orchestrator("Write", {"file_path": doc}),
        "silent",
        CLAUDE_ROUTING="off",
    )


def routing_reads_cases(suite, home, other, tree, log_root, read_main, bash_read):
    """all-writes honours ~/.claude/routing-reads; pilots scope and pilot sessions do not."""
    doc = "docs/a.md"
    project_env = {"CLAUDE_PROJECT_DIR": str(other)}
    read_line = read_main + READ_LINE_EXTRA
    grep_deny = read_deny("Grep on the main thread")
    reads_path = home / ".claude" / "routing-reads"
    note = str(tree["note"])
    readme = str(other / "README.md")
    cursor_run = home / "bin" / "cursor-run"
    if not cursor_run.exists():
        cursor_run.parent.mkdir(parents=True, exist_ok=True)
        cursor_run.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        cursor_run.chmod(0o755)

    def set_reads(value):
        reads_path.parent.mkdir(parents=True, exist_ok=True)
        if value is None:
            if reads_path.exists():
                reads_path.unlink()
            return
        reads_path.write_text(value if value.endswith("\n") else value + "\n", encoding="utf-8")

    def expect_aw(name, body, kind, reason=None, **extra):
        env = dict(project_env)
        env.update(extra)
        return suite.expect("8", name, body, kind, reason, scope="all-writes", env_extra=env)

    def reads_cmd(args, log_path):
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["ROUTING_LOG"] = str(log_path)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, str(ROUTING_READS), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    reads_log = log_root / "routing-reads.jsonl"
    set_reads(None)
    missing = reads_cmd([], reads_log)
    suite.check(
        "8",
        "routing-reads with no file prints off (default)",
        missing.returncode == 0 and missing.stdout == "off (default)\n" and missing.stderr == ""
        and not reads_log.exists(),
        "exit=%s out=%r err=%r" % (missing.returncode, missing.stdout, missing.stderr),
    )
    status = reads_cmd(["status"], reads_log)
    suite.check(
        "8",
        "routing-reads status matches the default",
        status.returncode == 0 and status.stdout == "off (default)\n" and status.stderr == "",
        "out=%r err=%r" % (status.stdout, status.stderr),
    )
    expect_aw(
        "all-writes missing reads cat -> silent",
        orchestrator("Bash", bash("cat docs/a.md")),
        "silent",
    )
    switched = reads_cmd(["on"], reads_log)
    bits = stat.S_IMODE(reads_path.stat().st_mode) if reads_path.exists() else None
    records = log_records(reads_log)
    leftovers = list((home / ".claude").glob(".routing-reads.*.tmp"))
    suite.check(
        "8",
        "routing-reads on writes 0600 and logs the switch",
        switched.returncode == 0
        and switched.stdout == "on (file)\n"
        and switched.stderr == ""
        and reads_path.read_text(encoding="utf-8") == "on\n"
        and bits == 0o600
        and leftovers == []
        and len(records) == 1
        and set(records[0]) == {"ts", "class", "value"}
        and records[0]["class"] == "reads"
        and records[0]["value"] == "on"
        and isinstance(records[0]["ts"], str)
        and records[0]["ts"],
        "exit=%s out=%r bits=%r records=%r leftovers=%r" % (
            switched.returncode, switched.stdout, bits, records, leftovers,
        ),
    )
    expect_aw(
        "all-writes reads on applies on the next cat",
        orchestrator("Bash", bash("cat docs/a.md")),
        "deny",
        bash_read,
        CURSOR_RUN_PATH=str(cursor_run),
    )
    expect_aw(
        "all-writes reads on cursor-agent -> deny",
        orchestrator("Bash", bash("cursor-agent -p hi")),
        "deny",
        CURSOR_AGENT_DENY,
        CURSOR_RUN_PATH=str(cursor_run),
    )
    back = reads_cmd(["off"], reads_log)
    records = log_records(reads_log)
    suite.check(
        "8",
        "routing-reads off switches back and logs",
        back.returncode == 0
        and back.stdout == "off (file)\n"
        and reads_path.read_text(encoding="utf-8") == "off\n"
        and len(records) == 2
        and records[1]["class"] == "reads"
        and records[1]["value"] == "off"
        and set(records[1]) == {"ts", "class", "value"},
        "exit=%s out=%r records=%r" % (back.returncode, back.stdout, records),
    )
    expect_aw(
        "all-writes reads off applies on the next cat",
        orchestrator("Bash", bash("cat docs/a.md")),
        "silent",
        CURSOR_RUN_PATH=str(cursor_run),
    )
    opus_deny = (
        'effective model opus (per-call); pass model: "sonnet" (or "haiku"), '
        "or use a pinned agent."
    )
    agent_log = log_root / "agent-reads-off.jsonl"
    expect_aw(
        "all-writes reads off Agent opus -> deny",
        payload("Agent", {"model": "opus", "description": "batch", "prompt": "go"}),
        "deny",
        opus_deny,
        ROUTING_LOG=str(agent_log),
    )
    agent_records = log_records(agent_log)
    suite.check(
        "8",
        "all-writes reads off Agent opus is logged",
        len(agent_records) == 1
        and agent_records[0].get("tool") == "Agent"
        and agent_records[0].get("decision") == "deny"
        and agent_records[0].get("role") == "orchestrator"
        and agent_records[0].get("subagent_type") == "general-purpose"
        and agent_records[0].get("model") == "opus"
        and agent_records[0].get("description") == "batch"
        and agent_records[0].get("mode") == "strict",
        f"records={agent_records!r}",
    )
    expect_aw(
        "all-writes reads off cursor-agent -> silent",
        orchestrator("Bash", bash("cursor-agent -p hi")),
        "silent",
        CURSOR_RUN_PATH=str(cursor_run),
    )
    log_before = reads_log.read_text(encoding="utf-8")
    file_before = reads_path.read_text(encoding="utf-8")
    again = reads_cmd(["status"], reads_log)
    plain = reads_cmd([], reads_log)
    suite.check(
        "8",
        "status and no args print the value and do not log",
        again.returncode == 0
        and again.stdout == "off (file)\n"
        and plain.returncode == 0
        and plain.stdout == "off (file)\n"
        and reads_log.read_text(encoding="utf-8") == log_before,
        "status=%r plain=%r" % (again.stdout, plain.stdout),
    )
    usage = reads_cmd(["sideways"], reads_log)
    extra = reads_cmd(["on", "off"], reads_log)
    suite.check(
        "8",
        "bad routing-reads arguments exit 2 and change nothing",
        usage.returncode == 2
        and "usage:" in usage.stderr
        and usage.stdout == ""
        and extra.returncode == 2
        and "usage:" in extra.stderr
        and reads_path.read_text(encoding="utf-8") == file_before
        and reads_log.read_text(encoding="utf-8") == log_before,
        "usage=%s/%r extra=%s/%r" % (usage.returncode, usage.stderr, extra.returncode, extra.stderr),
    )
    set_reads(" \n")
    blank = reads_cmd(["status"], reads_log)
    suite.check(
        "8",
        "blank reads file is off (default)",
        blank.returncode == 0 and blank.stdout == "off (default)\n",
        "out=%r" % (blank.stdout,),
    )
    set_reads("nope")
    unknown = reads_cmd(["status"], reads_log)
    suite.check(
        "8",
        "unknown reads file is off (file)",
        unknown.returncode == 0 and unknown.stdout == "off (file)\n" and unknown.stderr == "",
        "out=%r err=%r" % (unknown.stdout, unknown.stderr),
    )

    for toggle in ("on", "off"):
        set_reads(toggle)
        routed = toggle == "on"
        for mode in ("task-aware", "strict"):
            label = "%s reads %s" % (mode, toggle)
            cat_kind = "deny" if routed else "silent"
            expect_aw(
                "all-writes %s cat -> %s" % (label, cat_kind),
                orchestrator("Bash", bash("cat docs/a.md")),
                cat_kind,
                bash_read if routed else None,
                CLAUDE_ROUTING_MODE=mode,
            )
            grep_kind = "deny" if routed else "silent"
            expect_aw(
                "all-writes %s Grep -> %s" % (label, grep_kind),
                orchestrator("Grep", {"pattern": "x", "path": doc}),
                grep_kind,
                grep_deny if routed else None,
                CLAUDE_ROUTING_MODE=mode,
            )
            if not routed:
                read_kind, read_reason = "silent", None
            elif mode == "task-aware":
                read_kind, read_reason = "deny", read_line
            else:
                read_kind, read_reason = "deny", read_main
            expect_aw(
                "all-writes %s Read past budget -> %s" % (label, read_kind),
                orchestrator(
                    "Read",
                    {"file_path": "docs/wide.md", "limit": 2001},
                    session_id="wide-%s-%s" % (toggle, mode),
                ),
                read_kind,
                read_reason,
                CLAUDE_ROUTING_MODE=mode,
            )
            if routed and mode == "task-aware":
                expect_aw(
                    "all-writes %s Read within budget -> silent" % label,
                    orchestrator(
                        "Read",
                        {"file_path": doc, "limit": 1},
                        session_id="small-%s-%s" % (toggle, mode),
                    ),
                    "silent",
                    CLAUDE_ROUTING_MODE=mode,
                )
            expect_aw(
                "all-writes %s writer Grep -> deny" % label,
                writer("Grep", {"pattern": "x", "path": doc}),
                "deny",
                WRITER_SEARCH_DENY,
                CLAUDE_ROUTING_MODE=mode,
            )
            expect_aw(
                "all-writes %s writer Read code -> deny" % label,
                writer("Read", {"file_path": "src/a.py"}),
                "deny",
                WRITER_READ_DENY,
                CLAUDE_ROUTING_MODE=mode,
            )
            expect_aw(
                "all-writes %s Edit 21 lines -> deny" % label,
                orchestrator(
                    "Edit",
                    {"file_path": doc, "old_string": "a\n", "new_string": line_block(21)},
                    session_id="edit21-%s-%s" % (toggle, mode),
                ),
                "deny",
                ORCH_DOC_DENY,
                CLAUDE_ROUTING_MODE=mode,
            )
            small_kind = "silent" if mode == "task-aware" else "deny"
            expect_aw(
                "all-writes %s Edit 5 lines -> %s" % (label, small_kind),
                orchestrator(
                    "Edit",
                    {"file_path": doc, "old_string": "a\n", "new_string": line_block(5)},
                    session_id="edit5-%s-%s" % (toggle, mode),
                ),
                small_kind,
                None if small_kind == "silent" else ORCH_DOC_DENY,
                CLAUDE_ROUTING_MODE=mode,
            )

    for label, value in (("missing", None), ("unknown", "nope")):
        set_reads(value)
        for mode in ("task-aware", "strict"):
            expect_aw(
                "all-writes %s reads %s cat -> silent" % (mode, label),
                orchestrator("Bash", bash("cat docs/a.md")),
                "silent",
                CLAUDE_ROUTING_MODE=mode,
            )
            expect_aw(
                "all-writes %s reads %s Grep -> silent" % (mode, label),
                orchestrator("Grep", {"pattern": "x", "path": doc}),
                "silent",
                CLAUDE_ROUTING_MODE=mode,
            )
            expect_aw(
                "all-writes %s reads %s Read past budget -> silent" % (mode, label),
                orchestrator(
                    "Read",
                    {"file_path": "docs/wide.md", "limit": 2001},
                    session_id="wide-%s-%s" % (label, mode),
                ),
                "silent",
                CLAUDE_ROUTING_MODE=mode,
            )

    for toggle in ("on", "off"):
        set_reads(toggle)
        for mode in ("task-aware", "strict"):
            suite.expect(
                "8",
                "pilot session %s reads %s Read -> silent" % (mode, toggle),
                orchestrator("Read", {"file_path": note}),
                "silent",
                scope="all-writes",
                env_extra={
                    "CLAUDE_PROJECT_DIR": str(tree["vault"]),
                    "CLAUDE_ROUTING_MODE": mode,
                },
            )
        suite.expect(
            "8",
            "pilots scope reads %s local Read -> silent" % toggle,
            orchestrator("Read", {"file_path": readme}),
            "silent",
            scope="pilots",
            env_extra={"CLAUDE_PROJECT_DIR": str(other), "CLAUDE_ROUTING_MODE": "strict"},
        )
        suite.expect(
            "8",
            "pilots scope reads %s vault Read -> deny" % toggle,
            orchestrator("Read", {"file_path": note}),
            "deny",
            read_main,
            scope="pilots",
            env_extra={"CLAUDE_PROJECT_DIR": str(other), "CLAUDE_ROUTING_MODE": "strict"},
        )
        suite.expect(
            "8",
            "unset scope reads %s local Read -> deny" % toggle,
            orchestrator("Read", {"file_path": readme}),
            "deny",
            read_main,
            scope=None,
            env_extra={"CLAUDE_PROJECT_DIR": str(other), "CLAUDE_ROUTING_MODE": "strict"},
        )
    set_reads("on")
    suite.expect(
        "8",
        "CLAUDE_ROUTING=off stays silent with reads on",
        orchestrator("Read", {"file_path": readme}),
        "silent",
        scope="all-writes",
        env_extra={
            "CLAUDE_PROJECT_DIR": str(other),
            "CLAUDE_ROUTING": "off",
            "CLAUDE_ROUTING_MODE": "strict",
        },
    )


def main():
    home = Path(os.path.realpath(tempfile.mkdtemp(prefix="route-scope-home-")))
    log_root = Path(os.path.realpath(tempfile.mkdtemp(prefix="route-scope-log-")))
    other = Path(os.path.realpath(tempfile.mkdtemp(prefix="route-scope-repo-")))
    log_path = log_root / "routing.jsonl"
    live_before = log_stat(LIVE_LOG)
    suite = Suite(home, home, log_path)
    try:
        tree = build_tree(home)
        note = str(tree["note"])
        page = str(tree["page"])
        outside = str(tree["outside"])
        sibling = str(tree["sibling"])
        link = str(tree["link"])
        py = str(tree["py"])
        skill_rel = "workplace/project/Obsidian_doc/.claude/skills/x/SKILL.md"
        note_rel = "workplace/project/Obsidian_doc/note.md"
        read_main = read_deny("Read on the main thread")
        bash_read = read_deny("reading files or full diffs through Bash")
        vault_read = read_deny("reading the vault from the main thread")

        suite.expect("1", "orch Read vault note -> deny", orchestrator("Read", {"file_path": note}), "deny", read_main)
        suite.expect("1", "orch Read pt-zh file -> deny", orchestrator("Read", {"file_path": page}), "deny", read_main)
        suite.expect(
            "1",
            "orch Write vault md -> deny",
            orchestrator("Write", {"file_path": note}),
            "deny",
            ORCH_DOC_DENY,
        )
        suite.expect(
            "1",
            "orch obsidian get_file_contents -> deny",
            orchestrator("mcp__mcp-obsidian__obsidian_get_file_contents", {"filepath": "note.md"}),
            "deny",
            vault_read,
        )
        suite.expect("1", "orch Read outside md -> silent", orchestrator("Read", {"file_path": outside}), "silent")
        suite.expect("1", "orch Write outside md -> silent", orchestrator("Write", {"file_path": outside}), "silent")
        suite.expect("1", "orch Grep no path -> silent", orchestrator("Grep", {"pattern": "TODO"}), "silent")
        suite.expect("1", "orch Bash cat vault file -> deny", orchestrator("Bash", bash(f"cat {note}")), "deny", bash_read)
        suite.expect("1", "orch Bash cat outside file -> silent", orchestrator("Bash", bash(f"cat {outside}")), "silent")
        suite.expect(
            "1",
            "orch Bash cat ~/vault file -> deny",
            orchestrator("Bash", bash("cat ~/workplace/project/Obsidian_doc/note.md")),
            "deny",
            bash_read,
        )
        suite.expect(
            "1",
            "orch Bash ls cwd vault -> silent",
            orchestrator("Bash", bash("ls"), cwd=str(tree["vault"])),
            "silent",
        )
        suite.expect(
            "1",
            "orch Bash cat notes.md cwd vault -> deny",
            orchestrator("Bash", bash("cat notes.md"), cwd=str(tree["vault"])),
            "deny",
            bash_read,
        )
        suite.expect(
            "1",
            "orch Bash cat notes.md tool_input cwd only -> silent",
            orchestrator("Bash", {"command": "cat notes.md", "cwd": str(tree["vault"])}),
            "silent",
        )
        suite.expect("1", "reader Read vault note -> silent", reader("Read", {"file_path": note}), "silent")
        suite.expect(
            "1",
            "reader Bash rm cwd vault -> deny",
            reader("Bash", bash("rm -rf lib"), cwd=str(tree["vault"])),
            "deny",
            READER_BASH_DENY,
        )
        suite.expect("1", "writer Edit vault md -> silent", writer("Edit", {"file_path": note}), "silent")
        suite.expect("1", "writer Edit vault py -> silent", writer("Edit", {"file_path": py}), "silent")
        suite.expect(
            "1",
            "orch Read relative vault note -> deny",
            orchestrator("Read", {"file_path": note_rel}),
            "deny",
            read_main,
        )
        suite.expect(
            "1",
            "reader Write symlink into vault -> deny",
            reader("Write", {"file_path": link}),
            "deny",
            READER_WRITE_DENY,
        )
        suite.expect("1", "orch Read vault-old sibling -> silent", orchestrator("Read", {"file_path": sibling}), "silent")
        suite.expect(
            "1",
            "orch Bash cat vault-old sibling -> silent",
            orchestrator("Bash", bash(f"cat {sibling}")),
            "silent",
        )
        suite.expect(
            "1",
            "orch Read pilot skill is skill_config -> silent",
            orchestrator("Read", {"file_path": skill_rel}),
            "silent",
        )
        both = f"echo hi > {tree['readme']} {note}"
        suite.expect(
            "1",
            "orch Bash first pilot in command wins -> deny",
            orchestrator("Bash", bash(both)),
            "deny",
            ORCH_BASH_WRITE_DENY,
        )

        suite.expect(
            "2",
            "project is the vault -> silent",
            orchestrator("Read", {"file_path": note}),
            "silent",
            env_extra={"CLAUDE_PROJECT_DIR": str(tree["vault"])},
        )
        suite.expect(
            "2",
            "project under the vault -> silent",
            orchestrator("Read", {"file_path": note}),
            "silent",
            env_extra={"CLAUDE_PROJECT_DIR": str(tree["vault"] / "daily")},
        )
        suite.expect(
            "2",
            "other repo Read vault note -> deny",
            orchestrator("Read", {"file_path": note}),
            "deny",
            read_main,
            env_extra={"CLAUDE_PROJECT_DIR": str(other)},
        )
        cursor = {
            "hook_event_name": "preToolUse",
            "cursor_version": "2026.10.22-27dcf5f",
            "tool_name": "Read",
            "tool_input": {"file_path": note},
        }
        suite.expect("2", "cursor payload -> allow", cursor, "cursor")
        suite.expect(
            "2",
            "CLAUDE_ROUTING=off -> silent",
            orchestrator("Read", {"file_path": note}),
            "silent",
            env_extra={"CLAUDE_ROUTING": "off"},
        )
        suite.expect("2", "malformed stdin -> silent", None, "silent", stdin="{")
        suite.expect(
            "2",
            "scope=all outside md stays denied",
            orchestrator("Read", {"file_path": outside}),
            "deny",
            read_main,
            scope="all",
        )
        suite.expect(
            "3",
            "scope unset Read vault note -> deny",
            orchestrator("Read", {"file_path": note}),
            "deny",
            read_main,
            scope=None,
        )
        suite.expect(
            "3",
            "scope unset Read outside md -> deny",
            orchestrator("Read", {"file_path": outside}),
            "deny",
            read_main,
            scope=None,
        )

        readme = other / "README.md"
        readme.write_text("# other\n", encoding="utf-8")
        suite.expect(
            "6",
            "reader Bash outside a pilot still decided -> deny",
            reader("Bash", bash("echo hi")),
            "deny",
            READER_BASH_DENY,
        )
        suite.expect(
            "6",
            "Explore Bash outside a pilot still decided -> deny",
            payload("Bash", bash("echo hi"), agent_id="ex-1", agent_type="Explore"),
            "deny",
            READER_BASH_DENY,
        )
        opus_deny = (
            'effective model opus (per-call); pass model: "sonnet" (or "haiku"), '
            "or use a pinned agent."
        )
        opus_log = log_root / "pilot-agent.jsonl"
        suite.expect(
            "6",
            "pilots scope Agent opus outside a pilot -> deny",
            payload("Agent", {"model": "opus", "description": "batch", "prompt": "go"}),
            "deny",
            opus_deny,
            env_extra={"ROUTING_LOG": str(opus_log)},
        )
        suite.expect(
            "6",
            "reader safe-read outside a pilot -> silent",
            reader("Bash", bash("~/.claude/hooks/bin/safe-read")),
            "silent",
        )
        suite.expect(
            "6",
            "writer Bash outside a pilot still decided -> deny",
            writer("Bash", bash("echo hi")),
            "deny",
            WRITER_BASH_DENY,
        )
        suite.expect(
            "6",
            "writer Write non-pilot spec still decided -> deny",
            writer("Write", {"file_path": str(home / ".claude" / "specs" / "t.md")}),
            "deny",
            STATE_DENY,
        )
        suite.expect(
            "6",
            "writer Write pilot spec does not retarget the project -> silent",
            writer("Write", {"file_path": str(tree["pt"] / ".claude" / "specs" / "t.md")}),
            "silent",
            env_extra={"CLAUDE_PROJECT_DIR": str(other)},
        )
        suite.expect(
            "6",
            "writer Grep -> deny",
            writer("Grep", {"pattern": "x", "path": note}),
            "deny",
            WRITER_SEARCH_DENY,
        )
        suite.expect(
            "6",
            "writer Glob -> deny",
            writer("Glob", {"pattern": "**/*", "path": str(tree["vault"])}),
            "deny",
            WRITER_SEARCH_DENY,
        )
        suite.expect(
            "6",
            "writer Read vault document -> silent",
            writer("Read", {"file_path": note}),
            "silent",
        )
        suite.expect(
            "6",
            "writer Read vault code -> deny",
            writer("Read", {"file_path": py}),
            "deny",
            WRITER_READ_DENY,
        )
        suite.expect(
            "6",
            "writer Read vault canvas -> silent",
            writer("Read", {"file_path": str(tree["vault"] / "board.canvas")}),
            "silent",
        )
        suite.expect(
            "6",
            "writer obsidian get_file_contents -> deny",
            writer("mcp__mcp-obsidian__obsidian_get_file_contents", {"filepath": "note.md"}),
            "deny",
            WRITER_OBSIDIAN_READ_DENY,
        )
        suite.expect(
            "6",
            "session inside a pilot: reader Bash stays silent",
            reader("Bash", bash("echo hi")),
            "silent",
            env_extra={"CLAUDE_PROJECT_DIR": str(tree["vault"])},
        )
        suite.expect(
            "6",
            "orch Read inside a non-pilot project stays silent",
            orchestrator("Read", {"file_path": str(readme)}),
            "silent",
            env_extra={"CLAUDE_PROJECT_DIR": str(other)},
        )
        suite.expect(
            "6",
            "scope unset orch Read inside that project -> deny",
            orchestrator("Read", {"file_path": str(readme)}),
            "deny",
            read_main,
            scope=None,
            env_extra={"CLAUDE_PROJECT_DIR": str(other)},
        )
        all_writes_cases(suite, home, other, tree, log_root, read_main, bash_read)
        routing_reads_cases(suite, home, other, tree, log_root, read_main, bash_read)
        suite.check(
            "3",
            "live routing log untouched",
            log_stat(LIVE_LOG) == live_before,
            f"before={live_before!r} after={log_stat(LIVE_LOG)!r}",
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(log_root, ignore_errors=True)
        shutil.rmtree(other, ignore_errors=True)

    print(f"Tally: {suite.passed} passed, {suite.failed} failed")
    return 0 if suite.failed == 0 and suite.passed > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
