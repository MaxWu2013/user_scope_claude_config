#!/usr/bin/env python3
"""Phase 3: bin/safe-read validation, hostile repos, bounds, and filesystem effects.

Each case uses a fresh temp git repo as CLAUDE_PROJECT_DIR. Prints a table and
`Tally: N passed, M failed`. Exits non-zero on failure.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pty
import re
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SAFE_READ = ROOT / "bin" / "safe-read"
MAX_STDOUT = 200 * 1024
RSS_LIMIT = 100 * 1024 * 1024

INSPECT = (
    ["git-status"],
    ["git-diff"],
    ["git-show", "HEAD"],
    ["git-log", "-p"],
    ["git-blame", "hello.txt"],
)


def load_module():
    # No .py suffix, so importlib will not guess a loader on its own.
    loader = importlib.machinery.SourceFileLoader("safe_read_under_test", str(SAFE_READ))
    spec = importlib.util.spec_from_file_location(
        "safe_read_under_test", SAFE_READ, loader=loader
    )
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def git_env():
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("GIT_"):
            env.pop(key)
    env["GIT_AUTHOR_NAME"] = "Safe Read"
    env["GIT_AUTHOR_EMAIL"] = "safe-read@example.com"
    env["GIT_COMMITTER_NAME"] = "Safe Read"
    env["GIT_COMMITTER_EMAIL"] = "safe-read@example.com"
    return env


def run_git(repo, *args):
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=git_env(),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} -> {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed


def make_root(prefix):
    repo = Path(os.path.realpath(tempfile.mkdtemp(prefix=prefix)))
    run_git(repo, "init", "-q", "-b", "main")
    return repo


def make_repo(prefix):
    repo = make_root(prefix)
    run_git(repo, "config", "user.email", "safe-read@example.com")
    run_git(repo, "config", "user.name", "Safe Read")
    run_git(repo, "config", "commit.gpgsign", "false")
    run_git(repo, "config", "color.ui", "false")
    (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
    run_git(repo, "add", "hello.txt")
    run_git(repo, "commit", "-q", "-m", "first")
    (repo / "hello.txt").write_text("hello\nworld\n", encoding="utf-8")
    run_git(repo, "add", "hello.txt")
    run_git(repo, "commit", "-q", "-m", "second")
    (repo / "hello.txt").write_text("hello\nworld\ndirty\n", encoding="utf-8")
    return repo


def remove_tree(path):
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)


def invoke(mod, argv, cwd, project, env_extra=None):
    keys = {"CLAUDE_PROJECT_DIR"}
    if env_extra:
        keys.update(env_extra)
    saved_env = {key: os.environ.get(key) for key in keys}
    saved_cwd = os.getcwd()
    os.chdir(cwd)
    os.environ["CLAUDE_PROJECT_DIR"] = str(project)
    if env_extra:
        os.environ.update(env_extra)
    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = mod.main(list(argv))
    finally:
        os.chdir(saved_cwd)
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return code, stdout.getvalue(), stderr.getvalue()


def marker_names(repo):
    found = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames.sort()
        for name in filenames:
            if "MARKER" in name:
                found.append(str(Path(dirpath, name).relative_to(repo)))
    return sorted(found)


def snapshot(root):
    rows = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            st = path.lstat()
            if stat.S_ISLNK(st.st_mode):
                digest = hashlib.sha256(os.readlink(path).encode()).hexdigest()
            elif stat.S_ISREG(st.st_mode):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                digest = f"mode:{stat.S_IFMT(st.st_mode):o}"
            rows.append((rel, st.st_size, st.st_mtime_ns, digest))
    rows.sort()
    return tuple(rows)


def changed_paths(before, after):
    old = {row[0]: row for row in before}
    new = {row[0]: row for row in after}
    changed = []
    for key in sorted(set(old) | set(new)):
        if old.get(key) != new.get(key):
            changed.append(key)
    return changed


def rss_bytes(raw):
    # macOS reports ru_maxrss in bytes; Linux reports kilobytes.
    if sys.platform == "darwin":
        return raw
    return raw * 1024


def run_on_pty(argv, cwd, project, timeout=20):
    """Run safe-read with stdout on a tty so an inherited pager would fire."""
    master, slave = pty.openpty()
    env = git_env()
    env["CLAUDE_PROJECT_DIR"] = str(project)
    proc = subprocess.Popen(
        [sys.executable, str(SAFE_READ), *argv],
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)
    chunks = []
    try:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                proc.wait(timeout=5)
                raise AssertionError(f"timed out: {argv}")
            ready, _, _ = select.select([master], [], [], min(0.2, remaining))
            if ready:
                try:
                    data = os.read(master, 8192)
                except OSError:
                    data = b""
                if data:
                    chunks.append(data)
                    continue
            if proc.poll() is not None:
                while True:
                    ready, _, _ = select.select([master], [], [], 0)
                    if not ready:
                        break
                    try:
                        data = os.read(master, 8192)
                    except OSError:
                        break
                    if not data:
                        break
                    chunks.append(data)
                break
        code = proc.wait(timeout=5)
    finally:
        os.close(master)
    return code, b"".join(chunks).decode("utf-8", "replace")


MEASURER = r"""
import json, os, resource, subprocess, sys
repo, project, program, out_path, err_path = sys.argv[1:6]
argv = sys.argv[6:]
os.chdir(repo)
env = os.environ.copy()
env["CLAUDE_PROJECT_DIR"] = project
for key in list(env):
    if key.startswith("GIT_"):
        del env[key]
with open(out_path, "wb") as stdout, open(err_path, "wb") as stderr:
    proc = subprocess.run(
        [sys.executable, program, *argv],
        cwd=repo,
        env=env,
        stdout=stdout,
        stderr=stderr,
    )
rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
json.dump({"code": proc.returncode, "rss": rss}, sys.stdout)
"""


def run_bounded(repo, argv):
    work = Path(tempfile.mkdtemp(prefix="safe-read-rss-"))
    try:
        measurer = work / "measure.py"
        measurer.write_text(MEASURER, encoding="utf-8")
        out_path = work / "stdout"
        err_path = work / "stderr"
        started = time.monotonic()
        completed = subprocess.run(
            [
                sys.executable,
                str(measurer),
                str(repo),
                str(repo),
                str(SAFE_READ),
                str(out_path),
                str(err_path),
                *argv,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        elapsed = time.monotonic() - started
        if completed.returncode != 0:
            raise AssertionError(
                f"measurer exit {completed.returncode}: {completed.stderr.strip()}"
            )
        meta = json.loads(completed.stdout)
        return meta, out_path.read_bytes(), err_path.read_bytes(), elapsed
    finally:
        remove_tree(work)


class Suite:
    def __init__(self):
        self.n = 0
        self.passed = 0
        self.failed = 0

    def case(self, name, fn):
        self.n += 1
        try:
            fn()
        except Exception as exc:
            self.failed += 1
            detail = str(exc).replace("\n", " ")
            if len(detail) > 300:
                detail = detail[:300] + "..."
            print(f"{self.n:>4}. {name} | FAIL ({detail})")
        else:
            self.passed += 1
            print(f"{self.n:>4}. {name} | PASS")


def check(cond, detail):
    if not cond:
        raise AssertionError(detail)


def test_a(mod, suite):
    repo = make_root("safe-read-a-")
    outside = None
    calls = {"run": 0, "popen": 0}
    real_run = mod.run
    real_popen = mod.subprocess.Popen

    def track_run(plan):
        calls["run"] += 1
        return mod.Result(99, "launched", "launched")

    def track_popen(*args, **kwargs):
        calls["popen"] += 1
        raise AssertionError("subprocess.Popen was called")

    try:
        (repo / "link-hosts").symlink_to("/etc/hosts")
        outside = Path(os.path.realpath(tempfile.mkdtemp(prefix="safe-read-out-")))
        vault = os.path.realpath(os.path.expanduser(mod.VAULT_ROOT))
        check(
            not (str(outside) == str(repo) or str(outside).startswith(str(repo) + os.sep)),
            "outside cwd is inside the repo",
        )
        check(
            not (str(outside) == vault or str(outside).startswith(vault + os.sep)),
            "outside cwd is inside the vault",
        )
        mod.run = track_run
        mod.subprocess.Popen = track_popen
        cases = [
            ("A unknown subcommand", ["not-a-command"], repo),
            ("A --pre=sh", ["git-diff", "--pre=sh"], repo),
            ("A --output=x", ["git-diff", "--output=x"], repo),
            ("A -c x=y", ["git-status", "-c", "x=y"], repo),
            ("A REV -p", ["git-show", "-p"], repo),
            ("A REV with semicolon", ["git-show", "HEAD;touch"], repo),
            ("A path ../../etc/passwd", ["ls", "../../etc/passwd"], repo),
            ("A symlink to /etc/hosts", ["wc", "link-hosts"], repo),
            ("A NUL byte", ["rg", "bad\0pat"], repo),
            ("A --git-dir=..", ["git-status", "--git-dir=.."], repo),
            ("A -C /", ["git-log", "-C", "/"], repo),
            ("A glob starting with -", ["rg", "pat", "-g", "-evil"], repo),
            ("A -U999", ["git-diff", "-U999"], repo),
            ("A -n 0", ["git-log", "-n", "0"], repo),
            ("A cwd outside roots", ["git-status"], outside),
            ("A --ext-diff", ["git-diff", "--ext-diff"], repo),
            ("A --textconv", ["git-diff", "--textconv"], repo),
            ("A --work-tree=/", ["git-status", "--work-tree=/"], repo),
        ]
        check(len(cases) >= 15, "need at least 15 bad inputs")

        def run_one(argv, cwd):
            calls["run"] = 0
            calls["popen"] = 0
            code, out, err = invoke(mod, argv, cwd, repo)
            check(code == 2, f"exit {code} stderr={err!r} stdout={out!r}")
            check(calls["run"] == 0 and calls["popen"] == 0, f"launched run={calls['run']} popen={calls['popen']}")
            check(out == "", f"stdout={out!r}")
            check(err.startswith("safe-read: rejected:"), f"stderr={err!r}")

        for name, argv, cwd in cases:
            suite.case(name, lambda argv=argv, cwd=cwd: run_one(argv, cwd))
    finally:
        mod.run = real_run
        mod.subprocess.Popen = real_popen
        remove_tree(repo)
        remove_tree(outside)


def test_b(mod, suite):
    repo = make_repo("safe-read-b-")
    try:
        def expect(argv, pred, label):
            code, out, err = invoke(mod, argv, repo, repo)
            check(code == 0, f"exit {code} stderr={err!r} stdout={out[:200]!r}")
            check(pred(out), f"{label} stdout={out[:300]!r} stderr={err[:200]!r}")

        suite.case("B git-status", lambda: expect(
            ["git-status"], lambda out: "hello.txt" in out, "status"))
        suite.case("B git-diff", lambda: expect(
            ["git-diff"], lambda out: "dirty" in out, "diff"))
        suite.case("B git-diff --stat", lambda: expect(
            ["git-diff", "--stat"], lambda out: "hello.txt" in out, "diff --stat"))
        suite.case("B git-show HEAD", lambda: expect(
            ["git-show", "HEAD"], lambda out: "second" in out and "world" in out, "show"))
        suite.case("B git-show HEAD --name-only", lambda: expect(
            ["git-show", "HEAD", "--name-only"], lambda out: "hello.txt" in out, "show --name-only"))
        suite.case("B git-log", lambda: expect(
            ["git-log"], lambda out: "first" in out and "second" in out, "log"))
        suite.case("B git-log -n 1 --oneline", lambda: expect(
            ["git-log", "-n", "1", "--oneline"],
            lambda out: "second" in out and "first" not in out,
            "log -n 1",
        ))
        suite.case("B git-blame hello.txt", lambda: expect(
            ["git-blame", "hello.txt"], lambda out: "hello" in out and "world" in out, "blame"))
        suite.case("B git-blame -L 1,1", lambda: expect(
            ["git-blame", "-L", "1,1", "hello.txt"], lambda out: "hello" in out, "blame -L"))
        suite.case("B rg dirty", lambda: expect(
            ["rg", "dirty", "hello.txt"], lambda out: "dirty" in out, "rg"))
        suite.case("B rg -n -i", lambda: expect(
            ["rg", "-n", "-i", "Dirty", "hello.txt"], lambda out: "dirty" in out, "rg -n -i"))
        suite.case("B ls hello.txt", lambda: expect(
            ["ls", "hello.txt"], lambda out: "hello.txt" in out, "ls"))
        suite.case("B wc hello.txt", lambda: expect(
            ["wc", "hello.txt"], lambda out: out.split()[:1] == ["3"], "wc"))
        suite.case("B help", lambda: expect(
            ["help"], lambda out: "git-status" in out and "git-blame" in out and "wc PATH" in out, "help"))

        def rg_help():
            code, out, err = invoke(mod, ["help"], repo, repo)
            check(code == 0 and err == "", f"help {code} {err!r}")
            row = "rg [-e PATTERN]\u2026 | PATTERN [PATH\u2026]"
            check(row in out, f"rg help row missing: {out!r}")

        suite.case("B help rg -e", rg_help)

        def rg_union():
            def lines(argv):
                code, out, err = invoke(mod, argv, repo, repo)
                check(code == 0 and err == "", f"{argv} {code} {err!r} {out!r}")
                return set(out.splitlines())

            combined = lines(["rg", "-e", "hello", "-e", "dirty", "hello.txt"])
            separate = lines(["rg", "hello", "hello.txt"]) | lines(["rg", "dirty", "hello.txt"])
            check(combined == separate, f"combined={combined!r} separate={separate!r}")
            check("world" not in combined, combined)

        suite.case("B rg -e union", rg_union)

        def rg_e_flags():
            (repo / "marks.txt").write_text("a.b\naxb\n-n\n", encoding="utf-8")
            (repo / "skip.md").write_text("dirty\n", encoding="utf-8")
            code, out, err = invoke(
                mod, ["rg", "-F", "-e", "a.b", "-e", "-n", "marks.txt"], repo, repo,
            )
            check(code == 0 and err == "", f"-F {code} {err!r} {out!r}")
            check(set(out.splitlines()) == {"a.b", "-n"}, out)
            code, out, err = invoke(
                mod, ["rg", "-l", "-i", "-g", "*.txt", "-e", "DIRTY"], repo, repo,
            )
            check(code == 0 and err == "", f"-l -i -g {code} {err!r} {out!r}")
            check("hello.txt" in out, out)
            check("skip.md" not in out and "marks.txt" not in out, out)

        suite.case("B rg -e -l -i -F -g", rg_e_flags)

        def rg_e_limits():
            ten = ["rg"]
            for i in range(9):
                ten.extend(["-e", f"no-such-{i}"])
            ten.extend(["-e", "dirty", "hello.txt"])
            code, out, err = invoke(mod, ten, repo, repo)
            check(code == 0 and err == "", f"10 patterns {code} {err!r} {out!r}")
            check(out == "dirty\n", out)
            eleven = ["rg"]
            for i in range(11):
                eleven.extend(["-e", f"p{i}"])
            eleven.append("hello.txt")
            code, out, err = invoke(mod, eleven, repo, repo)
            check(code == 2 and out == "", f"11 patterns {code} {out!r} {err!r}")
            check("too many patterns (max 10)" in err, err)
            code, out, err = invoke(mod, ["rg", "-e"], repo, repo)
            check(code == 2 and out == "", f"bare -e {code} {out!r} {err!r}")
            check("missing value for -e" in err, err)
            code, out, err = invoke(mod, ["rg", "-e", ""], repo, repo)
            check(code == 2 and out == "", f"empty -e {code} {out!r} {err!r}")
            check("empty pattern" in err, err)

        suite.case("B rg -e limits", rg_e_limits)

        def rg_positional_unchanged():
            code, out, err = invoke(mod, ["rg", "dirty", "hello.txt"], repo, repo)
            exe = shutil.which("rg", path=mod.FIXED_PATH)
            direct = subprocess.run(
                [exe, "--no-config", "dirty", str(repo / "hello.txt")],
                cwd=repo,
                env=mod._env(),
                capture_output=True,
                check=False,
            )
            check(code == direct.returncode, f"exit {code} != {direct.returncode}")
            check(out == direct.stdout.decode(), f"stdout {out!r} != {direct.stdout!r}")
            check(err == direct.stderr.decode(), f"stderr {err!r} != {direct.stderr!r}")
            check(out == "dirty\n", out)

        suite.case("B rg positional unchanged", rg_positional_unchanged)
    finally:
        remove_tree(repo)


def hostile_outputs(mod, repo, commands):
    problems = []
    for argv in commands:
        code, out, err = invoke(mod, argv, repo, repo)
        blob = out + err
        if code != 0:
            problems.append(f"{' '.join(argv)} exit {code} stderr={err.strip()[:160]!r}")
        if "MARKER" in blob:
            problems.append(f"{argv[0]} printed marker text")
    found = marker_names(repo)
    if found:
        problems.append(f"marker files: {found}")
    if problems:
        raise AssertionError("; ".join(problems))


def test_c(mod, suite):
    def with_repo(configure, commands):
        repo = make_repo("safe-read-c-")
        try:
            configure(repo)
            hostile_outputs(mod, repo, commands)
        finally:
            remove_tree(repo)

    suite.case(
        "C fsmonitor",
        lambda: with_repo(
            lambda repo: run_git(repo, "config", "core.fsmonitor", "touch MARKER_FSM"),
            INSPECT,
        ),
    )
    suite.case(
        "C diff.external",
        lambda: with_repo(
            lambda repo: run_git(repo, "config", "diff.external", "sh -c 'touch MARKER_EXT'"),
            INSPECT,
        ),
    )

    def textconv(repo):
        (repo / ".gitattributes").write_text("* diff=evil\n", encoding="utf-8")
        run_git(repo, "config", "diff.evil.textconv", "sh -c 'touch MARKER_TXT'")

    suite.case("C textconv", lambda: with_repo(textconv, INSPECT))

    def hooks(repo):
        hook_dir = repo / "hostile-hooks"
        hook_dir.mkdir()
        hook = hook_dir / "post-index-change"
        hook.write_text("#!/bin/sh\ntouch MARKER_HOOK\n", encoding="utf-8")
        hook.chmod(0o755)
        run_git(repo, "config", "core.hooksPath", str(hook_dir))

    suite.case("C hooksPath", lambda: with_repo(hooks, INSPECT))

    def pager(repo):
        run_git(repo, "config", "core.pager", "sh -c 'touch MARKER_PAGER'")
        problems = []
        for argv in INSPECT:
            code, out = run_on_pty(argv, repo, repo)
            if code != 0:
                problems.append(f"{' '.join(argv)} exit {code} out={out[:160]!r}")
            if "MARKER" in out:
                problems.append(f"{argv[0]} printed marker text")
        found = marker_names(repo)
        if found:
            problems.append(f"marker files: {found}")
        if problems:
            raise AssertionError("; ".join(problems))

    suite.case("C pager", lambda: with_repo(pager, ()))

    def filters(repo):
        run_git(repo, "config", "filter.x.clean", "touch MARKER_FILTER")
        problems = []
        for argv in INSPECT:
            code, out, err = invoke(mod, argv, repo, repo)
            blob = out + err
            if code != 3:
                problems.append(f"{' '.join(argv)} exit {code} stderr={err.strip()[:160]!r}")
            if "repo defines filters" not in err:
                problems.append(f"{argv[0]} missing refusal")
            if "MARKER" in blob:
                problems.append(f"{argv[0]} printed marker text")
        found = marker_names(repo)
        if found:
            problems.append(f"marker files: {found}")
        if problems:
            raise AssertionError("; ".join(problems))

    suite.case("C filter.x.clean", lambda: with_repo(filters, ()))


def test_d(mod, suite):
    repo = make_repo("safe-read-d-")
    try:
        fsm = repo / "fsm.sh"
        ext = repo / "ext.sh"
        pager = repo / "pager.sh"
        fsm.write_text(f"#!/bin/sh\ntouch {repo / 'MARKER_D_FSM'}\n", encoding="utf-8")
        ext.write_text(f"#!/bin/sh\ntouch {repo / 'MARKER_D_EXT'}\nexit 0\n", encoding="utf-8")
        pager.write_text(f"#!/bin/sh\ntouch {repo / 'MARKER_D_PAGER'}\ncat\n", encoding="utf-8")
        for script in (fsm, ext, pager):
            script.chmod(0o755)
        env_extra = {
            "GIT_DIR": "/nonexistent",
            "GIT_CONFIG_PARAMETERS": f"'core.fsmonitor'='{fsm}'",
            "GIT_EXTERNAL_DIFF": str(ext),
            "GIT_PAGER": str(pager),
        }

        def run(argv, pred):
            code, out, err = invoke(mod, argv, repo, repo, env_extra=env_extra)
            check(code == 0, f"exit {code} stderr={err!r} stdout={out[:200]!r}")
            check(pred(out), f"stdout={out[:300]!r}")
            check("MARKER" not in out + err, "marker text in output")
            found = [name for name in marker_names(repo) if name.startswith("MARKER_D")]
            check(not found, f"marker files: {found}")

        suite.case("D env overrides git-status", lambda: run(["git-status"], lambda out: "hello.txt" in out))
        suite.case("D env overrides git-diff", lambda: run(["git-diff"], lambda out: "dirty" in out))
    finally:
        remove_tree(repo)


def test_e(mod, suite):
    repo = make_repo("safe-read-e-")
    try:
        commands = [
            ["git-status"],
            ["git-diff"],
            ["git-show", "HEAD"],
            ["git-log"],
            ["git-blame", "hello.txt"],
            ["rg", "dirty", "hello.txt"],
            ["ls", "hello.txt"],
            ["wc", "hello.txt"],
            ["help"],
        ]

        def once(argv):
            before = snapshot(repo)
            code, out, err = invoke(mod, argv, repo, repo)
            after = snapshot(repo)
            check(code == 0, f"exit {code} stderr={err!r}")
            delta = changed_paths(before, after)
            check(not delta, f"filesystem changed: {delta}")

        for argv in commands:
            label = "E " + " ".join(argv)
            suite.case(label, lambda argv=argv: once(argv))
    finally:
        remove_tree(repo)


def test_f(mod, suite):
    del mod
    repo = make_root("safe-read-f-")
    try:
        big = repo / "big.txt"
        big.write_bytes(b"A" * (5 * 1024 * 1024) + b"\n")

        def long_line():
            meta, stdout, stderr, elapsed = run_bounded(repo, ["rg", "-F", "A", "big.txt"])
            check(len(stdout) <= MAX_STDOUT, f"stdout {len(stdout)} bytes")
            check(len(stderr) <= 20 * 1024, f"stderr {len(stderr)} bytes")
            check(b"[line truncated]" in stdout, f"line not cut: {stdout[:120]!r}")
            check(b"A" * 3000 not in stdout, "line was not cut")
            check(b"[safe-read: output truncated at " in stdout, f"no notice: {stdout[-80]!r}")
            check(elapsed < 5, f"elapsed {elapsed:.2f}s")
            check(meta["code"] == 0, f"exit {meta['code']}")
            rss = rss_bytes(meta["rss"])
            check(rss < RSS_LIMIT, f"rss {rss} bytes")

        suite.case("F 5MB line bounded", long_line)

        many = repo / "many.txt"
        with many.open("w", encoding="utf-8") as handle:
            for _ in range(100_000):
                handle.write("match\n")

        def many_lines():
            meta, stdout, stderr, elapsed = run_bounded(repo, ["rg", "match", "many.txt"])
            text = stdout.decode("utf-8", "replace")
            notice = "[safe-read: output truncated at 2000 lines]"
            check(notice in text, f"no line notice: {text[-120]!r}")
            head, _, tail = text.partition(notice)
            check(head.count("\n") <= 2000, f"content lines {head.count(chr(10))}")
            check(tail.strip() == "", f"trailing {tail!r}")
            check(len(stdout) <= MAX_STDOUT, f"stdout {len(stdout)} bytes")
            check(len(stderr) <= 20 * 1024, f"stderr {len(stderr)} bytes")
            check(elapsed < 5, f"elapsed {elapsed:.2f}s")
            check(meta["code"] == 0, f"exit {meta['code']}")
            rss = rss_bytes(meta["rss"])
            check(rss < RSS_LIMIT, f"rss {rss} bytes")

        suite.case("F 100k lines bounded", many_lines)
    finally:
        remove_tree(repo)


def test_g(mod, suite):
    repo = make_root("safe-read-g-")
    real_timeout = mod.TIMEOUT
    real_popen = mod.subprocess.Popen

    def popen(argv, **kwargs):
        # config is the filter guard. rev-parse (toplevel) and ls-files are the
        # LFS-path guard and only run when that guard found an allowlisted
        # driver (this machine's HOME has one). The inspection command is what
        # must time out.
        if isinstance(argv, (list, tuple)) and (
            "config" in argv or "rev-parse" in argv or "ls-files" in argv
        ):
            return real_popen(argv, **kwargs)
        return real_popen(["/bin/sleep", "10"], **kwargs)

    try:
        mod.TIMEOUT = 1
        mod.subprocess.Popen = popen

        def timed():
            started = time.monotonic()
            code, out, err = invoke(mod, ["git-status"], repo, repo)
            elapsed = time.monotonic() - started
            check(code == 4, f"exit {code} stderr={err!r} stdout={out!r}")
            check("safe-read: timeout" in err, f"stderr={err!r}")
            check(elapsed < 5, f"elapsed {elapsed:.2f}s")

        suite.case("G timeout", timed)
    finally:
        mod.TIMEOUT = real_timeout
        mod.subprocess.Popen = real_popen
        remove_tree(repo)


def assert_git_refused(mod, repo, home, reason):
    problems = []
    for argv in INSPECT:
        code, out, err = invoke(mod, argv, repo, repo, env_extra={"HOME": str(home)})
        if code != 3:
            problems.append(f"{' '.join(argv)} exit {code} stderr={err.strip()[:180]!r}")
        expected = f"safe-read: refused: {reason}\n"
        if err != expected:
            problems.append(f"{argv[0]} stderr={err!r}")
        if out:
            problems.append(f"{argv[0]} stdout={out[:80]!r}")
        if "MARKER" in out + err:
            problems.append(f"{argv[0]} printed marker text")
    found = marker_names(repo)
    if found:
        problems.append(f"marker files: {found}")
    if (repo / "MARKER").exists():
        problems.append("MARKER exists")
    if problems:
        raise AssertionError("; ".join(problems))


def test_h(mod, suite):
    def h1():
        home = Path(os.path.realpath(tempfile.mkdtemp(prefix="safe-read-h1-home-")))
        repo = None
        try:
            (home / ".gitconfig").write_text(
                "[filter \"x\"]\n\tclean = sh -c 'touch MARKER'\n",
                encoding="utf-8",
            )
            repo = make_repo("safe-read-h1-")
            assert_git_refused(mod, repo, home, "repo defines filters")
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("H1 global filter", h1)

    def h2():
        home = Path(os.path.realpath(tempfile.mkdtemp(prefix="safe-read-h2-home-")))
        repo = None
        try:
            xdg = home / ".config" / "git"
            xdg.mkdir(parents=True)
            (xdg / "config").write_text(
                "[filter \"x\"]\n\tclean = sh -c 'touch MARKER'\n",
                encoding="utf-8",
            )
            repo = make_repo("safe-read-h2-")
            assert_git_refused(mod, repo, home, "repo defines filters")
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("H2 XDG global filter", h2)

    def h3():
        home = Path(os.path.realpath(tempfile.mkdtemp(prefix="safe-read-h3-home-")))
        repo = None
        try:
            repo = make_repo("safe-read-h3-")
            (repo / "evil.cfg").write_text(
                "[filter \"x\"]\n\tsmudge = sh -c 'touch MARKER'\n",
                encoding="utf-8",
            )
            run_git(repo, "config", "include.path", "../evil.cfg")
            assert_git_refused(mod, repo, home, "repo defines filters")
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("H3 include.path", h3)

    def h4():
        home = Path(os.path.realpath(tempfile.mkdtemp(prefix="safe-read-h4-home-")))
        repo = None
        try:
            repo = make_repo("safe-read-h4-")
            evil = home / "evil-process.cfg"
            evil.write_text(
                "[filter \"x\"]\n\tprocess = sh -c 'touch MARKER'\n",
                encoding="utf-8",
            )
            (home / ".gitconfig").write_text(
                f"[includeIf \"gitdir:{repo}/\"]\n\tpath = {evil}\n",
                encoding="utf-8",
            )
            assert_git_refused(mod, repo, home, "repo defines filters")
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("H4 includeIf", h4)

    def h5():
        home = Path(os.path.realpath(tempfile.mkdtemp(prefix="safe-read-h5-home-")))
        repo = None
        try:
            repo = make_repo("safe-read-h5-")
            injected = home / "injected.gitconfig"
            injected.write_text(
                "[filter \"x\"]\n\tclean = sh -c 'touch MARKER'\n",
                encoding="utf-8",
            )
            env_extra = {
                "HOME": str(home),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "filter.x.clean",
                "GIT_CONFIG_VALUE_0": "sh -c 'touch MARKER'",
                "GIT_CONFIG_GLOBAL": str(injected),
            }
            problems = []
            for argv in INSPECT:
                code, out, err = invoke(mod, argv, repo, repo, env_extra=env_extra)
                if code != 0:
                    problems.append(f"{' '.join(argv)} exit {code} stderr={err.strip()[:180]!r}")
                if "MARKER" in out + err:
                    problems.append(f"{argv[0]} printed marker text")
                if "refused" in err:
                    problems.append(f"{argv[0]} refused: {err.strip()[:180]!r}")
            code, out, err = invoke(mod, ["git-status"], repo, repo, env_extra=env_extra)
            if "hello.txt" not in out:
                problems.append(f"status stdout={out[:200]!r} stderr={err[:200]!r}")
            found = marker_names(repo)
            if found:
                problems.append(f"marker files: {found}")
            if problems:
                raise AssertionError("; ".join(problems))
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("H5 env override ignored", h5)

    def h6():
        home = Path(os.path.realpath(tempfile.mkdtemp(prefix="safe-read-h6-home-")))
        repo = None
        real = mod._bounded_run
        seen = []

        def wrapped(argv, cwd, env):
            seen.append(list(argv))
            return real(argv, cwd, env)

        mod._bounded_run = wrapped
        try:
            (home / ".gitconfig").write_text("[\n", encoding="utf-8")
            repo = make_repo("safe-read-h6-")
            problems = []
            inspection = {"status", "diff", "show", "log", "blame"}
            for argv in INSPECT:
                seen.clear()
                code, out, err = invoke(mod, argv, repo, repo, env_extra={"HOME": str(home)})
                if code != 3:
                    problems.append(f"{' '.join(argv)} exit {code} stderr={err!r}")
                if err != "safe-read: refused: could not read git filter config\n":
                    problems.append(f"{argv[0]} stderr={err!r}")
                if out:
                    problems.append(f"{argv[0]} stdout={out[:80]!r}")
                if not seen:
                    problems.append(f"{argv[0]} did not query config")
                for cmd in seen:
                    if not inspection.isdisjoint(cmd):
                        problems.append(f"{argv[0]} inspection ran: {cmd}")
            if problems:
                raise AssertionError("; ".join(problems))
        finally:
            mod._bounded_run = real
            remove_tree(repo)
            remove_tree(home)

    suite.case("H6 config read failure", h6)

    def h7():
        home = Path(os.path.realpath(tempfile.mkdtemp(prefix="safe-read-h7-home-")))
        repo = None
        pc_root = None
        try:
            repo = Path(os.path.realpath(tempfile.mkdtemp(prefix="safe-read-h7-")))
            setup = git_env()
            setup["HOME"] = str(home)
            setup["GIT_CONFIG_NOSYSTEM"] = "1"

            def git_at(*args):
                completed = subprocess.run(
                    ["git", *args],
                    cwd=repo,
                    env=setup,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if completed.returncode != 0:
                    raise AssertionError(
                        f"git {' '.join(args)} -> {completed.returncode}: {completed.stderr.strip()}"
                    )

            git_at("init", "-q", "-b", "main")
            (repo / ".gitattributes").write_text(
                "*.bin filter=lfs diff=lfs merge=lfs -text\n",
                encoding="utf-8",
            )
            (repo / "a.bin").write_text("v1\n", encoding="utf-8")
            git_at("add", ".gitattributes", "a.bin")
            git_at("commit", "-q", "-m", "first")
            (repo / "a.bin").write_text("v2\n", encoding="utf-8")
            (home / ".gitconfig").write_text(
                "[filter \"lfs\"]\n"
                "\tclean = git-lfs clean -- %f\n"
                "\tsmudge = git-lfs smudge -- %f\n"
                "\tprocess = git-lfs filter-process\n"
                "\trequired = true\n",
                encoding="utf-8",
            )
            pc_root = Path(os.path.realpath(tempfile.mkdtemp(prefix="safe-read-h7-pc-")))
            pc = pc_root / "repo"
            shutil.copytree(repo, pc, symlinks=True)
            check(not (pc / ".git" / "lfs").exists(), "positive-control repo already had .git/lfs")
            pc_env = git_env()
            pc_env["HOME"] = str(home)
            positive = subprocess.run(
                ["git", "status"],
                cwd=pc,
                env=pc_env,
                text=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
            created = (pc / ".git" / "lfs").is_dir()
            check(
                positive.returncode == 0 and created,
                "H7 positive control: plain git status "
                f"rc={positive.returncode} created={created} "
                f"stderr={positive.stderr!r} stdout={positive.stdout!r}",
            )
            objects = [p for p in (pc / ".git" / "lfs").rglob("*") if p.is_file()]
            print(
                f"      H7 positive-control: plain git status created .git/lfs "
                f"({len(objects)} files)"
            )
            # a.bin is filter=lfs and the driver is neutralized, so worktree
            # comparisons are unresolved and blame is refused. show/log still
            # print the raw blob stored before the LFS config existed.
            commands = [
                ("keep", ["git-status"], "a.bin"),
                ("diff", ["git-diff"], "a.bin"),
                ("keep", ["git-show", "HEAD"], "v1"),
                ("keep", ["git-log", "-p"], "v1"),
                ("blame", ["git-blame", "a.bin"], "a.bin"),
            ]
            env_extra = {"HOME": str(home)}
            for kind, argv, needle in commands:
                before = snapshot(repo)
                code, out, err = invoke(mod, argv, repo, repo, env_extra=env_extra)
                after = snapshot(repo)
                if kind == "blame":
                    check(code == 3, f"blame exit {code} stderr={err!r} stdout={out!r}")
                    check(out == "", f"blame stdout={out!r}")
                    check(
                        err == "safe-read: refused: a.bin is LFS-tracked; "
                        "blame against the worktree is unreliable with LFS filters disabled\n",
                        f"blame stderr={err!r}",
                    )
                elif kind == "diff":
                    check(code == 0, f"diff exit {code} stderr={err!r} stdout={out[:300]!r}")
                    body = out.split("[safe-read:", 1)[0]
                    check("diff --git a/a.bin" not in body, f"diff showed a.bin hunk: {out[:400]!r}")
                    check("v2" not in body, f"diff showed worktree bytes: {out[:400]!r}")
                    check("unresolved:" in out and "a.bin" in out, f"diff stdout={out[:400]!r}")
                else:
                    check(code == 0, f"{' '.join(argv)} exit {code} stderr={err!r} stdout={out[:200]!r}")
                    check(needle in out, f"{' '.join(argv)} stdout={out[:300]!r} stderr={err[:200]!r}")
                delta = changed_paths(before, after)
                check(not delta, f"{' '.join(argv)} filesystem changed: {delta}")
                check(not (repo / ".git" / "lfs").exists(), f"{argv[0]} created .git/lfs")
        finally:
            remove_tree(repo)
            remove_tree(home)
            remove_tree(pc_root)

    suite.case("H7 LFS allowlist", h7)

    def h8():
        home = Path(os.path.realpath(tempfile.mkdtemp(prefix="safe-read-h8-home-")))
        repo = None
        try:
            (home / ".gitconfig").write_text(
                "[filter \"lfs\"]\n"
                "\tclean = \"git-lfs clean -- %f; touch MARKER\"\n",
                encoding="utf-8",
            )
            repo = make_repo("safe-read-h8-")
            assert_git_refused(mod, repo, home, "repo defines filters")
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("H8 LFS lookalike", h8)

    def h9():
        code, out, err = invoke(mod, ["git-status"], ROOT, ROOT)
        check(code == 0, f"exit {code} stderr={err!r} stdout={out[:300]!r}")
        check("refused" not in err, err)

    suite.case("H9 real HOME git-status", h9)


LFS_CONFIG = (
    "[filter \"lfs\"]\n"
    "\tclean = git-lfs clean -- %f\n"
    "\tsmudge = git-lfs smudge -- %f\n"
    "\tprocess = git-lfs filter-process\n"
    "\trequired = true\n"
)
UNRESOLVED = (
    "[safe-read: unresolved: LFS-tracked paths are excluded; "
    "with LFS filters disabled their worktree state cannot be compared reliably: "
)
POINTER_NOTE = (
    "[safe-read: note: LFS-tracked paths are shown as LFS pointer text, not file content: "
)


def porcelain_paths(text):
    names = []
    for line in text.splitlines():
        if len(line) >= 4 and line[2] == " ":
            names.append(line[3:])
    return names


def note_body(out):
    return out.split("[safe-read:", 1)[0]


@contextlib.contextmanager
def lfs_trap(mod):
    """Put a git-lfs wrapper first on safe-read's fixed PATH."""
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="safe-read-lfs-trap-")))
    marker = root / "invoked"
    bindir = root / "bin"
    bindir.mkdir()
    script = bindir / "git-lfs"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$0 $*\" >> '{marker}'\n"
        "exit 99\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    saved = mod.FIXED_PATH
    mod.FIXED_PATH = str(bindir) + os.pathsep + saved
    try:
        yield marker
    finally:
        mod.FIXED_PATH = saved
        remove_tree(root)


