#!/usr/bin/env python3
"""Phase 6a: control-plane hashing, cursor-run, control-check, permission probe.

Stand-alone. Prints a table and `Tally: N passed, M failed`. Exits non-zero
on failure. The fake agent is the only cursor-agent these tests run.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import _control
import _paths

CURSOR_RUN = ROOT / "bin" / "cursor-run"
CONTROL_CHECK = ROOT / "bin" / "control-check"
PROBE = ROOT / "bin" / "cursor-permission-probe"
FIXTURE = ROOT / "fixtures" / "cursor-pretooluse-shell.json"

FAKE_AGENT = r'''#!/usr/bin/env python3
import json, os, sys, time

def record():
    path = os.environ.get("FAKE_AGENT_ARGV")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv) + "\n")

def mark():
    path = os.environ.get("FAKE_AGENT_MARKER")
    if not path:
        return
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("started\n")

record()
mark()
if "--version" in sys.argv:
    print("fake-cursor 1.0")
    raise SystemExit(0)

mode = os.environ.get("FAKE_AGENT_MODE", "ok")
if mode == "ok":
    print("agent-ok")
elif mode == "fail":
    print("agent-fail")
    raise SystemExit(1)
elif mode == "sleep":
    time.sleep(int(os.environ.get("FAKE_AGENT_SLEEP", "30")))
    print("awoke")
elif mode == "touch":
    with open(os.environ["FAKE_AGENT_TOUCH"], "a", encoding="utf-8") as handle:
        handle.write("\n# touched\n")
    print("touched")
elif mode == "fail-touch":
    with open(os.environ["FAKE_AGENT_TOUCH"], "a", encoding="utf-8") as handle:
        handle.write("\n# touched\n")
    print("touched")
    raise SystemExit(1)
elif mode == "volatile":
    with open(os.environ["FAKE_AGENT_GIT_HEAD"], "a", encoding="utf-8") as handle:
        handle.write("x")
    pyc = os.environ.get("FAKE_AGENT_PYC")
    if pyc:
        with open(pyc, "ab") as handle:
            handle.write(b"z")
    cfg = os.environ["FAKE_AGENT_CLI_CONFIG"]
    data = json.loads(open(cfg, encoding="utf-8").read())
    data["model"] = "changed-model"
    data["privacyCache"] = {"x": 99}
    with open(cfg, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    print("volatile")
elif mode == "spec-write":
    path = os.environ["FAKE_AGENT_EXTRA"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("extra\n")
    other = os.environ.get("FAKE_AGENT_SPEC_EXTRA")
    if other:
        with open(other, "w", encoding="utf-8") as handle:
            handle.write("spec-side\n")
    print("spec-write")
elif mode == "lines":
    for i in range(int(os.environ.get("FAKE_AGENT_LINES", "10000"))):
        print(f"L{i}")
elif mode == "bad-json":
    with open(os.environ["FAKE_AGENT_CLI_CONFIG"], "w", encoding="utf-8") as handle:
        handle.write("{not json")
    print("bad-json")
elif mode == "chmod-spec":
    os.chmod(os.environ["FAKE_AGENT_SPEC"], 0)
    print("chmod-spec")
elif mode == "readonly-dir":
    print("readonly-dir")
    os.chmod(os.environ["FAKE_AGENT_SPEC_DIR"], 0o555)
elif mode == "probe-write":
    ws = sys.argv[sys.argv.index("--workspace") + 1]
    path = os.path.join(ws, "protected", "x.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("changed\n")
    print("wrote")
elif mode == "probe-noop":
    print("noop")
else:
    print("unknown", mode)
    raise SystemExit(9)
'''

CLI_CONFIG = {
    "model": "a",
    "privacyCache": {"x": 1},
    "permissions": {"allow": []},
}


class Suite:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, number, name, ok, detail=""):
        status = "PASS" if ok else f"FAIL ({detail})"
        print(f"{number:>4}. {name} | {status}")
        if ok:
            self.passed += 1
        else:
            self.failed += 1


def _rm(path):
    def onexc(func, target, _exc):
        try:
            os.chmod(target, 0o700)
        except OSError:
            pass
        parent = os.path.dirname(target)
        if parent:
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass
        func(target)

    shutil.rmtree(path, onexc=onexc)


def _norm(path) -> str:
    return os.path.normpath(str(path))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_world(root: Path):
    home = root / "home"
    proj = root / "proj"
    agent = root / "fake-agent.py"
    _write(home / ".claude" / "settings.json", '{"hooks":{}}\n')
    _write(home / ".claude" / "hooks" / "x.py", "print(1)\n")
    _write(home / ".claude" / "hooks" / ".git" / "HEAD", "ref: refs/heads/master\n")
    (home / ".claude" / "hooks" / "__pycache__").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "hooks" / "__pycache__" / "y.pyc").write_bytes(b"pyc")
    _write(home / ".claude" / "hooks" / "nested" / ".claude" / "specs" / "sneak.md", "sneak\n")
    _write(home / ".cursor" / "cli-config.json", json.dumps(CLI_CONFIG) + "\n")
    _write(home / ".cursor" / "hooks" / "inside.json", "{}\n")
    _write(proj / ".claude" / "specs" / "t.md", "# task\nDo the thing.\n")
    _write(
        proj / ".agents" / "cursor-agent.md",
        "---\nmodel: test-model\npermission: ask\n---\nbody\n",
    )
    agent.write_text(FAKE_AGENT, encoding="utf-8")
    agent.chmod(0o755)
    return home, proj, agent


def hash_plane(home: Path, proj: Path):
    old = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        return _control.hash_control_plane(str(proj))
    finally:
        if old is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old


def run_cursor(home, proj, agent, spec, mode="ok", extra=None, arg=None):
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    env["CLAUDE_ROUTING_MODE"] = "strict"
    env["CURSOR_RUN_AGENT_BIN"] = str(agent)
    env["FAKE_AGENT_MODE"] = mode
    env.pop("SUB_AGENTS_DIR", None)
    env.pop("CURSOR_RUN_TIMEOUT", None)
    if extra:
        env.update(extra)
    if arg is None:
        arg = str(spec)
    return subprocess.run(
        [str(CURSOR_RUN), arg],
        cwd=str(proj),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _new_spec(home, proj, tmp_root, task="t", body="# task\nDo the thing.\n") -> Path:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    env["CURSOR_RUN_TMP_ROOT"] = str(tmp_root)
    env.pop("SUB_AGENTS_DIR", None)
    completed = subprocess.run(
        [str(CURSOR_RUN), "new", task],
        cwd=str(proj),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"cursor-run new failed: {completed.returncode} {completed.stdout!r} {completed.stderr!r}"
        )
    spec = Path(completed.stdout.strip())
    spec.write_text(body, encoding="utf-8")
    return spec


def last_json(stdout: str):
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise ValueError(f"no stdout: {stdout!r}")
    return json.loads(lines[-1])


def run_check(payload, home, proj, script=CONTROL_CHECK, extra=None):
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    env.pop("PYTHONPATH", None)
    if extra:
        env.update(extra)
    return subprocess.run(
        [str(script)],
        input=json.dumps(payload) if not isinstance(payload, str) else payload,
        cwd=str(proj),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def bash_payload(command, event="PreToolUse", tool_use_id="tid", tool="Bash"):
    return {
        "hook_event_name": event,
        "tool_name": tool,
        "tool_input": {"command": command},
        "tool_use_id": tool_use_id,
    }


def test_hash(suite: Suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="p6a-hash-")))
    try:
        home, proj, _agent = make_world(root)
        hashes = hash_plane(home, proj)
        x_py = _norm(home / ".claude" / "hooks" / "x.py")
        settings = _norm(home / ".claude" / "settings.json")
        cli = _norm(home / ".cursor" / "cli-config.json")
        hooks_json = _norm(home / ".cursor" / "hooks.json")
        inside = _norm(home / ".cursor" / "hooks" / "inside.json")
        hooks_dir = _norm(home / ".claude" / "hooks")
        cursor_hooks_dir = _norm(home / ".cursor" / "hooks")
        git_head = _norm(home / ".claude" / "hooks" / ".git" / "HEAD")
        pyc = _norm(home / ".claude" / "hooks" / "__pycache__" / "y.pyc")
        spec = _norm(proj / ".claude" / "specs" / "t.md")
        sneak = _norm(home / ".claude" / "hooks" / "nested" / ".claude" / "specs" / "sneak.md")
        real_home = os.path.expanduser("~")
        under_world = all(
            key.startswith(str(home)) or key.startswith(str(proj)) for key in hashes
        )
        suite.check(
            "1",
            "hash stays inside the temp HOME and project",
            under_world and real_home != str(home),
            f"keys={list(hashes)[:4]!r}",
        )
        suite.check(
            "2",
            "hash includes settings, hooks file, cli-config; skips directories",
            x_py in hashes and settings in hashes and cli in hashes
            and hooks_dir not in hashes and cursor_hooks_dir not in hashes
            and inside in hashes,
            f"has x={x_py in hashes} settings={settings in hashes} cli={cli in hashes}",
        )
        excluded_hit = [git_head, pyc, spec, sneak]
        suite.check(
            "3",
            "hash excludes .git, __pycache__, and .claude/specs",
            all(path not in hashes for path in excluded_hit)
            and not any(
                ".git" in Path(key).parts or "__pycache__" in Path(key).parts
                for key in hashes
            )
            and not any(
                ".claude" in Path(key).parts and "specs" in Path(key).parts
                for key in hashes
            ),
            f"present={[p for p in excluded_hit if p in hashes]!r}",
        )
        suite.check(
            "4",
            "missing non-glob control file is absent",
            hashes.get(hooks_json) == "absent",
            f"value={hashes.get(hooks_json)!r}",
        )
        same = hash_plane(home, proj)
        suite.check(
            "5",
            "unchanged plane has an empty diff",
            _control.diff_hashes(hashes, same) == [],
            f"diff={_control.diff_hashes(hashes, same)!r}",
        )
        cfg_path = home / ".cursor" / "cli-config.json"
        volatile = {
            "privacyCache": {"y": 2},
            "model": "other",
            "permissions": {"allow": []},
        }
        cfg_path.write_text(json.dumps(volatile, indent=2), encoding="utf-8")
        after_volatile = hash_plane(home, proj)
        suite.check(
            "6",
            "cli-config model and privacyCache are not a control change",
            _control.diff_hashes(hashes, after_volatile) == []
            and after_volatile[cli] == hashes[cli],
            f"diff={_control.diff_hashes(hashes, after_volatile)!r}",
        )
        changed_perms = {
            "model": "a",
            "privacyCache": {"x": 1},
            "permissions": {"allow": ["Shell(ls)"]},
        }
        cfg_path.write_text(json.dumps(changed_perms), encoding="utf-8")
        after_perms = hash_plane(home, proj)
        suite.check(
            "7",
            "cli-config permissions change is detected",
            _control.diff_hashes(after_volatile, after_perms) == [cli],
            f"diff={_control.diff_hashes(after_volatile, after_perms)!r}",
        )
        cfg_path.write_text(json.dumps(CLI_CONFIG) + "\n", encoding="utf-8")
        before_create = hash_plane(home, proj)
        (home / ".cursor" / "hooks.json").write_text("{}\n", encoding="utf-8")
        after_create = hash_plane(home, proj)
        suite.check(
            "8",
            "missing then created non-glob file is detected",
            before_create.get(hooks_json) == "absent"
            and hooks_json in _control.diff_hashes(before_create, after_create)
            and after_create[hooks_json] != "absent",
            f"diff={_control.diff_hashes(before_create, after_create)!r}",
        )
        (home / ".cursor" / "hooks.json").unlink()
        cfg_path.write_text("{", encoding="utf-8")
        raised = False
        try:
            hash_plane(home, proj)
        except Exception:
            raised = True
        suite.check("9", "invalid cli-config JSON raises", raised, "no exception")
        for bad in ("[]", "null", '"str"'):
            cfg_path.write_text(bad, encoding="utf-8")
            try:
                hash_plane(home, proj)
                raised = False
            except Exception:
                raised = True
            if not raised:
                break
        suite.check("10", "non-object cli-config JSON raises", raised, f"last={bad}")
        cfg_path.write_text(json.dumps(CLI_CONFIG) + "\n", encoding="utf-8")
        locked = home / ".claude" / "hooks" / "locked"
        locked.mkdir()
        (locked / "z.txt").write_text("z\n", encoding="utf-8")
        locked.chmod(0)
        raised = False
        try:
            hash_plane(home, proj)
        except OSError:
            raised = True
        finally:
            locked.chmod(0o755)
        suite.check(
            "11",
            "unreadable control directory raises (no partial hash)",
            raised,
            "no OSError",
        )
        x_path = home / ".claude" / "hooks" / "x.py"
        x_path.chmod(0)
        raised = False
        try:
            hash_plane(home, proj)
        except OSError:
            raised = True
        finally:
            x_path.chmod(0o644)
        suite.check("12", "unreadable control file raises", raised, "no OSError")
        suite.check(
            "13",
            "diff_hashes unions keys and sorts them",
            _control.diff_hashes({"b": "1", "a": "1"}, {"b": "1", "c": "2"}) == ["a", "c"]
            and _control.diff_hashes({"a": "absent"}, {"a": "abc"}) == ["a"]
            and _control.CLI_CONFIG_VOLATILE == {"model", "privacyCache"},
            f"volatile={_control.CLI_CONFIG_VOLATILE!r}",
        )
    finally:
        _rm(root)


def _marker(root: Path) -> Path:
    return root / "marker"


def _argv_log(root: Path) -> Path:
    return root / "argv.log"


def test_bad_specs(suite: Suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="p6a-bad-")))
    try:
        home, proj, agent = make_world(root)
        marker = _marker(root)
        extra = {"FAKE_AGENT_MARKER": str(marker)}
        outside = root / "outside.md"
        outside.write_text("# no\n", encoding="utf-8")
        nested = proj / ".claude" / "specs" / "nested" / "t.md"
        _write(nested, "# nested\n")
        text = proj / ".claude" / "specs" / "t.txt"
        _write(text, "text\n")
        cases = (
            ("14", "spec outside .claude/specs", str(outside)),
            ("15", "nested spec", os.path.relpath(nested, proj)),
            ("16", "spec ending in .txt", os.path.relpath(text, proj)),
            ("17", "missing spec", ".claude/specs/missing.md"),
        )
        for number, name, arg in cases:
            if marker.exists():
                marker.unlink()
            completed = run_cursor(home, proj, agent, proj / ".claude" / "specs" / "t.md", extra=extra, arg=arg)
            try:
                payload = last_json(completed.stdout)
                status = payload.get("status")
            except Exception as exc:
                status = f"unparsed:{exc}"
            ok = (
                completed.returncode == 2
                and status == "bad_input"
                and not marker.exists()
            )
            suite.check(
                number,
                f"{name} -> exit 2, agent not started",
                ok,
                f"exit={completed.returncode} status={status} marker={marker.exists()} stdout={completed.stdout!r} stderr={completed.stderr!r}",
            )
    finally:
        _rm(root)


def test_runs(suite: Suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="p6a-run-")))
    try:
        home, proj, agent = make_world(root)
        tmp_root = root / "tmproot"
        tmp_root.mkdir(mode=0o700)
        spec = _new_spec(home, proj, tmp_root)
        marker = _marker(root)
        argv_log = _argv_log(root)
        x_py = home / ".claude" / "hooks" / "x.py"
        x_key = _norm(x_py)
        extra_base = {
            "FAKE_AGENT_MARKER": str(marker),
            "FAKE_AGENT_ARGV": str(argv_log),
            "FAKE_AGENT_TOUCH": str(x_py),
            "FAKE_AGENT_GIT_HEAD": str(home / ".claude" / "hooks" / ".git" / "HEAD"),
            "FAKE_AGENT_PYC": str(home / ".claude" / "hooks" / "__pycache__" / "y.pyc"),
            "FAKE_AGENT_CLI_CONFIG": str(home / ".cursor" / "cli-config.json"),
            "FAKE_AGENT_EXTRA": str(home / ".claude" / "hooks" / "nested" / ".claude" / "specs" / "out.md"),
            "FAKE_AGENT_SPEC_EXTRA": str(proj / ".claude" / "specs" / "extra.txt"),
            "FAKE_AGENT_SPEC": str(spec),
            "FAKE_AGENT_SPEC_DIR": str(spec.parent),
            "CURSOR_RUN_TMP_ROOT": str(tmp_root),
        }

        def go(mode, more=None, spec_path=None, arg=None):
            if marker.exists():
                marker.unlink()
            if argv_log.exists():
                argv_log.unlink()
            extra = dict(extra_base)
            if more:
                extra.update(more)
            return run_cursor(
                home, proj, agent, spec_path or spec, mode=mode, extra=extra, arg=arg,
            )

        completed = go("ok")
        try:
            payload = last_json(completed.stdout)
        except Exception as exc:
            payload = {"_error": str(exc), "_stdout": completed.stdout, "_stderr": completed.stderr}
        result_path = Path(str(spec).removesuffix(".md") + ".result.json")
        log_path = Path(str(spec).removesuffix(".md") + ".log")
        mode_ok = False
        if result_path.exists():
            mode_ok = stat.S_IMODE(result_path.stat().st_mode) == 0o600
        log_mode_ok = log_path.exists() and stat.S_IMODE(log_path.stat().st_mode) == 0o600
        log_ok = log_path.exists() and "agent-ok" in log_path.read_text(encoding="utf-8")
        recorded = []
        if argv_log.exists():
            recorded = [json.loads(line) for line in argv_log.read_text(encoding="utf-8").splitlines() if line.strip()]
        ask_argv = recorded[-1] if recorded else []
        suite.check(
            "18",
            "success, no control change -> exit 0, result 0600, log, JSON",
            completed.returncode == 0
            and payload.get("status") == "ok"
            and payload.get("control_changed") is False
            and payload.get("changed_paths") == []
            and payload.get("verification_required") is False
            and "run_id" not in payload
            and mode_ok
            and log_ok
            and log_mode_ok
            and payload.get("project") == str(proj)
            and payload.get("run_dir") == str(spec.parent.parent)
            and marker.exists()
            and "--force" not in ask_argv
            and ask_argv[1:3] == ["-p", "--trust"],
            f"exit={completed.returncode} payload={payload!r} mode={mode_ok} log={log_ok} log_mode={log_mode_ok} argv={ask_argv!r} stderr={completed.stderr!r}",
        )

        completed = go("touch")
        payload = last_json(completed.stdout)
        suite.check(
            "19",
            "success plus a control file touched -> exit 3",
            completed.returncode == 3
            and payload.get("control_changed") is True
            and x_key in payload.get("changed_paths", [])
            and payload.get("status") == "ok"
            and payload.get("verification_required") is False,
            f"exit={completed.returncode} payload={payload!r}",
        )
        x_py.write_text("print(1)\n", encoding="utf-8")

        spec.write_text("# task\nverification_required: false\n", encoding="utf-8")
        completed = go("fail-touch")
        payload = last_json(completed.stdout)
        suite.check(
            "20",
            "agent exit 1 plus control touch still compared -> exit 3",
            completed.returncode == 3
            and payload.get("status") == "failed"
            and payload.get("control_changed") is True
            and x_key in payload.get("changed_paths", [])
            and payload.get("verification_required") is True,
            f"exit={completed.returncode} payload={payload!r}",
        )
        x_py.write_text("print(1)\n", encoding="utf-8")

        started = time.monotonic()
        completed = go("sleep", {"CURSOR_RUN_TIMEOUT": "1", "FAKE_AGENT_SLEEP": "30"})
        elapsed = time.monotonic() - started
        payload = last_json(completed.stdout)
        suite.check(
            "21",
            "timeout -> exit 3, status timeout, compared",
            completed.returncode == 3
            and payload.get("status") == "timeout"
            and payload.get("control_changed") is False
            and payload.get("verification_required") is True
            and elapsed < 20,
            f"exit={completed.returncode} elapsed={elapsed:.1f}s payload={payload!r} stderr={completed.stderr!r}",
        )
        spec.write_text("# task\nDo the thing.\n", encoding="utf-8")

        completed = go("volatile")
        payload = last_json(completed.stdout)
        suite.check(
            "22",
            ".git/HEAD, __pycache__, and cli-config model change -> exit 0",
            completed.returncode == 0
            and payload.get("status") == "ok"
            and payload.get("control_changed") is False,
            f"exit={completed.returncode} payload={payload!r}",
        )

        completed = go("spec-write")
        payload = last_json(completed.stdout)
        wrote_specs = (proj / ".claude" / "specs" / "extra.txt").is_file()
        suite.check(
            "23",
            "writing under .claude/specs is not a control change",
            completed.returncode == 0
            and payload.get("control_changed") is False
            and wrote_specs,
            f"exit={completed.returncode} wrote={wrote_specs} payload={payload!r}",
        )

        x_py.chmod(0)
        try:
            completed = go("ok")
            payload = last_json(completed.stdout)
            ok = (
                completed.returncode == 4
                and payload.get("status") == "check_error"
                and payload.get("stage") == "hash_before"
                and not marker.exists()
            )
        finally:
            x_py.chmod(0o644)
        suite.check(
            "24",
            "hash_before failure -> exit 4, agent not started",
            ok,
            f"exit={completed.returncode} payload={payload!r} marker={marker.exists()} stderr={completed.stderr!r}",
        )

        completed = go("bad-json")
        payload = last_json(completed.stdout)
        suite.check(
            "25",
            "hash_after invalid cli-config -> exit 4 stage hash_after",
            completed.returncode == 4
            and payload.get("status") == "check_error"
            and payload.get("stage") == "hash_after"
            and payload.get("control_changed") is None
            and payload.get("verification_required") is True,
            f"exit={completed.returncode} payload={payload!r}",
        )
        (home / ".cursor" / "cli-config.json").write_text(json.dumps(CLI_CONFIG) + "\n", encoding="utf-8")

        completed = go("chmod-spec")
        payload = last_json(completed.stdout)
        suite.check(
            "26",
            "unreadable spec on a successful run stays ok",
            completed.returncode == 0
            and payload.get("status") == "ok"
            and payload.get("verification_required") is False
            and "stage" not in payload,
            f"exit={completed.returncode} payload={payload!r}",
        )
        spec.chmod(0o644)

        spec.write_text("# task\nverification_required: true\n", encoding="utf-8")
        completed = go("ok")
        payload = last_json(completed.stdout)
        suite.check(
            "27",
            "verification_required: true does not flag a successful run",
            completed.returncode == 0
            and payload.get("status") == "ok"
            and payload.get("files_changed") is None
            and payload.get("verification_required") is False,
            f"exit={completed.returncode} payload={payload!r}",
        )
        spec.write_text("# task\nDo the thing.\n", encoding="utf-8")
        if result_path.exists():
            result_path.unlink()

        completed = go("readonly-dir")
        payload = last_json(completed.stdout)
        suite.check(
            "28",
            "unwritable result path on a clean run -> exit 5 with report_error",
            completed.returncode == 5
            and "report_error" in payload
            and payload.get("status") == "ok"
            and payload.get("control_changed") is False,
            f"exit={completed.returncode} payload={payload!r} stderr={completed.stderr!r}",
        )
        spec.parent.chmod(0o755)

        completed = go("lines", {"FAKE_AGENT_LINES": "10000"})
        payload = last_json(completed.stdout)
        log_lines = log_path.read_text(encoding="utf-8").splitlines()
        summary = payload.get("summary", "")
        summary_lines = summary.splitlines() if isinstance(summary, str) else list(summary)
        suite.check(
            "29",
            "10k lines -> summary is the last 200, log has all",
            completed.returncode == 0
            and len(log_lines) == 10000
            and log_lines[0] == "L0"
            and log_lines[-1] == "L9999"
            and summary_lines == log_lines[-200:]
            and len(summary_lines) <= 200,
            f"exit={completed.returncode} log={len(log_lines)} summary={len(summary_lines)} stderr={completed.stderr!r}",
        )

        definition = proj / ".agents" / "cursor-agent.md"
        definition.write_text("---\nmodel: test-model\npermission: yolo\n---\nbody\n", encoding="utf-8")
        completed = go("ok")
        payload = last_json(completed.stdout)
        recorded = []
        if argv_log.exists():
            recorded = [json.loads(line) for line in argv_log.read_text(encoding="utf-8").splitlines() if line.strip()]
        yolo_argv = recorded[-1] if recorded else []
        suite.check(
            "30",
            "permission yolo inserts --force immediately after -p",
            completed.returncode == 0
            and yolo_argv[1:3] == ["-p", "--force"]
            and "--trust" in yolo_argv
            and "--model" in yolo_argv,
            f"exit={completed.returncode} argv={yolo_argv!r} payload_status={payload.get('status')}",
        )
        definition.write_text("---\nmodel: test-model\npermission: ask\n---\nbody\n", encoding="utf-8")

        definition.unlink()
        completed = go("ok")
        payload = last_json(completed.stdout)
        suite.check(
            "31",
            "unreadable agent definition -> exit 4, agent not started",
            completed.returncode == 4
            and payload.get("status") == "check_error"
            and payload.get("stage") == "agent_def"
            and not marker.exists(),
            f"exit={completed.returncode} payload={payload!r} marker={marker.exists()}",
        )
    finally:
        _rm(root)


def _context(stdout: str):
    data = json.loads(stdout)
    return data["hookSpecificOutput"]["hookEventName"], data["hookSpecificOutput"]["additionalContext"]


def test_control_check(suite: Suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="p6a-cc-")))
    try:
        home, proj, _agent = make_world(root)
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        completed = run_check(fixture, home, proj)
        try:
            allow = json.loads(completed.stdout)
        except json.JSONDecodeError:
            allow = None
        suite.check(
            "32",
            "Cursor preToolUse payload -> allow",
            completed.returncode == 0 and allow == {"permission": "allow"} and completed.stderr == "",
            f"exit={completed.returncode} stdout={completed.stdout!r} stderr={completed.stderr!r}",
        )
        post_fixture = dict(fixture)
        post_fixture["hook_event_name"] = "postToolUse"
        completed = run_check(post_fixture, home, proj)
        suite.check(
            "33",
            "Cursor post payload -> silent",
            completed.returncode == 0 and completed.stdout == "",
            f"exit={completed.returncode} stdout={completed.stdout!r} stderr={completed.stderr!r}",
        )

        alone = root / "alone"
        alone.mkdir()
        copied = alone / "control-check"
        shutil.copy(CONTROL_CHECK, copied)
        copied.chmod(0o755)
        completed = run_check(fixture, home, proj, script=copied)
        try:
            allow = json.loads(completed.stdout)
        except json.JSONDecodeError:
            allow = None
        post_copy = run_check(post_fixture, home, proj, script=copied)
        suite.check(
            "34",
            "Cursor payloads work with the script copied alone",
            completed.returncode == 0
            and allow == {"permission": "allow"}
            and post_copy.returncode == 0
            and post_copy.stdout == ""
            and completed.stderr == ""
            and post_copy.stderr == "",
            f"pre={completed.returncode}/{completed.stdout!r}/{completed.stderr!r} post={post_copy.returncode}/{post_copy.stdout!r}/{post_copy.stderr!r}",
        )
        broken = run_check(bash_payload("cursor-agent -p hi", tool_use_id="import-1"), home, proj, script=copied)
        snap = home / ".claude" / "state" / "control-check" / "import-1.json"
        suite.check(
            "35",
            "import failure on a cursor command -> exit 0 and a stderr note",
            broken.returncode == 0
            and broken.stdout == ""
            and "_control" in broken.stderr
            and not snap.exists(),
            f"exit={broken.returncode} stdout={broken.stdout!r} stderr={broken.stderr!r}",
        )

        state = home / ".claude" / "state" / "control-check"
        completed = run_check(bash_payload("ls -la", tool_use_id="ls-1"), home, proj)
        suite.check(
            "36",
            "non-cursor Bash -> silent, no snapshot",
            completed.returncode == 0
            and completed.stdout == ""
            and not (state / "ls-1.json").exists(),
            f"exit={completed.returncode} stdout={completed.stdout!r} stderr={completed.stderr!r}",
        )
        for command, number, label in (
            ("mycursor-agent -p x", "37", "mycursor-agent is not a cursor command"),
            ("cursor-runbook", "38", "cursor-runbook is not a cursor command"),
        ):
            completed = run_check(bash_payload(command, tool_use_id="neg"), home, proj)
            suite.check(
                number,
                f"{label} -> silent, no snapshot",
                completed.returncode == 0
                and completed.stdout == ""
                and not (state / "neg.json").exists(),
                f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
            )
        completed = run_check(
            bash_payload("cursor-agent -p hi", tool="Shell", tool_use_id="shell-1"),
            home,
            proj,
        )
        suite.check(
            "39",
            "Shell tool_name is not monitored",
            completed.returncode == 0 and completed.stdout == "" and not (state / "shell-1.json").exists(),
            f"stdout={completed.stdout!r}",
        )

        state.mkdir(parents=True, exist_ok=True)
        old = state / "old.json"
        fresh = state / "fresh.json"
        old.write_text("{}\n", encoding="utf-8")
        fresh.write_text("{}\n", encoding="utf-8")
        old_time = time.time() - (25 * 60 * 60)
        os.utime(old, (old_time, old_time))
        completed = run_check(bash_payload("ls", tool_use_id="sweep-1"), home, proj)
        suite.check(
            "40",
            "snapshots older than 24h are deleted, fresh ones stay",
            completed.returncode == 0
            and not old.exists()
            and fresh.exists()
            and not (state / "sweep-1.json").exists(),
            f"old={old.exists()} fresh={fresh.exists()} stdout={completed.stdout!r}",
        )

        pre = run_check(bash_payload("cursor-agent -p hi", tool_use_id="same"), home, proj)
        snap = state / "same.json"
        dir_mode = stat.S_IMODE(state.stat().st_mode) if state.is_dir() else None
        file_mode = stat.S_IMODE(snap.stat().st_mode) if snap.is_file() else None
        pre_ok = (
            pre.returncode == 0
            and pre.stdout == ""
            and snap.is_file()
            and file_mode == 0o600
            and dir_mode == 0o700
        )
        post = run_check(
            bash_payload("cursor-agent -p hi", event="PostToolUse", tool_use_id="same"),
            home,
            proj,
        )
        suite.check(
            "41",
            "pre+post with no change -> silent, snapshot removed, modes 0700/0600",
            pre_ok and post.returncode == 0 and post.stdout == "" and not snap.exists(),
            f"pre={pre.returncode}/{pre.stdout!r} dir={dir_mode} file={file_mode} post={post.returncode}/{post.stdout!r} snap={snap.exists()} stderr={pre.stderr!r}{post.stderr!r}",
        )

        x_py = home / ".claude" / "hooks" / "x.py"
        x_key = _norm(x_py)
        pre = run_check(bash_payload("/usr/bin/cursor-agent -p hi", tool_use_id="chg"), home, proj)
        x_py.write_text("print(2)\n", encoding="utf-8")
        post = run_check(
            bash_payload("/usr/bin/cursor-agent -p hi", event="PostToolUse", tool_use_id="chg"),
            home,
            proj,
        )
        try:
            event, text = _context(post.stdout)
        except Exception as exc:
            event, text = None, f"{exc}: {post.stdout!r}"
        suite.check(
            "42",
            "pre, touch control file, post -> additionalContext lists the path",
            pre.returncode == 0
            and pre.stdout == ""
            and post.returncode == 0
            and event == "PostToolUse"
            and "CONTROL PLANE CHANGED during this cursor run: " in text
            and x_key in text
            and text.endswith("Stop and ask the user before continuing.")
            and not (state / "chg.json").exists(),
            f"event={event} text={text!r} stderr={post.stderr!r}",
        )
        x_py.write_text("print(1)\n", encoding="utf-8")

        pre = run_check(bash_payload("cursor-run .claude/specs/t.md", tool_use_id="fail"), home, proj)
        x_py.write_text("print(3)\n", encoding="utf-8")
        post = run_check(
            bash_payload(
                "cursor-run .claude/specs/t.md",
                event="PostToolUseFailure",
                tool_use_id="fail",
            ),
            home,
            proj,
        )
        try:
            event, text = _context(post.stdout)
        except Exception as exc:
            event, text = None, f"{exc}: {post.stdout!r}"
        suite.check(
            "43",
            "PostToolUseFailure reports the same control-plane change",
            pre.returncode == 0
            and post.returncode == 0
            and event == "PostToolUseFailure"
            and x_key in text
            and "CONTROL PLANE CHANGED during this cursor run: " in text
            and not (state / "fail.json").exists(),
            f"event={event} text={text!r}",
        )
        x_py.write_text("print(1)\n", encoding="utf-8")

        missing = run_check(
            bash_payload("cursor-agent -p hi", event="PostToolUse", tool_use_id="no-pre"),
            home,
            proj,
        )
        try:
            event, text = _context(missing.stdout)
        except Exception as exc:
            event, text = None, f"{exc}: {missing.stdout!r}"
        suite.check(
            "44",
            "post without pre -> no pre-run snapshot",
            missing.returncode == 0
            and event == "PostToolUse"
            and text == "control-check: no pre-run snapshot; control plane not compared",
            f"event={event} text={text!r}",
        )

        pre = run_check(bash_payload("cursor-agent -p hi", tool_use_id="hasherr"), home, proj)
        x_py.chmod(0)
        try:
            post = run_check(
                bash_payload("cursor-agent -p hi", event="PostToolUse", tool_use_id="hasherr"),
                home,
                proj,
            )
        finally:
            x_py.chmod(0o644)
        try:
            _event, text = _context(post.stdout)
        except Exception as exc:
            text = f"{exc}: {post.stdout!r}"
        suite.check(
            "45",
            "post hashing error names the failure",
            pre.returncode == 0
            and post.returncode == 0
            and "control-check could not hash the control plane:" in text,
            f"text={text!r} stderr={post.stderr!r}",
        )

        bad = run_check("{", home, proj)
        empty = run_check("", home, proj)
        suite.check(
            "46",
            "invalid stdin -> exit 0",
            bad.returncode == 0 and bad.stdout == "" and empty.returncode == 0 and empty.stdout == "",
            f"bad={bad.returncode}/{bad.stdout!r} empty={empty.returncode}/{empty.stdout!r}",
        )
    finally:
        _rm(root)


def test_probe(suite: Suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="p6a-probe-")))
    try:
        _home, _proj, agent = make_world(root)
        argv_log = _argv_log(root)

        def probe(mode):
            if argv_log.exists():
                argv_log.unlink()
            env = os.environ.copy()
            env["CURSOR_RUN_AGENT_BIN"] = str(agent)
            env["FAKE_AGENT_MODE"] = mode
            env["FAKE_AGENT_ARGV"] = str(argv_log)
            env.pop("SUB_AGENTS_DIR", None)
            return subprocess.run(
                [str(PROBE)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        def recorded():
            rows = []
            if argv_log.exists():
                for line in argv_log.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        rows.append(json.loads(line))
            return rows

        wrote = probe("probe-write")
        try:
            payload = last_json(wrote.stdout)
        except Exception as exc:
            payload = {"_error": str(exc), "_stdout": wrote.stdout}
        rows = recorded()
        flat = [arg for row in rows for arg in row]
        workspace = None
        for row in rows:
            if "--workspace" in row:
                workspace = row[row.index("--workspace") + 1]
        suite.check(
            "47",
            "probe: agent that writes the file -> deny_held false, exit 1, no --force",
            wrote.returncode == 1
            and payload.get("deny_held") is False
            and "--force" not in flat
            and "--trust" in flat
            and "Overwrite protected/x.txt with the word changed using your edit tool." in flat
            and "--output-format" not in flat
            and payload.get("cursor_version") == "fake-cursor 1.0"
            and workspace is not None
            and not os.path.exists(workspace),
            f"exit={wrote.returncode} payload={payload!r} argv={rows!r} stderr={wrote.stderr!r}",
        )

        noop = probe("probe-noop")
        try:
            payload = last_json(noop.stdout)
        except Exception as exc:
            payload = {"_error": str(exc)}
        rows = recorded()
        flat = [arg for row in rows for arg in row]
        suite.check(
            "48",
            "probe: agent that does nothing -> deny_held true, exit 0, no --force",
            noop.returncode == 0
            and payload.get("deny_held") is True
            and payload.get("cursor_version") == "fake-cursor 1.0"
            and "--force" not in flat
            and "-p" in flat,
            f"exit={noop.returncode} payload={payload!r} argv={rows!r} stderr={noop.stderr!r}",
        )
    finally:
        _rm(root)


def test_opus_arbitration_paths(suite: Suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="p6a-opus-")))
    try:
        home, proj, _agent = make_world(root)
        ledger = home / ".claude" / "logs" / "opus-arbitration" / "ledger.db"
        _write(ledger, "runtime\n")
        judge = home / ".claude" / "agents" / "opus-judge.md"
        _write(judge, "---\nmodel: opus\n---\n")
        mode = home / ".claude" / "opus-arbitration-mode"
        _write(mode, "active\n")
        hashes = hash_plane(home, proj)
        ledger_key = _norm(ledger)
        judge_key = _norm(judge)
        mode_key = _norm(mode)
        entries = _paths.REGISTRY["control"]
        pattern = str(home) + "/.claude/logs/opus-arbitration/**"
        suite.check(
            "19",
            "opus arbitration ledger is control but not hashed",
            "~/.claude/logs/opus-arbitration/**" in entries
            and "~/.claude/agents/opus-judge.md" in entries
            and "~/.claude/opus-arbitration-mode" in entries
            and _paths.match(str(ledger), pattern)
            and ledger_key not in hashes
            and judge_key in hashes
            and mode_key in hashes,
            f"ledger={ledger_key in hashes} judge={judge_key in hashes} mode={mode_key in hashes}",
        )
    finally:
        _rm(root)


def main() -> int:
    suite = Suite()
    test_hash(suite)
    test_opus_arbitration_paths(suite)
    test_bad_specs(suite)
    test_runs(suite)
    test_control_check(suite)
    test_probe(suite)
    print(f"Tally: {suite.passed} passed, {suite.failed} failed")
    return 0 if suite.failed == 0 and suite.passed > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
