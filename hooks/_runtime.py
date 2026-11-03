# Runtime detection shared by the routing hooks. Must import nothing local.

CURSOR_FORMAT_CHANGED = (
    "This looks like a Cursor hook payload (hook_event_name \"preToolUse\") but "
    "it has no cursor_version, so the hook can't tell which program is running it. "
    "Cursor's hook input format may have changed; update is_cursor_runtime() in "
    ".claude/hooks/_runtime.py."
)


def is_cursor_runtime(d) -> bool:
    # Env vars are inherited across processes, so they can't say who is
    # calling; the payload is written fresh by the program running the hook.
    if not isinstance(d, dict):
        return False
    version = d.get("cursor_version")
    return (
        d.get("hook_event_name") == "preToolUse"
        and isinstance(version, str)
        and bool(version.strip())
    )
