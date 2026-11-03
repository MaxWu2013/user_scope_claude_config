#!/usr/bin/env python3
"""Subprocess tests for validate-vault.py. The hook is not registered.

Stand-alone. Prints a table and `Tally: N passed, M failed`. Exits non-zero
on failure. HOME and the vault live under temp directories so VAULT_ROOTS,
read at _paths import time, points at the temp vault.
"""

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

import yaml

ROOT = Path(__file__).resolve().parent
HOOK = ROOT / "validate-vault.py"
FIXTURE = ROOT / "fixtures" / "cursor-pretooluse-write.json"
# PyYAML lives in the user site, which follows HOME. The hook is run with a
# temp HOME, so point PYTHONPATH at the real site-packages (not the hooks dir).
YAML_SITE = str(Path(yaml.__file__).resolve().parent.parent)
BACKUP_NAME = re.compile(r"^(\d{8}T\d{12})-(.+)$")
APPEND_DENY = (
    "append/patch is refused for .canvas/.base; "
    "write the whole file with put_content or Write."
)

VALID = json.dumps({
    "nodes": [{"id": "a"}, {"id": "b"}],
    "edges": [{"id": "e", "fromNode": "a", "toNode": "b"}],
}, separators=(",", ":"))


def canvas(**overrides):
    body = {
        "nodes": [{"id": "a"}, {"id": "b"}],
        "edges": [{"id": "e", "fromNode": "a", "toNode": "b"}],
    }
    body.update(overrides)
    return json.dumps(body, separators=(",", ":"))


def write_body(path, content, tool="Write"):
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"file_path": str(path), "content": content},
    }


def edit_body(path, old, new, replace_all=False):
    tool_input = {
        "file_path": str(path),
        "old_string": old,
        "new_string": new,
    }
    if replace_all:
        tool_input["replace_all"] = True
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": tool_input,
    }


def multiedit_body(path, edits):
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "MultiEdit",
        "tool_input": {"file_path": str(path), "edits": edits},
    }


def obsidian_body(op, filepath, **extra):
    tool_input = {"filepath": filepath}
    tool_input.update(extra)
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": f"mcp__mcp-obsidian__obsidian_{op}",
        "tool_input": tool_input,
    }


def detail_of(completed):
    return (
        f"exit={completed.returncode} stdout={completed.stdout!r} "
        f"stderr={completed.stderr[:400]!r}"
    )


class World:
    def __init__(self):
        self.home = Path(os.path.realpath(tempfile.mkdtemp(prefix="p6b-home-")))
        self.project = Path(os.path.realpath(tempfile.mkdtemp(prefix="p6b-proj-")))
        self.vault = self.home / "workplace" / "project" / "Obsidian_doc"
        self.vault.mkdir(parents=True)
        self.passed = 0
        self.failed = 0
        self.failures = []
        self.n = 0
        self.extra = []

    def cleanup(self):
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.project, ignore_errors=True)
        for path in self.extra:
            shutil.rmtree(path, ignore_errors=True)

    def plant(self, name, text):
        path = self.vault / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def backups(self, basename):
        directory = self.home / ".claude" / "backup" / "vault"
        if not directory.is_dir():
            return []
        found = []
        for path in directory.iterdir():
            match = BACKUP_NAME.match(path.name)
            if match and match.group(2) == basename:
                found.append(path)
        return sorted(found, key=lambda path: path.name)

    def run(self, mode, payload=None, *, stdin=None, hook=None, cwd=None):
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["CLAUDE_PROJECT_DIR"] = str(self.project)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = YAML_SITE
        completed = subprocess.run(
            [sys.executable, str(hook or HOOK), f"--{mode}"],
            input=stdin if stdin is not None else json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            cwd=str(cwd or self.project),
            check=False,
        )
        return completed

    def check(self, name, ok, detail=""):
        self.n += 1
        status = "PASS" if ok else "FAIL"
        print(f"{self.n:>4}  {name:<78} {status}")
        if ok:
            self.passed += 1
            return
        self.failed += 1
        self.failures.append(f"{self.n} {name}: {detail}")
        if detail:
            print(f"      {detail}")

    def expect_silent(self, name, completed):
        self.check(
            name,
            completed.returncode == 0 and completed.stdout == "" and completed.stderr == "",
            detail_of(completed),
        )

    def expect_deny(self, name, completed, needle=None, exact=None):
        ok = completed.returncode == 0 and completed.stderr == ""
        reason = None
        if ok:
            try:
                data = json.loads(completed.stdout)
            except json.JSONDecodeError:
                ok = False
            else:
                hook = data.get("hookSpecificOutput") or {}
                reason = hook.get("permissionDecisionReason")
                ok = (
                    hook.get("hookEventName") == "PreToolUse"
                    and hook.get("permissionDecision") == "deny"
                    and isinstance(reason, str)
                )
                if exact is not None:
                    ok = ok and reason == exact
                if needle is not None:
                    ok = ok and needle in reason
        self.check(name, ok, detail_of(completed) + (f" reason={reason!r}" if reason else ""))


