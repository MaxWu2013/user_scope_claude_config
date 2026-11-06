#!/usr/bin/env python3
"""Smoke-test the require-cursor-agent hook and its archived predecessor."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional


REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_DIR = Path(__file__).resolve().parent
MARKER_PREFIX = "require-cursor-agent-warned-"


@dataclass
class HookResult:
    returncode: Optional[int]
    stdout: str
    stderr: str
    error: Optional[str] = None


@dataclass
class Case:
    number: int
    name: str
    payload: Dict[str, object]
    expected_exit: int
    check: Callable[[HookResult], List[str]]
    bypass: bool = False
    env: Optional[Dict[str, str]] = None
    stdin: Optional[str] = None


def cursor_payload(name: str) -> Dict[str, object]:
    # Captured from cursor-agent 2026.10.22-27dcf5f, with IDs, email and paths replaced.
    return json.loads((HOOK_DIR / "fixtures" / name).read_text(encoding="utf-8"))


def edit_payload(path: str) -> Dict[str, object]:
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": path},
    }


def bash_payload(command: str) -> Dict[str, object]:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


def expected_exit_only(expected_exit: int) -> Callable[[HookResult], List[str]]:
    def check(result: HookResult) -> List[str]:
        failures = []
        if result.returncode != expected_exit:
            failures.append(f"expected exit {expected_exit}, got {result.returncode}")
        return failures

    return check


def read_frontmatter_value(path: Path, wanted_key: str) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{path} has no leading frontmatter")

    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() == wanted_key:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1]:
                if value[0] in ("'", '"'):
                    value = value[1:-1].strip()
            if value:
                return value

    raise ValueError(f"{wanted_key!r} is missing from {path}")


def resolve_hook(argument: Optional[str]) -> Path:
    if argument is None:
        return (HOOK_DIR / "require-cursor-agent.py").resolve()

    hook = Path(argument)
    if not hook.is_absolute():
        hook = Path.cwd() / hook
    return hook.resolve()


def command_for_hook(hook: Path) -> List[str]:
    suffix = hook.suffix.lower()
    if suffix == ".py":
        return ["python3", str(hook)]
    if suffix in (".sh", ".bak"):
        return ["bash", str(hook)]
    raise ValueError(f"unsupported hook extension: {hook}")


def run_hook(
    hook: Path,
    payload: Dict[str, object],
    *,
    bypass: bool,
    env_overrides: Optional[Dict[str, str]],
    stdin: Optional[str] = None,
) -> HookResult:
    environment = os.environ.copy()
    environment.pop("SUB_AGENTS_DIR", None)
    environment.pop("CURSOR_AGENT", None)
    environment["CURSOR_RUN_PATH"] = "/nonexistent"

    if bypass:
        environment["CURSOR_AGENT"] = "1"
    elif "CURSOR_AGENT" in environment:
        raise AssertionError("CURSOR_AGENT must be removed for non-bypass cases")

    environment["CLAUDE_PROJECT_DIR"] = str(REPO_ROOT)
    if env_overrides:
        environment.update(env_overrides)

    if not bypass and "CURSOR_AGENT" in environment:
        raise AssertionError("CURSOR_AGENT leaked into a non-bypass invocation")
    if bypass and environment.get("CURSOR_AGENT") != "1":
        raise AssertionError("bypass invocation did not set CURSOR_AGENT=1")

    try:
        completed = subprocess.run(
            command_for_hook(hook),
            cwd=str(REPO_ROOT),
            env=environment,
            input=stdin if stdin is not None else json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as error:
        return HookResult(None, "", "", f"{type(error).__name__}: {error}")

    return HookResult(
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


def bypass_marker_path(session_id: str) -> Path:
    safe_session_id = re.sub(r"[^A-Za-z0-9_-]", "", session_id)
    return Path(tempfile.gettempdir()) / f"{MARKER_PREFIX}{safe_session_id}"


def skill_records(path: Path) -> List[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def make_cases(
    *,
    yolo_dir: Path,
    read_only_dir: Path,
    empty_dir: Path,
    home_dir: Path,
    real_model: Optional[str],
    real_model_error: Optional[str],
    bypass_session_id: str,
    skill_home: Path,
    skill_project: Path,
    skill_log_dir: Path,
) -> List[Case]:
    def case_19_check(result: HookResult) -> List[str]:
        failures = expected_exit_only(2)(result)
        if "--force" not in result.stderr:
            failures.append("stderr is missing --force")
        if real_model_error:
            failures.append(f"could not read real model: {real_model_error}")
        elif not real_model or real_model not in result.stderr:
            failures.append("stderr is missing the model from .agents/cursor-agent.md")
        return failures

    def case_20_check(result: HookResult) -> List[str]:
        failures = expected_exit_only(2)(result)
        if "smoketest-model-x" not in result.stderr:
            failures.append("stderr is missing smoketest-model-x")
        if real_model and real_model in result.stderr:
            failures.append("stderr contains the repo model instead of the fixture model")
        return failures

    def case_21_check(result: HookResult) -> List[str]:
        failures = expected_exit_only(2)(result)
        if "--force" in result.stderr:
            failures.append("stderr unexpectedly contains --force")
        if "read-only" not in result.stderr:
            failures.append("stderr is missing read-only")
        return failures

    def case_22_check(result: HookResult) -> List[str]:
        failures = expected_exit_only(2)(result)
        if "--model" in result.stderr:
            failures.append("stderr unexpectedly contains --model")
        if "Traceback (most recent call last)" in result.stderr:
            failures.append("stderr contains a Python traceback")
        return failures

    def case_23_check(result: HookResult) -> List[str]:
        failures = expected_exit_only(2)(result)
        if result.stdout != "":
            failures.append("stdout is not empty (old bypass warning?)")
        if "cursor-agent" not in result.stderr:
            failures.append("stderr is missing the delegation message")
        return failures

    def case_24_check(result: HookResult) -> List[str]:
        failures = expected_exit_only(2)(result)
        if bypass_marker_path(bypass_session_id).exists():
            failures.append("old bypass warning marker was created")
        return failures

    def case_46_check(result: HookResult) -> List[str]:
        failures = expected_exit_only(2)(result)
        if "smoketest-home-model" not in result.stderr:
            failures.append("stderr is missing the model from ~/.agents/cursor-agent.md")
        return failures

    def cursor_allow_check(result: HookResult) -> List[str]:
        failures = expected_exit_only(0)(result)
        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError:
            failures.append("stdout is not JSON")
        else:
            if output != {"permission": "allow"}:
                failures.append(f"stdout is {output!r}, expected permission allow")
        return failures

    def format_changed_check(result: HookResult) -> List[str]:
        failures = expected_exit_only(2)(result)
        if "is_cursor_runtime" not in result.stderr:
            failures.append("stderr is missing the format-changed message")
        return failures

    cursor_write = cursor_payload("cursor-pretooluse-write.json")
    cursor_shell = cursor_payload("cursor-pretooluse-shell.json")
    cursor_no_version = {k: v for k, v in cursor_write.items() if k != "cursor_version"}
    claude_write = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": "lib/main.dart", "content": "void main() {}\n"},
    }

    return [
        Case(1, "Edit lib/main.dart", edit_payload("lib/main.dart"), 2, expected_exit_only(2)),
        Case(
            2,
            "Edit /Users/wujincheng/.claude.json",
            edit_payload("/Users/wujincheng/.claude.json"),
            2,
            expected_exit_only(2),
        ),
        Case(3, "Edit pubspec.yaml", edit_payload("pubspec.yaml"), 2, expected_exit_only(2)),
        Case(4, "Write lib/foo.dart", {
            "tool_name": "Write",
            "tool_input": {"file_path": "lib/foo.dart"},
        }, 2, expected_exit_only(2)),
        Case(5, "Bash cp lib/a.dart lib/b.dart", bash_payload("cp lib/a.dart lib/b.dart"), 2, expected_exit_only(2)),
        Case(6, "Bash mv lib/a.dart lib/b.dart", bash_payload("mv lib/a.dart lib/b.dart"), 2, expected_exit_only(2)),
        Case(7, "Bash tee lib/x.dart", bash_payload("tee lib/x.dart"), 2, expected_exit_only(2)),
        Case(
            8,
            "Bash sed -i lib/main.dart",
            bash_payload("sed -i '' s/a/b/ lib/main.dart"),
            2,
            expected_exit_only(2),
        ),
        Case(9, "Bash echo redirect to lib/out.dart", bash_payload("echo hello > lib/out.dart"), 2, expected_exit_only(2)),
        Case(10, "Edit docs/plan.md", edit_payload("docs/plan.md"), 0, expected_exit_only(0)),
        Case(
            11,
            "Edit /Users/wujincheng/notes/scratch.md",
            edit_payload("/Users/wujincheng/notes/scratch.md"),
            0,
            expected_exit_only(0),
        ),
        Case(12, "Bash ls foo 2>/dev/null", bash_payload("ls foo 2>/dev/null"), 0, expected_exit_only(0)),
        Case(13, "Bash find . -name x 2>&1", bash_payload("find . -name x 2>&1"), 0, expected_exit_only(0)),
        Case(14, "Bash echo arrow", bash_payload('echo "arrow -> here"'), 0, expected_exit_only(0)),
        Case(
            15,
            "Bash jq quoted redirect text",
            bash_payload(r'jq -r ".a|join(\" > \")" f.json'),
            0,
            expected_exit_only(0),
        ),
        Case(
            16,
            "Bash claude plugin install",
            bash_payload("claude plugin install runner@sub-agents-skills"),
            0,
            expected_exit_only(0),
        ),
        Case(
            17,
            "Bash cursor-agent quoted text",
            bash_payload('cursor-agent -p --force "please cp and truncate the file"'),
            0,
            expected_exit_only(0),
        ),
        Case(
            18,
            "Bash printf exit status",
            bash_payload(r'printf "exit=%s\n" "$?"'),
            0,
            expected_exit_only(0),
        ),
        Case(
            19,
            "Edit lib/main.dart uses real agent definition",
            edit_payload("lib/main.dart"),
            2,
            case_19_check,
        ),
        Case(
            20,
            "Edit with yolo fixture definition",
            edit_payload("lib/main.dart"),
            2,
            case_20_check,
            env={"SUB_AGENTS_DIR": str(yolo_dir)},
        ),
        Case(
            21,
            "Edit with read-only fixture definition",
            edit_payload("lib/main.dart"),
            2,
            case_21_check,
            env={"SUB_AGENTS_DIR": str(read_only_dir)},
        ),
        Case(
            22,
            "Edit with empty agent definition directory",
            edit_payload("lib/main.dart"),
            2,
            case_22_check,
            env={"SUB_AGENTS_DIR": str(empty_dir)},
        ),
        Case(
            23,
            "CURSOR_AGENT=1 no longer exempts a Claude Code edit",
            {
                **edit_payload("lib/main.dart"),
                "session_id": bypass_session_id,
            },
            2,
            case_23_check,
            bypass=True,
        ),
        Case(
            24,
            "CURSOR_AGENT=1 leaves no bypass warning marker",
            {
                **edit_payload("lib/main.dart"),
                "session_id": bypass_session_id,
            },
            2,
            case_24_check,
            bypass=True,
        ),
        Case(25, "Bash dart format lib/a.dart", bash_payload("dart format -l 120 lib/a.dart"), 2, expected_exit_only(2)),
        Case(
            26,
            "Bash cd && dart format",
            bash_payload("cd pkg && dart format --language-version=latest -l 120 lib/a.dart"),
            2,
            expected_exit_only(2),
        ),
        Case(
            27,
            "Bash dart format --output=none",
            bash_payload("dart format -l 120 --output=none --set-exit-if-changed lib/a.dart"),
            0,
            expected_exit_only(0),
        ),
        Case(
            28,
            "Bash protoc --dart_out",
            bash_payload("protoc -I x --dart_out=out a.proto"),
            2,
            expected_exit_only(2),
        ),
        Case(
            29,
            "Bash protoc --decode",
            bash_payload("protoc -I x --decode=p.R a.proto < f.pb"),
            0,
            expected_exit_only(0),
        ),
        Case(
            30,
            "Bash mock-gen-pb",
            bash_payload("tools/killgrave_api_mock/bin/mock-gen-pb -file r.json"),
            2,
            expected_exit_only(2),
        ),
        Case(
            31,
            "Bash switcheroo.exe",
            bash_payload("./tools/switcheroo.exe -c ./asset_parser_cfg.yml -f ."),
            2,
            expected_exit_only(2),
        ),
        Case(32, "Bash grep switcheroo", bash_payload("grep -n switcheroo SKILL.md"), 0, expected_exit_only(0)),
        Case(
            33,
            "CURSOR_AGENT=1 dart format is still denied",
            bash_payload("dart format -l 120 lib/a.dart"),
            2,
            expected_exit_only(2),
            bypass=True,
        ),
        Case(34, "Cursor Write lib/main.dart", cursor_write, 0, cursor_allow_check),
        Case(35, "Cursor Shell echo > lib/out.dart", cursor_shell, 0, cursor_allow_check),
        Case(36, "Claude Code Write lib/main.dart", claude_write, 2, expected_exit_only(2)),
        Case(
            37,
            "Claude Code Write with inherited CURSOR_VERSION",
            claude_write,
            2,
            expected_exit_only(2),
            env={"CURSOR_VERSION": "2026.10.22-27dcf5f", "CURSOR_PROJECT_DIR": "/workspace"},
        ),
        Case(38, "Cursor-shaped payload without cursor_version", cursor_no_version, 2, format_changed_check),
        Case(
            39,
            "Cursor-shaped payload with blank cursor_version",
            {**cursor_write, "cursor_version": "  "},
            2,
            format_changed_check,
        ),
        Case(40, "Invalid JSON on stdin", {}, 2, expected_exit_only(2), stdin="{not json"),
        Case(41, "JSON list on stdin", {}, 2, expected_exit_only(2), stdin="[]"),
        Case(
            42,
            "Cursor Write with CURSOR_AGENT=1 still gets allow JSON",
            cursor_write,
            0,
            cursor_allow_check,
            bypass=True,
        ),
        Case(43, "Edit src/App.vue", edit_payload("src/App.vue"), 2, expected_exit_only(2)),
        Case(44, "Edit docs/demo/app.css", edit_payload("docs/demo/app.css"), 2, expected_exit_only(2)),
        Case(45, "Edit docs/demo/index.html", edit_payload("docs/demo/index.html"), 2, expected_exit_only(2)),
        Case(
            46,
            "Project without .agents falls back to ~/.agents/cursor-agent.md",
            edit_payload("lib/main.dart"),
            2,
            case_46_check,
            env={"CLAUDE_PROJECT_DIR": str(empty_dir), "HOME": str(home_dir)},
        ),
        Case(
            47,
            "Bash python heredoc comparison is not a redirect",
            bash_payload("python3 - <<'E'\nprint(1 > 0)\nE"),
            0,
            expected_exit_only(0),
        ),
        Case(
            48,
            "Bash python tab-stripped heredoc comparison is not a redirect",
            bash_payload("python3 - <<-E\nx = a > b\nE"),
            0,
            expected_exit_only(0),
        ),
        Case(
            49,
            "Bash python double-quoted heredoc comparison is not a redirect",
            bash_payload('python3 - <<"E"\nx = a > b\nE'),
            0,
            expected_exit_only(0),
        ),
        Case(
            50,
            "Bash cat redirect with heredoc body",
            bash_payload("cat > src/a.py <<EOF\nprint(1)\nEOF"),
            2,
            expected_exit_only(2),
        ),
        Case(
            51,
            "Bash shell heredoc body redirect",
            bash_payload("bash <<EOF\necho x > src/a.py\nEOF"),
            2,
            expected_exit_only(2),
        ),
        Case(
            52,
            "Bash sudo shell heredoc body redirect",
            bash_payload("sudo bash <<EOF\necho x > src/a.py\nEOF"),
            2,
            expected_exit_only(2),
        ),
        Case(
            53,
            "Bash unterminated heredoc with a comparison",
            bash_payload("python3 - <<'E'\nprint(1 > 0)"),
            2,
            expected_exit_only(2),
        ),
        Case(
            54,
            "Bash here-string is not a heredoc",
            bash_payload('cat <<<"a > b"'),
            0,
            expected_exit_only(0),
        ),
        Case(
            55,
            "Bash protoc --custom_out",
            bash_payload(
                "protoc -I x --plugin=protoc-gen-custom=/tmp/plugin --custom_out=out a.proto"
            ),
            2,
            expected_exit_only(2),
        ),
        Case(
            56,
            "Bash dart run build_runner",
            bash_payload("dart run build_runner build --delete-conflicting-outputs"),
            2,
            expected_exit_only(2),
        ),
        Case(
            57,
            "Bash flutter pub run build_runner",
            bash_payload("flutter pub run build_runner build"),
            2,
            expected_exit_only(2),
        ),
        Case(
            58,
            "Bash dart pub run build_runner",
            bash_payload("dart pub run build_runner build"),
            2,
            expected_exit_only(2),
        ),
        Case(
            59,
            "Bash flutter pub run mobx_bin:gen",
            bash_payload("flutter pub run mobx_bin:gen"),
            2,
            expected_exit_only(2),
        ),
        Case(
            60,
            "Bash dart run mobx_bin:gen",
            bash_payload("dart run mobx_bin:gen"),
            2,
            expected_exit_only(2),
        ),
        Case(
            61,
            "Bash protoc -o /dev/null",
            bash_payload("protoc -I x -o /dev/null a.proto"),
            0,
            expected_exit_only(0),
        ),
        Case(
            62,
            "Bash protoc --decode stays allowed",
            bash_payload("protoc --decode=p.R a.proto < f.pb"),
            0,
            expected_exit_only(0),
        ),
        Case(
            63,
            "Bash git commit heredoc line starting mock-gen-pb",
            bash_payload("git commit -F - <<'EOF'\nmock-gen-pb -file r.json\nEOF"),
            0,
            expected_exit_only(0),
        ),
        *skill_config_cases(skill_home, skill_project, skill_log_dir),
        *data_class_cases(),
    ]


def skill_config_cases(skill_home: Path, skill_project: Path, skill_log_dir: Path) -> List[Case]:
    skill_path = os.path.normpath(str(skill_home / ".claude" / "skills" / "x" / "SKILL.md"))
    expected_project = os.path.realpath(str(skill_project))

    def env_for(log_path: Path) -> Dict[str, str]:
        return {
            "HOME": str(skill_home),
            "CLAUDE_PROJECT_DIR": str(skill_project),
            "ROUTING_LOG": str(log_path),
        }

    def payload(session_id, include_session: bool) -> Dict[str, object]:
        body: Dict[str, object] = {
            "tool_name": "Edit",
            "tool_input": {"file_path": skill_path},
        }
        if include_session:
            body["session_id"] = session_id
        return body

    def check_record(log_path: Path, session_id):
        def check(result: HookResult) -> List[str]:
            failures = expected_exit_only(0)(result)
            if result.stdout != "":
                failures.append(f"stdout is {result.stdout!r}")
            records = skill_records(log_path)
            if len(records) != 1:
                failures.append(f"records={records!r}")
                return failures
            rec = records[0]
            expected_keys = {"ts", "class", "tool", "path", "session_id", "project"}
            if set(rec) != expected_keys:
                failures.append(f"keys={sorted(rec)}")
            if rec.get("class") != "skill_config" or rec.get("tool") != "Edit" or rec.get("path") != skill_path:
                failures.append(f"record={rec!r}")
            if "session_id" not in rec or rec.get("session_id") != session_id:
                failures.append(f"session_id={rec.get('session_id')!r}")
            if rec.get("project") != expected_project:
                failures.append(f"project={rec.get('project')!r} expected={expected_project!r}")
            if not isinstance(rec.get("ts"), str) or not rec["ts"]:
                failures.append(f"ts={rec.get('ts')!r}")
            return failures

        return check

    blocked = skill_log_dir / "not-a-directory"
    bad_log = blocked / "routing.jsonl"

    def unwritable_check(result: HookResult) -> List[str]:
        failures = expected_exit_only(0)(result)
        if result.stdout != "":
            failures.append(f"stdout is {result.stdout!r}")
        if bad_log.exists():
            failures.append("unwritable log was created")
        return failures

    return [
        Case(
            64,
            "skill_config record has session_id and project",
            payload("skill-1", True),
            0,
            check_record(skill_log_dir / "skill.jsonl", "skill-1"),
            env=env_for(skill_log_dir / "skill.jsonl"),
        ),
        Case(
            65,
            "skill_config missing session_id logs null",
            payload(None, False),
            0,
            check_record(skill_log_dir / "missing.jsonl", None),
            env=env_for(skill_log_dir / "missing.jsonl"),
        ),
        Case(
            66,
            "skill_config non-string session_id logs null",
            payload(4, True),
            0,
            check_record(skill_log_dir / "number.jsonl", None),
            env=env_for(skill_log_dir / "number.jsonl"),
        ),
        Case(
            67,
            "unwritable skill_config log still allows the edit",
            payload("skill-blocked", True),
            0,
            unwritable_check,
            env=env_for(bad_log),
        ),
    ]


def data_class_cases() -> List[Case]:
    home = os.path.expanduser("~")
    root = os.path.join(home, "workplace", "OlaChat", "InfoTank", "data")
    allowed = os.path.join(root, "foo", "source.json")
    return [
        Case(
            68,
            "Write InfoTank data/foo/source.json",
            {
                "tool_name": "Write",
                "tool_input": {"file_path": allowed},
            },
            0,
            expected_exit_only(0),
        ),
        Case(69, "Edit InfoTank data/foo/source.json", edit_payload(allowed), 0, expected_exit_only(0)),
        Case(
            70,
            "Write InfoTank data/foo/bar.json",
            {
                "tool_name": "Write",
                "tool_input": {"file_path": os.path.join(root, "foo", "bar.json")},
            },
            2,
            expected_exit_only(2),
        ),
        Case(
            71,
            "Write InfoTank data/source.json",
            {
                "tool_name": "Write",
                "tool_input": {"file_path": os.path.join(root, "source.json")},
            },
            2,
            expected_exit_only(2),
        ),
        Case(
            72,
            "Write InfoTank data/a/b/source.json",
            {
                "tool_name": "Write",
                "tool_input": {"file_path": os.path.join(root, "a", "b", "source.json")},
            },
            2,
            expected_exit_only(2),
        ),
        Case(
            73,
            "Write InfoTank data/../x/source.json",
            {
                "tool_name": "Write",
                "tool_input": {"file_path": os.path.join(root, "..", "x", "source.json")},
            },
            2,
            expected_exit_only(2),
        ),
        Case(
            74,
            "Write OtherRepo data/foo/source.json",
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": os.path.join(home, "workplace", "OtherRepo", "data", "foo", "source.json"),
                },
            },
            2,
            expected_exit_only(2),
        ),
        Case(
            75,
            "Bash redirect to InfoTank data/foo/source.json",
            bash_payload("echo {} > %s" % allowed),
            2,
            expected_exit_only(2),
        ),
    ]


def run_case(case: Case, hook: Path) -> List[str]:
    try:
        result = run_hook(
            hook,
            case.payload,
            bypass=case.bypass,
            env_overrides=case.env,
            stdin=case.stdin,
        )
        failures = case.check(result)
        if result.error:
            failures.insert(0, result.error)
    except Exception as error:
        result = HookResult(None, "", "", f"{type(error).__name__}: {error}")
        failures = [result.error or "unknown runner error"]

    actual = result.returncode if result.returncode is not None else "ERROR"
    status = "PASS" if not failures else f"FAIL ({'; '.join(failures)})"
    print(
        f"{case.number:02d}. {case.name} | expected={case.expected_exit} "
        f"actual={actual} | {status}"
    )
    return failures


def remove_marker_files(marker_paths: List[Path]) -> List[Path]:
    for marker_path in marker_paths:
        try:
            marker_path.unlink()
        except FileNotFoundError:
            pass
    return [path for path in marker_paths if path.exists()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hook",
        help="hook to exercise (defaults to require-cursor-agent.py next to this test)",
    )
    args = parser.parse_args()
    hook = resolve_hook(args.hook)

    real_model: Optional[str]
    real_model_error: Optional[str]
    try:
        real_model = read_frontmatter_value(REPO_ROOT / ".agents" / "cursor-agent.md", "model")
        real_model_error = None
    except Exception as error:
        real_model = None
        real_model_error = f"{type(error).__name__}: {error}"

    bypass_session_id = f"smoketest-{os.getpid()}-{uuid.uuid4().hex}"
    marker_paths = [bypass_marker_path(bypass_session_id)]
    case_failures = 0
    case_count = 0
    fixture_dir: Optional[Path] = None
    fixture_removed = False

    try:
        with tempfile.TemporaryDirectory(prefix="require-cursor-agent-smoketest-") as temporary_directory:
            fixture_dir = Path(temporary_directory)
            yolo_dir = fixture_dir / "yolo"
            read_only_dir = fixture_dir / "read-only"
            empty_dir = fixture_dir / "empty"
            yolo_dir.mkdir()
            read_only_dir.mkdir()
            empty_dir.mkdir()
            (yolo_dir / "cursor-agent.md").write_text(
                "---\npermission: yolo\nmodel: smoketest-model-x\n---\n",
                encoding="utf-8",
            )
            (read_only_dir / "cursor-agent.md").write_text(
                "---\npermission: read-only\n---\n",
                encoding="utf-8",
            )
            home_dir = fixture_dir / "home"
            skill_home = Path(os.path.realpath(fixture_dir / "skill-home"))
            skill_project = fixture_dir / "skill-project"
            skill_log_dir = fixture_dir / "skill-logs"
            skill_home.mkdir()
            skill_project.mkdir()
            skill_log_dir.mkdir()
            (skill_log_dir / "not-a-directory").write_text("x", encoding="utf-8")
            (home_dir / ".agents").mkdir(parents=True)
            (home_dir / ".agents" / "cursor-agent.md").write_text(
                "---\npermission: yolo\nmodel: smoketest-home-model\n---\n",
                encoding="utf-8",
            )

            cases = make_cases(
                yolo_dir=yolo_dir,
                read_only_dir=read_only_dir,
                empty_dir=empty_dir,
                home_dir=home_dir,
                real_model=real_model,
                real_model_error=real_model_error,
                bypass_session_id=bypass_session_id,
                skill_home=skill_home,
                skill_project=skill_project,
                skill_log_dir=skill_log_dir,
            )
            case_count = len(cases)
            for case in cases:
                if run_case(case, hook):
                    case_failures += 1
    finally:
        if fixture_dir is not None:
            fixture_removed = not fixture_dir.exists()
        remaining_markers = remove_marker_files(marker_paths)

    cleanup_ok = fixture_removed and not remaining_markers
    print(f"Tally: {case_count - case_failures} passed, {case_failures} failed")
    print(f"Cleanup: {'PASS' if cleanup_ok else 'FAIL'}")
    return 0 if case_count == 75 and case_failures == 0 and cleanup_ok else 1


if __name__ == "__main__":
    sys.exit(main())
