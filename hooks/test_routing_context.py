#!/usr/bin/env python3
"""Subprocess tests for routing-context.py. The hook is not registered."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "routing-context.py")
REQUIRED = ("cursor-run new", "printed path", "file-writer", "main session")
CODE_SENTENCE = (
    "Code goes only through ~/.claude/hooks/bin/cursor-run new <task>, "
    "then write the spec at the printed path, "
    "then ~/.claude/hooks/bin/cursor-run <path>."
)
STATE_SENTENCE = "Specs, memory and plans belong to the main session."
DOC_STRICT = "Document edits go to the file-writer subagent."
DOC_TASK = (
    "One small edit of up to 20 lines in one file per session is allowed on the main thread, "
    "and other document work goes to file-writer."
)
READ_STRICT = (
    "Direct Read, Grep, Glob and Bash reads on the main thread are denied; use file-reader."
)
READ_TASK = (
    "Read is allowed for 10 files and 2000 lines per session, "
    "and larger reads, Grep, Glob, Bash reads and search go to file-reader."
)
SAFE_READ = "The file-reader searches with safe-read rg -e A -e B (no | or parentheses)."
ARBITRATION_TASK = (
    "Opus subagents need arbitration: if a hook replies ARBITRATION_REQUIRED or "
    "ARBITRATION_CANDIDATE, launch the opus-judge agent in the foreground with the short "
    "prompt the hook gives (ARBITRATION-PACKET request_id=…), then follow its "
    "ARBITRATION_DECISION (CONTINUE/RETRY at most once/STOP without Opus; ESCALATE: issue "
    "exactly the granted call). Never retry an Opus call directly, never launch opus-judge "
    "without a packet, and never use background or nested Opus agents."
)
ARBITRATION_STRICT = "Opus subagents are denied in strict mode; use sonnet or haiku."


def expected_text(mode, reads_routed):
    parts = [CODE_SENTENCE, STATE_SENTENCE]
    if mode == "task-aware":
        parts.append(DOC_TASK)
        parts.append(ARBITRATION_TASK)
        if reads_routed:
            parts.append(READ_TASK)
            parts.append(SAFE_READ)
    else:
        parts.append(DOC_STRICT)
        parts.append(ARBITRATION_STRICT)
        if reads_routed:
            parts.append(READ_STRICT)
            parts.append(SAFE_READ)
    return " ".join(parts)


def context_of(completed):
    try:
        parsed = json.loads(completed.stdout)
        return parsed, parsed["hookSpecificOutput"]["additionalContext"]
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return None, None


def run(stdin, env_extra=None):
    env = os.environ.copy()
    env.pop("CLAUDE_ROUTING", None)
    if env_extra:
        env.update(env_extra)
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, HOOK],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    elapsed = time.monotonic() - started
    return completed, elapsed


def main():
    passed = 0
    failed = 0

    def check(name, ok, detail=""):
        nonlocal passed, failed
        print(f"{name:<72} {'PASS' if ok else 'FAIL'}")
        if ok:
            passed += 1
        else:
            failed += 1
            if detail:
                print(f"  {detail}")

    def silent(completed):
        return completed.returncode == 0 and completed.stdout == "" and completed.stderr == ""

    source = open(HOOK, encoding="utf-8").read()
    mode = stat.S_IMODE(os.stat(HOOK).st_mode)
    check(
        "executable, shebang, no _paths import",
        source.startswith("#!/usr/bin/env python3\n")
        and mode & 0o111
        and "import _paths" not in source
        and "from _paths" not in source
        and "import _runtime" not in source
        and "from _runtime" not in source,
        f"mode={oct(mode)}",
    )

    payload = json.dumps({"hook_event_name": "SessionStart", "source": "startup"})
    completed, elapsed = run(payload)
    parsed, text = context_of(completed)
    check(
        "normal SessionStart JSON",
        completed.returncode == 0
        and completed.stderr == ""
        and isinstance(parsed, dict)
        and parsed.get("hookSpecificOutput", {}).get("hookEventName") == "SessionStart"
        and isinstance(text, str)
        and len(text) <= 1100
        and all(part in text for part in REQUIRED),
        f"exit={completed.returncode} stdout={completed.stdout!r} stderr={completed.stderr!r} parsed={parsed!r}",
    )
    check("normal run under 1s", elapsed < 1.0, f"elapsed={elapsed:.3f}s")
    if isinstance(text, str) and text:
        print(f"  additionalContext ({len(text)} chars): {text}")

    # Changed assertion: `{}` used to be accepted because any JSON object emitted.
    # A payload must now be a Claude SessionStart. Extra fields on one still
    # do not change the text.
    extra_payload = json.dumps({
        "hook_event_name": "SessionStart",
        "source": "startup",
        "session_id": "s-1",
        "cwd": "/tmp/ignored",
    })
    extra, _elapsed = run(extra_payload)
    _extra_parsed, extra_text = context_of(extra)
    check(
        "payload content ignored",
        extra.returncode == 0 and extra.stderr == "" and extra_text == text and text != "",
        f"stdout={extra.stdout!r}",
    )

    for source_name in ("startup", "resume", "clear", "compact", "fork"):
        body = json.dumps({"hook_event_name": "SessionStart", "source": source_name})
        completed, _elapsed = run(body)
        _parsed, got = context_of(completed)
        check(
            f"source {source_name} -> context",
            completed.returncode == 0 and completed.stderr == "" and got == text and text != "",
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
        )

    for agent_type in ("file-writer", "file-reader", "general-purpose"):
        body = json.dumps({
            "hook_event_name": "SessionStart",
            "source": "startup",
            "agent_type": agent_type,
        })
        completed, _elapsed = run(body)
        check(
            f"agent_type {agent_type} -> no output",
            silent(completed),
            f"exit={completed.returncode} stdout={completed.stdout!r} stderr={completed.stderr!r}",
        )

    for label, body in (
        ("agent_type empty", {"hook_event_name": "SessionStart", "source": "startup", "agent_type": ""}),
        ("agent_type whitespace", {"hook_event_name": "SessionStart", "source": "startup", "agent_type": "  "}),
        ("agent_type null", {"hook_event_name": "SessionStart", "source": "startup", "agent_type": None}),
    ):
        completed, _elapsed = run(json.dumps(body))
        _parsed, got = context_of(completed)
        check(
            f"{label} -> context",
            completed.returncode == 0 and completed.stderr == "" and got == text and text != "",
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
        )

    cursor_version = "2026.10.22-27dcf5f"
    for label, body in (
        (
            "Cursor sessionStart + cursor_version",
            {
                "hook_event_name": "sessionStart",
                "cursor_version": cursor_version,
                "source": "startup",
                "session_id": "cursor-1",
            },
        ),
        (
            "SessionStart + cursor_version",
            {
                "hook_event_name": "SessionStart",
                "cursor_version": cursor_version,
                "source": "startup",
            },
        ),
        (
            "sessionStart spelling without cursor_version",
            {"hook_event_name": "sessionStart", "source": "startup"},
        ),
    ):
        completed, _elapsed = run(json.dumps(body))
        check(
            f"{label} -> no output",
            silent(completed),
            f"exit={completed.returncode} stdout={completed.stdout!r} stderr={completed.stderr!r}",
        )

    for label, version in (("blank cursor_version", ""), ("whitespace cursor_version", " \t ")):
        body = json.dumps({
            "hook_event_name": "SessionStart",
            "source": "startup",
            "cursor_version": version,
        })
        completed, _elapsed = run(body)
        _parsed, got = context_of(completed)
        check(
            f"{label} -> context",
            completed.returncode == 0 and completed.stderr == "" and got == text and text != "",
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
        )

    for label, stdin in (
        ("hook_event_name missing", "{}"),
        ("hook_event_name missing with source", json.dumps({"source": "startup"})),
        ("PreToolUse", json.dumps({"hook_event_name": "PreToolUse", "source": "startup"})),
        ("JSON array", "[]"),
        ("JSON null", "null"),
        ("JSON string", "\"SessionStart\""),
    ):
        completed, _elapsed = run(stdin)
        check(
            f"{label} -> no output",
            silent(completed),
            f"exit={completed.returncode} stdout={completed.stdout!r} stderr={completed.stderr!r}",
        )

    off, _elapsed = run(payload, {"CLAUDE_ROUTING": "off"})
    check(
        "CLAUDE_ROUTING=off -> no output",
        off.returncode == 0 and off.stdout == "" and off.stderr == "",
        f"exit={off.returncode} stdout={off.stdout!r} stderr={off.stderr!r}",
    )

    for label, stdin in (("malformed JSON", "{"), ("empty stdin", ""), ("not json", "not-json")):
        completed, garbage_elapsed = run(stdin)
        check(
            f"{label} -> exit 0 silent",
            completed.returncode == 0 and completed.stdout == "" and completed.stderr == "",
            f"exit={completed.returncode} stdout={completed.stdout!r} stderr={completed.stderr!r}",
        )
        check(f"{label} under 1s", garbage_elapsed < 1.0, f"elapsed={garbage_elapsed:.3f}s")

    text_cases(check)

    print(f"Tally: {passed} passed, {failed} failed")
    return 0 if failed == 0 and passed > 0 else 1


def _write_reads(home, value):
    path = os.path.join(home, ".claude", "routing-reads")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if value is None:
        if os.path.exists(path):
            os.remove(path)
        return
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(value if value.endswith("\n") else value + "\n")


def _run_state(home, project, mode, reads):
    _write_reads(home, reads)
    env = os.environ.copy()
    for key in ("CLAUDE_ROUTING", "CLAUDE_ROUTING_SCOPE", "CLAUDE_ROUTING_MODE"):
        env.pop(key, None)
    env["HOME"] = home
    env["CLAUDE_PROJECT_DIR"] = project
    env["CLAUDE_ROUTING_MODE"] = mode
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["ROUTING_LOG"] = os.path.join(home, "routing.jsonl")
    payload = json.dumps({"hook_event_name": "SessionStart", "source": "startup"})
    return subprocess.run(
        [sys.executable, HOOK],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def text_cases(check):
    home = os.path.realpath(tempfile.mkdtemp(prefix="routing-context-home-"))
    copy_dir = os.path.realpath(tempfile.mkdtemp(prefix="routing-context-copy-"))
    try:
        vault = os.path.realpath(os.path.join(home, "workplace", "project", "Obsidian_doc"))
        repo = os.path.realpath(os.path.join(home, "repo"))
        pt = os.path.realpath(os.path.join(
            home, "workplace", "OlaChat", "H5_repo", "pt-zh-web-page"
        ))
        os.makedirs(vault)
        os.makedirs(repo)
        os.makedirs(pt)
        seen = {}
        for mode in ("task-aware", "strict"):
            for toggle in ("on", "off"):
                for where, project in (("pilot", vault), ("non-pilot", repo)):
                    routed = where == "pilot" or toggle == "on"
                    completed = _run_state(home, project, mode, toggle)
                    parsed, got = context_of(completed)
                    if isinstance(got, str):
                        print(f"  {mode} reads={toggle} {where} ({len(got)} chars): {got}")
                    seen[(mode, toggle, where)] = got
                    expected = expected_text(mode, routed)
                    check(
                        f"{mode} reads={toggle} {where}",
                        completed.returncode == 0
                        and completed.stderr == ""
                        and isinstance(parsed, dict)
                        and parsed.get("hookSpecificOutput", {}).get("hookEventName") == "SessionStart"
                        and got == expected
                        and len(expected) <= 1100,
                        f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
                    )
        check(
            "pilot text ignores the reads toggle",
            seen[("task-aware", "on", "pilot")] == seen[("task-aware", "off", "pilot")]
            and seen[("strict", "on", "pilot")] == seen[("strict", "off", "pilot")]
            and "file-reader" in (seen[("strict", "off", "pilot")] or ""),
        )
        check(
            "non-pilot off omits the read rule",
            "file-reader" not in (seen[("task-aware", "off", "non-pilot")] or "")
            and "safe-read" not in (seen[("strict", "off", "non-pilot")] or "")
            and "file-reader" in (seen[("task-aware", "on", "non-pilot")] or ""),
        )
        for mode in ("task-aware", "strict"):
            missing = _run_state(home, repo, mode, None)
            _parsed, got = context_of(missing)
            if isinstance(got, str):
                print(f"  {mode} reads=missing non-pilot ({len(got)} chars): {got}")
            check(
                f"{mode} missing reads file matches off",
                missing.returncode == 0 and missing.stderr == "" and got == seen[(mode, "off", "non-pilot")],
                f"stdout={missing.stdout!r}",
            )
        unknown = _run_state(home, repo, "strict", "sideways")
        _parsed, got = context_of(unknown)
        if isinstance(got, str):
            print(f"  strict reads=sideways non-pilot ({len(got)} chars): {got}")
        check(
            "unknown reads value matches off",
            unknown.returncode == 0 and unknown.stderr == "" and got == seen[("strict", "off", "non-pilot")],
            f"stdout={unknown.stdout!r}",
        )
        pt_run = _run_state(home, pt, "strict", "off")
        _parsed, got = context_of(pt_run)
        if isinstance(got, str):
            print(f"  strict reads=off pt-zh ({len(got)} chars): {got}")
        check(
            "pt-zh is a pilot",
            pt_run.returncode == 0 and pt_run.stderr == "" and got == seen[("strict", "off", "pilot")],
            f"stdout={pt_run.stdout!r}",
        )

        copy = os.path.join(copy_dir, "routing-context.py")
        shutil.copy(HOOK, copy)
        os.chmod(copy, 0o755)
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("CLAUDE_ROUTING", None)
        env["HOME"] = home
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        isolated = subprocess.run(
            [sys.executable, copy],
            input=json.dumps({"hook_event_name": "SessionStart", "source": "startup"}),
            capture_output=True,
            text=True,
            env=env,
            cwd=copy_dir,
            check=False,
        )
        check(
            "resolver import failure exits 0 silent",
            isolated.returncode == 0 and isolated.stdout == "" and isolated.stderr == "",
            f"exit={isolated.returncode} stdout={isolated.stdout!r} stderr={isolated.stderr!r}",
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(copy_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