def unchanged(path: Path, before: str) -> bool:
    return path.read_text(encoding="utf-8") == before


def test_canvas_writes(world: World):
    new_path = world.vault / "new.canvas"
    world.expect_silent(
        "Write valid canvas -> silent",
        world.run("pre", write_body(new_path, VALID)),
    )
    world.check(
        "Write valid canvas does not create the file",
        not new_path.exists(),
        f"exists={new_path.exists()}",
    )

    empty = world.vault / "empty-object.canvas"
    world.expect_silent(
        "canvas with nodes and edges omitted -> silent",
        world.run("pre", write_body(empty, "{}")),
    )

    planted = world.plant("bad-json.canvas", VALID)
    before = world.backups(planted.name)
    completed = world.run("pre", write_body(planted, "{"))
    world.expect_deny("invalid JSON -> deny", completed, needle="invalid JSON")
    world.check(
        "invalid JSON -> nothing was written, file unchanged, no backup",
        unchanged(planted, VALID)
        and "nothing was written" in (json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecisionReason"])
        and world.backups(planted.name) == before,
        f"text={planted.read_text()!r} backups={world.backups(planted.name)!r}",
    )

    cases = (
        (
            "dangling edge",
            canvas(edges=[{"id": "e", "fromNode": "a", "toNode": "missing"}]),
            "does not name a node",
        ),
        (
            "duplicate node id",
            canvas(nodes=[{"id": "a"}, {"id": "a"}], edges=[]),
            "duplicate node id a",
        ),
        (
            "nodes not a list",
            canvas(nodes={"id": "a"}),
            "nodes must be a list",
        ),
        (
            "edges not a list",
            json.dumps({"nodes": [{"id": "a"}], "edges": {"id": "e"}}),
            "edges must be a list",
        ),
        (
            "duplicate edge id",
            canvas(edges=[
                {"id": "e", "fromNode": "a", "toNode": "b"},
                {"id": "e", "fromNode": "b", "toNode": "a"},
            ]),
            "duplicate edge id e",
        ),
        (
            "empty node id",
            canvas(nodes=[{"id": ""}], edges=[]),
            "non-empty",
        ),
        (
            "JSON array",
            "[]",
            "JSON object",
        ),
    )
    for label, content, needle in cases:
        path = world.plant(f"{label.split()[0]}.canvas", VALID)
        before_bytes = path.read_bytes()
        before_backups = world.backups(path.name)
        completed = world.run("pre", write_body(path, content))
        world.expect_deny(f"{label} -> deny", completed, needle=needle)
        reason = ""
        if completed.stdout:
            try:
                reason = json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
            except (json.JSONDecodeError, KeyError):
                reason = completed.stdout
        world.check(
            f"{label} -> file unchanged and nothing was written",
            path.read_bytes() == before_bytes
            and "nothing was written" in reason
            and world.backups(path.name) == before_backups,
            f"reason={reason!r} backups={world.backups(path.name)!r}",
        )

    upper = world.plant("Board.CANVAS", VALID)
    world.expect_deny(
        "uppercase .CANVAS invalid JSON -> deny",
        world.run("pre", write_body(upper, "{")),
        needle="invalid JSON",
    )


def test_edits(world: World):
    broken = world.plant("edit-break.canvas", VALID)
    before = broken.read_bytes()
    backups = world.backups(broken.name)
    world.expect_deny(
        "Edit that drops a referenced node -> deny",
        world.run("pre", edit_body(
            broken,
            '{"id":"a"},{"id":"b"}',
            '{"id":"a"}',
        )),
        needle="does not name a node",
    )
    world.check(
        "breaking Edit leaves the file and backups untouched",
        broken.read_bytes() == before and world.backups(broken.name) == backups,
        f"backups={world.backups(broken.name)!r}",
    )

    kept = world.plant("edit-keep.canvas", VALID)
    world.expect_silent(
        "Edit that keeps the canvas valid -> silent",
        world.run("pre", edit_body(kept, '{"id":"a"}', '{"id":"a","label":"A"}')),
    )
    copies = world.backups(kept.name)
    world.check(
        "valid Edit backs up the existing file and does not modify it",
        kept.read_text(encoding="utf-8") == VALID and len(copies) == 1 and copies[0].read_text(encoding="utf-8") == VALID,
        f"file={kept.read_text()!r} backups={copies!r}",
    )

    missing_old = world.plant("edit-miss.canvas", VALID)
    before_backups = world.backups(missing_old.name)
    world.expect_silent(
        "Edit with old_string absent -> silent",
        world.run("pre", edit_body(missing_old, "NO-SUCH-TEXT", "x")),
    )
    world.check(
        "absent old_string does not back up or change the file",
        missing_old.read_text(encoding="utf-8") == VALID
        and world.backups(missing_old.name) == before_backups,
        f"backups={world.backups(missing_old.name)!r}",
    )

    absent_file = world.vault / "edit-absent.canvas"
    world.expect_silent(
        "Edit of a missing canvas -> silent",
        world.run("pre", edit_body(absent_file, "a", "b")),
    )
    world.check(
        "Edit of a missing canvas creates nothing",
        not absent_file.exists() and world.backups(absent_file.name) == [],
        f"exists={absent_file.exists()} backups={world.backups(absent_file.name)!r}",
    )

    multi = world.plant("multi.canvas", VALID)
    before = multi.read_bytes()
    before_backups = world.backups(multi.name)
    world.expect_deny(
        "MultiEdit producing a dangling edge -> deny",
        world.run("pre", multiedit_body(multi, [
            {"old_string": '{"id":"b"}', "new_string": '{"id":"b","label":"B"}'},
            {
                "old_string": '{"id":"a"},{"id":"b","label":"B"}',
                "new_string": '{"id":"a"}',
            },
        ])),
        needle="does not name a node",
    )
    world.check(
        "invalid MultiEdit leaves the file unchanged",
        multi.read_bytes() == before and world.backups(multi.name) == before_backups,
        f"backups={world.backups(multi.name)!r}",
    )

    twice_text = json.dumps({
        "nodes": [{"id": "a", "t": "z"}, {"id": "b", "t": "z"}],
        "edges": [],
    }, separators=(",", ":"))
    twice = world.plant("twice.canvas", twice_text)
    before_backups = world.backups(twice.name)
    world.expect_silent(
        "Edit old_string twice without replace_all -> silent",
        world.run("pre", edit_body(twice, '"t":"z"', '"t":"y"')),
    )
    world.check(
        "non-unique old_string does not back up",
        twice.read_text(encoding="utf-8") == twice_text
        and world.backups(twice.name) == before_backups,
        f"backups={world.backups(twice.name)!r}",
    )

    rall = world.plant("replace-all.canvas", twice_text)
    world.expect_silent(
        "Edit replace_all that stays valid -> silent",
        world.run("pre", edit_body(rall, '"t":"z"', '"t":"y"', replace_all=True)),
    )
    world.check(
        "replace_all of an existing file creates one backup",
        rall.read_text(encoding="utf-8") == twice_text and len(world.backups(rall.name)) == 1,
        f"backups={world.backups(rall.name)!r}",
    )


def test_base(world: World):
    valid = world.vault / "view.base"
    world.expect_silent(
        ".base valid YAML mapping -> silent",
        world.run("pre", write_body(valid, "name: items\n")),
    )
    mapping = world.vault / "empty-map.base"
    world.expect_silent(
        ".base empty mapping -> silent",
        world.run("pre", write_body(mapping, "{}\n")),
    )

    bad = world.plant("bad.base", "name: items\n")
    before = bad.read_text(encoding="utf-8")
    completed = world.run("pre", write_body(bad, "name: [\n"))
    world.expect_deny("invalid YAML -> deny", completed, needle="invalid YAML")
    reason = json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    world.check(
        "invalid YAML -> file unchanged and nothing was written",
        bad.read_text(encoding="utf-8") == before and "nothing was written" in reason,
        f"reason={reason!r} text={bad.read_text()!r}",
    )

    listed = world.plant("list.base", "name: items\n")
    before = listed.read_text(encoding="utf-8")
    completed = world.run("pre", write_body(listed, "- a\n- b\n"))
    world.expect_deny("YAML list -> deny", completed, needle="YAML mapping")
    reason = json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    world.check(
        "YAML list -> file unchanged",
        listed.read_text(encoding="utf-8") == before and "nothing was written" in reason,
        f"reason={reason!r}",
    )

    empty = world.plant("empty.base", "name: items\n")
    before = empty.read_text(encoding="utf-8")
    completed = world.run("pre", write_body(empty, ""))
    world.expect_deny("empty .base -> deny", completed, needle="empty file is invalid")
    reason = json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    world.check(
        "empty .base -> file unchanged",
        empty.read_text(encoding="utf-8") == before and "nothing was written" in reason,
        f"reason={reason!r}",
    )


def test_obsidian(world: World):
    world.expect_silent(
        "obsidian put_content relative valid canvas -> silent",
        world.run("pre", obsidian_body("put_content", "nested/ok.canvas", content=VALID)),
    )
    world.check(
        "valid put_content does not create the vault file",
        not (world.vault / "nested" / "ok.canvas").exists(),
        "file appeared",
    )

    bad = world.plant("nested/bad.canvas", VALID)
    completed = world.run("pre", obsidian_body("put_content", "nested/bad.canvas", content="{"))
    world.expect_deny("obsidian put_content invalid canvas -> deny", completed, needle="invalid JSON")
    world.check(
        "invalid put_content leaves the vault file unchanged",
        bad.read_text(encoding="utf-8") == VALID,
        f"text={bad.read_text()!r}",
    )

    for op in ("append_content", "patch_content"):
        world.expect_deny(
            f"obsidian {op} on .canvas -> deny",
            world.run("pre", obsidian_body(op, "board.canvas", content="x")),
            exact=APPEND_DENY,
        )


def test_backup(world: World):
    path = world.plant("keep-me.canvas", VALID)
    path.chmod(0o644)
    updated = json.dumps({
        "nodes": [{"id": "a", "label": "A"}, {"id": "b"}],
        "edges": [{"id": "e", "fromNode": "a", "toNode": "b"}],
    }, separators=(",", ":"))
    world.expect_silent(
        "valid Write of an existing canvas -> silent",
        world.run("pre", write_body(path, updated)),
    )
    copies = world.backups(path.name)
    world.check(
        "backup created before allow, original file unchanged",
        path.read_text(encoding="utf-8") == VALID and len(copies) == 1,
        f"file={path.read_text()!r} backups={copies!r}",
    )
    if len(copies) == 1:
        copy = copies[0]
        file_mode = stat.S_IMODE(copy.stat().st_mode)
        dir_mode = stat.S_IMODE(copy.parent.stat().st_mode)
        world.check(
            "backup file is 0600 and vault dir is 0700",
            file_mode == 0o600 and dir_mode == 0o700 and copy.read_text(encoding="utf-8") == VALID,
            f"file={oct(file_mode)} dir={oct(dir_mode)} text={copy.read_text()!r} name={copy.name}",
        )
        world.check(
            "backup name is UTC timestamp plus basename",
            BACKUP_NAME.match(copy.name) is not None and copy.name.endswith(f"-{path.name}"),
            copy.name,
        )
    else:
        world.check("backup file is 0600 and vault dir is 0700", False, "no backup")
        world.check("backup name is UTC timestamp plus basename", False, "no backup")

    fresh = world.vault / "fresh.canvas"
    before = world.backups(fresh.name)
    world.expect_silent(
        "valid Write of a new canvas -> silent",
        world.run("pre", write_body(fresh, VALID)),
    )
    world.check(
        "new file gets no backup and is not created",
        not fresh.exists() and world.backups(fresh.name) == before,
        f"exists={fresh.exists()} backups={world.backups(fresh.name)!r}",
    )


def test_skips_import(world: World):
    alone = Path(os.path.realpath(tempfile.mkdtemp(prefix="p6b-alone-")))
    world.extra.append(alone)
    copied = alone / "validate-vault.py"
    shutil.copy(HOOK, copied)

    samples = (
        ("Write notes.md", {"tool_name": "Write", "tool_input": {"file_path": "notes.md", "content": "x"}}),
        ("Write a.py", {"tool_name": "Write", "tool_input": {"file_path": str(world.project / "a.py"), "content": "x"}}),
        ("Write with no file_path", {"tool_name": "Write", "tool_input": {"content": "{}"}}),
        ("Edit with no path", {"tool_name": "Edit", "tool_input": {"old_string": "a", "new_string": "b"}}),
    )
    for label, body in samples:
        body = {"hook_event_name": "PreToolUse", **body}
        world.expect_silent(f"real script {label} -> silent", world.run("pre", body))
        world.expect_silent(
            f"script alone {label} -> silent",
            world.run("pre", body, hook=copied, cwd=alone),
        )

    canvas_body = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": "board.canvas", "content": "{}"},
    }
    world.expect_deny(
        "script alone .canvas --pre -> deny (no _paths)",
        world.run("pre", canvas_body, hook=copied, cwd=alone),
        needle="_paths",
    )
    post = world.run("post", canvas_body, hook=copied, cwd=alone)
    world.check(
        "script alone .canvas --post -> exit 0 with a stderr note",
        post.returncode == 0 and post.stdout == "" and "_paths" in post.stderr,
        detail_of(post),
    )


