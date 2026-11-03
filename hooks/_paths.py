import json
import os
import re
import stat
from datetime import datetime
from pathlib import PurePath

# Union of the Flutter (pazzy-app) and web (H5_repo) lists. Only consulted for
# files under docs/; everything else that isn't *.md / *.mdx is code anyway.
CODE_EXT = (
    ".dart", ".kt", ".kts", ".java", ".swift", ".m", ".mm", ".h", ".hpp", ".c", ".cc", ".cpp",
    ".gradle", ".properties", ".xml", ".plist", ".pbxproj", ".xcconfig", ".podspec",
    ".yaml", ".yml", ".lock", ".toml", ".json", ".jsonc", ".json5", ".sh", ".bash", ".zsh",
    ".py", ".rb", ".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".jsx", ".vue",
    ".css", ".scss", ".sass", ".less", ".styl", ".html", ".htm", ".graphql", ".gql",
    ".go", ".rs", ".cs", ".proto", ".sql", ".template", ".env",
    ".aar", ".jar", ".so", ".dylib", ".framework",
)

HOME = os.path.expanduser("~")
VAULT_ROOTS = [os.path.join(HOME, "workplace/project/Obsidian_doc")]
OBSIDIAN_MCP_VAULT = VAULT_ROOTS[0]
ASSET_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".heic", ".pdf",
             ".mp3", ".m4a", ".wav", ".ogg", ".mp4", ".mov", ".webm"}
SPEC_EXT = {".md", ".diff", ".patch", ".txt", ".json"}
STRICTNESS = ["control", "outside", "skill_config", "vault_asset",
              "code", "state", "vault_structured", "document"]   # strictest first

# Patterns use the literal token {P} (replaced with str.replace, never str.format)
# and a leading ~ (replaced with HOME). No {a,b} brace patterns.
REGISTRY = {
    "control": [
        "~/.claude/settings*.json",
        "~/.claude/routing-mode",
        "~/.claude/routing-reads",
        "~/.claude/hooks/**",
        "~/.claude/agents/file-reader.md",
        "~/.claude/agents/file-writer.md",
        "~/.agents/cursor-agent.md",
        "~/.cursor/cli-config.json",
        "~/.cursor/hooks.json",
        "~/.cursor/hooks/**",
        "{P}/.claude/settings*.json",
        "{P}/.claude/hooks/**",
        "{P}/.agents/cursor-agent.md",
        "{P}/.cursor/cli.json",
        "{P}/.cursor/hooks.json",
        "{P}/.cursor/hooks/**",
    ],
    "skill_config": [
        "~/.claude/skills/**",
        "~/.claude/agents/**",
        "~/.claude/commands/**",
        "~/.claude/rules/**",
        "~/.claude/CLAUDE.md",
        "~/.agents/**",
        "{P}/.claude/skills/**",
        "{P}/.claude/agents/**",
        "{P}/.claude/commands/**",
        "{P}/.claude/rules/**",
        "{P}/.agents/**",
        "{P}/.cursor/rules/**",
        "**/CLAUDE.md",
        "**/AGENTS.md",
    ],
    "state": [
        "~/.claude/projects/*/memory/**",
        "~/.claude/plans/**",
    ],
}


def is_allowed_path(path: str) -> bool:
    if not path:
        return False
    p = path.replace("\\", "/")
    base = os.path.basename(p)
    lower = base.lower()
    if lower.endswith(".md") or lower.endswith(".mdx"):
        return True
    # Non-code artifacts under docs/ (checklists, plans, notes, etc.)
    if "/docs/" in f"/{p}" or p.startswith("docs/"):
        if any(lower.endswith(ext) for ext in CODE_EXT):
            return False
        return True
    return False


def looks_like_code_target(path: str) -> bool:
    return not is_allowed_path(path)


WRITE_HINT = re.compile(
    r"(?:^|&&|\|\||[;\n]|\|)\s*"
    r"(?:"
    r"(?:sed|perl|ruby)\s+[^\n;&|]*-i\b|"
    r"(?:tee|truncate|install|cp|mv|rsync|patch|dd)(?:\s|$)"
    r")"
    r"|>>?",
    re.IGNORECASE,
)

