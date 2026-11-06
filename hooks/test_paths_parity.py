#!/usr/bin/env python3
"""Parity: looks_like_code_target matches a frozen expected table.

classify_write_target now returns phase-2 classes (control, outside, state, …),
so this file compares looks_like_code_target to the frozen table instead of
that function's code/document split.

Baseline commit f39e923 is lost (not in ~/.claude history or origin/main).
The table is frozen from _paths.py at commit abce16b (2026-11-04).
"""

import importlib.util
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True

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
    "~/.claude/agents/opus-planner.md",
]

NOTEBOOK_PATHS = ["src/a.ts", "README.md", "docs/plan.txt"]

# Frozen from _paths.looks_like_code_target at abce16b (2026-11-04).
# Documents are *.md / *.mdx, and non-code files under docs/. Everything else
# is code. board.canvas and Untitled.base are code: looks_like_code_target does
# not treat .canvas / .base as documents (vault copies are vault_structured
# only inside _classify).
EXPECTED = {
    "": "code",
    "README.md": "document",
    "readme.MD": "document",
    "notes.mdx": "document",
    "a/b/c.md": "document",
    "docs/guide.md": "document",
    "docs/plan.txt": "document",
    "docs/sub/dir/notes.txt": "document",
    "docs/app.css": "code",
    "docs/demo/index.html": "code",
    "docs/config.yaml": "code",
    "docs/data.json": "code",
    "docs/Makefile": "document",
    "src/docs/note.txt": "document",
    "mydocs/note.txt": "code",
    "notes.txt": "code",
    "Makefile": "code",
    "scripts/deploy": "code",
    ".gitignore": "code",
    ".env": "code",
    "src/a.ts": "code",
    "src/App.vue": "code",
    "lib/a.dart": "code",
    "a.py": "code",
    "a.sh": "code",
    "pubspec.yaml": "code",
    "package.json": "code",
    "app.gradle": "code",
    "Info.plist": "code",
    "x.pbxproj": "code",
    "/abs/docs/x.txt": "document",
    "/abs/README.md": "document",
    "C:\\repo\\docs\\x.txt": "document",
    "docs\\x.txt": "document",
    "x.MD.py": "code",
    "x.py.md": "document",
    "board.canvas": "code",
    "Untitled.base": "code",
    "img.png": "code",
    ".claude/specs/t.diff": "code",
    "~/.claude/settings.json": "code",
    "../other/docs/x.txt": "document",
    "~/.claude/agents/opus-planner.md": "document",
}


def load_paths():
    spec = importlib.util.spec_from_file_location("_paths", HOOK_DIR / "_paths.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def classify_cases(paths_mod):
    """InfoTank source.json is data. The neighbouring paths stay code."""
    home = paths_mod.HOME
    tank = os.path.join(home, "workplace", "OlaChat", "InfoTank", "data")
    other = os.path.join(home, "workplace", "OtherRepo", "data", "foo", "source.json")
    return [
        (os.path.join(tank, "foo", "source.json"), "data"),
        (os.path.join(tank, "foo", "bar.json"), "code"),
        (os.path.join(tank, "source.json"), "code"),
        (os.path.join(tank, "a", "b", "source.json"), "code"),
        (os.path.join(tank, "..", "x", "source.json"), "code"),
        (other, "code"),
        (os.path.join(home, "workplace", "OlaChat", "InfoTank", "notes.json"), "code"),
    ]


def main():
    paths_mod = load_paths()
    rows = []
    failed = 0

    checks = [(p, {"file_path": p}, p) for p in PATHS]
    checks.extend(
        (p, {"notebook_path": p}, f"{p} [notebook_path]") for p in NOTEBOOK_PATHS
    )
    for path, _tool_input, label in checks:
        expected = EXPECTED[path]
        actual = "code" if paths_mod.looks_like_code_target(path) else "document"
        ok = actual == expected
        if not ok:
            failed += 1
        rows.append((label, expected, actual, "PASS" if ok else "FAIL"))

    for path, expected in classify_cases(paths_mod):
        actual = paths_mod.classify_write_target("Write", {"file_path": path})
        ok = actual == expected
        if not ok:
            failed += 1
        rows.append((path, expected, actual, "PASS" if ok else "FAIL"))

    width = max(len("path"), *(len(repr(label)) for label, _, _, _ in rows))
    print(f"{'path':<{width}}  {'baseline':<10}  {'new':<10}  result")
    for label, expected, actual, result in rows:
        print(f"{repr(label):<{width}}  {expected:<10}  {actual:<10}  {result}")
    passed = len(rows) - failed
    print(f"Tally: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
