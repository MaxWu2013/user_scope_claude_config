#!/usr/bin/env python3
"""Validate Obsidian .canvas and .base files before and after a write.

--pre (PreToolUse): deny an invalid would-be file, and back up an existing
file before a valid write is allowed. --post (PostToolUse): block when the
saved file is no longer valid. Not registered.

Cursor payloads are answered before any local import. Other extensions exit
immediately so this hook stays off the path of ordinary writes.
"""

import datetime
import json
import os
import re
import sys

sys.dont_write_bytecode = True

OBSIDIAN_PREFIX = "mcp__mcp-obsidian__obsidian_"
APPEND_TOOLS = {
    "mcp__mcp-obsidian__obsidian_append_content",
    "mcp__mcp-obsidian__obsidian_patch_content",
}
PUT_TOOLS = {
    "Write",
    "mcp__mcp-obsidian__obsidian_put_content",
}
APPEND_DENY = (
    "append/patch is refused for .canvas/.base; "
    "write the whole file with put_content or Write."
)
_BACKUP_NAME = re.compile(r"^(\d{8}T\d{12})-(.+)$")


def mode_from(argv) -> str:
    mode = "pre"
    for arg in argv[1:]:
        if arg == "--pre":
            mode = "pre"
        elif arg == "--post":
            mode = "post"
    return mode


def path_extension(path: str) -> str:
    base = os.path.basename(path).lower()
    if base.endswith(".canvas"):
        return ".canvas"
    if base.endswith(".base"):
        return ".base"
    return ""


def target_extension(tool: str, ti) -> str:
    if not isinstance(ti, dict):
        return ""
    raw = ti.get("file_path") or ti.get("notebook_path")
    if not raw and tool.startswith(OBSIDIAN_PREFIX):
        raw = ti.get("filepath")
    if not isinstance(raw, str) or raw == "":
        return ""
    return path_extension(raw)


def deny(reason: str) -> int:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }), flush=True)
    return 0


def deny_invalid(error) -> int:
    text = str(error).strip()
    if not text.endswith("."):
        text += "."
    return deny(f"{text} nothing was written")


def backup_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "backup", "vault")


def locate(lex: str, real: str):
    if os.path.isfile(lex):
        return lex
    if real != lex and os.path.isfile(real):
        return real
    return None


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def copy_backup(src: str, basename: str) -> str:
    directory = backup_dir()
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    with open(src, "rb") as handle:
        data = handle.read()
    last_error = None
    for _ in range(5):
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        dest = os.path.join(directory, f"{stamp}-{basename}")
        try:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            last_error = exc
            continue
        try:
            os.write(fd, data)
        except Exception:
            os.close(fd)
            try:
                os.unlink(dest)
            except OSError:
                pass
            raise
        os.close(fd)
        os.chmod(dest, 0o600)
        return dest
    raise OSError(f"could not allocate a backup name: {last_error}")


def backup_if_exists(lex: str, real: str) -> int:
    src = locate(lex, real)
    if src is None:
        return 0
    try:
        copy_backup(src, os.path.basename(lex))
    except Exception:
        return deny(f"could not back up {lex}")
    return 0


def newest_backup(basename: str):
    directory = backup_dir()
    if not os.path.isdir(directory):
        return None
    best_key = ""
    best_path = None
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    for name in names:
        match = _BACKUP_NAME.match(name)
        if not match or match.group(2) != basename:
            continue
        if match.group(1) >= best_key:
            best_key = match.group(1)
            best_path = os.path.join(directory, name)
    return best_path


def validate_canvas(text: str):
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return f"invalid JSON: {exc}"
    if not isinstance(data, dict):
        return "canvas must be a JSON object"
    if "nodes" in data and not isinstance(data["nodes"], list):
        return "nodes must be a list"
    if "edges" in data and not isinstance(data["edges"], list):
        return "edges must be a list"
    nodes = data["nodes"] if "nodes" in data else []
    edges = data["edges"] if "edges" in data else []

    seen_nodes = set()
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            return f"node {index} must be an object"
        node_id = node.get("id")
        if not isinstance(node_id, str) or node_id == "":
            return f"node {index} id must be a non-empty string"
        if node_id in seen_nodes:
            return f"duplicate node id {node_id}"
        seen_nodes.add(node_id)

    seen_edges = set()
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            return f"edge {index} must be an object"
        edge_id = edge.get("id")
        if not isinstance(edge_id, str):
            return f"edge {index} id must be a string"
        source = edge.get("fromNode")
        target = edge.get("toNode")
        if not isinstance(source, str):
            return f"edge {index} fromNode must be a string"
        if not isinstance(target, str):
            return f"edge {index} toNode must be a string"
        if source not in seen_nodes:
            return f"edge {edge_id} fromNode {source} does not name a node"
        if target not in seen_nodes:
            return f"edge {edge_id} toNode {target} does not name a node"
        if edge_id in seen_edges:
            return f"duplicate edge id {edge_id}"
        seen_edges.add(edge_id)
    return None