PATH_CANDIDATES = re.compile(
    r"(?:(?:^|[\s])(?:tee|cp|mv|install|rsync|patch)\s+)([^\s;|&]+)"
    r"|(?:>>?\s*)([^\s;|&]+)"
    r"|(?:sed\s+[^\n]*-i(?:\s+\S+)?\s+)([^\s;|&]+)"
    r"|(?:--?\s*)?(?:file[_-]?path[=:\s]+)([^\s;|&]+)",
    re.IGNORECASE,
)

GENERATOR_WRITE = re.compile(
    r"(?:^|&&|\|\||[;\n|])\s*(?:\S*/)?(?:"
    r"dart\s+format\b(?![^\n;&|]*--output[= ](?:none|show|json)\b)"
    r"|protoc\b[^\n;&|]*--(?:dart_out|custom_out)"
    r"|mock-gen-pb\b"
    r"|switcheroo(?:\.exe)?\b"
    r"|dart\s+run\s+build_runner\b"
    r"|flutter\s+pub\s+run\s+build_runner\b"
    r"|dart\s+pub\s+run\s+build_runner\b"
    r"|flutter\s+pub\s+run\s+mobx_bin:gen\b"
    r"|dart\s+run\s+mobx_bin:gen\b"
    r")",
    re.IGNORECASE,
)


def remove_quoted_spans(cmd: str) -> str:
    return re.sub(r"""'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*["]""", "", cmd or "")


def scrub_command(cmd: str) -> str:
    scrubbed = re.sub(r"(?:\d+)?\s*>\s*&\s*\d+", "", cmd or "")
    return re.sub(r"(?:\d+|&)?>>?\s*/dev/null", "", scrubbed)


# A heredoc body is data unless one of these shells actually runs it.
_HEREDOC_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_HEREDOC_WRAPPERS = {"sudo", "env"}
_HEREDOC_REDIRECT_PAIRS = (">>", ">&", "<&", "<>")


def _heredoc_basename(word):
    if len(word) >= 2 and word[0] == word[-1] and word[0] in "'\"":
        word = word[1:-1]
    word = word.replace("\\", "/")
    if len(word) > 1 and word.endswith("/"):
        word = word[:-1]
    slash = word.rfind("/")
    if slash != -1:
        word = word[slash + 1:]
    return word


def _heredoc_assignment(word):
    return re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word or "") is not None


def _heredoc_feeds_shell(words):
    idx = 0
    while idx < len(words) and _heredoc_assignment(words[idx]):
        idx += 1
    if idx >= len(words):
        return False
    name = _heredoc_basename(words[idx])
    if name in _HEREDOC_SHELLS:
        return True
    if name not in _HEREDOC_WRAPPERS:
        return False
    for word in words[idx + 1:]:
        if _heredoc_basename(word) in _HEREDOC_SHELLS:
            return True
    return False


def _quoted_span_end(cmd, i):
    quote = cmd[i]
    i += 1
    n = len(cmd)
    while i < n:
        if quote == '"' and cmd[i] == "\\" and i + 1 < n:
            i += 2
            continue
        if cmd[i] == quote:
            return i + 1
        i += 1
    return n


def _double_quoted_value(cmd, i):
    """Content and end index of a double-quoted word starting at i."""
    i += 1
    n = len(cmd)
    chars = []
    while i < n:
        if cmd[i] == "\\" and i + 1 < n:
            chars.append(cmd[i + 1])
            i += 2
            continue
        if cmd[i] == '"':
            return "".join(chars), i + 1
        chars.append(cmd[i])
        i += 1
    return None, None


