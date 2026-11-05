"""Agent model resolution shared by route-tools and the Opus gate.

The functions moved out of route-tools.py keep their previous behavior.
Gate-only helpers below them map inherit and plugin agents to a family.
"""

import contextvars
import json
import os
import re
import stat
from contextlib import contextmanager

_AGENT_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
# Frontmatter is at the top. Cap the read so a huge agent file cannot stall the hook.
_AGENT_DEF_CAP = 65536
_TRANSCRIPT_CAP = 256 * 1024
_MAIN_TRANSCRIPT_CAP = 2 * 1024 * 1024
_PLUGINS_CAP = 2 * 1024 * 1024
_FAMILIES = ("opus", "sonnet", "haiku")
_FRONTMATTER_LINES = 200
_SCRIPT_CAP = 512 * 1024
_KNOWN_BUILTINS = frozenset({
    "general-purpose",
    "Plan",
    "claude",
    "Explore",
    "statusline-setup",
    "claude-code-guide",
})
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_STRICT_FRONTMATTER_KEYS = frozenset({"model", "context", "tools", "agent"})
_SKILL_CAP = 256 * 1024
_CONTEXT_KEY_RE = re.compile(r"""^\s*["']?context["']?\s*:""", re.IGNORECASE)
_STRICT_IO = contextvars.ContextVar("agent_strict_io", default=False)


def _model_family(text):
    """opus, sonnet, haiku, inherit, or unknown.

    inherit is the whole token. The other families match an id that contains
    the name. opus wins when a string contains more than one family.
    """
    if not isinstance(text, str):
        return "unknown"
    low = text.strip().lower()
    if low == "":
        return "unknown"
    if "opus" in low:
        return "opus"
    if low == "inherit":
        return "inherit"
    if "sonnet" in low:
        return "sonnet"
    if "haiku" in low:
        return "haiku"
    return "unknown"


def _read_flags(nonblock=False):
    """Open flags for a definition or transcript.

    The default matches the historical route-tools read. Gate and post set
    strict IO, which adds O_NONBLOCK so a FIFO cannot stall the hook.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if nonblock or _STRICT_IO.get():
        flags |= os.O_NONBLOCK
    return flags


@contextmanager
def strict_io():
    """Regular-file reads in this block use O_NONBLOCK."""
    token = _STRICT_IO.set(True)
    try:
        yield
    finally:
        _STRICT_IO.reset(token)


def _env_for_gate():
    """Family from CLAUDE_CODE_SUBAGENT_MODEL for the gate.

    sonnet and haiku (alias or full id) are that family. A value containing
    opus is opus. Any other non-empty value is unknown. Unset or empty is
    None, and the caller keeps its previous fallback.
    """
    raw = os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL")
    if not isinstance(raw, str) or raw.strip() == "":
        return None
    family = _model_family(raw)
    if family in _FAMILIES:
        return family
    return "unknown"


def _env_subagent_family():
    """sonnet or haiku from CLAUDE_CODE_SUBAGENT_MODEL, else None.

    An opus or unknown env value does not count: the caller falls through to
    inherit-main. A blank value is unset.
    """
    raw = os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL")
    if not isinstance(raw, str) or raw.strip() == "":
        return None
    family = _model_family(raw)
    if family in ("sonnet", "haiku"):
        return family
    return None


def _agent_project(d):
    """Project whose .claude/agents directory wins over the user directory.

    Payload cwd, then CLAUDE_PROJECT_DIR, then the process cwd. This matches
    project_dir() for the env and process cwd, and also honours the cwd field
    Claude puts on the hook payload.
    """
    raw_cwd = d.get("cwd") if isinstance(d, dict) else None
    if isinstance(raw_cwd, str) and raw_cwd.strip():
        text = os.path.expanduser(raw_cwd.strip().replace("\\", "/"))
        if not os.path.isabs(text):
            base = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
            text = os.path.join(base, text)
        return os.path.realpath(text)
    raw = os.environ.get("CLAUDE_PROJECT_DIR")
    if isinstance(raw, str) and raw.strip():
        return os.path.realpath(raw)
    return os.path.realpath(os.getcwd())


def _read_frontmatter_model(path):
    """("ok", model or None) or ("fail", None).

    None means the frontmatter closed and had no model line. A missing file
    is the caller's concern. Unreadable, not a regular file, too large to
    hold a closed frontmatter, or malformed frontmatter is fail.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return "fail", None
    if not stat.S_ISREG(info.st_mode):
        return "fail", None
    try:
        fd = os.open(path, _read_flags() | os.O_NONBLOCK)
    except OSError:
        return "fail", None
    try:
        try:
            blob = os.read(fd, _AGENT_DEF_CAP)
        except OSError:
            return "fail", None
    finally:
        os.close(fd)
    text = blob.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "fail", None
    closing = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            closing = index
            break
    if closing is None:
        return "fail", None
    for line in lines[1:closing]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() != "model":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1].strip()
        return "ok", value or None
    return "ok", None


def _normalize_frontmatter_key(key):
    """Strip surrounding space and one matching quote pair, then lowercase."""
    text = key.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    return text.lower()


def _strip_unquoted_comment(raw):
    """Drop a trailing comment whose # is outside quotes and after whitespace."""
    if not isinstance(raw, str):
        return ""
    quote = None
    for index, ch in enumerate(raw):
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == "#" and index > 0 and raw[index - 1].isspace():
            return raw[:index]
    return raw