def test_cursor(world: World):
    fixture = FIXTURE.read_text(encoding="utf-8")
    pre = world.run("pre", stdin=fixture)
    try:
        allow = json.loads(pre.stdout)
    except json.JSONDecodeError:
        allow = None
    world.check(
        "cursor preToolUse --pre -> allow",
        pre.returncode == 0 and allow == {"permission": "allow"} and pre.stderr == "",
        detail_of(pre),
    )

    post_event = json.loads(fixture)
    post_event["hook_event_name"] = "postToolUse"
    post = world.run("post", stdin=json.dumps(post_event))
    world.expect_silent("cursor postToolUse --post -> silent", post)

    same = world.run("post", stdin=fixture)
    try:
        same_allow = json.loads(same.stdout)
    except json.JSONDecodeError:
        same_allow = None
    world.check(
        "cursor preToolUse event allows under --post too",
        same.returncode == 0 and same_allow == {"permission": "allow"} and same.stderr == "",
        detail_of(same),
    )

    cursor_canvas = write_body(world.vault / "cursor.canvas", "{")
    cursor_canvas["cursor_version"] = "2026.10.22-test"
    cursor_canvas["hook_event_name"] = "preToolUse"
    skipped = world.run("pre", cursor_canvas)
    try:
        skipped_allow = json.loads(skipped.stdout)
    except json.JSONDecodeError:
        skipped_allow = None
    world.check(
        "cursor preToolUse skips canvas validation",
        skipped.returncode == 0 and skipped_allow == {"permission": "allow"},
        detail_of(skipped),
    )

    alone = Path(os.path.realpath(tempfile.mkdtemp(prefix="p6b-cursor-alone-")))
    world.extra.append(alone)
    copied = alone / "validate-vault.py"
    shutil.copy(HOOK, copied)
    alone_pre = world.run("pre", stdin=fixture, hook=copied, cwd=alone)
    alone_post = world.run("post", stdin=json.dumps(post_event), hook=copied, cwd=alone)
    try:
        alone_allow = json.loads(alone_pre.stdout)
    except json.JSONDecodeError:
        alone_allow = None
    world.check(
        "cursor payloads work with the script copied alone",
        alone_pre.returncode == 0
        and alone_allow == {"permission": "allow"}
        and alone_pre.stderr == ""
        and alone_post.returncode == 0
        and alone_post.stdout == ""
        and alone_post.stderr == "",
        f"pre={detail_of(alone_pre)} post={detail_of(alone_post)}",
    )