def _parse_heredoc_op(cmd, i):
    """Parse <<WORD, <<'WORD', <<"WORD", or <<-WORD. None if it is not one."""
    n = len(cmd)
    if not cmd.startswith("<<", i) or cmd.startswith("<<<", i):
        return None
    j = i + 2
    strip_tabs = False
    if j < n and cmd[j] == "-":
        strip_tabs = True
        j += 1
    while j < n and cmd[j] in " \t":
        j += 1
    if j >= n or cmd[j] in "\r\n|&;<>()`":
        return None
    if cmd[j] == "'":
        end = cmd.find("'", j + 1)
        if end == -1:
            return None
        return j, end + 1, cmd[j + 1:end], strip_tabs
    if cmd[j] == '"':
        value, end = _double_quoted_value(cmd, j)
        if end is None:
            return None
        return j, end, value, strip_tabs
    k = j
    while k < n and cmd[k] not in " \t\r\n|&;<>()\"'`$\\":
        k += 1
    if k == j:
        return None
    return j, k, cmd[j:k], strip_tabs


def _redirect_end(cmd, i):
    n = len(cmd)
    if i >= n:
        return None
    pair = cmd[i:i + 2]
    if pair in _HEREDOC_REDIRECT_PAIRS:
        return i + 2
    if cmd[i] in "<>":
        return i + 1
    if not cmd[i].isdigit():
        return None
    j = i
    while j < n and cmd[j].isdigit():
        j += 1
    pair = cmd[j:j + 2]
    if pair in _HEREDOC_REDIRECT_PAIRS:
        return j + 2
    if j < n and cmd[j] in "<>" and not cmd.startswith("<<", j):
        return j + 1
    return None


def _scan_heredoc_line(cmd, i):
    """Walk one command line. Return (index after it, heredoc specs)."""
    n = len(cmd)
    word_chars = []
    words = []
    heredocs = []
    pending_redirect = False

    def flush_word():
        nonlocal pending_redirect
        if not word_chars:
            return
        text = "".join(word_chars)
        word_chars.clear()
        if pending_redirect:
            pending_redirect = False
            return
        words.append(text)

    def new_command():
        nonlocal words, pending_redirect
        flush_word()
        words = []
        pending_redirect = False

    while i < n:
        ch = cmd[i]
        if ch in "'\"":
            end = _quoted_span_end(cmd, i)
            word_chars.append(cmd[i:end])
            i = end
            continue
        if ch in " \t\r":
            flush_word()
            i += 1
            continue
        if ch == "\n":
            flush_word()
            i += 1
            break
        if cmd.startswith("&&", i) or cmd.startswith("||", i):
            new_command()
            i += 2
            continue
        if cmd.startswith("<<<", i):
            flush_word()
            pending_redirect = True
            i += 3
            continue
        if cmd.startswith("<<", i):
            parsed = _parse_heredoc_op(cmd, i)
            if parsed is not None:
                _start, end, delim, strip_tabs = parsed
                flush_word()
                heredocs.append((delim, strip_tabs, words))
                pending_redirect = False
                i = end
                continue
            word_chars.append(ch)
            i += 1
            continue
        if ch == "&" and cmd.startswith("&>", i):
            flush_word()
            pending_redirect = True
            i += 2
            continue
        redir_end = None
        if ch in "<>" or (not word_chars and ch.isdigit()):
            redir_end = _redirect_end(cmd, i)
        if redir_end is not None:
            if ch in "<>":
                while word_chars and word_chars[-1].isdigit():
                    word_chars.pop()
            flush_word()
            pending_redirect = True
            i = redir_end
            continue
        if ch in ";&|()":
            new_command()
            i += 1
            continue
        word_chars.append(ch)
        i += 1
    else:
        flush_word()
    return i, heredocs


def _heredoc_closing_index(cmd, i, delim, strip_tabs):
    n = len(cmd)
    while i < n:
        end = cmd.find("\n", i)
        if end == -1:
            line = cmd[i:]
            next_i = n
        else:
            line = cmd[i:end]
            next_i = end + 1
        if line.endswith("\r"):
            line = line[:-1]
        if strip_tabs:
            line = line.lstrip("\t")
        if line == delim:
            return next_i
        if end == -1:
            return None
        i = next_i
    return None