def _classify_frontmatter_value(raw):
    """(kind, value) for one strict frontmatter value.

    A trailing ` #comment` outside quotes is removed first. Whitespace and one
    matching quote pair are then stripped. kind is empty, scalar, flow, block,
    or bad-quote. block and bad-quote have no value. flow keeps the raw text
    so tools: [] can be checked exactly.
    """
    text = _strip_unquoted_comment(raw).strip()
    if text == "":
        return "empty", None
    if text[0] in "|>":
        return "block", None
    if text[0] in "[{":
        return "flow", text
    if text[0] in "'\"":
        quote = text[0]
        end = text.find(quote, 1)
        if end < 0 or text[end + 1 :].strip() != "":
            return "bad-quote", None
        return "scalar", text[1:end]
    return "scalar", text


def _model_scalar(value):
    """A single model token, or unknown when trailing text remains."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text == "":
        return None
    if any(ch.isspace() for ch in text):
        return "unknown"
    return text


def _parse_strict_frontmatter(text):
    """("ok", fields) or ("fail", None) for one definition or skill body.

    A leading UTF-8 BOM is stripped, and blank lines may precede the opening
    delimiter. Any line whose rstrip() is --- opens or closes the frontmatter.
    At most 200 lines from the opening delimiter. Keys are stripped, unquoted
    once, and lowercased. model, context, tools, and agent count on any line,
    including an empty value. A repeated one of those keys, or an indented
    line whose key is one of them, is fail. A repeated top-level key, a block
    or flow model, or an unclosed quote is fail. A model value with trailing
    text is the token unknown. context is lowercased. tools: [] is the empty
    list; other flow values stay raw text.
    """
    if not isinstance(text, str):
        return "fail", None
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines()
    start = 0
    while start < len(lines) and lines[start].strip() == "":
        start += 1
    if start >= len(lines) or lines[start].rstrip() != "---":
        return "fail", None
    limit = min(len(lines), start + _FRONTMATTER_LINES)
    closing = None
    for index in range(start + 1, limit):
        if lines[index].rstrip() == "---":
            closing = index
            break
    if closing is None:
        return "fail", None
    fields = {}
    for line in lines[start + 1 : closing]:
        if line == "" or ":" not in line:
            continue
        key, raw = line.split(":", 1)
        normalized = _normalize_frontmatter_key(key)
        if line[:1].isspace():
            if normalized in _STRICT_FRONTMATTER_KEYS:
                return "fail", None
            continue
        if normalized == "":
            continue
        if normalized in fields:
            return "fail", None
        kind, value = _classify_frontmatter_value(raw)
        if normalized == "model":
            if kind in ("block", "flow", "bad-quote"):
                return "fail", None
            fields[normalized] = _model_scalar(value) if kind == "scalar" else None
            continue
        if kind in ("block", "bad-quote"):
            return "fail", None
        if normalized == "tools" and kind == "flow" and value == "[]":
            fields[normalized] = []
            continue
        if kind == "empty":
            fields[normalized] = None
        elif normalized == "context" and isinstance(value, str):
            fields[normalized] = value.strip().lower()
        else:
            fields[normalized] = value
    return "ok", fields


def _read_strict_frontmatter(path):
    """("ok", fields) or ("fail", None).

    Agent definitions stay no-follow and at most 64 KiB. Skill files use
    _read_skill_bytes and the same parser.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return "fail", None
    if not stat.S_ISREG(info.st_mode):
        return "fail", None
    try:
        fd = os.open(path, _read_flags(nonblock=True) | os.O_NONBLOCK)
    except OSError:
        return "fail", None
    try:
        try:
            blob = os.read(fd, _AGENT_DEF_CAP + 1)
        except OSError:
            return "fail", None
    finally:
        os.close(fd)
    if len(blob) > _AGENT_DEF_CAP:
        blob = blob[:_AGENT_DEF_CAP]
    return _parse_strict_frontmatter(blob.decode("utf-8", errors="replace"))


def _read_strict_model(path):
    """("ok", model or None) or ("fail", None) using the gate frontmatter rules."""
    status, fields = _read_strict_frontmatter(path)
    if status != "ok":
        return "fail", None
    if "model" not in fields:
        return "ok", None
    value = fields.get("model")
    if not isinstance(value, str) or value.strip() == "":
        return "ok", None
    return "ok", value


def _from_definition_model(value, source):
    """Map a parsed model line to (effective_model, effective_source).

    An explicit inherit stays inherit-main even when the env default is
    sonnet or haiku: the definition beats the env default. A missing model
    line uses that env default.
    """
    if value is None:
        family = _env_subagent_family()
        if family:
            return family, "env-default"
        return "inherit-main", "builtin-inherit"
    family = _model_family(value)
    if family == "inherit":
        return "inherit-main", source
    if family in ("opus", "sonnet", "haiku"):
        return family, source
    return "unknown", source


def _lookup_definition(subagent_type, project):
    """(effective, source) when a project or user file exists, else None.

    Project wins. A file that exists but cannot be parsed is unknown, and
    the user file is not consulted after that.
    """
    if not isinstance(subagent_type, str) or not _AGENT_TYPE_RE.fullmatch(subagent_type):
        return None
    name = subagent_type + ".md"
    candidates = (
        ("project-def", os.path.join(project, ".claude", "agents", name)),
        ("user-def", os.path.join(os.path.expanduser("~"), ".claude", "agents", name)),
    )
    for source, path in candidates:
        if not os.path.lexists(path):
            continue
        status, value = _read_frontmatter_model(path)
        if status != "ok":
            return "unknown", "unknown"
        return _from_definition_model(value, source)
    return None


