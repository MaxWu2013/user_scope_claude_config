## Skill names

When creating, copying or renaming a skill (including with skill-creator),
name it `[<project>-]<type>-<skill-name>` as defined in
~/.claude/skills/wf-refactor-skill-names/SKILL.md ("The naming convention").
Read that section before picking the name; project codes are in its projects.md.
After creating the skill, add its path under "Mine" in that skill's ownership.md.
Never rename skills made by others.

## After creating a new skill

When a new skill of mine has been created (by skill-creator, by copying, or by
hand), do these in order before calling the work finished:

1. Run the `wf-refactor-skill-names` skill on it, so the name, `name:` field
   and ownership entry are right.
2. If its type is `wf` (workflow) or `or` (orchestrator), run the
   `wf-refactor-skill-composition` skill on it with its final name. That skill
   proposes changes and asks before applying any.

Skip both for skills made by someone else.

## Code changes

For code implementation, run ~/.claude/hooks/bin/cursor-run new <task> to get a
spec path under /tmp/<repo>/<date-time>/specs/, write the approved task spec there,
then invoke ~/.claude/hooks/bin/cursor-run <printed path>; cursor-run is a Bash
executable, not a subagent. Never put specs in .claude/specs and never call
cursor-agent -p directly.
