"""Control-plane snapshot hashing. Stdlib and _paths only."""

import hashlib
import json
import os
from pathlib import Path

import _paths

# Dropped before hashing ~/.cursor/cli-config.json. Model and the privacy
# cache change on their own; they are not routing-control edits.
CLI_CONFIG_VOLATILE = frozenset({"model", "privacyCache"})

_PRUNE_DIR_NAMES = frozenset({".git", "__pycache__"})


def _home() -> str:
    # Call time, not import time: tests point HOME at a temp directory.
    return os.path.expanduser("~")


def _cli_config_path(home: str) -> str:
    return os.path.normpath(os.path.join(home, ".cursor", "cli-config.json"))


def _is_glob(pattern: str) -> bool:
    return any(ch in pattern for ch in "*?[]")


def _expand_entry(entry: str, proj: str, home: str) -> str:
    pattern = entry.replace("{P}", proj)
    if pattern.startswith("~"):
        pattern = home + pattern[1:]
    return pattern


def _excluded(path: Path) -> bool:
    """True for paths under .git/, __pycache__/, or .claude/specs/."""
    parts = path.parts
    last = len(parts) - 1
    for index, part in enumerate(parts):
        if part in _PRUNE_DIR_NAMES and index < last:
            return True
        if (
            part == ".claude"
            and index + 1 < last
            and parts[index + 1] == "specs"
        ):
            return True
        if (
            part == ".claude"
            and index + 2 < len(parts)
            and parts[index + 1] == "logs"
            and parts[index + 2] == "opus-arbitration"
        ):
            return True
    return False


def _pruned_dir(path: Path) -> bool:
    try:
        if not path.is_dir():
            return False
    except OSError:
        return False
    name = path.name
    if name in _PRUNE_DIR_NAMES:
        return True
    return name == "specs" and path.parent.name == ".claude"


def _ensure_readable_dir(path: Path) -> None:
    """pathlib glob skips children of an unreadable directory without raising."""
    if path.is_symlink():
        return
    try:
        with os.scandir(path) as listing:
            for _entry in listing:
                break
    except OSError as exc:
        raise OSError(f"unreadable directory: {path}") from exc


def _glob_paths(pattern: str) -> list[Path]:
    parts = Path(pattern).parts
    for index, part in enumerate(parts):
        if not _is_glob(part):
            continue
        base = Path(*parts[:index])
        relative = str(Path(*parts[index:]))
        if not base.exists() or not base.is_dir():
            return []
        return list(base.glob(relative))
    return [Path(pattern)]


def _digest(path: Path, home: str) -> str:
    key = os.path.normpath(os.fspath(path))
    if not os.path.lexists(key):
        return "absent"
    if key == _cli_config_path(home):
        return _digest_cli_config(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest_cli_config(path: Path) -> str:
    data = path.read_bytes()
    try:
        obj = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid cli-config JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("cli-config JSON is not an object")
    for key in CLI_CONFIG_VOLATILE:
        obj.pop(key, None)
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _consider(path: Path, home: str, out: dict[str, str]) -> None:
    if _pruned_dir(path) or _excluded(path):
        return
    if path.is_dir():
        _ensure_readable_dir(path)
        return
    key = os.path.normpath(os.fspath(path))
    out[key] = _digest(path, home)


def hash_control_plane(proj: str) -> dict[str, str]:
    """Map each control-plane path to a sha256 hex digest, or "absent".

    Raises on unreadable files, invalid cli-config JSON, or a non-object
    cli-config. Never returns a partial map.
    """
    home = _home()
    root = os.path.realpath(proj)
    out: dict[str, str] = {}
    for entry in _paths.REGISTRY["control"]:
        pattern = _expand_entry(entry, root, home)
        if _is_glob(pattern):
            for match in _glob_paths(pattern):
                _consider(match, home, out)
        else:
            _consider(Path(pattern), home, out)
    return out


def diff_hashes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Sorted paths whose digest changed, including additions and deletions."""
    keys = set(before) | set(after)
    return sorted(key for key in keys if before.get(key) != after.get(key))
