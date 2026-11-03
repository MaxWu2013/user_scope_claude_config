#!/usr/bin/env python3
"""Subprocess tests for routing modes. The hook is not registered.

Prints a table and `Tally: N passed, M failed`. Exits non-zero on failure.
HOME, ROUTING_LOG and routing state are temp paths on every invocation, so
the live log, live state and live mode file are never written.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent
HOOK = ROOT / "route-tools.py"
ROUTING_MODE = ROOT / "bin" / "routing-mode"
CURSOR_RUN = ROOT / "bin" / "cursor-run"
LIVE_LOG = Path(os.path.expanduser("~")) / ".claude" / "logs" / "routing.jsonl"
LIVE_STATE = Path(os.path.expanduser("~")) / ".claude" / "logs" / "routing-state"
LIVE_MODE = Path(os.path.expanduser("~")) / ".claude" / "routing-mode"

READ_DENY = (
    "Not allowed here: Read on the main thread. "
    "Ask the file-reader subagent (Agent tool); batch your questions."
)
READ_LINE_DENY = (
    READ_DENY
    + " Read a smaller range with offset and limit, or use file-reader for large files."
)
GREP_DENY = (
    "Not allowed here: Grep on the main thread. "
    "Ask the file-reader subagent (Agent tool); batch your questions."
)
ORCH_DOC_DENY = (
    "Not allowed here: editing documents on the main thread. "
    "Give the file-writer subagent (Agent tool) the exact files and changes."
)
ORCH_BASH_WRITE_DENY = (
    "Not allowed here: writing documents through Bash. "
    "Give the file-writer subagent (Agent tool) the exact files and changes."
)
VAULT_READ_DENY = (
    "Not allowed here: reading the vault from the main thread. "
    "Ask the file-reader subagent (Agent tool); batch your questions."
)
BASH_READ_DENY = (
    "Not allowed here: reading files or full diffs through Bash. "
    "Ask the file-reader subagent (Agent tool); batch your questions."
)
WRITER_SEARCH_DENY = "file-writer may not search. Ask the file-reader subagent (Agent tool)."
WRITER_READ_DENY = "file-writer may only Read documents."
WRITER_OBSIDIAN_READ_DENY = "file-writer may not read the vault through Obsidian."
WRITER_BASH_DENY = "file-writer has no shell. Report what you could not do."
READER_BASH_DENY = "file-reader may only run ~/.claude/hooks/bin/safe-read."
STATE_DENY = "Specs, memory and plans belong to the orchestrator. Report back instead."

FAKE_AGENT = """#!/usr/bin/env python3
import os
from pathlib import Path

print("agent-ok")
raw = os.environ.get("FAKE_AGENT_WRITES", "")
for line in raw.split("\\n"):
    if not line:
        continue
    path, _, body = line.partition("\\t")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
action = os.environ.get("FAKE_AGENT_GIT", "")
repo = os.environ.get("FAKE_AGENT_REPO", "")
if action == "commit" and repo:
    import subprocess
    rels = [part for part in os.environ.get("FAKE_AGENT_COMMIT_PATHS", "").split(",") if part]
    if rels:
        subprocess.run(["git", "-C", repo, "add", *rels], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "during-run"], check=True)