def strip_heredoc_bodies(cmd: str) -> str:
    """Drop heredoc bodies so operators inside them are not shell syntax.

    The command line, including the << operator, stays, so `cat > notes.md <<EOF`
    is still a write. `<<WORD`, `<<'WORD'`, `<<"WORD"` and `<<-WORD` are heredocs.
    A missing closer leaves the text unchanged. Bodies that feed sh, bash, zsh,
    dash or ksh, or sudo or env in front of one of those, stay, because they
    run as commands. A here-string (<<<) is not a heredoc.
    """
    if not isinstance(cmd, str) or cmd == "":
        return cmd or ""
    out = []
    i = 0
    n = len(cmd)
    while i < n:
        start = i
        i, heredocs = _scan_heredoc_line(cmd, i)
        out.append(cmd[start:i])
        for delim, strip_tabs, words in heredocs:
            body = i
            closed = _heredoc_closing_index(cmd, i, delim, strip_tabs)
            if closed is None:
                out.append(cmd[body:])
                return "".join(out)
            if _heredoc_feeds_shell(words):
                out.append(cmd[body:closed])
            i = closed
    return "".join(out)


def extract_paths_from_command(cmd: str):
    paths = []
    for m in PATH_CANDIDATES.finditer(cmd or ""):
        for g in m.groups():
            if g:
                paths.append(g.strip("\"'"))
    return paths


def project_dir():
    raw = os.environ.get("CLAUDE_PROJECT_DIR")
    if raw:
        return os.path.realpath(raw)
    return os.path.realpath(os.getcwd())


_ROUTING_MODES = ("strict", "task-aware")


def routing_mode_path():
    return os.path.join(os.path.expanduser("~"), ".claude", "routing-mode")


def resolve_routing_mode():
    """Return (mode, source). source is env, file, or default.

    CLAUDE_ROUTING_MODE, then ~/.claude/routing-mode, then task-aware.
    A non-empty unknown value is strict. An empty value is unset.
    Does not look at CLAUDE_ROUTING.
    """
    env = os.environ.get("CLAUDE_ROUTING_MODE")
    if isinstance(env, str) and env.strip() != "":
        value = env.strip()
        if value in _ROUTING_MODES:
            return value, "env"
        return "strict", "env"
    path = routing_mode_path()
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return "task-aware", "default"
    except OSError:
        return "strict", "file"
    try:
        try:
            raw = os.read(fd, 4096)
        except OSError:
            return "strict", "file"
    finally:
        os.close(fd)
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    value = lines[0].strip() if lines else ""
    if value == "":
        return "task-aware", "default"
    if value in _ROUTING_MODES:
        return value, "file"
    return "strict", "file"


_ROUTING_READS = ("on", "off")


def routing_reads_path():
    return os.path.join(os.path.expanduser("~"), ".claude", "routing-reads")


def resolve_routing_reads():
    """Return (value, source). source is file or default.

    ~/.claude/routing-reads is on or off. A missing or empty file is off
    (default). An unknown value is off (file).
    """
    path = routing_reads_path()
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return "off", "default"
    except OSError:
        return "off", "file"
    try:
        try:
            raw = os.read(fd, 4096)
        except OSError:
            return "off", "file"
    finally:
        os.close(fd)
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    value = lines[0].strip() if lines else ""
    if value == "":
        return "off", "default"
    if value in _ROUTING_READS:
        return value, "file"
    return "off", "file"


def registry(cls, proj):
    return [pattern.replace("{P}", proj).replace("~", HOME) for pattern in REGISTRY[cls]]


def match(p, pattern):
    return PurePath(p).full_match(pattern)


_TASK_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_TS_RE = re.compile(r"^\d{8}-\d{6}-\d{6}$")
_RUN_JSON_MAX = 65536


def valid_task_name(task) -> bool:
    return isinstance(task, str) and _TASK_RE.fullmatch(task) is not None


def tmp_roots():
    """(given, resolved). resolved is the one allowed symlink hop of the tmp root."""
    raw = os.environ.get("CURSOR_RUN_TMP_ROOT") or "/tmp"
    if not os.path.isabs(raw):
        raw = os.path.abspath(raw)
    given = os.path.normpath(raw)
    return given, os.path.realpath(given)