def effective_agent_model(d, ti, subagent_type):
    """(effective_model, effective_source) for one Agent call.

    effective_model is opus, sonnet, haiku, inherit-main, or unknown.
    effective_source is per-call, user-def, project-def, env-default,
    builtin-inherit, or unknown. inherit-main means the hook treats the
    spawn as the main model, which strict mode assumes is Opus.
    """
    raw = ti.get("model") if isinstance(ti, dict) else None
    if isinstance(raw, str) and raw.strip():
        family = _model_family(raw)
        if family == "inherit":
            return "inherit-main", "per-call"
        return family, "per-call"
    found = _lookup_definition(subagent_type, _agent_project(d))
    if found is not None:
        return found
    if subagent_type == "general-purpose":
        family = _env_subagent_family()
        if family:
            return family, "env-default"
    return "inherit-main", "builtin-inherit"


def _per_call_opus(ti):
    raw = ti.get("model") if isinstance(ti, dict) else None
    return isinstance(raw, str) and "opus" in raw.lower()


def _read_capped(path, cap):
    """Return bytes or None. Regular files only; symlinks fail closed."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    try:
        fd = os.open(path, _read_flags() | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        try:
            if info.st_size > cap:
                os.lseek(fd, info.st_size - cap, os.SEEK_SET)
                blob = os.read(fd, cap)
                newline = blob.find(b"\n")
                if newline >= 0:
                    blob = blob[newline + 1 :]
                return blob
            return os.read(fd, cap)
        except OSError:
            return None
    finally:
        os.close(fd)


def _projects_root():
    return os.path.realpath(os.path.join(os.path.expanduser("~"), ".claude", "projects"))


def _transcript_file(path):
    """Real path of a regular transcript under ~/.claude/projects/, else None."""
    if not isinstance(path, str) or path.strip() == "":
        return None
    root = _projects_root()
    try:
        real = os.path.realpath(path)
    except OSError:
        return None
    if real != root and not real.startswith(root + os.sep):
        return None
    try:
        info = os.stat(real)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return real


def _read_complete_tail(path, cap):
    """Bytes of complete lines from the last `cap` bytes, or None.

    A line counts only when it starts at the file start or after a newline.
    The partial line at the start of a tail window is dropped. No newline in
    that window means there is no complete line.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    try:
        fd = os.open(path, _read_flags() | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        try:
            if info.st_size > cap:
                os.lseek(fd, info.st_size - cap, os.SEEK_SET)
                blob = os.read(fd, cap)
                newline = blob.find(b"\n")
                if newline < 0:
                    return b""
                return blob[newline + 1 :]
            return os.read(fd, cap)
        except OSError:
            return None
    finally:
        os.close(fd)


def _last_complete_assistant_model(path):
    """Raw message.model of the last complete assistant line, else None.

    Reads at most the last 2 MiB. An incomplete line at the window edge is
    ignored. None when no complete assistant line is found.
    """
    real = _transcript_file(path)
    if real is None:
        return None
    blob = _read_complete_tail(real, _MAIN_TRANSCRIPT_CAP)
    if not blob:
        return None
    text = blob.decode("utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line == "":
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        model = message.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
    return None


def _last_assistant_model(path):
    """Raw message.model of the last assistant line, else None."""
    real = _transcript_file(path)
    if real is None:
        return None
    blob = _read_capped(real, _TRANSCRIPT_CAP)
    if blob is None:
        return None
    text = blob.decode("utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line == "":
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        model = message.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
    return None


def _session_identity_model(path):
    """identity.modelId from the head of a transcript, else None.

    Claude Code writes this attachment at session start, before the first
    assistant line is flushed. A quoted "exact model ID" sentence is not
    that attachment and is not a model.
    """
    real = _transcript_file(path)
    if real is None:
        return None
    try:
        info = os.lstat(real)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    try:
        fd = os.open(real, _read_flags() | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        try:
            blob = os.read(fd, _TRANSCRIPT_CAP)
        except OSError:
            return None
    finally:
        os.close(fd)
    text = bytes(blob).decode("utf-8", errors="replace")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "" or stripped[0] != "{":
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        model_id = _identity_model_id(obj)
        if model_id:
            return model_id
    return None


def _raw_main_model(payload):
    """Last complete assistant model, else the session-start identity."""
    if not isinstance(payload, dict):
        return None
    path = payload.get("transcript_path")
    raw = _last_complete_assistant_model(path)
    if raw is not None:
        return raw
    return _session_identity_model(path)


def main_session_model(payload):
    """opus, sonnet, haiku, or unknown from the main transcript.

    Any failure, including a model id that is not one of those families, is
    unknown. The last complete assistant line wins. Before that line exists,
    the session-start identity.modelId is the model. Unknown stays unknown.
    """
    raw = _raw_main_model(payload)
    if raw is None:
        return "unknown"
    family = _model_family(raw)
    if family in _FAMILIES:
        return family
    return "unknown"


def main_session_model_id(payload):
    """Raw assistant model id, or the session-start identity, else unknown."""
    raw = _raw_main_model(payload)
    if raw is None:
        return "unknown"
    family = _model_family(raw)
    if family in _FAMILIES:
        return raw
    return "unknown"


def _corrupt_model(value):
    """A NUL in a model id is not a model. Callers fail closed."""
    if isinstance(value, str) and "\x00" in value:
        raise RuntimeError("corrupt model definition")


def _definition_value(subagent_type, project, strict=False):
    """(source, status, model) for the winning agent file, else None.

    Same project-then-user rule as _lookup_definition. A parsed file is
    returned before the env default is applied, so the gate can map a missing
    or inherit model onto the main session. strict=True uses the gate
    frontmatter parser and does not change the route-tools reader.
    """
    if not isinstance(subagent_type, str) or not _AGENT_TYPE_RE.fullmatch(subagent_type):
        return None
    name = subagent_type + ".md"
    reader = _read_strict_model if strict else _read_frontmatter_model
    candidates = (
        ("project-def", os.path.join(project, ".claude", "agents", name)),
        ("user-def", os.path.join(os.path.expanduser("~"), ".claude", "agents", name)),
    )
    for source, path in candidates:
        if not os.path.lexists(path):
            continue
        status, value = reader(path)
        return source, status, value
    return None


def _plugins_root():
    return os.path.realpath(os.path.join(os.path.expanduser("~"), ".claude", "plugins"))


def _realpath_inside(root, path):
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    return real == root or real.startswith(root + os.sep)


def _unlinked_below(root, path):
    """False when any component of path below root is a symlink."""
    raw = os.path.abspath(path)
    if raw != root and not raw.startswith(root + os.sep):
        return False
    if raw == root:
        return True
    current = root
    for part in raw[len(root) + 1 :].split(os.sep):
        if part in ("", "."):
            continue
        if part == "..":
            return False
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode):
            return False
    return True


def resolve_plugin_definition(subagent_type, strict=False):
    """(status, model or None) for a plugin:name agent.

    status is ok or fail. Any lookup, parse, or path failure is fail.
    An ok result with model None means the frontmatter closed with no model.
    """
    try:
        if not isinstance(subagent_type, str) or ":" not in subagent_type:
            return "fail", None
        plugin, name = subagent_type.split(":", 1)
        if plugin == "" or not _AGENT_TYPE_RE.fullmatch(name):
            return "fail", None
        catalog = os.path.join(
            os.path.expanduser("~"), ".claude", "plugins", "installed_plugins.json"
        )
        blob = _read_capped(catalog, _PLUGINS_CAP)
        if blob is None:
            return "fail", None
        try:
            info = os.stat(catalog)
        except OSError:
            return "fail", None
        if info.st_size > _PLUGINS_CAP:
            return "fail", None
        data = json.loads(blob.decode("utf-8"))
        plugins = data.get("plugins") if isinstance(data, dict) else None
        if not isinstance(plugins, dict):
            return "fail", None
        installs = plugins.get(plugin)
        if installs is None:
            matched = [
                value
                for key, value in plugins.items()
                if isinstance(key, str) and key.split("@", 1)[0] == plugin
            ]
            if len(matched) != 1:
                return "fail", None
            installs = matched[0]
        if not isinstance(installs, list) or not installs:
            return "fail", None
        chosen = None
        for item in installs:
            if (
                isinstance(item, dict)
                and item.get("scope") == "user"
                and isinstance(item.get("installPath"), str)
                and item.get("installPath").strip()
            ):
                chosen = item
                break
        if chosen is None:
            for item in installs:
                if isinstance(item, dict) and isinstance(item.get("installPath"), str):
                    if item.get("installPath").strip():
                        chosen = item
                        break
        if chosen is None:
            return "fail", None
        root = _plugins_root()
        install = chosen["installPath"]
        if not _realpath_inside(root, install) or not _unlinked_below(root, install):
            return "fail", None
        agent_path = os.path.join(install, "agents", name + ".md")
        if not _realpath_inside(root, agent_path) or not _unlinked_below(root, agent_path):
            return "fail", None
        reader = _read_strict_model if strict else _read_frontmatter_model
        status, model = reader(agent_path)
        if status != "ok":
            return "fail", None
        return "ok", model
    except (OSError, ValueError, TypeError, json.JSONDecodeError, UnicodeError):
        return "fail", None


def _pack(family, source, main_model, detail):
    return {
        "family": family,
        "source": source,
        "main_model": main_model,
        "detail": detail,
    }


def _family_or_main(value, source, main_model):
    """Map a definition model line. inherit and a missing line follow main."""
    _corrupt_model(value)
    if value is None:
        return _pack(main_model, source, main_model, "no-model")
    family = _model_family(value)
    if family == "inherit":
        return _pack(main_model, source, main_model, "inherit")
    if family in _FAMILIES:
        return _pack(family, source, main_model, value.strip())
    return _pack("unknown", source, main_model, "unparsed")


def _gate_defined_family(value, source, main_model):
    """Gate mapping. A missing model line uses the gate env rule."""
    _corrupt_model(value)
    if value is None:
        family = _env_for_gate()
        if family:
            return _pack(family, "env-default", main_model, family)
        return _pack(main_model, source, main_model, "no-model")
    family = _model_family(value)
    if family == "inherit":
        return _pack(main_model, source, main_model, "inherit")
    if family in _FAMILIES:
        return _pack(family, source, main_model, value.strip())
    return _pack("unknown", source, main_model, "unparsed")


def resolve_for_gate(payload, tool_input):
    """Family the Opus gate should enforce.

    Order: per-call, project def, user def, plugin def, general-purpose env
    default, then the main session model. inherit follows the main session.
    A definition with no model line, and general-purpose, use the gate env
    rule. Unparseable input is unknown. Definitions use the strict frontmatter
    parser. The route-tools resolver is unchanged.
    """
    if not isinstance(payload, dict):
        payload = {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    main_model = main_session_model(payload)
    raw_type = tool_input.get("subagent_type")
    if not isinstance(raw_type, str) or raw_type.strip() == "":
        return _pack("unknown", "unknown", main_model, "bad-type")
    subagent_type = raw_type.strip()
    raw = tool_input.get("model")
    if raw is not None and not isinstance(raw, str):
        return _pack("unknown", "per-call", main_model, "non-string")
    if isinstance(raw, str) and raw.strip():
        _corrupt_model(raw)
        family = _model_family(raw)
        if family == "inherit":
            return _pack(main_model, "per-call", main_model, "inherit")
        if family in _FAMILIES:
            return _pack(family, "per-call", main_model, raw.strip())
        return _pack("unknown", "per-call", main_model, "unparsed")
    if ":" not in subagent_type:
        if not _AGENT_TYPE_RE.fullmatch(subagent_type):
            return _pack("unknown", "unknown", main_model, "bad-type")
        found = _definition_value(subagent_type, _agent_project(payload), strict=True)
        if found is not None:
            source, status, value = found
            if status != "ok":
                return _pack("unknown", "unknown", main_model, "def-fail")
            return _gate_defined_family(value, source, main_model)
        if subagent_type not in _KNOWN_BUILTINS:
            return _pack("unknown", "unknown", main_model, "no-definition")
    else:
        status, value = resolve_plugin_definition(subagent_type, strict=True)
        if status != "ok":
            return _pack("unknown", "plugin-def", main_model, "plugin-fail")
        return _gate_defined_family(value, "plugin-def", main_model)
    if subagent_type == "general-purpose":
        family = _env_for_gate()
        if family:
            return _pack(family, "env-default", main_model, family)
    observed = main_session_model_id(payload)
    return _pack(main_model, "builtin-inherit", main_model, observed)


def opus_possible(res):
    """True when the resolved family is Opus or not known to be lower-tier."""
    if not isinstance(res, dict):
        return True
    return res.get("family") in ("opus", "unknown")


def _baseline_defined(value, main):
    """A definition's own model. inherit and a missing model follow main."""
    if not isinstance(value, str) or value.strip() == "":
        return main
    _corrupt_model(value)
    family = _model_family(value)
    if family == "inherit":
        return main
    if family in _FAMILIES:
        return value.strip()
    return main


def _main_observed(payload):
    """Full transcript model id, or the family when the id cannot be read."""
    model_id = main_session_model_id(payload)
    if isinstance(model_id, str) and model_id not in ("", "unknown"):
        return model_id
    return main_session_model(payload)


def non_opus_baseline(payload, tool_input):
    """Model that would run if this call had not requested a model.

    This is current_model_observed for a T02 registration: the resolver's
    non-Opus baseline, not the requested family. general-purpose with no
    definition uses the env default (sonnet or haiku). A project, user, or
    plugin definition uses its own model. Everything else uses the main
    session model id from the transcript line, or that model's family when
    the id cannot be read. The per-call model is ignored.
    """
    if not isinstance(payload, dict):
        payload = {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    main = _main_observed(payload)
    raw_type = tool_input.get("subagent_type")
    if isinstance(raw_type, str) and raw_type.strip():
        subagent_type = raw_type.strip()
    else:
        subagent_type = "general-purpose"
    if ":" not in subagent_type:
        found = _definition_value(subagent_type, _agent_project(payload))
        if found is not None:
            _source, status, value = found
            if status != "ok":
                return main
            return _baseline_defined(value, main)
    else:
        status, value = resolve_plugin_definition(subagent_type)
        if status != "ok":
            return main
        return _baseline_defined(value, main)
    if subagent_type == "general-purpose":
        family = _env_subagent_family()
        if family:
            return family
    return main


def _bare_tool_token(raw):
    """One tool name, or None when the token is empty or not a single name."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    if text == "":
        return None
    if any(ch.isspace() or ch in ",[]{}#:" for ch in text):
        return None
    return text


def _flow_tool_names(text):
    """Set of names in a flow list, or None when the list is not plain names."""
    if not isinstance(text, str) or not text.startswith("[") or not text.endswith("]"):
        return None
    inner = text[1:-1].strip()
    if inner == "":
        return set()
    names = set()
    buf = []
    quote = None
    for ch in inner:
        if quote is not None:
            if ch == quote:
                quote = None
            buf.append(ch)
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch == ",":
            token = _bare_tool_token("".join(buf))
            if token is None:
                return None
            names.add(token)
            buf = []
            continue
        if ch in "[]{}":
            return None
        buf.append(ch)
    if quote is not None:
        return None
    token = _bare_tool_token("".join(buf))
    if token is None:
        return None
    names.add(token)
    return names


def _tool_name_set(value):
    """Tool names from a scalar or a flow list. None when the value is not that."""
    if isinstance(value, list):
        if value == []:
            return set()
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("["):
        return _flow_tool_names(text)
    token = _bare_tool_token(text)
    if token is None:
        return None
    return {token}


def _tools_are_exactly_read(fields):
    """True when tools resolves to exactly {Read} and disallowedTools is absent.

    Accept the scalar Read or the flow list [Read], including quotes and
    surrounding space. An empty list, an omitted field, *, extra tools, and
    any disallowedTools key are rejected.
    """
    if not isinstance(fields, dict) or "tools" not in fields:
        return False
    if "disallowedtools" in fields:
        return False
    return _tool_name_set(fields.get("tools")) == {"Read"}


def _strict_opus_tools_status(payload, agent_type):
    """ok, shadowed, or invalid for a pinned opus agent whose only tool is Read.

    A project file at <project>/.claude/agents/<agent_type>.md shadows the
    user file. The user file must be a regular file whose strict frontmatter
    has model: opus and tools resolving to exactly {Read}.
    """
    project = _agent_project(payload if isinstance(payload, dict) else {})
    project_file = os.path.join(project, ".claude", "agents", agent_type + ".md")
    if os.path.lexists(project_file):
        return "shadowed"
    user_file = os.path.join(os.path.expanduser("~"), ".claude", "agents", agent_type + ".md")
    try:
        info = os.lstat(user_file)
    except OSError:
        return "invalid"
    if not stat.S_ISREG(info.st_mode):
        return "invalid"
    status, fields = _read_strict_frontmatter(user_file)
    if status != "ok" or not isinstance(fields, dict):
        return "invalid"
    if fields.get("model") != "opus" or not _tools_are_exactly_read(fields):
        return "invalid"
    return "ok"


def judge_definition_status(payload, judge_type):
    """ok, shadowed, or invalid for the opus-judge exemption.

    The exemption needs subagent type opus-judge, no project file at
    <project>/.claude/agents/opus-judge.md, and a regular user file whose
    strict frontmatter has model: opus and tools resolving to exactly {Read}.
    """
    if judge_type != "opus-judge":
        return "invalid"
    return _strict_opus_tools_status(payload, "opus-judge")


def _plugin_agent_named(agent_type):
    """True when any installed plugin has agents/<agent_type>.md."""
    if not isinstance(agent_type, str) or not _AGENT_TYPE_RE.fullmatch(agent_type):
        return False
    catalog = os.path.join(os.path.expanduser("~"), ".claude", "plugins", "installed_plugins.json")
    blob = _read_capped(catalog, _PLUGINS_CAP)
    if blob is None:
        return False
    try:
        data = json.loads(blob.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        return False
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return False
    root = _plugins_root()
    for installs in plugins.values():
        if not isinstance(installs, list):
            continue
        for item in installs:
            if not isinstance(item, dict):
                continue
            install = item.get("installPath")
            if not isinstance(install, str) or not install.strip():
                continue
            if not _realpath_inside(root, install) or not _unlinked_below(root, install):
                continue
            parent = os.path.join(install, "agents")
            if not _realpath_inside(root, parent) or not _unlinked_below(root, parent):
                continue
            agent_path = os.path.join(parent, agent_type + ".md")
            if os.path.lexists(agent_path):
                return True
    return False


def planner_definition_status(payload, planner_type):
    """ok, PLANNER_SHADOWED, or PLANNER_DEFINITION_INVALID.

    The user definition must be model opus with tools resolving to exactly
    {Read}. A project file or a plugin agent of the same name shadows it.
    """
    if not isinstance(planner_type, str) or not _AGENT_TYPE_RE.fullmatch(planner_type):
        return "PLANNER_DEFINITION_INVALID"
    status = _strict_opus_tools_status(payload, planner_type)
    if status == "shadowed" or _plugin_agent_named(planner_type):
        return "PLANNER_SHADOWED"
    if status != "ok":
        return "PLANNER_DEFINITION_INVALID"
    return "ok"


_AGENT_CALL_RE = re.compile(r"\bagent\s*\(")
_EXACT_MODEL_RE = re.compile(r"The exact model ID is ([A-Za-z0-9][A-Za-z0-9._:-]{0,120})")
_WORKFLOW_DIR_CAP = 64


def workflow_structural_denies(tool_input):
    """True for name, resumeFromRunId, an unreadable scriptPath, or opus in the script.

    These deny for every main model. A missing script and a missing scriptPath
    are allowed here; the caller applies the main-model rule next.
    """
    if not isinstance(tool_input, dict):
        return True
    if "name" in tool_input or "resumeFromRunId" in tool_input:
        return True
    texts = []
    if "scriptPath" in tool_input:
        path = tool_input.get("scriptPath")
        if not isinstance(path, str) or path.strip() == "":
            return True
        file_text = _read_home_file(path.strip(), _SCRIPT_CAP)
        if file_text is None:
            return True
        texts.append(file_text)
    script = tool_input.get("script")
    if isinstance(script, str):
        texts.append(script)
    return any("opus" in text.lower() for text in texts)


def workflow_agent_calls(tool_input):
    """Static count of agent( calls. Loops can exceed this at runtime."""
    text = read_workflow_script(tool_input)
    if not isinstance(text, str) or text == "":
        return 0
    return len(_AGENT_CALL_RE.findall(text))


def _safe_path_id(value):
    if not isinstance(value, str) or value == "":
        return None
    if "/" in value or "\\" in value or ".." in value or value.startswith("."):
        return None
    return value


def find_workflow_transcript(transcript_path, session_id, agent_id):
    """Worker jsonl under the main transcript's session workflow directory.

    At most 64 run directories are listed. The last regular-file match wins.
    """
    session_id = _safe_path_id(session_id)
    agent_id = _safe_path_id(agent_id)
    if session_id is None or agent_id is None:
        return None
    if not isinstance(transcript_path, str) or transcript_path.strip() == "":
        return None
    root = os.path.join(os.path.dirname(transcript_path), session_id, "subagents", "workflows")
    try:
        names = os.listdir(root)
    except OSError:
        return None
    names.sort()
    target = "agent-%s.jsonl" % agent_id
    found = None
    for name in names[:_WORKFLOW_DIR_CAP]:
        path = os.path.join(root, name, target)
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            found = path
    return found


def _workflow_agent_path(path, session_id, agent_id):
    """True when path is <session>/subagents/workflows/<runId>/agent-<id>.jsonl."""
    session_id = _safe_path_id(session_id)
    agent_id = _safe_path_id(agent_id)
    if session_id is None or agent_id is None or not isinstance(path, str) or path.strip() == "":
        return False
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    parts = os.path.normpath(path).split(os.sep)
    if len(parts) < 5 or parts[-1] != "agent-%s.jsonl" % agent_id:
        return False
    run_id = parts[-2]
    if run_id in ("", ".", "..") or parts[-3] != "workflows" or parts[-4] != "subagents":
        return False
    if parts[-5] != session_id:
        return False
    return True


def _spawn_log_workflow_run(session_id, agent_id):
    """True when the mod spawn log names this agent on a workflow run."""
    session_id = _safe_path_id(session_id)
    agent_id = _safe_path_id(agent_id)
    if session_id is None or agent_id is None:
        return False
    path = os.path.join(
        os.path.expanduser("~"),
        ".claude",
        "logs",
        "opus-arbitration",
        "mod",
        "spawns-%s.jsonl" % session_id,
    )
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if text == "":
                    continue
                try:
                    row = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict) or row.get("agentId") != agent_id:
                    continue
                run_id = row.get("runId")
                if isinstance(run_id, str) and run_id.strip() != "":
                    return True
    except OSError:
        return False
    return False


def workflow_worker_evidence(payload):
    """True when the transcript location or the spawn log shows a workflow worker.

    The agent_type workflow-subagent fast path is the caller's. This is the
    extra evidence for a worker that was started with some other agent type.
    """
    if not isinstance(payload, dict):
        return False
    agent_id = payload.get("agent_id")
    if not isinstance(agent_id, str) or agent_id.strip() == "":
        return False
    agent_id = agent_id.strip()
    session_id = payload.get("session_id")
    if _workflow_agent_path(payload.get("agent_transcript_path"), session_id, agent_id):
        return True
    found = find_workflow_transcript(payload.get("transcript_path"), session_id, agent_id)
    if isinstance(found, str) and found != "":
        return True
    return _spawn_log_workflow_run(session_id, agent_id)


def _identity_model_id(obj):
    if not isinstance(obj, dict) or obj.get("type") != "attachment":
        return None
    attachment = obj.get("attachment")
    if not isinstance(attachment, dict) or attachment.get("type") != "model":
        return None
    identity = attachment.get("identity")
    if not isinstance(identity, dict):
        return None
    model_id = identity.get("modelId")
    if isinstance(model_id, str) and model_id.strip():
        return model_id.strip()
    return None


def model_id_from_transcript_head(blob):
    """First identity.modelId in a transcript head, else the system-text fallback."""
    if not isinstance(blob, (bytes, bytearray)):
        return None
    text = bytes(blob).decode("utf-8", errors="replace")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "" or stripped[0] != "{":
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        model_id = _identity_model_id(obj)
        if model_id:
            return model_id
    match = _EXACT_MODEL_RE.search(text)
    if match:
        return match.group(1)
    return None


def read_worker_model_id(path):
    """Model id from the first 256 KiB of a regular worker transcript."""
    if not isinstance(path, str) or path == "":
        return None
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        try:
            blob = os.read(fd, _TRANSCRIPT_CAP)
        except OSError:
            return None
    finally:
        os.close(fd)
    return model_id_from_transcript_head(blob)


def model_family(text):
    """opus, sonnet, haiku, inherit, or unknown."""
    return _model_family(text)


def _read_home_file(path, cap):
    """Text of a regular file under the home directory, else None."""
    if not isinstance(path, str) or path.strip() == "":
        return None
    home = os.path.realpath(os.path.expanduser("~"))
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
        return None
    try:
        real = os.path.realpath(path)
    except OSError:
        return None
    if real != home and not real.startswith(home + os.sep):
        return None
    try:
        fd = os.open(path, _read_flags(nonblock=True) | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        try:
            blob = os.read(fd, cap + 1)
        except OSError:
            return None
    finally:
        os.close(fd)
    if len(blob) > cap:
        return None
    return blob.decode("utf-8", errors="replace")


def read_workflow_script(tool_input):
    """Inline script, a confined scriptPath, or None when the script is unknown."""
    if not isinstance(tool_input, dict):
        return None
    script = tool_input.get("script")
    if isinstance(script, str):
        return script
    path = tool_input.get("scriptPath")
    if isinstance(path, str) and path.strip():
        text = _read_home_file(path.strip(), _SCRIPT_CAP)
        if text is not None:
            return text
    return None


def _plugin_install_path(plugin):
    """Install path for one catalog plugin, else None.

    Same user-scope preference and path confinement as plugin agents.
    """
    if not isinstance(plugin, str) or plugin == "" or "/" in plugin or "\\" in plugin:
        return None
    catalog = os.path.join(os.path.expanduser("~"), ".claude", "plugins", "installed_plugins.json")
    blob = _read_capped(catalog, _PLUGINS_CAP)
    if blob is None:
        return None
    try:
        info = os.stat(catalog)
        if info.st_size > _PLUGINS_CAP:
            return None
        data = json.loads(blob.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return None
    installs = plugins.get(plugin)
    if installs is None:
        matched = [
            value
            for key, value in plugins.items()
            if isinstance(key, str) and key.split("@", 1)[0] == plugin
        ]
        if len(matched) != 1:
            return None
        installs = matched[0]
    if not isinstance(installs, list) or not installs:
        return None
    chosen = None
    for item in installs:
        if (
            isinstance(item, dict)
            and item.get("scope") == "user"
            and isinstance(item.get("installPath"), str)
            and item.get("installPath").strip()
        ):
            chosen = item
            break
    if chosen is None:
        for item in installs:
            if isinstance(item, dict) and isinstance(item.get("installPath"), str):
                if item.get("installPath").strip():
                    chosen = item
                    break
    if chosen is None:
        return None
    root = _plugins_root()
    install = chosen["installPath"]
    if not _realpath_inside(root, install) or not _unlinked_below(root, install):
        return None
    return install


def _skill_file(payload, name):
    """SKILL.md for a skill name, else None."""
    if not isinstance(name, str):
        return None
    text = name.strip().lstrip("/")
    if text == "":
        return None
    if ":" in text:
        plugin, skill = text.split(":", 1)
        if not _SKILL_NAME_RE.fullmatch(skill):
            return None
        install = _plugin_install_path(plugin)
        if install is None:
            return None
        path = os.path.join(install, "skills", skill, "SKILL.md")
        root = _plugins_root()
        if not _realpath_inside(root, path) or not _unlinked_below(root, path):
            return None
        if not os.path.lexists(path):
            return None
        return path
    if not _SKILL_NAME_RE.fullmatch(text):
        return None
    project = _agent_project(payload if isinstance(payload, dict) else {})
    home = os.path.expanduser("~")
    for path in (
        os.path.join(project, ".claude", "skills", text, "SKILL.md"),
        os.path.join(home, ".claude", "skills", text, "SKILL.md"),
    ):
        if os.path.lexists(path):
            return path
    return None


def _fork_family(payload, fields):
    """Model family for a context: fork skill."""
    model = fields.get("model") if isinstance(fields, dict) else None
    if isinstance(model, str) and model.strip():
        family = _model_family(model)
        if family == "inherit":
            return main_session_model(payload)
        if family in _FAMILIES:
            return family
        return "unknown"
    agent = fields.get("agent") if isinstance(fields, dict) else None
    if isinstance(agent, str) and agent.strip():
        resolved = resolve_for_gate(payload, {"subagent_type": agent.strip()})
        family = resolved.get("family") if isinstance(resolved, dict) else None
        if family in _FAMILIES or family == "unknown":
            return family
        return "unknown"
    return main_session_model(payload)


def _read_skill_bytes(path):
    """("ok", bytes) or ("unreadable", None).

    Skills may be symlinks. The real target must be a regular file of at most
    256 KiB and is opened with O_NONBLOCK.
    """
    try:
        real = os.path.realpath(path)
    except OSError:
        return "unreadable", None
    try:
        info = os.lstat(real)
    except OSError:
        return "unreadable", None
    if not stat.S_ISREG(info.st_mode) or info.st_size > _SKILL_CAP:
        return "unreadable", None
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return "unreadable", None
    try:
        try:
            blob = os.read(fd, _SKILL_CAP + 1)
        except OSError:
            return "unreadable", None
    finally:
        os.close(fd)
    if len(blob) > _SKILL_CAP:
        return "unreadable", None
    return "ok", blob


def _normalize_context_value(raw):
    """Comment-stripped, quote-stripped, lowercased frontmatter context value."""
    text = _strip_unquoted_comment(raw).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    return text.lower()


def _frontmatter_body_lines(text):
    """Lines inside the frontmatter window.

    A leading UTF-8 BOM is stripped, and blank lines may precede the opening
    delimiter. A line whose rstrip() is --- opens or closes the block, so a
    trailing space on the delimiter is ignored. At most 200 lines from the
    opener. No opener means there is no frontmatter. An unclosed block yields
    the lines after the opener up to that cap.
    """
    if not isinstance(text, str):
        return []
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines()
    start = 0
    while start < len(lines) and lines[start].strip() == "":
        start += 1
    if start >= len(lines) or lines[start].rstrip() != "---":
        return []
    limit = min(len(lines), start + _FRONTMATTER_LINES)
    closing = None
    for index in range(start + 1, limit):
        if lines[index].rstrip() == "---":
            closing = index
            break
    end = closing if closing is not None else limit
    return lines[start + 1 : end]


def _context_key_values(text):
    """Normalized values of frontmatter lines whose key is context.

    The key match allows any indentation, either quote style, and any case.
    Text that merely contains context: fork on another key's line, such as
    description, is not a context value.
    """
    values = []
    for line in _frontmatter_body_lines(text):
        match = _CONTEXT_KEY_RE.match(line)
        if match is None:
            continue
        values.append(_normalize_context_value(line[match.end() :]))
    return values


def resolve_skill_for_gate(payload, tool_input):
    """inline, fork, or unresolved for one Skill call.

    A skill is a fork only when some frontmatter line has the key context,
    at any indentation, and that line's normalized value is not exactly
    inline. No context line means inline, so a description that mentions
    fork or even the text context: fork does not. context: fork (any case)
    uses the model key, else the agent definition, else the main session.
    Any other context value is a fork with an unknown model. When strict
    parsing fails, a non-inline context line is still a fork, with an
    unknown model; no such line is inline. A clean sonnet or haiku family
    is the only fork that is allowed. A SKILL.md that exists but cannot be
    read is a fork of unknown model when the main session is opus or
    unknown, and unresolved otherwise. A skill that is not on disk is
    unresolved, because plugin and built-in skills run inline.
    """
    if not isinstance(payload, dict):
        payload = {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    raw = tool_input.get("skill")
    name = raw.strip().lstrip("/") if isinstance(raw, str) else ""
    path = _skill_file(payload, name)
    if path is None:
        return {"status": "unresolved", "family": None, "skill": name}
    state, blob = _read_skill_bytes(path)
    if state != "ok":
        if main_session_model(payload) in ("opus", "unknown"):
            return {"status": "fork", "family": "unknown", "skill": name}
        return {"status": "unresolved", "family": None, "skill": name}
    text = blob.decode("utf-8", errors="replace")
    values = _context_key_values(text)
    non_inline = [value for value in values if value != "inline"]
    if not non_inline:
        return {"status": "inline", "family": None, "skill": name}
    status, fields = _parse_strict_frontmatter(text)
    parsed = None
    if status == "ok" and isinstance(fields, dict):
        raw_context = fields.get("context")
        if isinstance(raw_context, str):
            parsed = raw_context.strip().lower()
    if parsed == "fork" and all(value == "fork" for value in non_inline):
        return {"status": "fork", "family": _fork_family(payload, fields), "skill": name}
    return {"status": "fork", "family": "unknown", "skill": name}
