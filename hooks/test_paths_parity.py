#!/usr/bin/env python3
"""Parity: looks_like_code_target matches the f39e923 baseline.

classify_write_target now returns phase-2 classes (control, outside, state, …),
so this file compares looks_like_code_target to the baseline instead of that
function's code/document split.
"""

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

BASELINE = "f39e923"
HOOK_DIR = Path(__file__).resolve().parent

PATHS = [
    "",
    "README.md",
    "readme.MD",
    "notes.mdx",
    "a/b/c.md",
    "docs/guide.md",
    "docs/plan.txt",
    "docs/sub/dir/notes.txt",
    "docs/app.css",
    "docs/demo/index.html",
    "docs/config.yaml",
    "docs/data.json",
    "docs/Makefile",
    "src/docs/note.txt",
    "mydocs/note.txt",
    "notes.txt",
    "Makefile",
    "scripts/deploy",
    ".gitignore",
    ".env",
    "src/a.ts",
    "src/App.vue",
    "lib/a.dart",
    "a.py",
    "a.sh",
    "pubspec.yaml",
    "package.json",
    "app.gradle",
    "Info.plist",
    "x.pbxproj",
    "/abs/docs/x.txt",
    "/abs/README.md",
    "C:\\repo\\docs\\x.txt",
    "docs\\x.txt",
    "x.MD.py",
    "x.py.md",
    "board.canvas",
    "Untitled.base",
    "img.png",
    ".claude/specs/t.diff",
    "~/.claude/settings.json",
    "../other/docs/x.txt",
]

NOTEBOOK_PATHS = ["src/a.ts", "README.md", "docs/plan.txt"]


def git_show(spec):
    return subprocess.check_output(
        ["git", "show", spec],
        cwd=HOOK_DIR,
        text=True,
    )


def load_baseline(tmp):
    (tmp / "require-cursor-agent.py").write_text(
        git_show(f"{BASELINE}:require-cursor-agent.py"),
        encoding="utf-8",
    )
    (tmp / "_agent_def.py").write_text(
        git_show(f"{BASELINE}:_agent_def.py"),
        encoding="utf-8",
    )
    sys.path.insert(0, str(tmp))
    spec = importlib.util.spec_from_file_location(
        "baseline_require_cursor_agent",
        tmp / "require-cursor-agent.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_paths():
    spec = importlib.util.spec_from_file_location("_paths", HOOK_DIR / "_paths.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    paths_mod = load_paths()
    rows = []
    failed = 0

    with tempfile.TemporaryDirectory() as temporary:
        baseline = load_baseline(Path(temporary))
        checks = [(p, {"file_path": p}, p) for p in PATHS]
        checks.extend(
            (p, {"notebook_path": p}, f"{p} [notebook_path]") for p in NOTEBOOK_PATHS
        )
        for path, _tool_input, label in checks:
            expected = "code" if baseline.looks_like_code_target(path) else "document"
            actual = "code" if paths_mod.looks_like_code_target(path) else "document"
            ok = actual == expected
            if not ok:
                failed += 1
            rows.append((label, expected, actual, "PASS" if ok else "FAIL"))

    width = max(len("path"), *(len(repr(label)) for label, _, _, _ in rows))
    print(f"{'path':<{width}}  {'baseline':<10}  {'new':<10}  result")
    for label, expected, actual, result in rows:
        print(f"{repr(label):<{width}}  {expected:<10}  {actual:<10}  {result}")
    passed = len(rows) - failed
    print(f"Tally: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