def make_lfs_repo(prefix):
    home = Path(os.path.realpath(tempfile.mkdtemp(prefix=prefix + "home-")))
    repo = Path(os.path.realpath(tempfile.mkdtemp(prefix=prefix)))
    (home / ".gitconfig").write_text(LFS_CONFIG, encoding="utf-8")
    env = git_env()
    env["HOME"] = str(home)
    env["GIT_CONFIG_NOSYSTEM"] = "1"

    def git(*args):
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} -> {completed.returncode}: {completed.stderr.strip()}"
            )
        return completed

    git("init", "-q", "-b", "main")
    git("config", "user.email", "safe-read@example.com")
    git("config", "user.name", "Safe Read")
    git("config", "commit.gpgsign", "false")
    git("config", "color.ui", "false")
    (repo / ".gitattributes").write_text(
        "*.bin filter=lfs diff=lfs merge=lfs -text\n",
        encoding="utf-8",
    )
    (repo / "a.bin").write_bytes(b"alpha\n")
    (repo / "b.bin").write_bytes(b"beta\n")
    (repo / "src.txt").write_text("source\n", encoding="utf-8")
    git("add", ".gitattributes", "a.bin", "b.bin", "src.txt")
    git("commit", "-q", "-m", "first")
    shown = git("show", ":a.bin")
    check(
        shown.stdout.startswith("version https://git-lfs.github.com/spec/v1\n"),
        f"index is not an LFS pointer: {shown.stdout!r}",
    )
    return home, repo, env


