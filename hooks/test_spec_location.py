#!/usr/bin/env python3
"""Tmp run specs: cursor-run new, execution checks, classification, hook policy.

Uses CURSOR_RUN_TMP_ROOT and CURSOR_RUN_AGENT_BIN. Prints a table and
`Tally: N passed, M failed`.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import _paths

CURSOR_RUN = ROOT / "bin" / "cursor-run"
REQUIRE = ROOT / "require-cursor-agent.py"
ROUTE = ROOT / "route-tools.py"
TS_RE = re.compile(r"^\d{8}-\d{6}-\d{6}$")
STATE_DENY = "Specs, memory and plans belong to the orchestrator. Report back instead."
READER_WRITE_DENY = "file-reader is read-only."

FAKE_AGENT = r'''#!/usr/bin/env python3
import os, sys, time

marker = os.environ.get("FAKE_AGENT_MARKER")
if marker:
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write("started\n")
mode = os.environ.get("FAKE_AGENT_MODE", "ok")
if mode == "ok":
    print("agent-ok")
elif mode == "fail":
    touch = os.environ.get("FAKE_AGENT_TOUCH")
    if touch:
        with open(touch, "a", encoding="utf-8") as handle:
            handle.write("\n# touched\n")
    print("agent-fail")
    raise SystemExit(1)
elif mode == "sleep":
    time.sleep(int(os.environ.get("FAKE_AGENT_SLEEP", "30")))
    print("awoke")
elif mode == "chmod-dir":
    print("chmod-dir")
    os.chmod(os.environ["FAKE_AGENT_SPEC_DIR"], 0o555)
else:
    print("unknown", mode)
    raise SystemExit(9)
'''


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


def _mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _world(root: Path):
    home = root / "home"
    proj = root / "proj"
    tmp_root = root / "tmproot"
    agent = root / "fake-agent.py"
    home.mkdir()
    proj.mkdir()
    tmp_root.mkdir(mode=0o700)
    (proj / ".agents").mkdir()
    (proj / ".agents" / "cursor-agent.md").write_text(
        "---\nmodel: test-model\npermission: ask\n---\nbody\n",
        encoding="utf-8",
    )
    (home / ".claude" / "hooks").mkdir(parents=True)
    (home / ".claude" / "hooks" / "x.py").write_text("print(1)\n", encoding="utf-8")
    agent.write_text(FAKE_AGENT, encoding="utf-8")
    agent.chmod(0o755)
    return home, proj, tmp_root, agent


def _env(home, proj, tmp_root, agent, **extra):
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    env["CLAUDE_ROUTING_MODE"] = "strict"
    env["CURSOR_RUN_TMP_ROOT"] = str(tmp_root)
    env["CURSOR_RUN_AGENT_BIN"] = str(agent)
    env.pop("SUB_AGENTS_DIR", None)
    env.pop("CURSOR_RUN_TIMEOUT", None)
    env.update(extra)
    return env


def _run(home, proj, tmp_root, agent, args, **extra):
    return subprocess.run(
        [str(CURSOR_RUN), *args],
        cwd=str(proj),
        env=_env(home, proj, tmp_root, agent, **extra),
        text=True,
        capture_output=True,
        check=False,
    )


def _json(stdout: str):
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise ValueError(f"no stdout: {stdout!r}")
    return json.loads(lines[-1])


def _new(home, proj, tmp_root, agent, task="task"):
    completed = _run(home, proj, tmp_root, agent, ["new", task])
    return completed, completed.stdout.strip()


def _marker(root: Path) -> Path:
    return root / "marker"


def _reject(suite, home, proj, tmp_root, agent, root, number, name, arg):
    marker = _marker(root)
    if marker.exists():
        marker.unlink()
    completed = _run(
        home, proj, tmp_root, agent, [arg], FAKE_AGENT_MARKER=str(marker),
    )
    try:
        status = _json(completed.stdout).get("status")
    except Exception as exc:
        status = f"unparsed:{exc}"
    suite.check(
        number,
        name,
        completed.returncode == 2 and status == "bad_input" and not marker.exists(),
        f"exit={completed.returncode} status={status} marker={marker.exists()} "
        f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
    )
    return completed


def _hook(script, payload, project, tmp_root, extra=None):
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project)
    env["CLAUDE_ROUTING_MODE"] = "strict"
    env["CURSOR_RUN_TMP_ROOT"] = str(tmp_root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    fd, log_path = tempfile.mkstemp(prefix="spec-loc-routing-", suffix=".jsonl")
    os.close(fd)
    env["ROUTING_LOG"] = log_path
    env.pop("CURSOR_AGENT", None)
    env.pop("SUB_AGENTS_DIR", None)
    env.pop("CLAUDE_ROUTING", None)
    if extra:
        env.update(extra)
    return subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=str(project),
        check=False,
    )


def _decision(stdout: str):
    text = stdout.strip()
    if not text:
        return None, None
    data = json.loads(text)
    hook_out = data.get("hookSpecificOutput") or {}
    return hook_out.get("permissionDecision"), hook_out.get("permissionDecisionReason")


def test_t1(suite: Suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="spec-loc-t1-")))
    try:
        home, proj, tmp_root, agent = _world(root)
        completed, spec = _new(home, proj, tmp_root, agent, "task")
        path = Path(spec)
        shape = re.compile(
            rf"^{re.escape(str(tmp_root))}/{re.escape(proj.name)}/"
            rf"\d{{8}}-\d{{6}}-\d{{6}}/specs/task\.md$"
        )
        run_dir = path.parent.parent
        run_json = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        created = run_json.get("created")
        parsed = None
        try:
            parsed = datetime.fromisoformat(created)
        except Exception:
            parsed = None
        suite.check(
            "T1",
            "new prints a 0700 run and does not create the spec",
            completed.returncode == 0
            and completed.stderr == ""
            and completed.stdout == spec + "\n"
            and shape.fullmatch(spec) is not None
            and TS_RE.fullmatch(run_dir.name) is not None
            and not path.exists()
            and _mode(tmp_root / proj.name) == 0o700
            and _mode(run_dir) == 0o700
            and _mode(path.parent) == 0o700
            and _mode(run_dir / "run.json") == 0o600
            and run_json.get("project") == str(proj)
            and run_json.get("repo_name") == proj.name
            and run_json.get("task") == "task"
            and run_json.get("head") is None
            and parsed is not None,
            f"exit={completed.returncode} stdout={completed.stdout!r} stderr={completed.stderr!r} "
            f"json={run_json!r} modes="
            f"{_mode(tmp_root / proj.name):o},{_mode(run_dir):o},{_mode(path.parent):o},"
            f"{_mode(run_dir / 'run.json'):o}",
        )
    finally:
        _rm(root)


def test_t2(suite: Suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="spec-loc-t2-")))
    try:
        home, proj, tmp_root, agent = _world(root)
        completed, spec = _new(home, proj, tmp_root, agent, "task")
        path = Path(spec)
        path.write_text("# task\nDo the thing.\n", encoding="utf-8")
        ran = _run(home, proj, tmp_root, agent, [spec], FAKE_AGENT_MARKER=str(_marker(root)))
        try:
            payload = _json(ran.stdout)
        except Exception as exc:
            payload = {"_error": str(exc), "_stdout": ran.stdout}
        log_path = Path(spec.removesuffix(".md") + ".log")
        result_path = Path(spec.removesuffix(".md") + ".result.json")
        legacy = proj / ".claude" / "specs"
        suite.check(
            "T2",
            "new, write, run -> exit 0 beside the spec; no .claude/specs",
            completed.returncode == 0
            and ran.returncode == 0
            and payload.get("status") == "ok"
            and payload.get("run_dir") == str(path.parent.parent)
            and payload.get("project") == str(proj)
            and log_path.is_file()
            and result_path.is_file()
            and _mode(log_path) == 0o600
            and _mode(result_path) == 0o600
            and "agent-ok" in log_path.read_text(encoding="utf-8")
            and not legacy.exists(),
            f"new={completed.returncode} exit={ran.returncode} payload={payload!r} "
            f"legacy={legacy.exists()} stderr={ran.stderr!r}",
        )
    finally:
        _rm(root)


def test_t3(suite: Suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="spec-loc-t3-")))
    try:
        home, proj, tmp_root, agent = _world(root)
        completed, spec = _new(home, proj, tmp_root, agent, "task")
        suite.check(
            "T3",
            "setup new",
            completed.returncode == 0 and spec.endswith("/specs/task.md"),
            completed.stdout + completed.stderr,
        )
        path = Path(spec)
        path.write_text("# task\n", encoding="utf-8")
        run_dir = path.parent.parent
        run_json = run_dir / "run.json"

        other = root / "elsewhere" / proj.name
        other.mkdir(parents=True)
        saved = json.loads(run_json.read_text(encoding="utf-8"))
        saved["project"] = str(other)
        run_json.write_text(json.dumps(saved) + "\n", encoding="utf-8")
        _reject(suite, home, proj, tmp_root, agent, root, "T3", "other project via run.json", spec)
        saved["project"] = str(proj)
        run_json.write_text(json.dumps(saved) + "\n", encoding="utf-8")
        os.chmod(run_json, 0o600)

        mismatched = spec.replace(f"/{proj.name}/", "/othername/", 1)
        _reject(suite, home, proj, tmp_root, agent, root, "T3", "repo-name mismatch", mismatched)

        aside = run_dir.parent / (run_dir.name + "-real")
        run_dir.rename(aside)
        run_dir.symlink_to(aside, target_is_directory=True)
        _reject(suite, home, proj, tmp_root, agent, root, "T3", "symlinked run dir", spec)
        run_dir.unlink()
        aside.rename(run_dir)

        target = root / "elsewhere.md"
        target.write_text("# task\n", encoding="utf-8")
        path.unlink()
        path.symlink_to(target)
        _reject(suite, home, proj, tmp_root, agent, root, "T3", "symlinked spec", spec)
        path.unlink()
        path.write_text("# task\n", encoding="utf-8")

        repo = tmp_root / proj.name
        repo_real = tmp_root / "repo-real"
        repo.rename(repo_real)
        repo.symlink_to(repo_real, target_is_directory=True)
        _reject(suite, home, proj, tmp_root, agent, root, "T3", "symlinked repo-name dir", spec)
        fresh = _run(home, proj, tmp_root, agent, ["new", "other"])
        suite.check(
            "T3",
            "new refuses a symlinked repo-name dir",
            fresh.returncode == 2 and _json(fresh.stdout).get("status") == "bad_input",
            f"exit={fresh.returncode} stdout={fresh.stdout!r} stderr={fresh.stderr!r}",
        )
        repo.unlink()
        repo_real.rename(repo)

        dotted = spec.replace("/specs/", "/specs/../specs/")
        _reject(suite, home, proj, tmp_root, agent, root, "T3", ".. traversal", dotted)

        run_dir.chmod(0o777)
        _reject(suite, home, proj, tmp_root, agent, root, "T3", "group/other-writable run dir", spec)
        run_dir.chmod(0o700)

        path.parent.chmod(0o707)
        _reject(suite, home, proj, tmp_root, agent, root, "T3", "other-writable specs dir", spec)
        path.parent.chmod(0o700)

        run_json.unlink()
        _reject(suite, home, proj, tmp_root, agent, root, "T3", "missing run.json", spec)
        run_json.write_text("{", encoding="utf-8")
        _reject(suite, home, proj, tmp_root, agent, root, "T3", "garbled run.json", spec)
        run_json.write_text(json.dumps(saved) + "\n", encoding="utf-8")
        os.chmod(run_json, 0o600)

        for bad in ("Bad", "a_b", "-no", "a" * 65, ""):
            refused = _run(home, proj, tmp_root, agent, ["new", bad])
            try:
                status = _json(refused.stdout).get("status")
            except Exception as exc:
                status = f"unparsed:{exc}"
            suite.check(
                "T3",
                f"bad task name {bad!r} on new",
                refused.returncode == 2 and status == "bad_input",
                f"exit={refused.returncode} stdout={refused.stdout!r} stderr={refused.stderr!r}",
            )
        nope = str(path.parent / "Nope.md")
        Path(nope).write_text("# no\n", encoding="utf-8")
        _reject(suite, home, proj, tmp_root, agent, root, "T3", "bad task name on run", nope)

        legacy = proj / ".claude" / "specs" / "x.md"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("keep\n", encoding="utf-8")
        before = legacy.read_bytes()
        for arg, label in (
            (".claude/specs/x.md", "legacy relative"),
            (str(legacy), "legacy absolute"),
        ):
            done = _reject(suite, home, proj, tmp_root, agent, root, "T3", label, arg)
            error = ""
            try:
                error = _json(done.stdout).get("error") or ""
            except Exception:
                error = done.stdout
            suite.check(
                "T3",
                f"{label} names cursor-run new and leaves the file",
                "cursor-run new <task>" in error and legacy.read_bytes() == before,
                f"error={error!r} bytes={legacy.read_bytes()!r}",
            )
    finally:
        _rm(root)


def test_t4(suite: Suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="spec-loc-t4-")))
    try:
        home, proj, tmp_root, agent = _world(root)
        env = _env(home, proj, tmp_root, agent)
        procs = [
            subprocess.Popen(
                [str(CURSOR_RUN), "new", "same-task"],
                cwd=str(proj),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(20)
        ]
        outs = [proc.communicate() for proc in procs]
        paths = []
        problems = []
        for proc, (stdout, stderr) in zip(procs, outs):
            if proc.returncode != 0 or stderr:
                problems.append((proc.returncode, stdout, stderr))
            paths.append(stdout.strip())
        suite.check(
            "T4",
            "20 parallel new calls -> 20 distinct run dirs",
            not problems and len(set(paths)) == 20 and all(Path(p).parent.parent.is_dir() for p in paths),
            f"problems={problems!r} paths={paths!r}",
        )
    finally:
        _rm(root)


def test_t5(suite: Suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="spec-loc-t5-")))
    try:
        home, proj, _plain, agent = _world(root)
        real = root / "real-tmp"
        link = root / "link-tmp"
        real.mkdir(mode=0o700)
        link.symlink_to(real, target_is_directory=True)
        completed, spec = _new(home, proj, link, agent, "task")
        path = Path(spec)
        path.write_text("# task\n", encoding="utf-8")
        ran = _run(home, proj, link, agent, [spec])
        try:
            payload = _json(ran.stdout)
        except Exception as exc:
            payload = {"_error": str(exc)}
        real_spec = os.path.realpath(spec)
        ran_real = _run(home, proj, link, agent, [real_spec])
        alias = path.parent / "alias.md"
        alias.symlink_to(path)
        # A symlink below the tmp root, even when it points at the real spec.
        rejected = _run(home, proj, link, agent, [str(alias)], FAKE_AGENT_MARKER=str(_marker(root)))
        suite.check(
            "T5",
            "symlink tmp root works; a deeper symlink is rejected",
            completed.returncode == 0
            and spec.startswith(str(link) + os.sep)
            and ran.returncode == 0
            and payload.get("status") == "ok"
            and ran_real.returncode == 0
            and rejected.returncode == 2
            and _json(rejected.stdout).get("status") == "bad_input"
            and not _marker(root).exists(),
            f"new={completed.returncode}/{completed.stdout!r} run={ran.returncode}/{payload!r} "
            f"real={ran_real.returncode}/{ran_real.stdout!r} "
            f"deep={rejected.returncode}/{rejected.stdout!r} stderr={rejected.stderr!r}",
        )
    finally:
        _rm(root)


def _classify(project, tmp_root, path):
    old_proj = os.environ.get("CLAUDE_PROJECT_DIR")
    old_tmp = os.environ.get("CURSOR_RUN_TMP_ROOT")
    os.environ["CLAUDE_PROJECT_DIR"] = str(project)
    os.environ["CURSOR_RUN_TMP_ROOT"] = str(tmp_root)
    try:
        return _paths.classify_write_target("Write", {"file_path": path})
    finally:
        if old_proj is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old_proj
        if old_tmp is None:
            os.environ.pop("CURSOR_RUN_TMP_ROOT", None)
        else:
            os.environ["CURSOR_RUN_TMP_ROOT"] = old_tmp


def test_t6(suite: Suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="spec-loc-t6-")))
    try:
        home, proj, _plain, agent = _world(root)
        real = root / "real-tmp"
        link = root / "link-tmp"
        real.mkdir(mode=0o700)
        link.symlink_to(real, target_is_directory=True)
        completed, spec = _new(home, proj, link, agent, "task")
        path = Path(spec)
        missing = _classify(proj, link, spec)
        path.write_text("# task\n", encoding="utf-8")
        present = _classify(proj, link, spec)
        run_dir = path.parent.parent
        run_dir.chmod(0o777)
        writable = _classify(proj, link, spec)
        run_dir.chmod(0o700)
        legacy = str(proj / ".claude" / "specs" / "old.md")
        legacy_cls = _classify(proj, link, legacy)
        suite.check(
            "T6",
            "valid spec is state, a failed check is outside, legacy stays state",
            completed.returncode == 0
            and missing == "state"
            and present == "state"
            and writable == "outside"
            and legacy_cls == "state",
            f"missing={missing} present={present} writable={writable} legacy={legacy_cls} "
            f"new={completed.returncode}/{completed.stderr!r}",
        )
    finally:
        _rm(root)


def test_t7(suite: Suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="spec-loc-t7-")))
    try:
        home, proj, tmp_root, agent = _world(root)
        completed, spec = _new(home, proj, tmp_root, agent, "task")
        write = {"tool_name": "Write", "tool_input": {"file_path": spec}}

        def role(agent_id=None, agent_type=None):
            body = dict(write)
            if agent_id is not None:
                body["agent_id"] = agent_id
            if agent_type is not None:
                body["agent_type"] = agent_type
            return body

        require_orch = _hook(REQUIRE, role(), proj, tmp_root)
        route_orch = _hook(ROUTE, role(), proj, tmp_root)
        reader = _hook(ROUTE, role("reader-1", "file-reader"), proj, tmp_root)
        writer = _hook(ROUTE, role("writer-1", "file-writer"), proj, tmp_root)
        other = _hook(ROUTE, role("other-1", "Explore"), proj, tmp_root)
        Path(spec).write_text("# task\n", encoding="utf-8")
        require_present = _hook(REQUIRE, role(), proj, tmp_root)
        route_present = _hook(ROUTE, role(), proj, tmp_root)
        reader_d, reader_r = _decision(reader.stdout)
        writer_d, writer_r = _decision(writer.stdout)
        other_d, other_r = _decision(other.stdout)
        suite.check(
            "T7",
            "orchestrator may Write the run spec; other roles are denied",
            completed.returncode == 0
            and require_orch.returncode == 0
            and require_orch.stdout == ""
            and route_orch.returncode == 0
            and route_orch.stdout == ""
            and require_present.returncode == 0
            and require_present.stdout == ""
            and route_present.returncode == 0
            and route_present.stdout == ""
            and reader.returncode == 0
            and reader_d == "deny"
            and reader_r == READER_WRITE_DENY
            and writer.returncode == 0
            and writer_d == "deny"
            and writer_r == STATE_DENY
            and other.returncode == 0
            and other_d == "deny"
            and other_r == STATE_DENY,
            f"require={require_orch.returncode}/{require_orch.stdout!r}/{require_orch.stderr[:200]!r} "
            f"route={route_orch.returncode}/{route_orch.stdout!r} "
            f"reader={reader_d}/{reader_r} writer={writer_d}/{writer_r} other={other_d}/{other_r}",
        )
    finally:
        _rm(root)


def test_t8(suite: Suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="spec-loc-t8-")))
    try:
        home, proj, tmp_root, agent = _world(root)
        _completed, spec = _new(home, proj, tmp_root, agent, "task")
        path = Path(spec)
        path.write_text("# task\n", encoding="utf-8")
        touch = home / ".claude" / "hooks" / "x.py"
        ok = _run(home, proj, tmp_root, agent, [spec], FAKE_AGENT_MODE="ok")
        ok_payload = _json(ok.stdout)
        failed = _run(
            home, proj, tmp_root, agent, [spec],
            FAKE_AGENT_MODE="fail", FAKE_AGENT_TOUCH=str(touch),
        )
        failed_payload = _json(failed.stdout)
        touch.write_text("print(1)\n", encoding="utf-8")
        timed = _run(
            home, proj, tmp_root, agent, [spec],
            FAKE_AGENT_MODE="sleep", FAKE_AGENT_SLEEP="30", CURSOR_RUN_TIMEOUT="1",
        )
        timed_payload = _json(timed.stdout)
        result_path = Path(str(path).removesuffix(".md") + ".result.json")
        if result_path.exists():
            result_path.unlink()
        blocked = _run(
            home, proj, tmp_root, agent, [spec],
            FAKE_AGENT_MODE="chmod-dir", FAKE_AGENT_SPEC_DIR=str(path.parent),
        )
        blocked_payload = _json(blocked.stdout)
        path.parent.chmod(0o700)
        suite.check(
            "T8",
            "control checks on success, failure, timeout; report failure exits 5",
            ok.returncode == 0
            and ok_payload.get("status") == "ok"
            and ok_payload.get("control_changed") is False
            and ok_payload.get("run_dir") == str(path.parent.parent)
            and ok_payload.get("project") == str(proj)
            and failed.returncode == 3
            and failed_payload.get("status") == "failed"
            and failed_payload.get("control_changed") is True
            and str(touch) in failed_payload.get("changed_paths", [])
            and timed.returncode == 3
            and timed_payload.get("status") == "timeout"
            and timed_payload.get("control_changed") is False
            and blocked.returncode == 5
            and blocked_payload.get("status") == "ok"
            and blocked_payload.get("control_changed") is False
            and "report_error" in blocked_payload,
            f"ok={ok.returncode}/{ok_payload!r} fail={failed.returncode}/{failed_payload!r} "
            f"timeout={timed.returncode}/{timed_payload!r} "
            f"report={blocked.returncode}/{blocked_payload!r} stderr={blocked.stderr!r}",
        )
    finally:
        _rm(root)


def test_t9(suite: Suite):
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="spec-loc-t9-")))
    try:
        home, proj, tmp_root, agent = _world(root)
        home_dir = os.path.expanduser("~")
        commands = (
            "~/.claude/hooks/bin/cursor-run new x",
            f"{home_dir}/.claude/hooks/bin/cursor-run new x",
            "~/.claude/hooks/bin/cursor-run /tmp/proj/20260101-010101-000001/specs/x.md",
            f"{home_dir}/.claude/hooks/bin/cursor-run /tmp/proj/20260101-010101-000001/specs/x.md",
        )
        problems = []
        for command in commands:
            payload = {"tool_name": "Bash", "tool_input": {"command": command}}
            extra = {"CURSOR_RUN_PATH": str(CURSOR_RUN)}
            for script, label in ((REQUIRE, "require"), (ROUTE, "route")):
                completed = _hook(script, payload, proj, tmp_root, extra)
                decision, _reason = (None, None)
                if completed.stdout.strip():
                    try:
                        decision, _reason = _decision(completed.stdout)
                    except Exception:
                        decision = "unparsed"
                if completed.returncode != 0 or completed.stdout != "" or decision == "allow":
                    problems.append((label, command, completed.returncode, completed.stdout, completed.stderr[:200]))
        suite.check(
            "T9",
            "neither hook emits an allow decision for cursor-run",
            not problems,
            f"problems={problems!r}",
        )
    finally:
        _rm(root)


def main() -> int:
    suite = Suite()
    test_t1(suite)
    test_t2(suite)
    test_t3(suite)
    test_t4(suite)
    test_t5(suite)
    test_t6(suite)
    test_t7(suite)
    test_t8(suite)
    test_t9(suite)
    print(f"Tally: {suite.passed} passed, {suite.failed} failed")
    return 0 if suite.failed == 0 and suite.passed > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
