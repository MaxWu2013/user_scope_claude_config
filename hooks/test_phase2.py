#!/usr/bin/env python3
"""Phase 2 routing decisions: subprocess calls to require-cursor-agent.py.

CLAUDE_PROJECT_DIR is a temp dir P. HOME is the real home. ROUTING_LOG points
at a temp file so skill_config records do not touch ~/.claude/logs.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
HOOK = HOOK_DIR / "require-cursor-agent.py"
HOME = Path(os.path.expanduser("~"))
ASK_SUFFIX = "needs your approval (routing control plane / outside workspace / vault asset)."


def run_hook(payload, *, project, hook=HOOK, env_extra=None, stdin=None):
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project)
    env.pop("CURSOR_AGENT", None)
    env.pop("SUB_AGENTS_DIR", None)
    if env_extra:
        env.update(env_extra)
    completed = subprocess.run(
        [sys.executable, str(hook)],
        input=stdin if stdin is not None else json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=str(project),
        check=False,
    )
    return completed


def classify(project, tool, tool_input):
    script = (
        "import json, os, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "os.environ['CLAUDE_PROJECT_DIR'] = sys.argv[2]\n"
        "from _paths import classify_write_target\n"
        "tool, raw = json.loads(sys.argv[3])\n"
        "print(classify_write_target(tool, raw))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(HOOK_DIR), str(project), json.dumps([tool, tool_input])],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip(), completed.returncode, completed.stderr


def decision_of(completed):
    if completed.returncode == 2:
        return "deny"
    if completed.returncode != 0:
        return f"exit-{completed.returncode}"
    text = completed.stdout.strip()
    if not text:
        return "allow-silent"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return "allow-unknown"
    if data == {"permission": "allow"}:
        return "allow-cursor"
    hook_out = data.get("hookSpecificOutput") or {}
    if hook_out.get("permissionDecision") == "ask":
        return "ask"
    return "allow-unknown"


def ask_class(completed):
    data = json.loads(completed.stdout)
    return data["hookSpecificOutput"]["permissionDecisionReason"]


def write_payload(path, tool="Write"):
    return {"tool_name": tool, "tool_input": {"file_path": path}}


def edit_payload(path):
    return {"tool_name": "Edit", "tool_input": {"file_path": path}}


def bash_payload(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def obsidian_payload(filepath, tool="mcp__mcp-obsidian__obsidian_put_content"):
    return {"tool_name": tool, "tool_input": {"filepath": filepath}}


class Suite:
    def __init__(self, project, log_path):
        self.project = project
        self.log_path = log_path
        self.failures = []
        self.passed = 0

    def env(self):
        return {"ROUTING_LOG": str(self.log_path)}

    def check(self, number, name, ok, detail=""):
        status = "PASS" if ok else f"FAIL ({detail})"
        print(f"{number:>4}. {name} | {status}")
        if ok:
            self.passed += 1
        else:
            self.failures.append(f"{number}. {name}: {detail}")

    def hook(self, payload, **kwargs):
        extra = self.env()
        if "env_extra" in kwargs:
            extra.update(kwargs.pop("env_extra"))
        return run_hook(payload, project=self.project, env_extra=extra, **kwargs)

    def expect_decision(self, number, name, payload, expected, *, cls=None, tool=None, tool_input=None):
        completed = self.hook(payload)
        got = decision_of(completed)
        detail = ""
        ok = got == expected and completed.returncode in (0, 2)
        if expected == "deny":
            ok = completed.returncode == 2 and got == "deny"
        elif expected == "ask":
            ok = completed.returncode == 0 and got == "ask"
            if ok and cls is not None:
                reason = ask_class(completed)
                prefix = f"{cls}: "
                if not reason.startswith(prefix) or ASK_SUFFIX not in reason:
                    ok = False
                    detail = f"reason={reason!r}"
        elif expected == "allow-silent":
            ok = completed.returncode == 0 and got == "allow-silent"
        elif expected == "allow-cursor":
            ok = completed.returncode == 0 and got == "allow-cursor"
        if cls is not None and tool is not None and expected != "ask":
            actual, code, err = classify(self.project, tool, tool_input)
            if code != 0 or actual != cls:
                ok = False
                detail = f"class={actual!r} exit={code} stderr={err!r}"
        if not ok and not detail:
            detail = (
                f"expected={expected} got={got} exit={completed.returncode} "
                f"stdout={completed.stdout!r} stderr={completed.stderr[:400]!r}"
            )
        self.check(number, name, ok, detail)
        return completed


def log_records(path):
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def main():
    raw = tempfile.mkdtemp(prefix="phase2-project-")
    project = Path(os.path.realpath(raw))
    log_dir_parent = Path(os.path.realpath(tempfile.mkdtemp(prefix="phase2-log-")))
    log_path = log_dir_parent / "logs" / "routing.jsonl"
    suite = Suite(project, log_path)
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
        case_12(suite)
        case_13(suite)
        case_14(suite)
        case_15(suite, project)
        case_16(suite)
    finally:
        shutil.rmtree(raw, ignore_errors=True)
        shutil.rmtree(project, ignore_errors=True)
        shutil.rmtree(log_dir_parent, ignore_errors=True)

    total = suite.passed + len(suite.failures)
    print(f"Tally: {suite.passed} passed, {len(suite.failures)} failed")
    return 0 if not suite.failures and total > 0 else 1


def case_1(suite):
    path = str(suite.project / "src" / "a.py")
    suite.expect_decision(
        "1", f"Edit {path} -> deny",
        edit_payload(path), "deny",
        cls="code", tool="Edit", tool_input={"file_path": path},
    )


def case_2(suite):
    for name in ("t.md", "t.diff", "t.patch", "t.txt", "t.json"):
        path = str(suite.project / ".claude" / "specs" / name)
        suite.expect_decision(
            "2", f"Write {name} -> allow state",
            write_payload(path), "allow-silent",
            cls="state", tool="Write", tool_input={"file_path": path},
        )


def case_3(suite):
    nested = str(suite.project / ".claude" / "specs" / "sub" / "t.md")
    suite.expect_decision(
        "3", "Write specs/sub/t.md -> allow document",
        write_payload(nested), "allow-silent",
        cls="document", tool="Write", tool_input={"file_path": nested},
    )
    code = str(suite.project / ".claude" / "specs" / "t.py")
    suite.expect_decision(
        "3", "Write specs/t.py -> deny",
        write_payload(code), "deny",
        cls="code", tool="Write", tool_input={"file_path": code},
    )


def case_4(suite):
    path = str(HOME / ".claude" / "projects" / "x" / "memory" / "m.md")
    suite.expect_decision(
        "4", "Write ~/.claude/projects/x/memory/m.md -> allow",
        write_payload(path), "allow-silent",
        cls="state", tool="Write", tool_input={"file_path": path},
    )


def case_5(suite):
    path = str(HOME / ".claude" / "plans" / "p.md")
    suite.expect_decision(
        "5", "Write ~/.claude/plans/p.md -> allow",
        write_payload(path), "allow-silent",
        cls="state", tool="Write", tool_input={"file_path": path},
    )


def case_6(suite):
    targets = [
        str(HOME / ".claude" / "hooks" / "x.py"),
        str(HOME / ".claude" / "settings.json"),
        str(suite.project / ".cursor" / "hooks.json"),
    ]
    for path in targets:
        suite.expect_decision(
            "6", f"Edit {path} -> ask control",
            edit_payload(path), "ask", cls="control",
        )


def case_7(suite):
    path = "/tmp/elsewhere/notes.md"
    suite.expect_decision(
        "7", "Edit /tmp/elsewhere/notes.md -> ask outside",
        edit_payload(path), "ask", cls="outside",
    )


def case_8(suite):
    path = str(HOME / ".claude" / "skills" / "x" / "SKILL.md")
    before = len(log_records(suite.log_path))
    suite.expect_decision(
        "8", "Edit ~/.claude/skills/x/SKILL.md -> allow-log",
        edit_payload(path), "allow-silent",
        cls="skill_config", tool="Edit", tool_input={"file_path": path},
    )
    records = log_records(suite.log_path)
    added = records[before:]
    file_mode = stat.S_IMODE(suite.log_path.stat().st_mode) if suite.log_path.exists() else None
    dir_mode = stat.S_IMODE(suite.log_path.parent.stat().st_mode) if suite.log_path.exists() else None
    ok = (
        len(added) == 1
        and added[0].get("class") == "skill_config"
        and added[0].get("tool") == "Edit"
        and added[0].get("path") == path
        and "ts" in added[0]
        and set(added[0]) == {"ts", "class", "tool", "path", "session_id", "project"}
        and file_mode == 0o600
        and dir_mode == 0o700
    )
    suite.check(
        "8", "routing.jsonl append (0600 file, 0700 dir)",
        ok,
        f"records={added!r} file={file_mode} dir={dir_mode}",
    )


def case_9(suite):
    paths = [
        suite.project / "AGENTS.md",
        suite.project / ".claude" / "rules" / "r.md",
        suite.project / ".cursor" / "rules" / "r.md",
    ]
    for path in paths:
        before = len(log_records(suite.log_path))
        suite.expect_decision(
            "9", f"Edit {path.name} under {path.parent.name} -> allow-log",
            edit_payload(str(path)), "allow-silent",
            cls="skill_config", tool="Edit", tool_input={"file_path": str(path)},
        )
        added = log_records(suite.log_path)[before:]
        ok = len(added) == 1 and added[0].get("class") == "skill_config" and added[0].get("path") == str(path)
        suite.check("9", f"log line for {path.name}", ok, f"records={added!r}")


def case_10(suite):
    specs = [
        ("x/data.json", "deny", "code"),
        ("x/note.md", "allow-silent", "document"),
        ("x/board.canvas", "allow-silent", "vault_structured"),
        ("img.png", "ask", "vault_asset"),
    ]
    tool = "mcp__mcp-obsidian__obsidian_put_content"
    for filepath, expected, cls in specs:
        suite.expect_decision(
            "10", f"obsidian_put_content {filepath} -> {expected} ({cls})",
            obsidian_payload(filepath), expected,
            cls=cls, tool=tool, tool_input={"filepath": filepath},
        )


def case_11(suite):
    link = suite.project / "link.md"
    target = HOME / ".claude" / "hooks" / "require-cursor-agent.py"
    link.symlink_to(target)
    suite.expect_decision(
        "11", "Write P/link.md symlink to control file -> ask",
        write_payload(str(link)), "ask", cls="control",
    )


def case_12(suite):
    path = str(suite.project / "docs" / ".." / ".." / "outside.md")
    suite.expect_decision(
        "12", "Write P/docs/../../outside.md -> ask outside",
        write_payload(path), "ask", cls="outside",
    )


def case_13(suite):
    notes = str(suite.project / "notes.md")
    code = str(suite.project / "a.py")
    suite.expect_decision("13", "Bash echo > P/notes.md -> allow", bash_payload(f"echo x > {notes}"), "allow-silent")
    suite.expect_decision("13", "Bash echo > P/a.py -> deny", bash_payload(f"echo x > {code}"), "deny")
    suite.expect_decision(
        "13", "Bash cp a b.canvas -> deny",
        bash_payload("cp a b.canvas"), "deny",
    )
    suite.expect_decision(
        "13", "Bash echo > ~/.claude/plans/p.md -> allow",
        bash_payload("echo x > ~/.claude/plans/p.md"), "allow-silent",
    )


def case_14(suite):
    completed = suite.hook({}, stdin="{not json")
    ok = completed.returncode == 2 and "Traceback" not in completed.stderr
    suite.check(
        "14", "malformed JSON -> exit 2",
        ok,
        f"exit={completed.returncode} stderr={completed.stderr[:300]!r}",
    )
    completed = suite.hook({"tool_name": "Edit", "tool_input": ["not-a-dict"]})
    ok = completed.returncode == 2 and "Traceback" not in completed.stderr
    suite.check(
        "14", "tool_input list -> exit 2",
        ok,
        f"exit={completed.returncode} stdout={completed.stdout!r} stderr={completed.stderr[:300]!r}",
    )


def case_15(suite, project):
    fixture = json.loads((HOOK_DIR / "fixtures" / "cursor-pretooluse-write.json").read_text(encoding="utf-8"))
    completed = suite.hook(fixture)
    ok = completed.returncode == 0 and json.loads(completed.stdout) == {"permission": "allow"}
    suite.check(
        "15", "Cursor fixture -> allow",
        ok,
        f"exit={completed.returncode} stdout={completed.stdout!r} stderr={completed.stderr[:300]!r}",
    )

    copy_root = Path(tempfile.mkdtemp(prefix="phase2-hooks-copy-"))
    try:
        dest = copy_root / "hooks"
        shutil.copytree(
            HOOK_DIR,
            dest,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        (dest / "_paths.py").unlink()
        copied = dest / "require-cursor-agent.py"
        completed = run_hook(fixture, project=project, hook=copied, env_extra=suite.env())
        ok = completed.returncode == 0 and json.loads(completed.stdout) == {"permission": "allow"}
        suite.check(
            "15", "Cursor fixture with _paths.py deleted -> allow",
            ok,
            f"exit={completed.returncode} stdout={completed.stdout!r} stderr={completed.stderr[:400]!r}",
        )
        completed = run_hook(
            edit_payload(str(project / "src" / "a.py")),
            project=project,
            hook=copied,
            env_extra=suite.env(),
        )
        ok = completed.returncode == 2
        suite.check(
            "15", "Claude Edit with _paths.py deleted -> exit 2",
            ok,
            f"exit={completed.returncode} stdout={completed.stdout!r} stderr={completed.stderr[:400]!r}",
        )
    finally:
        shutil.rmtree(copy_root, ignore_errors=True)


def case_16(suite):
    suite.expect_decision(
        "16", "Edit /tmp/x.py -> deny",
        edit_payload("/tmp/x.py"), "deny",
        cls="code", tool="Edit", tool_input={"file_path": "/tmp/x.py"},
    )
    other = str(HOME / "workplace" / "other-routing-probe" / "src" / "a.py")
    suite.expect_decision(
        "16", "Edit ~/workplace/other-routing-probe/src/a.py -> deny",
        edit_payload(other), "deny",
        cls="code", tool="Edit", tool_input={"file_path": other},
    )
    outside_py = str(suite.project / "docs" / ".." / ".." / "outside.py")
    suite.expect_decision(
        "16", "Write P/docs/../../outside.py -> deny",
        write_payload(outside_py), "deny",
        cls="code", tool="Write", tool_input={"file_path": outside_py},
    )
    fd, target = tempfile.mkstemp(suffix=".py", prefix="phase2-link2-", dir="/tmp")
    os.close(fd)
    link = suite.project / "link2.py"
    try:
        link.symlink_to(target)
        suite.expect_decision(
            "16", "Write P/link2.py symlink to /tmp/*.py -> deny",
            write_payload(str(link)), "deny",
            cls="code", tool="Write", tool_input={"file_path": str(link)},
        )
    finally:
        os.unlink(target)


if __name__ == "__main__":
    sys.exit(main())
