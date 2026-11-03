#!/usr/bin/env python3
"""Phase 6c: cursor-run deny message and headless cursor-agent denial.

Subprocess tests. CURSOR_RUN_PATH points at a temp executable unless a case
says otherwise. Prints a table and `Tally: N passed, M failed`.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
HOOK = HOOK_DIR / "require-cursor-agent.py"
FIXTURE = HOOK_DIR / "fixtures" / "cursor-pretooluse-write.json"
HEADLESS_PREFIX = (
    "Start cursor-agent only through ~/.claude/hooks/bin/cursor-run "
    "<printed path> (run cursor-run new <task> and write the spec first)."
)
CURSOR_RUN_LINE = "~/.claude/hooks/bin/cursor-run new <task>"
OLD_FAST_PATH = "Fast path: run this literal command"


def bash_payload(command, **extra):
    body = {"tool_name": "Bash", "tool_input": {"command": command}}
    body.update(extra)
    return body


def run_hook(payload, cursor_run, extra_env=None, project=None):
    env = os.environ.copy()
    env.pop("CURSOR_AGENT", None)
    env["CURSOR_RUN_PATH"] = cursor_run
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if project is not None:
        # Relative src/a.py must not land inside ~/.claude/hooks (that is control).
        env["CLAUDE_PROJECT_DIR"] = str(project)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=str(project) if project is not None else None,
        check=False,
    )


class Suite:
    def __init__(self):
        self.passed = 0
        self.failures = []

    def check(self, number, name, ok, detail=""):
        status = "PASS" if ok else f"FAIL ({detail})"
        print(f"{number:>4}. {name} | {status}")
        if ok:
            self.passed += 1
        else:
            self.failures.append(f"{number}. {name}: {detail}")


def detail_of(completed):
    return (
        f"exit={completed.returncode} stdout={completed.stdout!r} "
        f"stderr={completed.stderr[:400]!r}"
    )


def expect_headless(suite, number, name, command, cursor_run, extra_env=None, payload=None, project=None):
    completed = run_hook(payload or bash_payload(command), cursor_run, extra_env, project)
    ok = (
        completed.returncode == 2
        and completed.stdout == ""
        and "cursor-run" in completed.stderr
        and HEADLESS_PREFIX in completed.stderr
    )
    suite.check(number, name, ok, detail_of(completed))


def expect_allow(suite, number, name, command, cursor_run, extra_env=None, project=None):
    completed = run_hook(bash_payload(command), cursor_run, extra_env, project)
    ok = completed.returncode == 0 and completed.stdout == "" and completed.stderr == ""
    suite.check(number, name, ok, detail_of(completed))


def main():
    suite = Suite()
    home = os.path.expanduser("~")
    with tempfile.TemporaryDirectory(prefix="phase6c-") as temporary:
        cursor_run = Path(temporary) / "cursor-run"
        cursor_run.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        cursor_run.chmod(0o755)
        ready = str(cursor_run)
        project = Path(temporary) / "proj"
        project.mkdir()

        expect_headless(suite, "1", 'cursor-agent -p "x" -> deny', 'cursor-agent -p "x"', ready, project=project)
        expect_headless(
            suite,
            "2",
            "cursor-agent -p with CLAUDE_ROUTING=off -> deny",
            'cursor-agent -p "x"',
            ready,
            extra_env={"CLAUDE_ROUTING": "off"},
            project=project,
        )
        expect_headless(
            suite,
            "3",
            "subagent cursor-agent -p -> deny",
            'cursor-agent -p "x"',
            ready,
            payload=bash_payload(
                'cursor-agent -p "x"',
                agent_id="phase6c-agent",
                agent_type="general-purpose",
            ),
            project=project,
        )
        expect_headless(
            suite,
            "4",
            "cd && cursor-agent --force -p -> deny",
            'cd /tmp && cursor-agent --force -p --model m --workspace . "x"',
            ready,
            project=project,
        )
        expect_headless(suite, "4", "cursor-agent --print x -> deny", "cursor-agent --print x", ready, project=project)
        expect_headless(suite, "5", 'bash -c "cursor-agent -p x" -> deny', 'bash -c "cursor-agent -p x"', ready, project=project)
        expect_headless(suite, "5", "sh -c 'cursor-agent -p x' -> deny", "sh -c 'cursor-agent -p x'", ready, project=project)
        expect_headless(suite, "5", 'eval "cursor-agent -p x" -> deny', 'eval "cursor-agent -p x"', ready, project=project)
        expect_allow(suite, "6", "cursor-agent --version -> allow", "cursor-agent --version", ready, project=project)
        expect_allow(suite, "6", "cursor-agent --list-models -> allow", "cursor-agent --list-models", ready, project=project)
        expect_allow(suite, "6", "cursor-agent status -> allow", "cursor-agent status", ready, project=project)
        expect_allow(
            suite,
            "7",
            'grep "cursor-agent -p" -> allow',
            'grep -rn "cursor-agent -p" ~/.agents',
            ready,
            project=project,
        )
        expect_allow(
            suite,
            "8",
            "tilde cursor-run -> allow",
            "~/.claude/hooks/bin/cursor-run .claude/specs/t.md",
            ready,
            project=project,
        )
        expect_allow(
            suite,
            "8",
            "absolute cursor-run -> allow",
            f"{home}/.claude/hooks/bin/cursor-run .claude/specs/t.md",
            ready,
            project=project,
        )

        absent = "/nonexistent"
        expect_allow(
            suite,
            "9",
            "cursor-run absent: cursor-agent -p x -> allow",
            "cursor-agent -p x",
            absent,
            project=project,
        )
        edit = run_hook(
            {"tool_name": "Edit", "tool_input": {"file_path": "src/a.py"}},
            absent,
            project=project,
        )
        suite.check(
            "9",
            "cursor-run absent: Edit src/a.py uses the old message",
            edit.returncode == 2
            and edit.stdout == ""
            and OLD_FAST_PATH in edit.stderr,
            detail_of(edit),
        )

        ready_edit = run_hook(
            {"tool_name": "Edit", "tool_input": {"file_path": "src/a.py"}},
            ready,
            project=project,
        )
        suite.check(
            "10",
            "cursor-run ready: Edit src/a.py uses the cursor-run message",
            ready_edit.returncode == 2
            and ready_edit.stdout == ""
            and CURSOR_RUN_LINE in ready_edit.stderr
            and "~/.claude/hooks/bin/cursor-run <printed path>" in ready_edit.stderr
            and "Specs must not go in .claude/specs anymore." in ready_edit.stderr
            and "/runner:sub-agents" not in ready_edit.stderr,
            detail_of(ready_edit),
        )

        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cursor = run_hook(fixture, ready, project=project)
        try:
            allow = json.loads(cursor.stdout)
        except json.JSONDecodeError:
            allow = None
        suite.check(
            "11",
            "Cursor fixture -> allow",
            cursor.returncode == 0 and allow == {"permission": "allow"} and cursor.stderr == "",
            detail_of(cursor),
        )

    total = suite.passed + len(suite.failures)
    print(f"Tally: {suite.passed} passed, {len(suite.failures)} failed")
    return 0 if suite.failures == [] and total > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
