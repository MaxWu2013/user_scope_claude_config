---
name: wf-update-cursor-model
description: Switch the cursor-agent model in every cursor-agent.md under the home directory (~/.agents, ~/workplace projects) to a new model, after checking the exact model ID with `cursor-agent --list-models`. Use when asked to "change/update/switch the cursor-agent model", "set cursor-agent to <model>", or invoked as /wf-update-cursor-model <model>.
argument-hint: <target model, e.g. "opus 5.5 high fast">
---

# Update cursor-agent Model

Target model: `$ARGUMENTS` (if empty, ask the user which model they want).

## 1. Find the exact model ID — never guess

Run `cursor-agent --list-models` and pick the ID that matches the target
(e.g. "opus 5.5 high fast" → `claude-opus-5-5-high-fast`). If several IDs
could match, or none does, list the candidates and ask before editing anything.

## 2. Find every definition file

```bash
find ~ \( -name node_modules -o -name .Trash -o -path "$HOME/Library/Caches" \) -prune \
  -o -name "cursor-agent.md" -print 2>/dev/null
```

Known locations: `~/.agents/cursor-agent.md` and `<project>/.agents/cursor-agent.md`
under `~/workplace/`.

## 3. Show the current values

For each file, show the `model:` line in the frontmatter. Point out any current
value that does not appear in the `--list-models` output (an invalid ID, e.g.
`cursor-grok-4.7-xhigh-fast`).

Use the file list from step 2's `find` exactly; don't replace it with globs.

Then **stop and ask**: show `file | current model | new model` for every file, and
ask the user to approve the list or name files to leave out (e.g. scratch copies or
baseline worktrees such as `_phase0/`). Some of these files are tracked in other
repos. No approval → change nothing and end with the table.

## 4. Replace only the `model:` line — approved files only

```bash
sed -i '' -E '2,/^---$/ s/^model: .*$/model: <exact-id>/' <file>
```

The `2,/^---$/` range limits the change to the frontmatter, so a body line starting
with `model:` is never touched.

Leave every other line unchanged (`run-agent`, `permission`, the body). Each
project's body text can be project-specific, so edit files in place instead of
overwriting one with another.

## 5. Check coverage

The hook lives in user scope (`~/.claude/hooks/require-cursor-agent.py`,
registered in `~/.claude/settings.json`) and applies to every project. It reads
`<project>/.agents/cursor-agent.md` and falls back to
`~/.agents/cursor-agent.md` when a project has none, so check that the fallback
exists and has the new model:

```bash
cd ~/.claude/hooks && CLAUDE_PROJECT_DIR=/nonexistent \
  python3 -c "from _agent_def import read_agent_definition as r; print(r())"
```

If a project's `.claude/scripts/delegate.py` reads its own
`.claude/hooks/_agent_def.py`, run the same check from that project's
`.claude/hooks` with `CLAUDE_PROJECT_DIR=<project>`.

## 6. Check the hooks for a model name written into the code

Grep `require-cursor-agent.py` and `_agent_def.py` for a model name written
directly into the code. Normally there is none: they read `model:` from the
definition file and pass it to cursor-agent with `--model`. Report any you find.

## 7. Report

Grep all `cursor-agent.md` files again to check the result, then show a table:
file | old model | new model. Do not commit anything.