def _legacy_state_spec(p, proj) -> bool:
    pp = PurePath(p)
    return pp.parent == PurePath(proj, ".claude", "specs") and pp.suffix.lower() in SPEC_EXT


def is_legacy_run_arg(arg, proj) -> bool:
    """True for .claude/specs/<task>.md, relative or absolute. Nested paths are not."""
    if not isinstance(arg, str) or not isinstance(proj, str) or arg == "":
        return False
    norm = os.path.normpath(arg if os.path.isabs(arg) else os.path.join(proj, arg))
    legacy_dir = os.path.normpath(os.path.join(proj, ".claude", "specs"))
    base = os.path.basename(norm)
    return os.path.dirname(norm) == legacy_dir and base.endswith(".md")


def _traversal(path: str) -> bool:
    parts = path.split("/")
    # parts[0] is "" for an absolute path. Reject ".", "..", and empty
    # components (double slash or a trailing slash) below the root.
    return any(part in ("", ".", "..") for part in parts[1:])


def _rel_under(path, root):
    root = os.path.normpath(root)
    prefix = root + os.sep
    if path.startswith(prefix):
        return path[len(prefix):]
    return None


def _owned_dir(st) -> bool:
    return (
        stat.S_ISDIR(st.st_mode)
        and not stat.S_ISLNK(st.st_mode)
        and st.st_uid == os.getuid()
        and (st.st_mode & 0o022) == 0
    )


def _owned_file(st) -> bool:
    return stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid()


def _read_run_json(path):
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return "run.json is not a regular file"
    if st.st_uid != os.getuid():
        return "run.json is not owned by the current user"
    if st.st_size > _RUN_JSON_MAX:
        return "run.json is not valid"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        data = os.read(fd, _RUN_JSON_MAX + 1)
    finally:
        os.close(fd)
    if len(data) > _RUN_JSON_MAX:
        return "run.json is not valid"
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "run.json is not valid"
    if not isinstance(obj, dict):
        return "run.json is not valid"
    return obj


def accept_run_spec(path, proj, *, require_file=True):
    """None when path is a tmp run spec. Otherwise a short error string.

    require_file is False for classification, so the orchestrator can Write
    the .md that `cursor-run new` has not created yet. Every other check is
    the same as execution. Unexpected errors propagate; is_spec catches them.
    """
    if not isinstance(path, str) or not isinstance(proj, str):
        return "spec path is not valid"
    if not os.path.isabs(path) or _traversal(path):
        return "spec path is not valid"
    norm = os.path.normpath(path)
    given, resolved = tmp_roots()
    rel = _rel_under(norm, given)
    if rel is None and resolved != given:
        rel = _rel_under(norm, resolved)
        expected = norm if rel is not None else None
    elif rel is not None:
        expected = os.path.normpath(os.path.join(resolved, rel))
    else:
        expected = None
    if rel is None or expected is None:
        return "spec is outside the tmp run root"
    parts = rel.split(os.sep)
    if (
        len(parts) != 4
        or parts[0] != os.path.basename(os.path.normpath(proj))
        or parts[2] != "specs"
        or not parts[3].endswith(".md")
        or not valid_task_name(parts[3][:-3])
        or _TS_RE.fullmatch(parts[1]) is None
    ):
        return "spec path is not a tmp run spec"
    try:
        datetime.strptime(parts[1], "%Y%m%d-%H%M%S-%f")
    except ValueError:
        return "spec path is not a tmp run spec"
    try:
        if os.path.normpath(os.path.realpath(norm)) != os.path.normpath(expected):
            return "spec path resolves through an unexpected symlink"
    except OSError:
        return "spec path is not valid"
    current = resolved
    stats = []
    for index, part in enumerate(parts):
        current = os.path.join(current, part)
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            if index == len(parts) - 1 and not require_file:
                stats.append(None)
                break
            return "spec path is not a tmp run spec"
        except OSError:
            return "spec path is not valid"
        if stat.S_ISLNK(st.st_mode):
            return "spec path resolves through an unexpected symlink"
        stats.append(st)
    if any(not stat.S_ISDIR(st.st_mode) for st in stats[:3]):
        return "spec path is not a tmp run spec"
    if not _owned_dir(stats[1]) or not _owned_dir(stats[2]):
        return "run directory is not a private directory owned by the current user"
    file_stat = stats[3] if len(stats) == 4 else None
    if file_stat is None:
        if require_file:
            return "spec file is missing"
    elif not _owned_file(file_stat):
        return "spec file is not a regular file owned by the current user"
    run_json = os.path.join(resolved, parts[0], parts[1], "run.json")
    try:
        loaded = _read_run_json(run_json)
    except FileNotFoundError:
        return "run.json is missing or unreadable"
    except OSError:
        return "run.json is missing or unreadable"
    if isinstance(loaded, str):
        return loaded
    if loaded.get("project") != proj:
        return "run.json project does not match"
    return None