def test_outside(world: World):
    path = world.project / "out.canvas"
    path.write_text(VALID, encoding="utf-8")
    world.expect_silent(
        ".canvas in the project (not a vault) -> silent",
        world.run("pre", write_body(path, "{")),
    )
    world.check(
        "outside canvas is left unchanged",
        path.read_text(encoding="utf-8") == VALID and world.backups(path.name) == [],
        f"text={path.read_text()!r} backups={world.backups(path.name)!r}",
    )


def test_post(world: World):
    good = world.plant("post-ok.canvas", VALID)
    payload = write_body(good, "{")
    payload["hook_event_name"] = "PostToolUse"
    world.expect_silent(
        "--post valid saved canvas -> silent (ignores tool content)",
        world.run("post", payload),
    )

    bad = world.plant("post-bad.canvas", "{")
    directory = world.home / ".claude" / "backup" / "vault"
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    older = directory / "20200101T000000000000-post-bad.canvas"
    newer = directory / "20260101T000000000000-post-bad.canvas"
    decoy = directory / "20270101T000000000000-other.canvas"
    older.write_text("older\n", encoding="utf-8")
    newer.write_text("newer\n", encoding="utf-8")
    decoy.write_text("decoy\n", encoding="utf-8")
    payload = write_body(bad, VALID)
    payload["hook_event_name"] = "PostToolUse"
    completed = world.run("post", payload)
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        data = {}
    reason = data.get("reason", "")
    world.check(
        "--post invalid saved canvas -> decision block with newest backup",
        completed.returncode == 0
        and completed.stderr == ""
        and data.get("decision") == "block"
        and str(bad) in reason
        and "is now invalid:" in reason
        and "Restore it from the latest backup in ~/.claude/backup/vault/" in reason
        and str(newer) in reason
        and str(older) not in reason
        and str(decoy) not in reason,
        detail_of(completed),
    )

    none = world.plant("post-none.canvas", "{")
    payload = write_body(none, VALID)
    payload["hook_event_name"] = "PostToolUse"
    completed = world.run("post", payload)
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        data = {}
    reason = data.get("reason", "")
    world.check(
        "--post invalid canvas with no backup names none",
        completed.returncode == 0
        and data.get("decision") == "block"
        and "(none)" in reason
        and str(none) in reason,
        detail_of(completed),
    )

    missing = world.vault / "post-missing.canvas"
    payload = write_body(missing, VALID)
    payload["hook_event_name"] = "PostToolUse"
    completed = world.run("post", payload)
    world.check(
        "--post missing file -> exit 0 stderr note, no block",
        completed.returncode == 0
        and completed.stdout == ""
        and completed.stderr != ""
        and "decision" not in completed.stdout,
        detail_of(completed),
    )

    base = world.plant("post-ok.base", "name: items\n")
    payload = write_body(base, "- a\n")
    payload["hook_event_name"] = "PostToolUse"
    world.expect_silent("--post valid saved .base -> silent", world.run("post", payload))