def validate_base(text: str):
    if text.strip() == "":
        return "empty file is invalid"
    import yaml
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return f"invalid YAML: {exc}"
    if not isinstance(data, dict):
        return "base must be a YAML mapping"
    return None


def validate(ext: str, text: str):
    if ext == ".canvas":
        return validate_canvas(text)
    if ext == ".base":
        return validate_base(text)
    return f"unsupported extension {ext}"


def apply_edits(text: str, edits):
    """Return the edited text, or None when an edit would not apply."""
    for edit in edits:
        if not isinstance(edit, dict):
            return None
        old = edit.get("old_string")
        new = edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str) or old == "":
            return None
        count = text.count(old)
        if edit.get("replace_all") is True:
            if count < 1:
                return None
            text = text.replace(old, new)
        elif count != 1:
            return None
        else:
            text = text.replace(old, new, 1)
    return text


def pre_check(tool: str, ti: dict, lex: str, real: str) -> int:
    if tool in APPEND_TOOLS:
        return deny(APPEND_DENY)
    ext = path_extension(lex)
    if tool in PUT_TOOLS:
        content = ti.get("content")
        if not isinstance(content, str):
            return deny_invalid("content must be a string")
        error = validate(ext, content)
        if error:
            return deny_invalid(error)
        return backup_if_exists(lex, real)
    if tool in ("Edit", "MultiEdit"):
        located = locate(lex, real)
        if located is None:
            return 0
        if tool == "MultiEdit":
            edits = ti.get("edits")
            if not isinstance(edits, list):
                return deny_invalid("MultiEdit edits must be a list")
        else:
            edits = [ti]
        try:
            text = read_text(located)
        except UnicodeDecodeError as exc:
            return deny_invalid(f"invalid text: {exc}")
        updated = apply_edits(text, edits)
        if updated is None:
            return 0
        error = validate(ext, updated)
        if error:
            return deny_invalid(error)
        return backup_if_exists(lex, real)
    return deny_invalid(f"cannot validate {tool}")


def post_check(lex: str, real: str) -> int:
    ext = path_extension(lex)
    src = locate(lex, real)
    if src is None:
        raise FileNotFoundError(lex)
    try:
        text = read_text(src)
    except UnicodeDecodeError as exc:
        error = f"invalid text: {exc}"
    else:
        error = validate(ext, text)
    if not error:
        return 0
    backup = newest_backup(os.path.basename(lex)) or "none"
    reason = (
        f"{lex} is now invalid: {error}. "
        f"Restore it from the latest backup in ~/.claude/backup/vault/ ({backup})."
    )
    print(json.dumps({"decision": "block", "reason": reason}), flush=True)
    return 0


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv
    mode = mode_from(argv)
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    try:
        data = json.loads(raw)
    except Exception:
        if mode == "post":
            return 0
        if ".canvas" in raw or ".base" in raw:
            print(
                "validate-vault: malformed JSON mentions .canvas or .base",
                file=sys.stderr,
            )
            return 2
        return 0

    if not isinstance(data, dict):
        return 0

    # Cursor is allowed before any local import. The allow JSON is only the
    # preToolUse permission response; every other event exits with no output.
    version = data.get("cursor_version")
    if isinstance(version, str) and version.strip():
        if data.get("hook_event_name") == "preToolUse":
            print(json.dumps({"permission": "allow"}), flush=True)
        return 0

    tool = data.get("tool_name")
    if not isinstance(tool, str):
        tool = ""
    ext = target_extension(tool, data.get("tool_input"))
    if ext not in (".canvas", ".base"):
        return 0

    try:
        root = os.path.dirname(os.path.realpath(__file__))
        if root not in sys.path:
            sys.path.insert(0, root)
        from _paths import classify_write_target, resolve_target
    except Exception as exc:
        if mode == "post":
            print(f"validate-vault: could not import _paths: {exc}", file=sys.stderr)
            return 0
        return deny(f"could not import _paths: {exc}. nothing was written")

    try:
        ti = data.get("tool_input")
        if not isinstance(ti, dict):
            ti = {}
        if classify_write_target(tool, ti) != "vault_structured":
            return 0
        found = resolve_target(tool, ti)
        if not found:
            raise RuntimeError("could not resolve the target path")
        lex, real = found
        if mode == "post":
            return post_check(lex, real)
        return pre_check(tool, ti, lex, real)
    except Exception as exc:
        if mode == "post":
            print(f"validate-vault: {exc}", file=sys.stderr)
            return 0
        return deny(f"{exc}. nothing was written")


if __name__ == "__main__":
    sys.exit(main())