def is_spec(p, proj):
    """Legacy .claude/specs records, or a tmp run spec (file may not exist yet).

    Any error while checking a tmp path means it is not a spec.
    """
    try:
        if _legacy_state_spec(p, proj):
            return True
        return accept_run_spec(p, proj, require_file=False) is None
    except Exception:
        return False


def under(p, root):
    root = os.path.normpath(root)
    return p == root or p.startswith(root + os.sep)


def resolve_target(tool, ti):
    """-> (literal, real) absolute paths, or None if the call has no target path."""
    ti = ti or {}
    raw = ti.get("file_path") or ti.get("notebook_path")
    if not raw and tool.startswith("mcp__mcp-obsidian__") and ti.get("filepath"):
        raw = os.path.join(OBSIDIAN_MCP_VAULT, ti["filepath"])
    if not raw:
        return None
    p = os.path.expanduser(raw.replace("\\", "/"))
    lex = os.path.normpath(p if os.path.isabs(p) else os.path.join(project_dir(), p))
    return lex, os.path.realpath(lex)


def _classify(p):
    proj = project_dir()
    for cls in ("control", "skill_config", "state"):
        if any(match(p, g) for g in registry(cls, proj)):
            return cls
        if cls == "state" and is_spec(p, proj):
            return "state"
    vault = next((v for v in VAULT_ROOTS if under(p, v)), None)
    if not (under(p, proj) or vault):
        # Outside the workspace: documents need approval, code stays code (deny).
        return "outside" if is_allowed_path(p) else "code"
    ext = os.path.splitext(p)[1].lower()
    if vault and ext in ASSET_EXT:                return "vault_asset"
    if vault and ext in (".canvas", ".base"):     return "vault_structured"
    if vault and ext in (".md", ".mdx", ".txt"):  return "document"
    rel = os.path.relpath(p, proj) if under(p, proj) else p
    if is_allowed_path(rel):                      return "document"   # today's rule
    return "code"


def classify_path(raw_path):            # for Bash-extracted paths
    return classify_write_target("Write", {"file_path": raw_path})


def classify_write_target(tool, ti):
    t = resolve_target(tool, ti)
    if t is None:
        return "code"
    a, b = _classify(t[0]), _classify(t[1])
    return min((a, b), key=STRICTNESS.index)


__all__ = [
    "CODE_EXT",
    "HOME",
    "VAULT_ROOTS",
    "OBSIDIAN_MCP_VAULT",
    "ASSET_EXT",
    "SPEC_EXT",
    "STRICTNESS",
    "REGISTRY",
    "is_allowed_path",
    "looks_like_code_target",
    "WRITE_HINT",
    "PATH_CANDIDATES",
    "GENERATOR_WRITE",
    "remove_quoted_spans",
    "scrub_command",
    "strip_heredoc_bodies",
    "extract_paths_from_command",
    "project_dir",
    "routing_mode_path",
    "resolve_routing_mode",
    "routing_reads_path",
    "resolve_routing_reads",
    "registry",
    "match",
    "valid_task_name",
    "tmp_roots",
    "is_legacy_run_arg",
    "accept_run_spec",
    "is_spec",
    "under",
    "resolve_target",
    "classify_path",
    "classify_write_target",
]