def test_malformed(world: World):
    samples = (
        ("--pre malformed JSON mentioning .canvas -> exit 2", "pre", '{"file_path": "a.canvas"', 2, True),
        ("--pre malformed JSON mentioning .base -> exit 2", "pre", "not-json .base {", 2, True),
        ("--pre malformed JSON without canvas or base -> exit 0", "pre", "{", 0, False),
        ("--pre empty stdin -> exit 0", "pre", "", 0, False),
        ("--pre non-json text -> exit 0", "pre", "not-json", 0, False),
        ("--post malformed JSON mentioning .canvas -> exit 0", "post", '{"file_path": "a.canvas"', 0, False),
    )
    for name, mode, text, code, needs_stderr in samples:
        completed = world.run(mode, stdin=text)
        if needs_stderr:
            ok = completed.returncode == code and completed.stdout == "" and completed.stderr != ""
        else:
            ok = (
                completed.returncode == code
                and completed.stdout == ""
                and completed.stderr == ""
            )
        world.check(name, ok, detail_of(completed))


def main():
    world = World()
    try:
        test_canvas_writes(world)
        test_edits(world)
        test_base(world)
        test_obsidian(world)
        test_backup(world)
        test_skips_import(world)
        test_cursor(world)
        test_outside(world)
        test_post(world)
        test_malformed(world)
    finally:
        world.cleanup()
    print(f"Tally: {world.passed} passed, {world.failed} failed")
    return 0 if world.failed == 0 and world.passed > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
