import os


def find_definition_path():
    sub_agents_dir = os.environ.get("SUB_AGENTS_DIR")
    if sub_agents_dir:
        return os.path.join(sub_agents_dir, "cursor-agent.md")
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    project_path = os.path.join(project_dir, ".agents", "cursor-agent.md")
    if os.path.exists(project_path):
        return project_path
    # Projects without their own definition use the user-scope one.
    return os.path.join(os.path.expanduser("~"), ".agents", "cursor-agent.md")


def strip_wrapping_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1].strip()
    return value


def read_agent_definition():
    definition_path = find_definition_path()

    with open(definition_path, encoding="utf-8") as definition_file:
        lines = definition_file.read().splitlines()

    if not lines or lines[0].strip() != "---":
        raise ValueError("agent definition has no leading frontmatter")

    closing_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            closing_index = index
            break
    if closing_index is None:
        raise ValueError("agent definition frontmatter is not closed")

    values = {}
    for line in lines[1:closing_index]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in ("model", "permission"):
            values[key] = strip_wrapping_quotes(value)
    return values.get("model") or None, values.get("permission") or None