def git_in(repo, env, *args):
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} -> {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed


def make_lfs_subdir_repo(prefix, extra_bins=()):
    """LFS files at the worktree root and under sub/, plus ordinary text."""
    home = Path(os.path.realpath(tempfile.mkdtemp(prefix=prefix + "home-")))
    repo = Path(os.path.realpath(tempfile.mkdtemp(prefix=prefix)))
    (home / ".gitconfig").write_text(LFS_CONFIG, encoding="utf-8")
    env = git_env()
    env["HOME"] = str(home)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    git_in(repo, env, "init", "-q", "-b", "main")
    git_in(repo, env, "config", "user.email", "safe-read@example.com")
    git_in(repo, env, "config", "user.name", "Safe Read")
    git_in(repo, env, "config", "commit.gpgsign", "false")
    git_in(repo, env, "config", "color.ui", "false")
    (repo / ".gitattributes").write_text(
        "*.bin filter=lfs diff=lfs merge=lfs -text\n",
        encoding="utf-8",
    )
    (repo / "top.bin").write_bytes(b"alpha\n")
    (repo / "other.txt").write_text("other\n", encoding="utf-8")
    (repo / "sub").mkdir()
    (repo / "sub" / "in.bin").write_bytes(b"inside\n")
    (repo / "sub" / "src.txt").write_text("source\n", encoding="utf-8")
    for name in extra_bins:
        (repo / name).write_bytes(b"kept\n")
    paths = [".gitattributes", "top.bin", "other.txt", "sub/in.bin", "sub/src.txt", *extra_bins]
    git_in(repo, env, "add", *paths)
    git_in(repo, env, "commit", "-q", "-m", "first")
    shown = git_in(repo, env, "show", ":top.bin")
    check(
        shown.stdout.startswith("version https://git-lfs.github.com/spec/v1\n"),
        f"index is not an LFS pointer: {shown.stdout!r}",
    )
    return home, repo, env


def test_l(mod, suite):
    """Phase 7b: LFS worktree comparisons are unresolved; history stays pointer text."""

    def quiet_invoke(repo, home, marker, argv):
        if marker.exists():
            marker.unlink()
        before = snapshot(repo)
        code, out, err = invoke(mod, argv, repo, repo, env_extra={"HOME": str(home)})
        after = snapshot(repo)
        delta = changed_paths(before, after)
        invoked = marker.read_text(encoding="utf-8") if marker.exists() else ""
        check(not delta, f"{' '.join(argv)} filesystem changed: {delta}")
        check(invoked == "", f"{' '.join(argv)} started git-lfs: {invoked!r}")
        return code, out, err

    def l_parsers():
        stat_plan = mod.Plan("git-show", "/", ("--stat", "HEAD"))
        found = mod._detect_paths(
            " b.bin | 4 ++--\n 1 file changed, 2 insertions(+), 2 deletions(-)\n",
            stat_plan,
        )
        check(found == {"b.bin"}, repr(found))
        check(mod._detect_paths(" a.bin => c.bin | 1 +\n", stat_plan) is None, "rename stat")
        padded = mod.Plan("git-show", "/repo/sub", ("--stat", "HEAD"))
        found = mod._detect_paths(
            " sub/in.bin | 4 ++--\n top.bin    | 4 ++--\n"
            " 2 files changed, 4 insertions(+), 4 deletions(-)\n",
            padded,
        )
        check(found == {"sub/in.bin", "top.bin"}, repr(found))
        rooted = mod.Plan("git-diff", "/repo/sub", ("HEAD~1..HEAD",))
        note = mod._history_note_paths(
            "diff --git a/sub/in.bin b/sub/in.bin\ndiff --git a/top.bin b/top.bin\n",
            rooted,
            ("sub/in.bin", "top.bin"),
            "/repo",
        )
        check(note == ["sub/in.bin", "top.bin"], repr(note))
        missed = mod._history_note_paths(
            "diff --git a/in.bin b/in.bin\n",
            rooted,
            ("sub/in.bin", "top.bin"),
            "/repo",
        )
        check(missed == [], repr(missed))
        relative = mod.Plan("git-diff", "/repo/sub", ("--relative", "HEAD~1..HEAD"))
        note = mod._history_note_paths(
            "diff --git a/in.bin b/in.bin\n",
            relative,
            ("sub/in.bin", "top.bin"),
            "/repo",
        )
        check(note == ["sub/in.bin", "top.bin"], repr(note))
        relative_eq = mod.Plan("git-log", "/repo/sub", ("-p", "-n", "1", "--relative=sub"))
        note = mod._history_note_paths(
            "diff --git a/in.bin b/in.bin\n",
            relative_eq,
            ("sub/in.bin", "top.bin"),
            "/repo",
        )
        check(note == ["sub/in.bin", "top.bin"], repr(note))
        ns_plan = mod.Plan("git-show", "/", ("--name-status", "HEAD"))
        found = mod._detect_paths("commit abc\n\n    second\n\nM\tb.bin\n", ns_plan)
        check(found == {"b.bin"}, repr(found))
        patch_plan = mod.Plan("git-diff", "/", ("--cached",))
        found = mod._detect_paths("diff --git a/b.bin b/b.bin\n", patch_plan)
        check(found == {"b.bin"}, repr(found))
        found = mod._detect_paths(
            'diff --git "a/foo bar.bin" "b/foo bar.bin"\n',
            patch_plan,
        )
        check(found == {"foo bar.bin"}, repr(found))
        log_plan = mod.Plan("git-log", "/", ("--oneline",))
        check(mod._detect_paths("abcdef second\n", log_plan) == set(), "log without -p")
        both = mod.Plan("git-diff", "/", ("--stat", "--name-only"))
        check(mod._detect_paths("b.bin\n", both) is None, "mixed format flags")
        check(mod._parse_z_paths("") == [], "empty list")
        check(mod._parse_z_paths("a.bin\0b.bin\0") == ["a.bin", "b.bin"], "two paths")
        check(mod._parse_z_paths("a.bin") is None, "missing NUL terminator")
        fifty = [f"p{i:02d}.bin" for i in range(50)]
        check("\u2026" not in mod._fmt_paths(fifty), "50 paths stay literal")
        capped = mod._fmt_paths(fifty + ["p50.bin"])
        check(capped.endswith("\u2026 and 1 more"), capped)
        check("p50.bin" not in capped, capped)

    suite.case("L parsers and path cap", l_parsers)

    def l1():
        home, repo, env = make_lfs_repo("safe-read-l1-")
        pc_root = None
        try:
            os.utime(repo / "a.bin", None)
            pc_root = Path(os.path.realpath(tempfile.mkdtemp(prefix="safe-read-l1-pc-")))
            pc = pc_root / "repo"
            shutil.copytree(repo, pc, symlinks=True)
            control = subprocess.run(
                [
                    "git",
                    "-c", "filter.lfs.clean=",
                    "-c", "filter.lfs.smudge=",
                    "-c", "filter.lfs.process=",
                    "-c", "filter.lfs.required=false",
                    "status", "--short",
                ],
                cwd=pc,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            reports = any(
                len(line) >= 4 and line[2] == " " and line[3:] == "a.bin"
                for line in control.stdout.splitlines()
            )
            print(f"      L1 positive-control: {control.stdout!r} reports_a_bin={reports}")
            print(f"      L1 exclude pathspec: {mod._LFS_EXCLUDE_SPEC}")
            check(control.returncode == 0, f"control exit {control.returncode} {control.stderr!r}")
            with lfs_trap(mod) as marker:
                code, out, err = quiet_invoke(repo, home, marker, ["git-status"])
            check(code == 0, f"exit {code} stderr={err!r} stdout={out!r}")
            check(err == "", err)
            check("a.bin" not in porcelain_paths(note_body(out)), f"listed as modified: {out!r}")
            check(out == UNRESOLVED + "a.bin, b.bin]\n", f"stdout={out!r}")
        finally:
            remove_tree(repo)
            remove_tree(home)
            remove_tree(pc_root)

    suite.case("L1 stat-dirty LFS unresolved", l1)

    def l2():
        home, repo, _env = make_lfs_repo("safe-read-l2-")
        try:
            (repo / "a.bin").write_bytes(b"alpha-changed\n")
            with lfs_trap(mod) as marker:
                code, out, err = quiet_invoke(repo, home, marker, ["git-status"])
                check(code == 0 and err == "", f"status {code} {err!r} {out!r}")
                check("a.bin" not in porcelain_paths(note_body(out)), out)
                check(out == UNRESOLVED + "a.bin, b.bin]\n", f"status stdout={out!r}")
                code, out, err = quiet_invoke(repo, home, marker, ["git-diff"])
            check(code == 0 and err == "", f"diff {code} {err!r} {out!r}")
            body = note_body(out)
            check("diff --git" not in body and "@@" not in body, f"hunk leaked: {out!r}")
            check("alpha-changed" not in out, f"content leaked: {out!r}")
            check(out == UNRESOLVED + "a.bin, b.bin]\n", f"diff stdout={out!r}")
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("L2 changed LFS content unresolved", l2)

    def l3():
        home, repo, _env = make_lfs_repo("safe-read-l3-")
        try:
            (repo / "a.bin").write_bytes(b"alpha-changed\n")
            (repo / "src.txt").write_text("source\nedited\n", encoding="utf-8")
            with lfs_trap(mod) as marker:
                code, out, err = quiet_invoke(repo, home, marker, ["git-status"])
                check(code == 0 and err == "", f"status {code} {err!r} {out!r}")
                check(porcelain_paths(note_body(out)) == ["src.txt"], f"status={out!r}")
                check(out.endswith(UNRESOLVED + "a.bin, b.bin]\n"), f"status={out!r}")
                code, out, err = quiet_invoke(repo, home, marker, ["git-diff"])
            check(code == 0 and err == "", f"diff {code} {err!r} {out!r}")
            body = note_body(out)
            check("diff --git a/src.txt b/src.txt" in body, body)
            check("+edited" in body and "\n source\n" in body, body)
            check("diff --git a/a.bin" not in body, body)
            check("alpha-changed" not in out, out)
            check(out.endswith(UNRESOLVED + "a.bin, b.bin]\n"), out)
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("L3 ordinary file beside LFS", l3)

    def l4():
        home, repo, _env = make_lfs_repo("safe-read-l4-")
        try:
            (repo / "a.bin").write_bytes(b"alpha-changed\n")
            (repo / "src.txt").write_text("source\nedited\n", encoding="utf-8")
            with lfs_trap(mod) as marker:
                code, out, err = quiet_invoke(repo, home, marker, ["git-diff", "--", "src.txt"])
                check(code == 0 and err == "", f"src diff {code} {err!r} {out!r}")
                check("[safe-read:" not in out, f"unexpected footer: {out!r}")
                check("diff --git a/src.txt b/src.txt" in out, out)
                check("edited" in out, out)
                check("a.bin" not in out, out)
                code, out, err = quiet_invoke(repo, home, marker, ["git-diff", "--", "a.bin"])
            check(code == 0 and err == "", f"a.bin diff {code} {err!r} {out!r}")
            check("diff --git" not in out and "@@" not in out, out)
            check(out == UNRESOLVED + "a.bin]\n", f"stdout={out!r}")
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("L4 path-limited diff", l4)

    def assert_pointer(code, out, err, label):
        check(code == 0 and err == "", f"{label} {code} {err!r} {out[:400]!r}")
        check("diff --git a/b.bin b/b.bin" in out, f"{label} stdout={out[:500]!r}")
        check("version https://git-lfs.github.com/spec/v1" in out, f"{label} not a pointer diff")
        check("beta-new" not in out, f"{label} showed worktree bytes")
        check("unresolved:" not in out, f"{label} used the worktree footer")
        check(out.endswith(POINTER_NOTE + "b.bin]\n"), f"{label} note={out[-240:]!r}")

    def l5():
        home, repo, env = make_lfs_repo("safe-read-l5-")
        try:
            (repo / "b.bin").write_bytes(b"beta-new\n")
            added = subprocess.run(
                ["git", "add", "b.bin"],
                cwd=repo,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            check(added.returncode == 0, f"git add: {added.stderr!r}")
            staged = subprocess.run(
                ["git", "show", ":b.bin"],
                cwd=repo,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            check(
                staged.stdout.startswith("version https://git-lfs.github.com/spec/v1\n"),
                f"staged blob is not a pointer: {staged.stdout!r}",
            )
            with lfs_trap(mod) as marker:
                code, out, err = quiet_invoke(repo, home, marker, ["git-diff", "--cached"])
            assert_pointer(code, out, err, "cached")
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("L5 staged pointer diff", l5)

    def l6():
        home, repo, env = make_lfs_repo("safe-read-l6-")
        try:
            (repo / "b.bin").write_bytes(b"beta-new\n")
            for args in (["add", "b.bin"], ["commit", "-q", "-m", "update b"]):
                completed = subprocess.run(
                    ["git", *args],
                    cwd=repo,
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                check(completed.returncode == 0, f"git {' '.join(args)}: {completed.stderr!r}")
            commands = [
                ["git-diff", "HEAD~1..HEAD"],
                ["git-show", "HEAD"],
                ["git-log", "-p", "-n", "1"],
            ]
            with lfs_trap(mod) as marker:
                for argv in commands:
                    code, out, err = quiet_invoke(repo, home, marker, argv)
                    assert_pointer(code, out, err, " ".join(argv))
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("L6 history pointer diff", l6)

    def l7():
        home, repo, _env = make_lfs_repo("safe-read-l7-")
        try:
            with lfs_trap(mod) as marker:
                code, out, err = quiet_invoke(repo, home, marker, ["git-blame", "a.bin"])
                check(code == 3, f"exit {code} stderr={err!r} stdout={out!r}")
                check(out == "", out)
                check(
                    err == "safe-read: refused: a.bin is LFS-tracked; "
                    "blame against the worktree is unreliable with LFS filters disabled\n",
                    err,
                )
                code, out, err = quiet_invoke(repo, home, marker, ["git-blame", "src.txt"])
            check(code == 0 and err == "", f"src blame {code} {err!r} {out[:300]!r}")
            check("source" in out, out[:300])
            check("[safe-read:" not in out, out)
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("L7 blame refused for LFS path", l7)

    def l9():
        home = Path(os.path.realpath(tempfile.mkdtemp(prefix="safe-read-l9-home-")))
        repo = None
        try:
            (home / ".gitconfig").write_text("", encoding="utf-8")
            repo = make_repo("safe-read-l9-")
            commands = [
                ["git-status"],
                ["git-diff"],
                ["git-show", "HEAD"],
                ["git-log", "-p"],
                ["git-blame", "hello.txt"],
            ]

            def comparable(argv, text):
                # Blame stamps uncommitted lines with the current time, so two
                # back-to-back runs can differ by a second without a footer.
                if argv[0] == "git-blame":
                    return re.sub(
                        r"Not Committed Yet \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
                        "Not Committed Yet TIMESTAMP",
                        text,
                    )
                return text

            def raw(argv):
                saved_cwd = os.getcwd()
                saved = {
                    "HOME": os.environ.get("HOME"),
                    "CLAUDE_PROJECT_DIR": os.environ.get("CLAUDE_PROJECT_DIR"),
                }
                os.chdir(repo)
                os.environ["HOME"] = str(home)
                os.environ["CLAUDE_PROJECT_DIR"] = str(repo)
                try:
                    plan = mod.parse(argv)
                    exe = shutil.which("git", path=mod.FIXED_PATH)
                    result = mod._bounded_run(mod._command(exe, plan, ()), plan.cwd, mod._env())
                finally:
                    os.chdir(saved_cwd)
                    for key, value in saved.items():
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value
                return result

            for argv in commands:
                expected = raw(argv)
                code, out, err = invoke(mod, argv, repo, repo, env_extra={"HOME": str(home)})
                check(code == expected.code, f"{argv} exit {code} != {expected.code} err={err!r}")
                check(
                    comparable(argv, out) == comparable(argv, expected.stdout),
                    f"{argv} stdout mismatch {out!r} != {expected.stdout!r}",
                )
                check(
                    comparable(argv, err) == comparable(argv, expected.stderr),
                    f"{argv} stderr mismatch {err!r} != {expected.stderr!r}",
                )
                check("[safe-read:" not in out, f"{argv} added a footer")
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("L9 no LFS config identical", l9)

    def l10():
        home = Path(os.path.realpath(tempfile.mkdtemp(prefix="safe-read-l10-home-")))
        repo = None
        try:
            (home / ".gitconfig").write_text(LFS_CONFIG, encoding="utf-8")
            repo = make_root("safe-read-l10-")
            (repo / ".git" / "index").write_bytes(b"garbage")
            code, out, err = invoke(mod, ["git-status"], repo, repo, env_extra={"HOME": str(home)})
            check(code == 3, f"exit {code} stderr={err!r} stdout={out!r}")
            check(out == "", out)
            check(err == "safe-read: refused: could not list LFS paths\n", err)
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("L10 LFS list failure", l10)


def test_s(mod, suite):
    """Phase 7b follow-up: LFS handling from a subdirectory of the worktree."""
    neutral = (
        "-c", "filter.lfs.clean=",
        "-c", "filter.lfs.smudge=",
        "-c", "filter.lfs.process=",
        "-c", "filter.lfs.required=false",
    )

    def quiet_invoke(repo, home, marker, argv, cwd):
        if marker.exists():
            marker.unlink()
        before = snapshot(repo)
        code, out, err = invoke(mod, argv, cwd, repo, env_extra={"HOME": str(home)})
        after = snapshot(repo)
        delta = changed_paths(before, after)
        invoked = marker.read_text(encoding="utf-8") if marker.exists() else ""
        check(not delta, f"{' '.join(argv)} filesystem changed: {delta}")
        check(invoked == "", f"{' '.join(argv)} started git-lfs: {invoked!r}")
        return code, out, err

    def git_text(cwd, env, args):
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        check(
            completed.returncode == 0,
            f"git {' '.join(args)} -> {completed.returncode}: {completed.stderr.strip()}",
        )
        return completed.stdout

    def s1():
        home, repo, env = make_lfs_subdir_repo("safe-read-s1-")
        sub = repo / "sub"
        try:
            listed = git_text(
                sub, env, ["ls-files", "-z", "--full-name", "--", ":(top,attr:filter=lfs)"],
            )
            prefix_listed = git_text(sub, env, ["ls-files", "-z", "--", ":(attr:filter=lfs)"])
            print(f"      S1 list :(top,attr:filter=lfs) --full-name: {listed!r}")
            print(f"      S1 list :(attr:filter=lfs): {prefix_listed!r}")
            check(listed == "sub/in.bin\0top.bin\0", repr(listed))
            check(prefix_listed == "in.bin\0", repr(prefix_listed))
            os.utime(repo / "top.bin", None)
            os.utime(sub / "in.bin", None)
            with lfs_trap(mod) as marker:
                code, out, err = quiet_invoke(repo, home, marker, ["git-status"], sub)
            check(code == 0 and err == "", f"touch status {code} {err!r} {out!r}")
            check(porcelain_paths(note_body(out)) == [], f"touch listed files: {out!r}")
            check(out == UNRESOLVED + "sub/in.bin, top.bin]\n", f"touch stdout={out!r}")

            (repo / "top.bin").write_bytes(b"alpha-changed\n")
            (sub / "in.bin").write_bytes(b"inside-changed\n")
            (sub / "src.txt").write_text("source\nedited\n", encoding="utf-8")
            (repo / "other.txt").write_text("other\nedited\n", encoding="utf-8")

            def status(spec=None):
                args = [*neutral, "status", "--short"]
                if spec is not None:
                    args.extend(["--", spec])
                return git_text(sub, env, args)

            bare = status()
            prefix = status(":(exclude,attr:filter=lfs)")
            topped = status(":(top,exclude,attr:filter=lfs)")
            print(f"      S1 status no pathspec: {bare!r}")
            print(f"      S1 status :(exclude,attr:filter=lfs): {prefix!r}")
            print(f"      S1 status :(top,exclude,attr:filter=lfs): {topped!r}")
            check(porcelain_paths(prefix) == ["../other.txt", "src.txt", "../top.bin"], prefix)
            check(porcelain_paths(topped) == ["../other.txt", "src.txt"], topped)
            check(porcelain_paths(bare) == ["../other.txt", "in.bin", "src.txt", "../top.bin"], bare)

            def non_lfs(text):
                return [path for path in porcelain_paths(text) if not path.endswith(".bin")]

            check(non_lfs(bare) == non_lfs(topped) == ["../other.txt", "src.txt"], (bare, topped))
            with lfs_trap(mod) as marker:
                code, out, err = quiet_invoke(repo, home, marker, ["git-status"], sub)
            check(code == 0 and err == "", f"edit status {code} {err!r} {out!r}")
            check(porcelain_paths(note_body(out)) == ["../other.txt", "src.txt"], out)
            check("in.bin" not in note_body(out) and "top.bin" not in note_body(out), out)
            check(out.endswith(UNRESOLVED + "sub/in.bin, top.bin]\n"), out)
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("S1 status from subdir", s1)

    def s2():
        home, repo, env = make_lfs_subdir_repo("safe-read-s2-")
        sub = repo / "sub"
        try:
            (repo / "top.bin").write_bytes(b"alpha-changed\n")
            (sub / "in.bin").write_bytes(b"inside-changed\n")
            (sub / "src.txt").write_text("source\nedited\n", encoding="utf-8")
            (repo / "other.txt").write_text("other\nedited\n", encoding="utf-8")

            def headers(spec=None):
                args = [*neutral, "diff", "--no-ext-diff", "--no-textconv", "--no-color"]
                if spec is not None:
                    args.extend(["--", spec])
                text = git_text(sub, env, args)
                return [line for line in text.splitlines() if line.startswith("diff --git ")]

            bare = headers()
            prefix = headers(":(exclude,attr:filter=lfs)")
            topped = headers(":(top,exclude,attr:filter=lfs)")
            print(f"      S2 diff no pathspec: {bare}")
            print(f"      S2 diff :(exclude,attr:filter=lfs): {prefix}")
            print(f"      S2 diff :(top,exclude,attr:filter=lfs): {topped}")
            check("diff --git a/top.bin b/top.bin" in prefix, prefix)
            check("diff --git a/sub/in.bin b/sub/in.bin" not in prefix, prefix)
            check(topped == [
                "diff --git a/other.txt b/other.txt",
                "diff --git a/sub/src.txt b/sub/src.txt",
            ], topped)
            non_lfs = [line for line in bare if ".bin " not in line]
            check(non_lfs == topped, (non_lfs, topped))
            with lfs_trap(mod) as marker:
                code, out, err = quiet_invoke(repo, home, marker, ["git-diff"], sub)
            check(code == 0 and err == "", f"diff {code} {err!r} {out!r}")
            body = note_body(out)
            check("diff --git a/sub/src.txt b/sub/src.txt" in body, body)
            check("diff --git a/other.txt b/other.txt" in body, body)
            check("+edited" in body, body)
            check("diff --git a/top.bin" not in body and "diff --git a/sub/in.bin" not in body, body)
            check("alpha-changed" not in out and "inside-changed" not in out, out)
            check(out.endswith(UNRESOLVED + "sub/in.bin, top.bin]\n"), out)
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("S2 diff from subdir", s2)

    def s3():
        home, repo, _env = make_lfs_subdir_repo("safe-read-s3-")
        sub = repo / "sub"
        root = Path(os.path.realpath(tempfile.mkdtemp(prefix="safe-read-scope-")))
        try:
            (root / "sub").mkdir()
            (root / "top.bin").write_bytes(b"a")
            (root / "sub" / "in.bin").write_bytes(b"b")
            top = str(root)
            in_abs = str(root / "sub" / "in.bin")
            limited = mod.Plan("git-diff", str(root / "sub"), ("--", in_abs))
            check(
                mod._scoped_lfs(("top.bin", "sub/in.bin"), limited, top) == ["sub/in.bin"],
                "scope",
            )
            rel = mod.Plan("git-diff", str(root / "sub"), ("--relative", "--", in_abs))
            note = mod._history_note_paths(
                "diff --git a/in.bin b/in.bin\n",
                rel,
                ("top.bin", "sub/in.bin"),
                top,
            )
            check(note == ["sub/in.bin"], repr(note))
            (repo / "top.bin").write_bytes(b"alpha-changed\n")
            (sub / "in.bin").write_bytes(b"inside-changed\n")
            with lfs_trap(mod) as marker:
                code, out, err = quiet_invoke(repo, home, marker, ["git-diff", "--", "in.bin"], sub)
            check(code == 0 and err == "", f"diff {code} {err!r} {out!r}")
            check("diff --git" not in out and "@@" not in out, out)
            check("top.bin" not in out, out)
            check(out == UNRESOLVED + "sub/in.bin]\n", f"stdout={out!r}")
        finally:
            remove_tree(repo)
            remove_tree(home)
            remove_tree(root)

    suite.case("S3 path-limited diff from subdir", s3)

    def s4():
        home, repo, env = make_lfs_subdir_repo("safe-read-s4-", extra_bins=("c.bin",))
        sub = repo / "sub"
        try:
            (repo / "top.bin").write_bytes(b"top-new\n")
            (sub / "in.bin").write_bytes(b"in-new\n")
            git_in(repo, env, "add", "top.bin", "sub/in.bin")
            git_in(repo, env, "commit", "-q", "-m", "update both lfs")
            commands = [
                ["git-diff", "HEAD~1..HEAD"],
                ["git-show", "HEAD"],
                ["git-show", "--stat", "HEAD"],
                ["git-log", "-p", "-n", "1"],
            ]
            with lfs_trap(mod) as marker:
                for argv in commands:
                    code, out, err = quiet_invoke(repo, home, marker, argv, sub)
                    label = " ".join(argv)
                    check(code == 0 and err == "", f"{label} {code} {err!r} {out[:400]!r}")
                    check("unresolved:" not in out, f"{label} used the worktree footer")
                    check("c.bin" not in out, f"{label} listed untouched LFS path: {out[-240:]!r}")
                    check(
                        out.endswith(POINTER_NOTE + "sub/in.bin, top.bin]\n"),
                        f"{label} note={out[-240:]!r}",
                    )
                    if argv == ["git-show", "--stat", "HEAD"]:
                        check(" sub/in.bin |" in out and " top.bin" in out, out)
                        continue
                    check("diff --git a/sub/in.bin b/sub/in.bin" in out, label)
                    check("diff --git a/top.bin b/top.bin" in out, label)
                    check("version https://git-lfs.github.com/spec/v1" in out, label)
                    check("top-new" not in out and "in-new" not in out, label)
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("S4 history from subdir", s4)

    def s5():
        home, repo, _env = make_lfs_subdir_repo("safe-read-s5-")
        sub = repo / "sub"
        try:
            with lfs_trap(mod) as marker:
                code, out, err = quiet_invoke(repo, home, marker, ["git-blame", "in.bin"], sub)
                check(code == 3, f"exit {code} stderr={err!r} stdout={out!r}")
                check(out == "", out)
                check(
                    err == "safe-read: refused: sub/in.bin is LFS-tracked; "
                    "blame against the worktree is unreliable with LFS filters disabled\n",
                    err,
                )
                code, out, err = quiet_invoke(repo, home, marker, ["git-blame", "src.txt"], sub)
            check(code == 0 and err == "", f"src blame {code} {err!r} {out[:300]!r}")
            check("source" in out, out[:300])
            check("[safe-read:" not in out, out)
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("S5 blame from subdir", s5)

    def s6():
        home, repo, _env = make_lfs_subdir_repo("safe-read-s6-")
        sub = repo / "sub"
        try:
            (repo / "top.bin").write_bytes(b"alpha-changed\n")
            (sub / "in.bin").write_bytes(b"inside-changed\n")
            (sub / "src.txt").write_text("source\nedited\n", encoding="utf-8")
            commands = [
                ["git-status"],
                ["git-diff"],
                ["git-show", "HEAD"],
                ["git-log", "-p", "-n", "1"],
                ["git-blame", "src.txt"],
            ]
            with lfs_trap(mod) as marker:
                for argv in commands:
                    code, out, err = quiet_invoke(repo, home, marker, argv, sub)
                    check(code == 0 and err == "", f"{argv} {code} {err!r} {out[:200]!r}")
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("S6 no git-lfs and no .git change", s6)

    def s7():
        home = Path(os.path.realpath(tempfile.mkdtemp(prefix="safe-read-s7-home-")))
        repo = None
        try:
            (home / ".gitconfig").write_text("", encoding="utf-8")
            repo = Path(os.path.realpath(tempfile.mkdtemp(prefix="safe-read-s7-")))
            env = git_env()
            env["HOME"] = str(home)
            env["GIT_CONFIG_NOSYSTEM"] = "1"
            git_in(repo, env, "init", "-q", "-b", "main")
            git_in(repo, env, "config", "user.email", "safe-read@example.com")
            git_in(repo, env, "config", "user.name", "Safe Read")
            git_in(repo, env, "config", "commit.gpgsign", "false")
            git_in(repo, env, "config", "color.ui", "false")
            (repo / "other.txt").write_text("other\n", encoding="utf-8")
            (repo / "sub").mkdir()
            (repo / "sub" / "src.txt").write_text("source\n", encoding="utf-8")
            git_in(repo, env, "add", "other.txt", "sub/src.txt")
            git_in(repo, env, "commit", "-q", "-m", "first")
            (repo / "other.txt").write_text("other\nchanged\n", encoding="utf-8")
            (repo / "sub" / "src.txt").write_text("source\nedited\n", encoding="utf-8")
            sub = repo / "sub"
            commands = [
                ["git-status"],
                ["git-diff"],
                ["git-show", "HEAD"],
                ["git-log", "-p"],
                ["git-blame", "src.txt"],
            ]

            def comparable(argv, text):
                if argv[0] == "git-blame":
                    return re.sub(
                        r"Not Committed Yet \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
                        "Not Committed Yet TIMESTAMP",
                        text,
                    )
                return text

            def raw(argv):
                saved_cwd = os.getcwd()
                saved = {
                    "HOME": os.environ.get("HOME"),
                    "CLAUDE_PROJECT_DIR": os.environ.get("CLAUDE_PROJECT_DIR"),
                }
                os.chdir(sub)
                os.environ["HOME"] = str(home)
                os.environ["CLAUDE_PROJECT_DIR"] = str(repo)
                try:
                    plan = mod.parse(argv)
                    exe = shutil.which("git", path=mod.FIXED_PATH)
                    result = mod._bounded_run(mod._command(exe, plan, ()), plan.cwd, mod._env())
                finally:
                    os.chdir(saved_cwd)
                    for key, value in saved.items():
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value
                return result

            for argv in commands:
                expected = raw(argv)
                code, out, err = invoke(mod, argv, sub, repo, env_extra={"HOME": str(home)})
                check(code == expected.code, f"{argv} exit {code} != {expected.code} err={err!r}")
                check(
                    comparable(argv, out) == comparable(argv, expected.stdout),
                    f"{argv} stdout mismatch {out!r} != {expected.stdout!r}",
                )
                check(
                    comparable(argv, err) == comparable(argv, expected.stderr),
                    f"{argv} stderr mismatch {err!r} != {expected.stderr!r}",
                )
                check("[safe-read:" not in out, f"{argv} added a footer")
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("S7 non-LFS repo from subdir", s7)


def test_r(mod, suite):
    """Review 13: long LFS lists, pathspecs, names, truncation, hidden paths."""

    def quiet_invoke(repo, home, marker, argv, cwd=None):
        if cwd is None:
            cwd = repo
        if marker.exists():
            marker.unlink()
        before = snapshot(repo)
        code, out, err = invoke(mod, argv, cwd, repo, env_extra={"HOME": str(home)})
        after = snapshot(repo)
        delta = changed_paths(before, after)
        invoked = marker.read_text(encoding="utf-8") if marker.exists() else ""
        check(not delta, f"{' '.join(argv)} filesystem changed: {delta}")
        check(invoked == "", f"{' '.join(argv)} started git-lfs: {invoked!r}")
        return code, out, err

    def footer_lines(out):
        return [line for line in out.splitlines() if line.startswith("[safe-read:")]

    def r1():
        home, repo, env = make_lfs_repo("safe-read-r1-")
        try:
            names = [f"{i:03d}-" + ("p" * 40) + ".bin" for i in range(150)]
            for name in names:
                (repo / name).write_bytes(b"p\n")
            git_in(repo, env, "add", *names)
            git_in(repo, env, "commit", "-q", "-m", "many")
            listed = git_in(
                repo, env, "ls-files", "-z", "--full-name", "--", ":(top,attr:filter=lfs)",
            ).stdout
            paths = [part for part in listed.split("\0") if part]
            check(len(paths) >= 150, f"listed {len(paths)}")
            check(sum(len(path) for path in paths) > 6000, "path list shorter than 6000")
            with lfs_trap(mod) as marker:
                code, out, err = quiet_invoke(repo, home, marker, ["git-status"])
            check(code == 0 and err == "", f"status {code} {err!r} {out[:200]!r}")
            check("refused" not in err, err)
            extra = len(paths) - 50
            check(out.startswith(UNRESOLVED), out[:200])
            check(f"\u2026 and {extra} more" in out, out[-120:])
            check(paths[0] in out and paths[49] in out, "first 50 missing")
            check(paths[50] not in out, f"path past the cap was printed: {paths[50]}")
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("R1 many long LFS paths", r1)

    def r2():
        home, repo, env = make_lfs_subdir_repo("safe-read-r2-")
        try:
            (repo / "top.bin").write_bytes(b"top-changed\n")
            (repo / "sub" / "in.bin").write_bytes(b"in-changed\n")
            (repo / "sub" / "src.txt").write_text("source\nedited\n", encoding="utf-8")
            (repo / "other.txt").write_text("other\nedited\n", encoding="utf-8")

            def diff(argv):
                with lfs_trap(mod) as marker:
                    return quiet_invoke(repo, home, marker, argv)

            code, out, err = diff(["git-diff", "--", "*.bin"])
            check(code == 0 and err == "", f"glob {code} {err!r} {out[:300]!r}")
            check("diff --git" not in out and "@@" not in out, out)
            check("top-changed" not in out and "in-changed" not in out, out)
            check(out.startswith(UNRESOLVED), out[:80])
            for name in ("top.bin", "sub/in.bin"):
                check(name in out, f"{name} missing: {out!r}")
            check("other.txt" not in out and "src.txt" not in out, out)

            code, out, err = diff(["git-diff", "--", ":(glob)**/*.bin"])
            check(code == 0 and err == "", f"magic {code} {err!r} {out[:300]!r}")
            check("diff --git" not in out, out)
            check("top.bin" in out and "sub/in.bin" in out, out)
            check("other.txt" not in out, out)

            code, out, err = diff(["git-diff", "--", "sub"])
            check(code == 0 and err == "", f"dir {code} {err!r} {out!r}")
            check("diff --git a/sub/src.txt b/sub/src.txt" in out, out)
            check("diff --git a/sub/in.bin" not in out and "diff --git a/top.bin" not in out, out)
            check("in-changed" not in out and "top-changed" not in out, out)
            check("top.bin" not in out, out)
            check(out.endswith(UNRESOLVED + "sub/in.bin]\n"), f"dir stdout={out!r}")

            code, out, err = diff(["git-diff", "--", "*.txt"])
            check(code == 0 and err == "", f"txt {code} {err!r} {out[:400]!r}")
            check("[safe-read:" not in out, f"txt footer: {out!r}")
            check("diff --git a/other.txt" in out and "diff --git a/sub/src.txt" in out, out)
            check("+edited" in out, out)
            check("top.bin" not in out and "in.bin" not in out, out)
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("R2 glob and directory pathspecs", r2)

    def r3():
        home, repo, env = make_lfs_repo("safe-read-r3-")
        try:
            names = ["é.bin", "空.bin"]
            for name in names:
                (repo / name).write_bytes(b"first\n")
            git_in(repo, env, "add", "--", *names)
            git_in(repo, env, "commit", "-q", "-m", "add unicode")
            for name in names:
                (repo / name).write_bytes(b"second\n")
            git_in(repo, env, "add", "--", *names)
            git_in(repo, env, "commit", "-q", "-m", "edit unicode")
            stored = git_in(
                repo, env, "diff", "--name-only", "-z", "HEAD~1", "HEAD",
            ).stdout
            touched = [part for part in stored.split("\0") if part]
            check(set(touched) == set(names) or len(touched) == 2, repr(touched))
            commands = [
                ["git-diff", "HEAD~1..HEAD"],
                ["git-show", "HEAD"],
                ["git-show", "--stat", "HEAD"],
                ["git-log", "-p", "-n", "1"],
            ]
            with lfs_trap(mod) as marker:
                for argv in commands:
                    code, out, err = quiet_invoke(repo, home, marker, argv)
                    label = " ".join(argv)
                    check(code == 0 and err == "", f"{label} {code} {err!r}")
                    check("unresolved:" not in out, label)
                    footer = footer_lines(out)
                    check(len(footer) == 1, f"{label} footers={footer!r} out={out[-300]!r}")
                    for name in touched:
                        check(name in footer[0], f"{label} note missed {name!r}: {footer[0]!r}")
                    check("a.bin" not in footer[0] and "b.bin" not in footer[0], footer[0])
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("R3 non-ASCII history names", r3)

    def r4():
        home, repo, env = make_lfs_repo("safe-read-r4-")
        try:
            name = ("L" * 120) + ".bin"
            check(len(name) > 100, name)
            (repo / name).write_bytes(b"v1\n")
            git_in(repo, env, "add", "--", name)
            git_in(repo, env, "commit", "-q", "-m", "long")
            (repo / name).write_bytes(b"v2\n")
            git_in(repo, env, "add", "--", name)
            git_in(repo, env, "commit", "-q", "-m", "long2")
            with lfs_trap(mod) as marker:
                code, out, err = quiet_invoke(repo, home, marker, ["git-show", "--stat", "HEAD"])
            check(code == 0 and err == "", f"{code} {err!r} {out[:300]!r}")
            body, _, footer = out.rpartition("[safe-read:")
            check(name not in body, "stat kept the full path")
            check(name in footer, f"note missed full path: {footer!r}")
            check(footer.startswith(" note:"), footer[:80])
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("R4 long path in show --stat", r4)

    def r5():
        home, repo, env = make_lfs_repo("safe-read-r5-")
        try:
            # Long enough to pass 200 KB before the 2000-line cap (under 2000 diff lines).
            payload = "x" * 180 + "\n"
            (repo / "big.txt").write_text(payload * 800, encoding="utf-8")
            git_in(repo, env, "add", "big.txt")
            git_in(repo, env, "commit", "-q", "-m", "big")
            (repo / "big.txt").write_text(payload.replace("x", "y") * 800, encoding="utf-8")
            with lfs_trap(mod) as marker:
                code, out, err = quiet_invoke(repo, home, marker, ["git-diff"])
            check(code == 0 and err == "", f"{code} {err!r}")
            encoded = out.encode("utf-8")
            check(len(encoded) <= MAX_STDOUT, f"stdout {len(encoded)} exceeds {MAX_STDOUT}")
            check(
                "[safe-read: output truncated at 200 KB]" in out,
                f"byte cap notice missing ({len(encoded)} bytes): {out[-180]!r}",
            )
            trunc_at = out.find("[safe-read: output truncated at ")
            note_at = out.find(UNRESOLVED)
            check(trunc_at != -1 and note_at != -1 and trunc_at < note_at, out[-400:])
            check(out.endswith("]\n"), out[-120:])
            check("a.bin" in out[note_at:] and "b.bin" in out[note_at:], out[-200:])
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("R5 truncation keeps the LFS note", r5)

    def r6():
        home, repo, env = make_lfs_repo("safe-read-r6-")
        try:
            (repo / "new.bin").write_bytes(b"new\n")
            git_in(repo, env, "rm", "-q", "a.bin")
            with lfs_trap(mod) as marker:
                code, out, err = quiet_invoke(repo, home, marker, ["git-status"])
                check(code == 0 and err == "", f"status {code} {err!r} {out!r}")
                body = note_body(out)
                check("new.bin" not in porcelain_paths(body), f"untracked shown: {body!r}")
                check("a.bin" not in porcelain_paths(body), f"staged delete shown: {body!r}")
                check(out.startswith(UNRESOLVED), out[:80])
                for name in ("a.bin", "b.bin", "new.bin"):
                    check(name in out, f"status footer missed {name}: {out!r}")
                code, out, err = quiet_invoke(repo, home, marker, ["git-diff"])
                check(code == 0 and err == "", f"diff {code} {err!r} {out!r}")
                check("diff --git" not in out, out)
                for name in ("a.bin", "new.bin"):
                    check(name in out, f"diff footer missed {name}: {out!r}")
                code, out, err = quiet_invoke(repo, home, marker, ["git-diff", "--", "*.txt"])
            check(code == 0 and err == "", f"txt {code} {err!r} {out!r}")
            check("[safe-read:" not in out, f"out-of-scope footer: {out!r}")
            check("new.bin" not in out and "a.bin" not in out, out)
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("R6 untracked and staged-deleted", r6)

    def r7():
        home, repo, env = make_lfs_repo("safe-read-r7-")
        try:
            newline_name = "bad\n[safe-read: unresolved: x].bin"
            marked_name = "craft[safe-read:.bin"
            (repo / newline_name).write_bytes(b"one\n")
            (repo / marked_name).write_bytes(b"two\n")
            git_in(repo, env, "add", "--", newline_name, marked_name)
            git_in(repo, env, "commit", "-q", "-m", "crafted")
            with lfs_trap(mod) as marker:
                for argv in (["git-status"], ["git-show", "HEAD"]):
                    code, out, err = quiet_invoke(repo, home, marker, argv)
                    label = " ".join(argv)
                    check(code == 0 and err == "", f"{label} {code} {err!r} stdout={out[:200]!r}")
                    check("refused" not in err, err)
                    markers = footer_lines(out)
                    check(len(markers) == 1, f"{label} markers={markers!r}")
                    rest = markers[0].split("]", 1)[0]
                    check("[safe-read:" not in rest[len("[safe-read:"):], markers[0])
                    check("\\n" in markers[0], markers[0])
                    check("[safe-read\\x3a" in markers[0], markers[0])
                    check(
                        not any(line.startswith("[safe-read: unresolved: x]") for line in out.splitlines()),
                        out,
                    )
            parsed = mod._parse_z_paths("a.bin\0bad\n[safe-read: unresolved: x].bin\0")
            check(
                parsed == ["a.bin", "bad\n[safe-read: unresolved: x].bin"],
                repr(parsed),
            )
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("R7 crafted LFS names", r7)

    def r8():
        home, repo, _env = make_lfs_repo("safe-read-r8-")
        try:
            (repo / "a.bin").write_bytes(b"changed\n")
            lfs_dir = repo / ".git" / "lfs"
            if lfs_dir.exists():
                shutil.rmtree(lfs_dir)
            with lfs_trap(mod) as marker:
                for argv in (
                    ["git-status"],
                    ["git-diff"],
                    ["git-show", "HEAD"],
                    ["git-log", "-p", "-n", "1"],
                ):
                    code, out, err = quiet_invoke(repo, home, marker, argv)
                    check(code == 0 and err == "", f"{argv} {code} {err!r}")
                    check(not marker.exists(), f"{argv} started git-lfs")
                    check(not lfs_dir.exists(), f"{argv} created .git/lfs")
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("R8 no git-lfs and no repo change", r8)

    def r9():
        home = Path(os.path.realpath(tempfile.mkdtemp(prefix="safe-read-r9-home-")))
        repo = None
        try:
            (home / ".gitconfig").write_text("", encoding="utf-8")
            repo = make_repo("safe-read-r9-")
            commands = [
                ["git-status"],
                ["git-diff"],
                ["git-show", "HEAD"],
                ["git-log", "-p"],
                ["git-blame", "hello.txt"],
            ]

            def comparable(argv, text):
                if argv[0] == "git-blame":
                    return re.sub(
                        r"Not Committed Yet \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
                        "Not Committed Yet TIMESTAMP",
                        text,
                    )
                return text

            def raw(argv):
                saved_cwd = os.getcwd()
                saved = {
                    "HOME": os.environ.get("HOME"),
                    "CLAUDE_PROJECT_DIR": os.environ.get("CLAUDE_PROJECT_DIR"),
                }
                os.chdir(repo)
                os.environ["HOME"] = str(home)
                os.environ["CLAUDE_PROJECT_DIR"] = str(repo)
                try:
                    plan = mod.parse(argv)
                    exe = shutil.which("git", path=mod.FIXED_PATH)
                    result = mod._bounded_run(mod._command(exe, plan, ()), plan.cwd, mod._env())
                finally:
                    os.chdir(saved_cwd)
                    for key, value in saved.items():
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value
                return result

            for argv in commands:
                expected = raw(argv)
                code, out, err = invoke(mod, argv, repo, repo, env_extra={"HOME": str(home)})
                check(code == expected.code, f"{argv} exit {code} != {expected.code} err={err!r}")
                check(
                    comparable(argv, out) == comparable(argv, expected.stdout),
                    f"{argv} stdout mismatch {out!r} != {expected.stdout!r}",
                )
                check(
                    comparable(argv, err) == comparable(argv, expected.stderr),
                    f"{argv} stderr mismatch {err!r} != {expected.stderr!r}",
                )
                check("[safe-read:" not in out, f"{argv} added a footer")
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("R9 non-LFS output unchanged", r9)

    def r10():
        home, repo, env = make_lfs_repo("safe-read-r10-")
        try:
            tracked = [f"m{i:02d}.bin" for i in range(61)]
            for name in tracked:
                (repo / name).write_bytes(b"t\n")
            (repo / "z-gone.bin").write_bytes(b"gone\n")
            (repo / "z-work.bin").write_bytes(b"work\n")
            git_in(repo, env, "add", "--", *tracked, "z-gone.bin", "z-work.bin")
            git_in(repo, env, "commit", "-q", "-m", "many")
            git_in(repo, env, "rm", "-q", "--", "z-gone.bin")
            (repo / "z-work.bin").unlink()
            (repo / "z-new.bin").write_bytes(b"new\n")
            listed = git_in(
                repo, env, "ls-files", "-z", "--full-name", "--", ":(top,attr:filter=lfs)",
            ).stdout
            index = [part for part in listed.split("\0") if part]
            check("z-work.bin" in index, "worktree deletion left the index")
            check("z-gone.bin" not in index and "z-new.bin" not in index, index)
            check(
                all(path < "z-gone.bin" for path in index if path != "z-work.bin"),
                "tracked names do not sort before the known changes",
            )
            check(len(index) > 60, f"tracked {len(index)}")
            total = len(index) + 2
            with lfs_trap(mod) as marker:
                code, out, err = quiet_invoke(repo, home, marker, ["git-status"])
            check(code == 0 and err == "", f"status {code} {err!r} {out[:200]!r}")
            check(out.startswith(UNRESOLVED), out[:80])
            named = out[len(UNRESOLVED):].split("\u2026", 1)[0]
            check(
                named.startswith("z-gone.bin, z-new.bin, z-work.bin, a.bin, b.bin, m00.bin,"),
                named[:120],
            )
            check("m44.bin" in named and "m45.bin" not in out, named[-80:])
            match = re.search(r"\u2026 and (\d+) more\]", out)
            check(match is not None, out[-80:])
            check(50 + int(match.group(1)) == total, f"count {match.group(1)} != {total - 50}")
            check(len(footer_lines(out)) == 1, out[-200:])
        finally:
            remove_tree(repo)
            remove_tree(home)

    suite.case("R10 known LFS names before the cap", r10)


def init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    run_git(path, "init", "-q", "-b", "main")
    run_git(path, "config", "user.email", "safe-read@example.com")
    run_git(path, "config", "user.name", "Safe Read")
    run_git(path, "config", "commit.gpgsign", "false")
    run_git(path, "config", "color.ui", "false")
    (path / "hello.txt").write_text("hello\n", encoding="utf-8")
    run_git(path, "add", "hello.txt")
    run_git(path, "commit", "-q", "-m", "first")
    return path


def test_roots(mod, suite):
    """Extra roots from safe-read-roots, and git --repo inside one of them."""
    home = Path(os.path.realpath(tempfile.mkdtemp(prefix="safe-read-roots-home-")))
    project = None
    outside_repo = None
    outside_dir = None
    try:
        project = make_repo("safe-read-roots-proj-")
        (project / "project-only.txt").write_text("project-only\n", encoding="utf-8")
        run_git(project, "add", "project-only.txt")
        run_git(project, "commit", "-q", "-m", "project-commit")
        extra = init_repo(home / "extra-repo")
        (extra / "only-extra.txt").write_text("only-extra\n", encoding="utf-8")
        run_git(extra, "add", "only-extra.txt")
        run_git(extra, "commit", "-q", "-m", "extra-commit")
        (home / "empty-dir").mkdir()
        outside_repo = make_repo("safe-read-roots-out-")
        outside_dir = Path(os.path.realpath(tempfile.mkdtemp(prefix="safe-read-roots-file-")))
        secret = outside_dir / "secret.txt"
        secret.write_text("secret\n", encoding="utf-8")
        (extra / "escape").symlink_to(secret)
        roots_dir = home / ".claude" / "hooks" / "data"
        roots_dir.mkdir(parents=True)
        roots_path = roots_dir / "safe-read-roots"
        roots_path.write_text(
            "\n".join([
                "# comment",
                "",
                "~/no-such-safe-read-root",
                "~/extra-repo/hello.txt",
                "~/empty-dir",
                "~/extra-repo",
                "",
            ]) + "\n",
            encoding="utf-8",
        )
        env = {"HOME": str(home)}

        def run(argv, cwd):
            return invoke(mod, argv, cwd, project, env_extra=env)

        def reachable():
            code, out, err = run(["wc", str(extra / "only-extra.txt")], project)
            check(code == 0 and err == "", f"wc extra {code} {err!r} {out!r}")
            check(out.split()[:1] == ["1"], out)
            code, out, err = run(["ls", "only-extra.txt"], extra)
            check(code == 0 and err == "" and "only-extra.txt" in out, f"cwd extra {code} {err!r} {out!r}")

        def outside_rejected():
            code, out, err = run(["wc", str(secret)], project)
            check(code == 2 and out == "", f"outside {code} {out!r} {err!r}")
            check("outside allowed roots" in err, err)

        def symlink_rejected():
            code, out, err = run(["wc", "escape"], extra)
            check(code == 2 and out == "", f"symlink {code} {out!r} {err!r}")
            check("outside allowed roots" in err, err)
            code, out, err = run(["wc", str(extra / "escape")], project)
            check(code == 2 and out == "", f"symlink abs {code} {out!r} {err!r}")
            check("outside allowed roots" in err, err)

        def repo_inside():
            code, out, err = run(
                ["git-log", "--repo", str(extra), "-n", "1", "--oneline"], project,
            )
            check(code == 0 and err == "", f"log {code} {err!r} {out!r}")
            check("extra-commit" in out and "project-commit" not in out, out)
            code, out, err = run(["git-blame", "--repo", str(extra), "only-extra.txt"], project)
            check(code == 0 and err == "", f"blame {code} {err!r} {out!r}")
            check("only-extra" in out, out)
            code, out, err = run(
                ["git-blame", "--repo", str(extra), "project-only.txt"], project,
            )
            check("project-only" not in out, f"resolved against project: {code} {out!r} {err!r}")
            rel = os.path.relpath(extra, project)
            code, out, err = run(
                ["git-log", "--repo", rel, "-n", "1", "--oneline"], project,
            )
            check(code == 0 and "extra-commit" in out, f"relative repo {code} {err!r} {out!r}")

        def repo_outside():
            code, out, err = run(["git-log", "--repo", str(outside_repo), "-n", "1"], project)
            check(code == 2 and out == "", f"repo outside {code} {out!r} {err!r}")
            check("outside allowed roots" in err, err)
            code, out, err = run(["git-status", "--repo", "~/empty-dir"], project)
            check(code == 2 and out == "", f"not a work tree {code} {out!r} {err!r}")
            check("invalid --repo" in err, err)

        def help_mentions_repo():
            code, out, err = run(["help"], project)
            check(code == 0 and err == "", f"help {code} {err!r}")
            check("--repo <path>" in out, out)

        def missing_file():
            roots_path.unlink()
            code, out, err = run(["wc", str(extra / "only-extra.txt")], project)
            check(code == 2 and out == "", f"missing file {code} {out!r} {err!r}")
            check("outside allowed roots" in err, err)
            code, out, err = run(["git-status"], extra)
            check(code == 2 and out == "", f"cwd without file {code} {out!r} {err!r}")
            check("working directory is outside allowed roots" in err, err)

        def commented_file():
            roots_path.write_text("# ~/extra-repo\n\n# comment only\n", encoding="utf-8")
            code, out, err = run(["wc", str(extra / "only-extra.txt")], project)
            check(code == 2 and out == "", f"comments {code} {out!r} {err!r}")
            check("outside allowed roots" in err, err)
            code, out, err = run(["ls", "only-extra.txt"], extra)
            check(code == 2 and out == "", f"cwd comments {code} {out!r} {err!r}")
            check("working directory is outside allowed roots" in err, err)

        suite.case("roots extra root reachable", reachable)
        suite.case("roots path outside every root", outside_rejected)
        suite.case("roots symlink escaping a root", symlink_rejected)
        suite.case("roots --repo inside", repo_inside)
        suite.case("roots --repo outside", repo_outside)
        suite.case("roots help mentions --repo", help_mentions_repo)
        suite.case("roots missing file adds none", missing_file)
        suite.case("roots commented file adds none", commented_file)
    finally:
        remove_tree(project)
        remove_tree(outside_repo)
        remove_tree(outside_dir)
        remove_tree(home)


def main():
    mod = load_module()
    suite = Suite()
    print(f"{'#':>4}  {'case':<32} result")
    test_a(mod, suite)
    test_b(mod, suite)
    test_c(mod, suite)
    test_d(mod, suite)
    test_e(mod, suite)
    test_f(mod, suite)
    test_g(mod, suite)
    test_h(mod, suite)
    test_l(mod, suite)
    test_s(mod, suite)
    test_r(mod, suite)
    test_roots(mod, suite)
    print(f"Tally: {suite.passed} passed, {suite.failed} failed")
    return 0 if suite.failed == 0 and suite.passed > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