"""


def snapshot(path):
    if not path.exists():
        return None
    st = path.stat()
    listing = tuple(sorted(os.listdir(path))) if path.is_dir() else None
    return (st.st_ino, st.st_size, st.st_mtime_ns, listing)


def lines(count, word="l"):
    return "".join("%s%s\n" % (word, index) for index in range(count))


def write_lines(path, count):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(lines(count), encoding="utf-8")


def write_state(world, session, payload):
    path = world.state_path(session)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = json.dumps(payload, separators=(",", ":")) + "\n"
    path.write_text(text, encoding="utf-8")
    os.chmod(path.parent, 0o700)
    os.chmod(path, 0o600)
    return path, text


def charged_lines(stored):
    """Distinct lines in a stored read charge, or None when it is not merged."""
    if isinstance(stored, bool) or stored is None:
        return None
    if isinstance(stored, int):
        return stored if stored >= 0 else None
    if not isinstance(stored, list):
        return None
    total = 0
    prev_end = None
    for item in stored:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or isinstance(item[0], bool) or not isinstance(item[0], int)
            or isinstance(item[1], bool) or not isinstance(item[1], int)
            or item[0] < 1
            or item[1] < item[0]
        ):
            return None
        if prev_end is not None and item[0] <= prev_end + 1:
            return None
        total += item[1] - item[0] + 1
        prev_end = item[1]
    return total


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


class World:
    def __init__(self, root):
        self.root = root
        self.home = root / "home"
        self.project = root / "project"
        self.log_path = root / "routing.jsonl"
        self.home.mkdir(parents=True)
        self.project.mkdir(parents=True)
        (self.project / "docs").mkdir()
        (self.project / "src").mkdir()
        write_lines(self.project / "docs" / "a.md", 1)
        write_lines(self.project / "src" / "a.py", 1)

    def env(self, extra=None):
        env = os.environ.copy()
        for key in (
            "CLAUDE_ROUTING",
            "CLAUDE_ROUTING_MODE",
            "CLAUDE_ROUTING_OTHER",
            "CLAUDE_ROUTING_SCOPE",
            "ROUTING_LOG",
            "CURSOR_RUN_PATH",
            "CURSOR_RUN_AGENT_BIN",
            "CURSOR_RUN_TMP_ROOT",
            "CURSOR_RUN_TIMEOUT",
            "FAKE_AGENT_WRITES",
        ):
            env.pop(key, None)
        env["HOME"] = str(self.home)
        env["CLAUDE_PROJECT_DIR"] = str(self.project)
        env["ROUTING_LOG"] = str(self.log_path)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if extra:
            env.update(extra)
        return env

    def hook(self, body, extra=None):
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(body),
            capture_output=True,
            text=True,
            env=self.env(extra),
            cwd=str(self.project),
            check=False,
        )

    def mode_cmd(self, args, extra=None):
        return subprocess.run(
            [sys.executable, str(ROUTING_MODE), *args],
            capture_output=True,
            text=True,
            env=self.env(extra),
            cwd=str(self.project),
            check=False,
        )

    def state_path(self, session_id):
        return self.home / ".claude" / "logs" / "routing-state" / (session_id + ".json")

    def state(self, session_id):
        path = self.state_path(session_id)
        if not path.exists() or path.stat().st_size == 0:
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def log_records(self):
        if not self.log_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


def payload(tool, tool_input, *, session_id="sess", agent_id=None, agent_type=None):
    body = {
        "hook_event_name": "PreToolUse",
        "session_id": session_id,
        "tool_name": tool,
        "tool_input": tool_input,
    }
    if agent_id is not None:
        body["agent_id"] = agent_id
    if agent_type is not None:
        body["agent_type"] = agent_type
    return body


def orch(tool, tool_input, session_id="sess"):
    return payload(tool, tool_input, session_id=session_id)


def writer(tool, tool_input, session_id="sess"):
    return payload(tool, tool_input, session_id=session_id, agent_id="writer-1", agent_type="file-writer")


def reader(tool, tool_input, session_id="sess"):
    return payload(tool, tool_input, session_id=session_id, agent_id="reader-1", agent_type="file-reader")


def decision(completed):
    text = completed.stdout.strip()
    if not text:
        return "silent", None
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return "bad-json", completed.stdout
    hook_out = data.get("hookSpecificOutput") or {}
    return hook_out.get("permissionDecision"), hook_out.get("permissionDecisionReason")


def expect(suite, world, number, name, body, kind, reason=None, extra=None):
    completed = world.hook(body, extra)
    got, got_reason = decision(completed)
    ok = (
        completed.returncode == 0
        and completed.stderr == ""
        and got == kind
        and (reason is None or got_reason == reason)
    )
    suite.check(
        number,
        name,
        ok,
        "exit=%s stderr=%r stdout=%r" % (completed.returncode, completed.stderr[:300], completed.stdout[:500]),
    )
    return completed


def real(path):
    return os.path.realpath(path)


def test_mode_resolution(suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="route-mode-resolve-")))
    try:
        world = World(root)
        doc = str(world.project / "docs" / "a.md")
        completed = world.mode_cmd([])
        suite.check(
            "1",
            "no mode set -> task-aware (default)",
            completed.returncode == 0 and completed.stdout == "task-aware (default)\n" and completed.stderr == "",
            "exit=%s out=%r err=%r" % (completed.returncode, completed.stdout, completed.stderr),
        )
        status = world.mode_cmd(["status"])
        suite.check(
            "1",
            "routing-mode status matches the default",
            status.returncode == 0 and status.stdout == "task-aware (default)\n",
            "out=%r" % (status.stdout,),
        )
        expect(suite, world, "1", "default mode allows a small Read", orch("Read", {"file_path": doc}), "silent")

        mode_file = world.home / ".claude" / "routing-mode"
        mode_file.parent.mkdir(parents=True, exist_ok=True)
        mode_file.write_text("strict\n", encoding="utf-8")
        from_file = world.mode_cmd(["status"])
        suite.check(
            "1",
            "saved strict is strict (file)",
            from_file.returncode == 0 and from_file.stdout == "strict (file)\n",
            "out=%r" % (from_file.stdout,),
        )
        expect(
            suite, world, "1", "saved strict denies the Read",
            orch("Read", {"file_path": doc}, "strict-file"), "deny", READ_DENY,
        )
        env_wins = world.mode_cmd(["status"], {"CLAUDE_ROUTING_MODE": "task-aware"})
        suite.check(
            "1",
            "env task-aware beats the file",
            env_wins.returncode == 0 and env_wins.stdout == "task-aware (env)\n",
            "out=%r" % (env_wins.stdout,),
        )
        expect(
            suite, world, "1", "env task-aware allows the Read",
            orch("Read", {"file_path": doc}, "env-aware"), "silent",
            extra={"CLAUDE_ROUTING_MODE": "task-aware"},
        )
        unknown_env = world.mode_cmd(["status"], {"CLAUDE_ROUTING_MODE": "nope"})
        suite.check(
            "1",
            "unknown env value is strict (env)",
            unknown_env.returncode == 0 and unknown_env.stdout == "strict (env)\n",
            "out=%r" % (unknown_env.stdout,),
        )
        expect(
            suite, world, "1", "unknown env denies the Read",
            orch("Read", {"file_path": doc}, "bad-env"), "deny", READ_DENY,
            extra={"CLAUDE_ROUTING_MODE": "nope"},
        )
        mode_file.write_text("sideways\n", encoding="utf-8")
        unknown_file = world.mode_cmd(["status"])
        suite.check(
            "1",
            "unknown file value is strict (file)",
            unknown_file.returncode == 0 and unknown_file.stdout == "strict (file)\n",
            "out=%r" % (unknown_file.stdout,),
        )
        mode_file.write_text("\n", encoding="utf-8")
        empty = world.mode_cmd(["status"])
        suite.check(
            "1",
            "empty mode file is the default",
            empty.returncode == 0 and empty.stdout == "task-aware (default)\n",
            "out=%r" % (empty.stdout,),
        )
        blank = world.mode_cmd(["status"], {"CLAUDE_ROUTING_MODE": "  "})
        suite.check(
            "1",
            "blank env falls through to the file",
            blank.returncode == 0 and blank.stdout == "task-aware (default)\n",
            "out=%r" % (blank.stdout,),
        )
        off_home = World(root / "off")
        off = off_home.hook(
            orch("Read", {"file_path": str(off_home.project / "docs" / "a.md")}, "off-1"),
            {"CLAUDE_ROUTING": "off", "CLAUDE_ROUTING_MODE": "strict"},
        )
        suite.check(
            "1",
            "CLAUDE_ROUTING=off beats strict",
            off.returncode == 0 and off.stdout == "" and off.stderr == ""
            and not (off_home.home / ".claude" / "logs" / "routing-state").exists(),
            "exit=%s stdout=%r stderr=%r" % (off.returncode, off.stdout, off.stderr),
        )
        writer_off = off_home.hook(
            writer("Grep", {"pattern": "x"}, "off-1"),
            {"CLAUDE_ROUTING": "off"},
        )
        suite.check(
            "1",
            "CLAUDE_ROUTING=off beats a file-writer deny",
            writer_off.returncode == 0 and writer_off.stdout == "" and writer_off.stderr == "",
            "stdout=%r stderr=%r" % (writer_off.stdout, writer_off.stderr),
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_mode_command(suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="route-mode-cmd-")))
    try:
        world = World(root)
        doc_b = world.project / "docs" / "b.md"
        doc_c = world.project / "docs" / "c.md"
        write_lines(doc_b, 1)
        write_lines(doc_c, 1)
        switched = world.mode_cmd(["strict"])
        mode_file = world.home / ".claude" / "routing-mode"
        mode_bits = stat.S_IMODE(mode_file.stat().st_mode) if mode_file.exists() else None
        records = world.log_records()
        suite.check(
            "2",
            "routing-mode strict writes 0600 and logs the switch",
            switched.returncode == 0
            and switched.stdout == "strict (file)\n"
            and mode_file.read_text(encoding="utf-8") == "strict\n"
            and mode_bits == 0o600
            and len(records) == 1
            and set(records[0]) == {"ts", "class", "mode"}
            and records[0]["class"] == "mode"
            and records[0]["mode"] == "strict"
            and isinstance(records[0]["ts"], str)
            and records[0]["ts"],
            "exit=%s out=%r bits=%r records=%r" % (switched.returncode, switched.stdout, mode_bits, records),
        )
        expect(
            suite, world, "2", "strict applies on the next hook call",
            orch("Read", {"file_path": str(doc_b)}, "switch-1"), "deny", READ_DENY,
        )
        back = world.mode_cmd(["task-aware"])
        records = [record for record in world.log_records() if record.get("class") == "mode"]
        suite.check(
            "2",
            "routing-mode task-aware switches back",
            back.returncode == 0
            and back.stdout == "task-aware (file)\n"
            and mode_file.read_text(encoding="utf-8") == "task-aware\n"
            and len(records) == 2
            and records[1]["mode"] == "task-aware"
            and records[1]["class"] == "mode",
            "exit=%s out=%r records=%r" % (back.returncode, back.stdout, records),
        )
        expect(
            suite, world, "2", "task-aware applies on the next hook call",
            orch("Read", {"file_path": str(doc_c)}, "switch-2"), "silent",
        )
        before = mode_file.read_text(encoding="utf-8")
        log_before = world.log_path.read_text(encoding="utf-8")
        usage = world.mode_cmd(["sideways"])
        extra = world.mode_cmd(["strict", "task-aware"])
        suite.check(
            "2",
            "bad routing-mode arguments exit 2 and change nothing",
            usage.returncode == 2
            and "usage:" in usage.stderr
            and extra.returncode == 2
            and mode_file.read_text(encoding="utf-8") == before
            and world.log_path.read_text(encoding="utf-8") == log_before,
            "usage=%s/%r extra=%s/%r" % (usage.returncode, usage.stderr, extra.returncode, extra.stderr),
        )
        status = world.mode_cmd(["status"])
        suite.check(
            "2",
            "status does not append a switch",
            status.returncode == 0 and world.log_path.read_text(encoding="utf-8") == log_before,
            "log changed",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_read_budget(suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="route-mode-read-")))
    try:
        world = World(root)
        session = "read-1"
        target = world.project / "docs" / "budget.md"
        write_lines(target, 100)
        expect(
            suite, world, "3", "limit 40 charges lines 1-40",
            orch("Read", {"file_path": str(target), "limit": 40}, session), "silent",
        )
        expect(
            suite, world, "3", "re-read under the previous charge is free",
            orch("Read", {"file_path": str(target), "limit": 30}, session), "silent",
        )
        state = world.state(session)
        suite.check(
            "3",
            "re-read did not raise the charge",
            state.get("reads", {}).get(real(target)) == [[1, 40]],
            "state=%r" % (state,),
        )
        expect(
            suite, world, "3", "full re-read charges only the extra lines",
            orch("Read", {"file_path": str(target)}, session), "silent",
        )
        expect(
            suite, world, "3", "limit above the file does not raise the charge",
            orch("Read", {"file_path": str(target), "limit": 600}, session), "silent",
        )
        state = world.state(session)
        suite.check(
            "3",
            "a 100-line file is clipped to 100 lines",
            state.get("reads", {}).get(real(target)) == [[1, 100]],
            "state=%r" % (state,),
        )

        tail = world.project / "docs" / "tail.md"
        write_lines(tail, 3000)
        expect(
            suite, world, "3", "lines 2900-3000 are allowed",
            orch("Read", {"file_path": str(tail), "offset": 2900, "limit": 101}, "read-tail"), "silent",
        )
        suite.check(
            "3",
            "lines 2900-3000 cost 101 lines",
            world.state("read-tail").get("reads", {}).get(real(tail)) == [[2900, 3000]],
            "state=%r" % (world.state("read-tail"),),
        )
        expect(
            suite, world, "3", "1899 earlier lines land on 2000",
            orch("Read", {"file_path": str(tail), "limit": 1899}, "read-tail"), "silent",
        )
        suite.check(
            "3",
            "the tail and the prefix stay separate ranges",
            world.state("read-tail").get("reads", {}).get(real(tail)) == [[1, 1899], [2900, 3000]],
            "state=%r" % (world.state("read-tail"),),
        )
        expect(
            suite, world, "3", "line 1900 is denied once 2000 lines are charged",
            orch("Read", {"file_path": str(tail), "offset": 1900, "limit": 1}, "read-tail"), "deny", READ_LINE_DENY,
        )
        suite.check(
            "3",
            "the denied line leaves the 101-line tail charge",
            world.state("read-tail").get("reads", {}).get(real(tail)) == [[1, 1899], [2900, 3000]],
            "state=%r" % (world.state("read-tail"),),
        )

        wide = world.project / "docs" / "wide.md"
        write_lines(wide, 5000)
        expect(
            suite, world, "3", "a 5000-line file with no limit is allowed",
            orch("Read", {"file_path": str(wide)}, "read-window"), "silent",
        )
        suite.check(
            "3",
            "no limit charges the 2000-line read window",
            world.state("read-window").get("reads", {}).get(real(wide)) == [[1, 2000]],
            "state=%r" % (world.state("read-window"),),
        )
        expect(
            suite, world, "3", "line 2001 after a full window is denied",
            orch("Read", {"file_path": str(wide), "offset": 2001, "limit": 1}, "read-window"), "deny", READ_LINE_DENY,
        )
        suite.check(
            "3",
            "the denied line leaves the 2000-line window",
            world.state("read-window").get("reads", {}).get(real(wide)) == [[1, 2000]],
            "state=%r" % (world.state("read-window"),),
        )

        overlap = world.project / "docs" / "overlap.md"
        write_lines(overlap, 100)
        expect(
            suite, world, "3", "first overlap page charges its own lines",
            orch("Read", {"file_path": str(overlap), "offset": 10, "limit": 20}, "read-overlap"), "silent",
        )
        expect(
            suite, world, "3", "an overlapping page charges only new lines",
            orch("Read", {"file_path": str(overlap), "offset": 20, "limit": 20}, "read-overlap"), "silent",
        )
        suite.check(
            "3",
            "overlap merges to the newly covered lines",
            world.state("read-overlap").get("reads", {}).get(real(overlap)) == [[10, 39]],
            "state=%r" % (world.state("read-overlap"),),
        )
        expect(
            suite, world, "3", "an exact re-read is free",
            orch("Read", {"file_path": str(overlap), "offset": 10, "limit": 20}, "read-overlap"), "silent",
        )
        suite.check(
            "3",
            "an exact re-read does not extend the ranges",
            world.state("read-overlap").get("reads", {}).get(real(overlap)) == [[10, 39]],
            "state=%r" % (world.state("read-overlap"),),
        )

        files = []
        session = "read-files"
        for index in range(11):
            path = world.project / "docs" / ("f%s.md" % index)
            write_lines(path, 1)
            files.append(path)
        for index in range(10):
            expect(
                suite, world, "3", "file %s of 10 is allowed" % (index + 1),
                orch("Read", {"file_path": str(files[index])}, session), "silent",
            )
        expect(
            suite, world, "3", "eleventh distinct file is denied",
            orch("Read", {"file_path": str(files[10])}, session), "deny", READ_DENY,
        )
        expect(
            suite, world, "3", "re-read of a counted file is still allowed",
            orch("Read", {"file_path": str(files[0])}, session), "silent",
        )
        state = world.state(session)
        suite.check(
            "3",
            "state remembers exactly ten files",
            set(state.get("reads", {})) == {real(path) for path in files[:10]}
            and all(value == [[1, 1]] for value in state.get("reads", {}).values()),
            "state=%r" % (state,),
        )

        boundary = world.project / "docs" / "boundary.md"
        write_lines(boundary, 2001)
        expect(
            suite, world, "3", "1999 lines are inside the budget",
            orch("Read", {"file_path": str(boundary), "limit": 1999}, "read-boundary"), "silent",
        )
        expect(
            suite, world, "3", "the line that lands on 2000 is allowed",
            orch("Read", {"file_path": str(boundary), "offset": 2000, "limit": 1}, "read-boundary"), "silent",
        )
        suite.check(
            "3",
            "the charge stops at exactly 2000 lines",
            world.state("read-boundary").get("reads", {}).get(real(boundary)) == [[1, 2000]],
            "state=%r" % (world.state("read-boundary"),),
        )
        expect(
            suite, world, "3", "line 2001 is denied",
            orch("Read", {"file_path": str(boundary), "offset": 2001, "limit": 1}, "read-boundary"), "deny", READ_LINE_DENY,
        )
        suite.check(
            "3",
            "the denied 2001st line is not charged",
            world.state("read-boundary").get("reads", {}).get(real(boundary)) == [[1, 2000]],
            "state=%r" % (world.state("read-boundary"),),
        )
        expect(
            suite, world, "3", "a first read of 2001 lines is denied",
            orch("Read", {"file_path": str(boundary), "limit": 2001}, "read-over"), "deny", READ_LINE_DENY,
        )
        suite.check(
            "3",
            "a denied first read records no charge",
            world.state("read-over") == {},
            "state=%r" % (world.state("read-over"),),
        )

        aged = world.project / "docs" / "aged-int.md"
        write_lines(aged, 100)
        _path, _text = write_state(world, "read-old", {"reads": {real(aged): 40}})
        expect(
            suite, world, "3", "old integer state extends past its high line",
            orch("Read", {"file_path": str(aged), "offset": 41, "limit": 10}, "read-old"), "silent",
        )
        suite.check(
            "3",
            "old integer 40 means lines 1-40",
            world.state("read-old").get("reads", {}).get(real(aged)) == [[1, 50]],
            "state=%r" % (world.state("read-old"),),
        )
        expect(
            suite, world, "3", "a re-read inside the old integer is free",
            orch("Read", {"file_path": str(aged), "offset": 10, "limit": 5}, "read-old"), "silent",
        )
        suite.check(
            "3",
            "the free re-read keeps lines 1-50",
            world.state("read-old").get("reads", {}).get(real(aged)) == [[1, 50]],
            "state=%r" % (world.state("read-old"),),
        )
        blocked = world.project / "docs" / "blocked.md"
        write_lines(blocked, 10)
        old_path, old_text = write_state(world, "read-old-full", {"reads": {real(aged): 2000}})
        expect(
            suite, world, "3", "old integer 2000 blocks another line",
            orch("Read", {"file_path": str(blocked), "limit": 1}, "read-old-full"), "deny", READ_LINE_DENY,
        )
        suite.check(
            "3",
            "a denied read leaves the old integer in place",
            old_path.read_text(encoding="utf-8") == old_text,
            "text=%r" % (old_path.read_text(encoding="utf-8"),),
        )

        past = world.project / "docs" / "past.md"
        write_lines(past, 100)
        expect(
            suite, world, "3", "offset past the end is allowed",
            orch("Read", {"file_path": str(past), "offset": 150, "limit": 10}, "read-past"), "silent",
        )
        suite.check(
            "3",
            "offset past the end charges nothing",
            world.state("read-past").get("reads", {}).get(real(past)) == [],
            "state=%r" % (world.state("read-past"),),
        )
        expect(
            suite, world, "3", "offset past the end with no limit charges nothing",
            orch("Read", {"file_path": str(past), "offset": 400}, "read-past"), "silent",
        )
        expect(
            suite, world, "3", "a later in-range read charges only its own lines",
            orch("Read", {"file_path": str(past), "limit": 1}, "read-past"), "silent",
        )
        suite.check(
            "3",
            "the past-end reads added no lines",
            world.state("read-past").get("reads", {}).get(real(past)) == [[1, 1]],
            "state=%r" % (world.state("read-past"),),
        )

        negative = world.project / "docs" / "negative.md"
        write_lines(negative, 20)
        expect(
            suite, world, "3", "a negative offset starts at line 1",
            orch("Read", {"file_path": str(negative), "offset": -5, "limit": 4}, "read-neg"), "silent",
        )
        suite.check(
            "3",
            "negative offset limit 4 charges lines 1-4",
            world.state("read-neg").get("reads", {}).get(real(negative)) == [[1, 4]],
            "state=%r" % (world.state("read-neg"),),
        )

        link = world.project / "docs" / "link.md"
        link.symlink_to(files[0])
        session = "read-link"
        expect(suite, world, "3", "symlink read counts the real file", orch("Read", {"file_path": str(link), "limit": 1}, session), "silent")
        expect(suite, world, "3", "reading the target is the same file", orch("Read", {"file_path": str(files[0]), "limit": 1}, session), "silent")
        state = world.state(session)
        suite.check(
            "3",
            "symlink and target share one budget slot",
            list(state.get("reads", {})) == [real(files[0])] and state["reads"][real(files[0])] == [[1, 1]],
            "state=%r" % (state,),
        )

        claude = world.project / "CLAUDE.md"
        write_lines(claude, 1)
        session = "read-free"
        expect(suite, world, "3", "CLAUDE.md stays allowed without a charge", orch("Read", {"file_path": str(claude)}, session), "silent")
        for index in range(3):
            expect(
                suite, world, "3", "allowed read does not consume slot %s" % (index + 1),
                orch("Read", {"file_path": str(files[index])}, session), "silent",
            )
        state = world.state(session)
        suite.check(
            "3",
            "CLAUDE.md is not in the read budget",
            real(claude) not in state.get("reads", {}) and len(state.get("reads", {})) == 3,
            "state=%r" % (state,),
        )

        locked = world.project / "docs" / "locked.md"
        write_lines(locked, 5)
        os.chmod(locked, 0)
        try:
            expect(
                suite, world, "3", "limit is charged without reading the file",
                orch("Read", {"file_path": str(locked), "limit": 20}, "read-limit"), "silent",
            )
        finally:
            os.chmod(locked, 0o644)
        suite.check(
            "3",
            "an unreadable file keeps the requested window",
            world.state("read-limit").get("reads", {}).get(real(locked)) == [[1, 20]],
            "state=%r" % (world.state("read-limit"),),
        )
        hidden = world.project / "docs" / "hidden.md"
        write_lines(hidden, 5)
        os.chmod(hidden, 0)
        try:
            expect(
                suite, world, "3", "an unreadable file with no limit gets no budget",
                orch("Read", {"file_path": str(hidden)}, "read-hidden"), "deny", READ_DENY,
            )
        finally:
            os.chmod(hidden, 0o644)
        suite.check(
            "3",
            "no-budget read records nothing",
            world.state("read-hidden") == {},
            "state=%r" % (world.state("read-hidden"),),
        )
        expect(
            suite, world, "3", "Grep stays denied in task-aware",
            orch("Grep", {"pattern": "x", "path": str(world.project)}, "read-grep"), "deny", GREP_DENY,
        )
        shared = world.project / "docs" / "shared.md"
        write_lines(shared, 20)
        expect(suite, world, "3", "relative read joins the same file", orch("Read", {"file_path": "docs/shared.md", "limit": 10}, "read-rel"), "silent")
        expect(suite, world, "3", "absolute re-read does not double-charge", orch("Read", {"file_path": str(shared), "limit": 10}, "read-rel"), "silent")
        suite.check(
            "3",
            "relative and absolute paths are one file",
            world.state("read-rel").get("reads") == {real(shared): [[1, 10]]},
            "state=%r" % (world.state("read-rel"),),
        )
        page = world.project / "docs" / "page.md"
        write_lines(page, 100)
        expect(
            suite, world, "3", "offset 0 is the start of the file",
            orch("Read", {"file_path": str(page), "offset": 0, "limit": 25}, "read-offset-zero"), "silent",
        )
        suite.check(
            "3",
            "offset 0 limit 25 charges lines 1-25",
            world.state("read-offset-zero").get("reads", {}).get(real(page)) == [[1, 25]],
            "state=%r" % (world.state("read-offset-zero"),),
        )
        expect(
            suite, world, "3", "offset without limit charges the read window",
            orch("Read", {"file_path": str(page), "offset": 80}, "read-offset-rest"), "silent",
        )
        suite.check(
            "3",
            "offset 80 on a 100-line file charges lines 80-100",
            world.state("read-offset-rest").get("reads", {}).get(real(page)) == [[80, 100]],
            "state=%r" % (world.state("read-offset-rest"),),
        )
        locked_off = world.project / "docs" / "locked-off.md"
        write_lines(locked_off, 10)
        os.chmod(locked_off, 0)
        try:
            expect(
                suite, world, "3", "offset past the end is not clipped when unreadable",
                orch("Read", {"file_path": str(locked_off), "offset": 11, "limit": 5}, "read-offset-locked"), "silent",
            )
        finally:
            os.chmod(locked_off, 0o644)
        suite.check(
            "3",
            "offset 11 limit 5 on an unreadable file charges lines 11-15",
            world.state("read-offset-locked").get("reads", {}).get(real(locked_off)) == [[11, 15]],
            "state=%r" % (world.state("read-offset-locked"),),
        )

        state_path = world.state_path("read-1")
        suite.check(
            "3",
            "state file is 0600 and its directory 0700",
            stat.S_IMODE(state_path.stat().st_mode) == 0o600
            and stat.S_IMODE(state_path.parent.stat().st_mode) == 0o700,
            "file=%s dir=%s" % (oct(stat.S_IMODE(state_path.stat().st_mode)), oct(stat.S_IMODE(state_path.parent.stat().st_mode))),
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_small_edits(suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="route-mode-edit-")))
    try:
        world = World(root)
        vault = world.home / "workplace" / "project" / "Obsidian_doc"
        vault.mkdir(parents=True)
        note = vault / "note.md"
        note.write_text("note\n", encoding="utf-8")
        canvas = vault / "board.canvas"
        canvas.write_text("{}\n", encoding="utf-8")
        first = world.project / "docs" / "edit.md"
        second = world.project / "docs" / "other.md"
        write_lines(first, 1)
        write_lines(second, 1)
        session = "edit-20"
        expect(
            suite, world, "4", "20 changed lines are allowed",
            orch("Edit", {"file_path": str(first), "old_string": "a\n", "new_string": lines(20)}, session),
            "silent",
        )
        state = world.state(session)
        suite.check(
            "4",
            "the edit charge is 20 on that file",
            state.get("edit") == {"path": real(first), "lines": 20},
            "state=%r" % (state,),
        )
        expect(
            suite, world, "4", "the 21st changed line is denied",
            orch("Edit", {"file_path": str(first), "old_string": "a\n", "new_string": "b\n"}, session),
            "deny",
            ORCH_DOC_DENY,
        )
        suite.check(
            "4",
            "a denied edit does not add a line",
            world.state(session).get("edit", {}).get("lines") == 20,
            "state=%r" % (world.state(session),),
        )
        session = "edit-21"
        expect(
            suite, world, "4", "a 21-line edit is denied up front",
            orch("Edit", {"file_path": str(first), "old_string": "a\n", "new_string": lines(21)}, session),
            "deny",
            ORCH_DOC_DENY,
        )
        suite.check(
            "4",
            "the denied 21-line edit is not stored",
            world.state(session).get("edit") is None,
            "state=%r" % (world.state(session),),
        )
        expect(
            suite, world, "4", "old text longer than 20 lines is denied",
            orch("Edit", {"file_path": str(first), "old_string": lines(21), "new_string": "b\n"}, "edit-old"),
            "deny",
            ORCH_DOC_DENY,
        )
        session = "edit-second"
        expect(
            suite, world, "4", "first file of a session is allowed",
            orch("Edit", {"file_path": str(first), "old_string": "a\n", "new_string": "b\n"}, session),
            "silent",
        )
        expect(
            suite, world, "4", "a second file is denied",
            orch("Edit", {"file_path": str(second), "old_string": "a\n", "new_string": "b\n"}, session),
            "deny",
            ORCH_DOC_DENY,
        )
        suite.check(
            "4",
            "the allowance stays on the first file",
            world.state(session).get("edit", {}).get("path") == real(first)
            and world.state(session)["edit"]["lines"] == 1,
            "state=%r" % (world.state(session),),
        )
        session = "edit-split"
        expect(
            suite, world, "4", "two 10-line edits on one file are allowed",
            orch("Edit", {"file_path": str(first), "old_string": "a\n", "new_string": lines(10)}, session),
            "silent",
        )
        expect(
            suite, world, "4", "the second 10-line edit reaches 20",
            orch("Edit", {"file_path": str(first), "old_string": "a\n", "new_string": lines(10, "n")}, session),
            "silent",
        )
        suite.check("4", "split edits total 20", world.state(session)["edit"]["lines"] == 20, "state=%r" % (world.state(session),))
        session = "edit-multi"
        expect(
            suite, world, "4", "MultiEdit of 20 lines is allowed",
            orch("MultiEdit", {
                "file_path": str(second),
                "edits": [
                    {"old_string": "a\n", "new_string": lines(10)},
                    {"old_string": "b\n", "new_string": lines(10, "m")},
                ],
            }, session),
            "silent",
        )
        expect(
            suite, world, "4", "MultiEdit of 21 lines is denied",
            orch("MultiEdit", {
                "file_path": str(first),
                "edits": [
                    {"old_string": lines(15), "new_string": "a\n"},
                    {"old_string": "b\n", "new_string": lines(6)},
                ],
            }, "edit-multi-over"),
            "deny",
            ORCH_DOC_DENY,
        )
        expect(
            suite, world, "4", "Write is not a small edit",
            orch("Write", {"file_path": str(second), "content": "x\n"}, "edit-write"),
            "deny",
            ORCH_DOC_DENY,
        )
        expect(
            suite, world, "4", "Bash document writes stay denied",
            orch("Bash", {"command": "echo x > %s" % second}, "edit-bash"),
            "deny",
            ORCH_BASH_WRITE_DENY,
        )
        expect(
            suite, world, "4", "vault canvas edits stay denied",
            orch("Edit", {"file_path": str(canvas), "old_string": "a\n", "new_string": "b\n"}, "edit-canvas"),
            "deny",
            ORCH_DOC_DENY,
        )
        expect(
            suite, world, "4", "append of 20 lines is allowed",
            orch("mcp__mcp-obsidian__obsidian_append_content", {"filepath": "note.md", "content": lines(20)}, "edit-append"),
            "silent",
        )
        expect(
            suite, world, "4", "append of 21 lines is denied",
            orch("mcp__mcp-obsidian__obsidian_append_content", {"filepath": "note.md", "content": lines(21)}, "edit-append"),
            "deny",
            ORCH_DOC_DENY,
        )
        expect(
            suite, world, "4", "patch of one line on the same note is allowed",
            orch("mcp__mcp-obsidian__obsidian_patch_content", {
                "filepath": "note.md",
                "operation": "replace",
                "target_type": "heading",
                "target": "Title",
                "content": "x\n",
            }, "edit-patch"),
            "silent",
        )
        expect(
            suite, world, "4", "put_content stays denied",
            orch("mcp__mcp-obsidian__obsidian_put_content", {"filepath": "note.md", "content": "x\n"}, "edit-put"),
            "deny",
            ORCH_DOC_DENY,
        )
        code = world.project / "src" / "a.py"
        completed = world.hook(orch("Edit", {"file_path": str(code), "old_string": "a\n", "new_string": "b\n"}, "edit-code"))
        suite.check(
            "4",
            "code edits stay unrouted",
            completed.returncode == 0 and completed.stdout == "" and completed.stderr == "",
            "stdout=%r stderr=%r" % (completed.stdout, completed.stderr),
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_state_edges(suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="route-mode-state-")))
    try:
        world = World(root)
        doc = str(world.project / "docs" / "a.md")
        for session_id, label in (
            ("bad id", "space"),
            ("../x", "traversal"),
            ("a.b", "dot"),
            ("a" * 129, "too long"),
            ("", "empty"),
        ):
            expect(
                suite, world, "5", "bad session id (%s) gets no budget" % label,
                orch("Read", {"file_path": doc}, session_id), "deny", READ_DENY,
            )
        suite.check(
            "5",
            "bad session ids create no state directory",
            not (world.home / ".claude" / "logs" / "routing-state").exists(),
            "dir exists",
        )
        long_id = "a" * 128
        expect(suite, world, "5", "128-character session id is accepted", orch("Read", {"file_path": doc, "limit": 1}, long_id), "silent")

        broken = World(root / "broken")
        logs = broken.home / ".claude" / "logs"
        logs.parent.mkdir(parents=True)
        logs.write_text("not-a-directory\n", encoding="utf-8")
        expect(
            suite, broken, "5", "unwritable state falls back to a deny",
            orch("Read", {"file_path": str(broken.project / "docs" / "a.md")}, "writable"),
            "deny",
            READ_DENY,
        )

        corrupt = World(root / "corrupt")
        session = "corrupt-1"
        path = corrupt.state_path(session)
        path.parent.mkdir(parents=True, mode=0o700)
        path.write_text("{", encoding="utf-8")
        os.chmod(path.parent, 0o700)
        expect(
            suite, corrupt, "5", "corrupt state denies the read",
            orch("Read", {"file_path": str(corrupt.project / "docs" / "a.md"), "limit": 1}, session),
            "deny",
            READ_DENY,
        )
        suite.check(
            "5",
            "corrupt state is left untouched",
            path.read_text(encoding="utf-8") == "{",
            "text=%r" % (path.read_text(encoding="utf-8"),),
        )

        malformed = World(root / "malformed")
        bad_doc = malformed.project / "docs" / "a.md"
        bad_path, bad_text = write_state(
            malformed, "bad-range", {"reads": {real(bad_doc): [[1, 2], [4]]}},
        )
        expect(
            suite, malformed, "5", "malformed ranges deny the read",
            orch("Read", {"file_path": str(bad_doc), "limit": 1}, "bad-range"),
            "deny",
            READ_DENY,
        )
        suite.check(
            "5",
            "malformed ranges are left untouched",
            bad_path.read_text(encoding="utf-8") == bad_text,
            "text=%r" % (bad_path.read_text(encoding="utf-8"),),
        )
        neg_path, neg_text = write_state(
            malformed, "bad-int", {"reads": {real(bad_doc): -3}},
        )
        expect(
            suite, malformed, "5", "a negative integer charge denies the read",
            orch("Read", {"file_path": str(bad_doc), "limit": 1}, "bad-int"),
            "deny",
            READ_DENY,
        )
        suite.check(
            "5",
            "a negative integer charge is left untouched",
            neg_path.read_text(encoding="utf-8") == neg_text,
            "text=%r" % (neg_path.read_text(encoding="utf-8"),),
        )

        aged = World(root / "aged")
        state_dir = aged.home / ".claude" / "logs" / "routing-state"
        state_dir.mkdir(parents=True, mode=0o700)
        old = state_dir / "old.json"
        recent = state_dir / "recent.json"
        old.write_text("{}\n", encoding="utf-8")
        recent.write_text("{}\n", encoding="utf-8")
        now = time.time()
        os.utime(old, (now - 8 * 86400, now - 8 * 86400))
        os.utime(recent, (now - 6 * 86400, now - 6 * 86400))
        expect(
            suite, aged, "5", "a charged read triggers state cleanup",
            orch("Read", {"file_path": str(aged.project / "docs" / "a.md"), "limit": 1}, "keep"),
            "silent",
        )
        suite.check(
            "5",
            "state older than 7 days is deleted",
            not old.exists() and recent.exists() and aged.state_path("keep").exists(),
            "old=%s recent=%s keep=%s" % (old.exists(), recent.exists(), aged.state_path("keep").exists()),
        )

        concurrent = World(root / "concurrent")
        paths = []
        for index in range(16):
            path = concurrent.project / "docs" / ("c%s.md" % index)
            write_lines(path, 1)
            paths.append(str(path))

        def one(path):
            return concurrent.hook(orch("Read", {"file_path": path}, "concurrent"))

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(one, paths))
        allows = sum(1 for item in results if item.returncode == 0 and item.stdout == "" and item.stderr == "")
        denies = sum(1 for item in results if decision(item) == ("deny", READ_DENY))
        stored = concurrent.state("concurrent").get("reads", {})
        suite.check(
            "5",
            "concurrent reads stay inside the 10-file budget",
            allows == 10 and denies == 6 and len(stored) == 10
            and all(value == [[1, 1]] for value in stored.values()),
            "allows=%s denies=%s state=%r stderr=%r" % (
                allows,
                denies,
                stored,
                [item.stderr[:200] for item in results if item.stderr],
            ),
        )

        concurrent_lines = World(root / "concurrent-lines")
        wide = concurrent_lines.project / "docs" / "wide.md"
        write_lines(wide, 4000)
        offsets = [1 + index * 500 for index in range(8)]

        def one_span(offset):
            return concurrent_lines.hook(orch(
                "Read",
                {"file_path": str(wide), "offset": offset, "limit": 500},
                "concurrent-lines",
            ))

        with ThreadPoolExecutor(max_workers=8) as pool:
            line_results = list(pool.map(one_span, offsets))
        line_allows = sum(
            1 for item in line_results
            if item.returncode == 0 and item.stdout == "" and item.stderr == ""
        )
        line_denies = sum(1 for item in line_results if decision(item) == ("deny", READ_LINE_DENY))
        line_stored = concurrent_lines.state("concurrent-lines").get("reads", {})
        suite.check(
            "5",
            "concurrent reads stay inside the 2000-line budget",
            line_allows == 4
            and line_denies == 4
            and len(line_stored) == 1
            and charged_lines(line_stored.get(real(wide))) == 2000,
            "allows=%s denies=%s state=%r stderr=%r" % (
                line_allows,
                line_denies,
                line_stored,
                [item.stderr[:200] for item in line_results if item.stderr],
            ),
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_strict_comparison(suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="route-mode-strict-")))
    try:
        world = World(root)
        doc = str(world.project / "docs" / "a.md")
        extra = {"CLAUDE_ROUTING_MODE": "strict"}
        expect(suite, world, "6", "strict Read has no budget", orch("Read", {"file_path": doc, "limit": 1}, "strict-read"), "deny", READ_DENY, extra)
        expect(
            suite, world, "6", "strict Edit has no allowance",
            orch("Edit", {"file_path": doc, "old_string": "a\n", "new_string": "b\n"}, "strict-edit"),
            "deny",
            ORCH_DOC_DENY,
            extra,
        )
        suite.check(
            "6",
            "strict creates no routing state",
            not (world.home / ".claude" / "logs" / "routing-state").exists(),
            "state dir exists",
        )
        records = world.log_records()
        suite.check(
            "6",
            "strict writes no allowance records",
            [rec.get("decision") for rec in records] == ["deny", "deny"]
            and all(rec.get("mode") == "strict" for rec in records)
            and all(rec.get("decision") not in ("allow-budget", "allow-edit") for rec in records),
            "records=%r" % (records,),
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_agent_permissions(suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="route-mode-agents-")))
    try:
        world = World(root)
        vault = world.home / "workplace" / "project" / "Obsidian_doc"
        vault.mkdir(parents=True)
        note = vault / "note.md"
        note.write_text("note\n", encoding="utf-8")
        canvas = vault / "board.canvas"
        canvas.write_text("{}\n", encoding="utf-8")
        skill = world.home / ".claude" / "skills" / "x" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# skill\n", encoding="utf-8")
        doc = world.project / "docs" / "a.md"
        code = world.project / "src" / "a.py"
        outside = world.root / "outside.md"
        outside.write_text("out\n", encoding="utf-8")
        for mode in ("strict", "task-aware"):
            extra = {"CLAUDE_ROUTING_MODE": mode}
            expect(suite, world, "7", "%s writer Grep denied" % mode, writer("Grep", {"pattern": "x", "path": str(doc)}), "deny", WRITER_SEARCH_DENY, extra)
            expect(suite, world, "7", "%s writer Glob denied" % mode, writer("Glob", {"pattern": "**/*", "path": str(world.project)}), "deny", WRITER_SEARCH_DENY, extra)
            expect(suite, world, "7", "%s writer Read document allowed" % mode, writer("Read", {"file_path": str(doc)}), "silent", extra=extra)
            expect(suite, world, "7", "%s writer Read canvas allowed" % mode, writer("Read", {"file_path": str(canvas)}), "silent", extra=extra)
            expect(suite, world, "7", "%s writer Read code denied" % mode, writer("Read", {"file_path": str(code)}), "deny", WRITER_READ_DENY, extra)
            expect(suite, world, "7", "%s writer Read outside denied" % mode, writer("Read", {"file_path": str(outside)}), "deny", WRITER_READ_DENY, extra)
            expect(suite, world, "7", "%s writer Read skill denied" % mode, writer("Read", {"file_path": str(skill)}), "deny", WRITER_READ_DENY, extra)
            expect(
                suite, world, "7", "%s writer obsidian read denied" % mode,
                writer("mcp__mcp-obsidian__obsidian_get_file_contents", {"filepath": "note.md"}),
                "deny",
                WRITER_OBSIDIAN_READ_DENY,
                extra,
            )
            expect(suite, world, "7", "%s writer Edit document still allowed" % mode, writer("Edit", {"file_path": str(doc)}), "silent", extra=extra)
            expect(
                suite, world, "7", "%s writer Write state still denied" % mode,
                writer("Write", {"file_path": str(world.project / ".claude" / "specs" / "t.md")}),
                "deny",
                STATE_DENY,
                extra,
            )

        other = root / "other-repo"
        other.mkdir()
        (other / "README.md").write_text("# other\n", encoding="utf-8")
        scope = {"CLAUDE_ROUTING_SCOPE": "pilots", "CLAUDE_PROJECT_DIR": str(other)}
        expect(suite, world, "7", "scoped reader Bash in a non-pilot project is denied", reader("Bash", {"command": "echo hi"}), "deny", READER_BASH_DENY, extra=scope)
        expect(
            suite, world, "7", "scoped writer state write in a non-pilot project is denied",
            writer("Write", {"file_path": str(other / ".claude" / "specs" / "t.md")}),
            "deny",
            STATE_DENY,
            extra=scope,
        )
        pt = world.home / "workplace" / "OlaChat" / "H5_repo" / "pt-zh-web-page"
        expect(
            suite, world, "7", "scoped writer does not retarget a pilot spec",
            writer("Write", {"file_path": str(pt / ".claude" / "specs" / "t.md")}),
            "silent",
            extra=scope,
        )
        inside = dict(scope)
        inside["CLAUDE_PROJECT_DIR"] = str(vault)
        expect(suite, world, "7", "a session inside a pilot stays silent", reader("Bash", {"command": "echo hi"}), "silent", extra=inside)
        expect(
            suite, world, "7", "scoped orchestrator ignores a non-pilot document",
            orch("Read", {"file_path": str(other / "README.md")}),
            "silent",
            extra=scope,
        )
        expect(
            suite, world, "7", "scoped orchestrator still denies a pilot document",
            orch("Read", {"file_path": str(note)}),
            "deny",
            READ_DENY,
            extra={"CLAUDE_ROUTING_SCOPE": "pilots", "CLAUDE_ROUTING_MODE": "strict"},
        )
        expect(
            suite, world, "7", "scoped orchestrator vault read stays denied",
            orch("mcp__mcp-obsidian__obsidian_get_file_contents", {"filepath": "note.md"}),
            "deny",
            VAULT_READ_DENY,
            {"CLAUDE_ROUTING_SCOPE": "pilots"},
        )
        expect(
            suite, world, "7", "scoped orchestrator Bash read of a pilot stays denied",
            orch("Bash", {"command": "cat %s" % note}),
            "deny",
            BASH_READ_DENY,
            {"CLAUDE_ROUTING_SCOPE": "pilots"},
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def git(repo, *args, env):
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("%s failed: %s %s" % (args, completed.stdout, completed.stderr))
    return completed


def prepare_repo(repo, env):
    git(repo, "init", "-q", "-b", "main", env=env)
    git(repo, "config", "user.email", "modes@example.com", env=env)
    git(repo, "config", "user.name", "Modes", env=env)
    git(repo, "config", "commit.gpgsign", "false", env=env)
    git(repo, "config", "status.showUntrackedFiles", "normal", env=env)
    git(repo, "config", "core.quotePath", "true", env=env)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    (repo / "kept.txt").write_text("kept\n", encoding="utf-8")
    (repo / "edited.txt").write_text("one\n", encoding="utf-8")
    git(repo, "add", "base.txt", "kept.txt", "edited.txt", env=env)
    git(repo, "commit", "-q", "-m", "first", env=env)
    (repo / "kept.txt").write_text("kept-dirty\n", encoding="utf-8")
    (repo / "edited.txt").write_text("two\n", encoding="utf-8")
    (repo / "same.txt").write_text("before\n", encoding="utf-8")


def cursor_world(root):
    home = root / "home"
    proj = root / "proj"
    agent = root / "fake-agent.py"
    tmp_root = root / "tmp"
    home.mkdir(parents=True)
    proj.mkdir(parents=True)
    tmp_root.mkdir(parents=True, mode=0o700)
    (proj / ".agents").mkdir()
    (proj / ".agents" / "cursor-agent.md").write_text(
        "---\nmodel: test-model\npermission: ask\n---\nbody\n",
        encoding="utf-8",
    )
    agent.write_text(FAKE_AGENT, encoding="utf-8")
    agent.chmod(0o755)
    env = os.environ.copy()
    for key in ("CLAUDE_ROUTING", "CLAUDE_ROUTING_MODE", "CURSOR_RUN_TIMEOUT", "SUB_AGENTS_DIR"):
        env.pop(key, None)
    env["HOME"] = str(home)
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    env["CURSOR_RUN_AGENT_BIN"] = str(agent)
    env["CURSOR_RUN_TMP_ROOT"] = str(tmp_root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["GIT_AUTHOR_NAME"] = "Modes"
    env["GIT_AUTHOR_EMAIL"] = "modes@example.com"
    env["GIT_COMMITTER_NAME"] = "Modes"
    env["GIT_COMMITTER_EMAIL"] = "modes@example.com"
    return home, proj, env


def new_spec(proj, env, body):
    completed = subprocess.run(
        [str(CURSOR_RUN), "new", "task"],
        cwd=str(proj),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("cursor-run new: %s %s %s" % (completed.returncode, completed.stdout, completed.stderr))
    spec = Path(completed.stdout.strip())
    spec.write_text(body, encoding="utf-8")
    return spec


def run_spec(proj, env, spec, writes, mode, extra=None):
    run_env = dict(env)
    run_env["CLAUDE_ROUTING_MODE"] = mode
    if writes:
        run_env["FAKE_AGENT_WRITES"] = "\n".join("%s\t%s" % (path, body) for path, body in writes)
    else:
        run_env.pop("FAKE_AGENT_WRITES", None)
    if extra:
        run_env.update(extra)
    completed = subprocess.run(
        [str(CURSOR_RUN), str(spec)],
        cwd=str(proj),
        env=run_env,
        text=True,
        capture_output=True,
        check=False,
    )
    lines_out = [line for line in completed.stdout.splitlines() if line.strip()]
    payload = json.loads(lines_out[-1]) if lines_out else {}
    return completed, payload


def test_verification(suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="route-mode-verify-")))
    try:
        home, proj, env = cursor_world(root)
        spec = new_spec(proj, env, "# task\nverification_required: false\n")
        completed, payload = run_spec(proj, env, spec, (), "task-aware")
        suite.check(
            "8",
            "task-aware outside git reports no verification on success",
            completed.returncode == 0
            and payload.get("status") == "ok"
            and payload.get("files_changed") is None
            and payload.get("verification_required") is False
            and "run_id" not in payload
            and payload.get("control_changed") is False,
            "exit=%s payload=%r stderr=%r" % (completed.returncode, payload, completed.stderr[:300]),
        )
        completed, payload = run_spec(proj, env, spec, (), "strict")
        suite.check(
            "8",
            "strict outside git reports no verification on success",
            completed.returncode == 0
            and payload.get("files_changed") is None
            and payload.get("verification_required") is False,
            "exit=%s payload=%r" % (completed.returncode, payload),
        )

        repo_root = root / "repo-case"
        _home, repo, repo_env = cursor_world(repo_root)
        prepare_repo(repo, repo_env)
        spec = new_spec(repo, repo_env, "# task\nverification_required: false\n")
        completed, payload = run_spec(repo, repo_env, spec, (), "task-aware")
        suite.check(
            "8",
            "pre-existing dirt is not files_changed",
            completed.returncode == 0
            and payload.get("files_changed") == 0
            and payload.get("verification_required") is False,
            "exit=%s payload=%r stderr=%r" % (completed.returncode, payload, completed.stderr[:300]),
        )
        spec.write_text("# task\nDo the thing.\n", encoding="utf-8")
        completed, payload = run_spec(repo, repo_env, spec, (), "task-aware")
        suite.check(
            "8",
            "zero files on success does not require verification",
            completed.returncode == 0
            and payload.get("status") == "ok"
            and payload.get("files_changed") == 0
            and payload.get("verification_required") is False,
            "payload=%r" % (payload,),
        )
        spec.write_text("# task\nverification_required: false\n", encoding="utf-8")
        completed, payload = run_spec(
            repo, repo_env, spec,
            [(str(repo / "edited.txt"), "three\n")],
            "task-aware",
        )
        suite.check(
            "8",
            "a same-status content change counts",
            completed.returncode == 0
            and payload.get("files_changed") == 1
            and payload.get("verification_required") is False
            and payload.get("control_changed") is False,
            "payload=%r stderr=%r" % (payload, completed.stderr[:300]),
        )
        completed, payload = run_spec(
            repo, repo_env, spec,
            [(str(repo / "same.txt"), "after\n")],
            "task-aware",
        )
        suite.check(
            "8",
            "an untracked file whose text changes counts",
            payload.get("files_changed") == 1 and payload.get("verification_required") is False,
            "payload=%r" % (payload,),
        )
        completed, payload = run_spec(
            repo, repo_env, spec,
            [
                (str(repo / "new.txt"), "new\n"),
                (str(repo / "my file.txt"), "spaced\n"),
                (str(repo / "weird.pyc"), "pyc\n"),
            ],
            "task-aware",
        )
        suite.check(
            "8",
            "new paths count, including spaced names and .pyc",
            completed.returncode == 0 and payload.get("files_changed") == 3 and payload.get("verification_required") is False,
            "payload=%r stderr=%r" % (payload, completed.stderr[:300]),
        )
        completed, payload = run_spec(
            repo, repo_env, spec,
            [(str(repo / "strict-only.txt"), "strict\n")],
            "strict",
        )
        suite.check(
            "8",
            "strict success with a file change does not require verification",
            completed.returncode == 0
            and payload.get("files_changed") == 1
            and payload.get("verification_required") is False
            and payload.get("status") == "ok",
            "payload=%r" % (payload,),
        )
        completed, payload = run_spec(
            repo, repo_env, spec,
            [(str(repo / "base.txt"), "committed\n")],
            "task-aware",
            extra={
                "FAKE_AGENT_GIT": "commit",
                "FAKE_AGENT_REPO": str(repo),
                "FAKE_AGENT_COMMIT_PATHS": "base.txt,edited.txt",
            },
        )
        suite.check(
            "8",
            "a commit during the run counts each path once",
            completed.returncode == 0
            and payload.get("status") == "ok"
            and payload.get("files_changed") == 2
            and payload.get("verification_required") is False,
            "exit=%s payload=%r stderr=%r" % (
                completed.returncode, payload, completed.stderr[:300],
            ),
        )
        completed, payload = run_spec(
            repo, repo_env, spec,
            [(str(repo / "kept.txt"), "kept\n")],
            "task-aware",
        )
        suite.check(
            "8",
            "a dirty file reverted during the run counts",
            completed.returncode == 0
            and payload.get("status") == "ok"
            and payload.get("files_changed") == 1
            and payload.get("verification_required") is False,
            "exit=%s payload=%r stderr=%r" % (
                completed.returncode, payload, completed.stderr[:300],
            ),
        )

        unborn_root = root / "unborn-case"
        _home, unborn, unborn_env = cursor_world(unborn_root)
        git(unborn, "init", "-q", "-b", "main", env=unborn_env)
        spec = new_spec(unborn, unborn_env, "# task\nverification_required: false\n")
        completed, payload = run_spec(
            unborn, unborn_env, spec,
            [(str(unborn / "born.txt"), "x\n")],
            "strict",
        )
        suite.check(
            "8",
            "unresolved HEAD makes the count null",
            completed.returncode == 0
            and payload.get("status") == "ok"
            and payload.get("files_changed") is None
            and payload.get("verification_required") is False
            and "run_id" not in payload,
            "exit=%s payload=%r stderr=%r" % (completed.returncode, payload, completed.stderr[:300]),
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def read_log(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def logged(world, body, log_path, extra=None):
    env = {"ROUTING_LOG": str(log_path)}
    if extra:
        env.update(extra)
    return world.hook(body, env)


def context_matches(rec, project, session_id, mode):
    return (
        "session_id" in rec
        and rec.get("session_id") == session_id
        and rec.get("project") == real(project)
        and rec.get("mode") == mode
    )


def test_log_context(suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="route-log-context-")))
    try:
        world = World(root)
        doc = str(world.project / "docs" / "a.md")
        deny_log = root / "deny.jsonl"
        completed = logged(
            world,
            writer("Bash", {"command": "ls"}, "sess-a"),
            deny_log,
            {"CLAUDE_ROUTING_MODE": "task-aware"},
        )
        records = read_log(deny_log)
        suite.check(
            "10",
            "a deny record has session_id, project and mode",
            completed.returncode == 0
            and decision(completed) == ("deny", WRITER_BASH_DENY)
            and len(records) == 1
            and records[0].get("decision") == "deny"
            and context_matches(records[0], world.project, "sess-a", "task-aware"),
            "stdout=%r records=%r" % (completed.stdout[:300], records),
        )

        for label, body in (
            ("missing", {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "agent_id": "writer-1",
                "agent_type": "file-writer",
            }),
            ("null", {
                "hook_event_name": "PreToolUse",
                "session_id": None,
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "agent_id": "writer-1",
                "agent_type": "file-writer",
            }),
            ("number", {
                "hook_event_name": "PreToolUse",
                "session_id": 4,
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "agent_id": "writer-1",
                "agent_type": "file-writer",
            }),
        ):
            log_path = root / ("null-%s.jsonl" % label)
            completed = logged(world, body, log_path, {"CLAUDE_ROUTING_MODE": "strict"})
            records = read_log(log_path)
            suite.check(
                "10",
                "a %s session_id logs null" % label,
                completed.returncode == 0
                and decision(completed) == ("deny", WRITER_BASH_DENY)
                and len(records) == 1
                and context_matches(records[0], world.project, None, "strict"),
                "stdout=%r records=%r" % (completed.stdout[:300], records),
            )

        other = root / "pazzy-app"
        other.mkdir()
        vault = world.home / "workplace" / "project" / "Obsidian_doc"
        vault.mkdir(parents=True)
        note = vault / "note.md"
        note.write_text("note\n", encoding="utf-8")
        pilot_log = root / "pilot.jsonl"
        completed = logged(
            world,
            orch("Read", {"file_path": str(note)}, "from-pazzy"),
            pilot_log,
            {
                "CLAUDE_PROJECT_DIR": str(other),
                "CLAUDE_ROUTING_SCOPE": "pilots",
                "CLAUDE_ROUTING_MODE": "strict",
            },
        )
        records = read_log(pilot_log)
        suite.check(
            "10",
            "a pilot-path call from another project logs that project",
            completed.returncode == 0
            and decision(completed) == ("deny", READ_DENY)
            and len(records) == 1
            and records[0].get("decision") == "deny"
            and context_matches(records[0], other, "from-pazzy", "strict")
            and records[0].get("project") != real(vault),
            "stdout=%r records=%r" % (completed.stdout[:300], records),
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_allowance_log(suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="route-log-allow-")))
    try:
        world = World(root)
        budget = world.project / "docs" / "budget-log.md"
        more = world.project / "docs" / "budget-more.md"
        write_lines(budget, 100)
        write_lines(more, 20)
        log_path = root / "budget.jsonl"
        session = "budget-log"
        first = logged(world, orch("Read", {"file_path": str(budget), "limit": 40}, session), log_path)
        second = logged(world, orch("Read", {"file_path": str(budget), "limit": 30}, session), log_path)
        third = logged(world, orch("Read", {"file_path": str(more), "limit": 10}, session), log_path)
        records = read_log(log_path)
        suite.check(
            "10",
            "a budget Read writes one allow-budget record with totals",
            first.returncode == 0
            and first.stdout == ""
            and first.stderr == ""
            and len(records) >= 1
            and records[0].get("decision") == "allow-budget"
            and records[0].get("tool") == "Read"
            and records[0].get("lines") == 40
            and records[0].get("budget_files") == 1
            and records[0].get("budget_lines") == 40
            and context_matches(records[0], world.project, session, "task-aware")
            and "edit_lines" not in records[0],
            "stdout=%r records=%r" % (first.stdout[:200], records[:1]),
        )
        suite.check(
            "10",
            "a re-read of the same lines writes lines 0",
            second.returncode == 0
            and second.stdout == ""
            and len(records) >= 2
            and records[1].get("decision") == "allow-budget"
            and records[1].get("lines") == 0
            and records[1].get("budget_files") == 1
            and records[1].get("budget_lines") == 40
            and context_matches(records[1], world.project, session, "task-aware"),
            "stdout=%r records=%r" % (second.stdout[:200], records[1:2]),
        )
        suite.check(
            "10",
            "a later file updates the session totals",
            third.returncode == 0
            and third.stdout == ""
            and len(records) == 3
            and records[2].get("decision") == "allow-budget"
            and records[2].get("lines") == 10
            and records[2].get("budget_files") == 2
            and records[2].get("budget_lines") == 50,
            "stdout=%r records=%r" % (third.stdout[:200], records[2:3]),
        )

        outside = root / "outside.md"
        outside.write_text("out\n", encoding="utf-8")
        free_log = root / "free.jsonl"
        free = logged(world, orch("Read", {"file_path": str(outside)}, "free-read"), free_log)
        writer_log = root / "writer.jsonl"
        writer_edit = logged(
            world,
            writer("Edit", {"file_path": str(world.project / "docs" / "a.md"), "old_string": "a\n", "new_string": "b\n"}, "writer-edit"),
            writer_log,
        )
        suite.check(
            "10",
            "calls allowed for other reasons write nothing",
            free.returncode == 0
            and free.stdout == ""
            and free.stderr == ""
            and not free_log.exists()
            and writer_edit.returncode == 0
            and writer_edit.stdout == ""
            and writer_edit.stderr == ""
            and not writer_log.exists(),
            "free=%r writer=%r" % (free.stdout[:200], writer_edit.stdout[:200]),
        )

        edited = world.project / "docs" / "edit-log.md"
        write_lines(edited, 1)
        edit_log = root / "edit.jsonl"
        edit_session = "edit-log"
        allowed = logged(
            world,
            orch("Edit", {"file_path": str(edited), "old_string": "a\n", "new_string": lines(20)}, edit_session),
            edit_log,
        )
        denied = logged(
            world,
            orch("Edit", {"file_path": str(edited), "old_string": "a\n", "new_string": "b\n"}, edit_session),
            edit_log,
        )
        records = read_log(edit_log)
        suite.check(
            "10",
            "a small edit writes one allow-edit record",
            allowed.returncode == 0
            and allowed.stdout == ""
            and allowed.stderr == ""
            and len(records) >= 1
            and records[0].get("decision") == "allow-edit"
            and records[0].get("tool") == "Edit"
            and records[0].get("lines") == 20
            and records[0].get("edit_lines") == 20
            and context_matches(records[0], world.project, edit_session, "task-aware")
            and "budget_lines" not in records[0],
            "stdout=%r records=%r" % (allowed.stdout[:200], records[:1]),
        )
        suite.check(
            "10",
            "the denied 21st line writes a deny record, not allow-edit",
            denied.returncode == 0
            and decision(denied) == ("deny", ORCH_DOC_DENY)
            and len(records) == 2
            and records[1].get("decision") == "deny"
            and records[1].get("reason") == ORCH_DOC_DENY[:120]
            and "edit_lines" not in records[1]
            and context_matches(records[1], world.project, edit_session, "task-aware"),
            "stdout=%r records=%r" % (denied.stdout[:300], records[1:2]),
        )

        summed = world.project / "docs" / "edit-sum.md"
        write_lines(summed, 1)
        sum_log = root / "edit-sum.jsonl"
        sum_session = "edit-sum"
        logged(world, orch("Edit", {"file_path": str(summed), "old_string": "a\n", "new_string": lines(8)}, sum_session), sum_log)
        logged(world, orch("Edit", {"file_path": str(summed), "old_string": "a\n", "new_string": lines(3, "n")}, sum_session), sum_log)
        records = read_log(sum_log)
        suite.check(
            "10",
            "edit_lines is the session total after the charge",
            len(records) == 2
            and records[0].get("decision") == "allow-edit"
            and records[0].get("lines") == 8
            and records[0].get("edit_lines") == 8
            and records[1].get("decision") == "allow-edit"
            and records[1].get("lines") == 3
            and records[1].get("edit_lines") == 11,
            "records=%r" % (records,),
        )

        blocker = root / "not-a-directory"
        blocker.write_text("x", encoding="utf-8")
        bad_log = blocker / "routing.jsonl"
        target = world.project / "docs" / "unwritable.md"
        write_lines(target, 10)
        bad = logged(
            world,
            orch("Read", {"file_path": str(target), "limit": 5}, "budget-unwritable"),
            bad_log,
        )
        good_log = root / "writable.jsonl"
        good = logged(
            world,
            orch("Read", {"file_path": str(target), "limit": 5}, "budget-writable"),
            good_log,
        )
        state = world.state("budget-unwritable")
        suite.check(
            "10",
            "an unwritable log changes no decision",
            bad.returncode == 0
            and bad.stdout == ""
            and bad.stderr == ""
            and good.returncode == 0
            and good.stdout == ""
            and good.stderr == ""
            and bad.stdout == good.stdout
            and not bad_log.exists()
            and state.get("reads", {}).get(real(target)) == [[1, 5]]
            and read_log(good_log)
            and read_log(good_log)[0].get("decision") == "allow-budget",
            "bad=%r good=%r state=%r" % (bad.stdout[:200], good.stdout[:200], state),
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    live = {
        "log": snapshot(LIVE_LOG),
        "state": snapshot(LIVE_STATE),
        "mode": snapshot(LIVE_MODE),
    }
    suite = Suite()
    try:
        test_mode_resolution(suite)
        test_mode_command(suite)
        test_read_budget(suite)
        test_small_edits(suite)
        test_state_edges(suite)
        test_strict_comparison(suite)
        test_agent_permissions(suite)
        test_verification(suite)
        test_log_context(suite)
        test_allowance_log(suite)
        after = {
            "log": snapshot(LIVE_LOG),
            "state": snapshot(LIVE_STATE),
            "mode": snapshot(LIVE_MODE),
        }
        suite.check("9", "live log, state and mode file are untouched", after == live, "before=%r after=%r" % (live, after))
    finally:
        pass
    print("Tally: %s passed, %s failed" % (suite.passed, suite.failed))
    return 0 if suite.failed == 0 and suite.passed > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
