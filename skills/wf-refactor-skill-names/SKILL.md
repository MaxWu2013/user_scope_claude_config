---
name: wf-refactor-skill-names
description: Rename the user's own Claude Code skills (never plugin, installed, synced or teammates' skills) to the naming convention [<project>-]<type>-<skill-name> (type = wf/kn/tl/tp/st/or, optional leading project code such as pt for Party or pz for Pazzy). Audits ~/.claude/skills and project .claude/skills, classifies each skill by reading its SKILL.md, proposes new names in a table, and after approval renames the folders, updates the `name:` frontmatter and fixes every reference to the old names. Use when asked to "rename/refactor/normalize my skill names", "apply my skill naming rule", "check which skills break the naming convention", or invoked as /wf-refactor-skill-names [skill names | all | project path] [--check].
argument-hint: "[skill names | all | <project dir>] [--check to only report]"
---

# Refactor Skill Names

Target: `$ARGUMENTS` (empty → every skill in `~/.claude/skills` plus the current
project's `.claude/skills`). With `--check`, stop after step 4 (report only).

## Only rename skills the user created

**Never rename, edit or propose a new name for a skill made by someone else**,
even when the user names it in `$ARGUMENTS` or it breaks the convention. Other
people's skills include plugin skills, skills installed by `npx skills` or
another installer, skills synced from claude.ai, and skills a teammate added
to a shared repo. Renaming them would break their updates and other people's
setups. List them in the report as "external, not renamed" and stop there.

Ownership comes from the audit script's `owner` column, which checks, in order:

1. `ownership.md` lists the path under **External** → external; under **Mine** → mine.
2. The path is under `~/.claude/plugins` → external (plugin).
3. The path is under `~/.claude/skills/synced` → external (claude.ai).
4. An installer lock file (`~/.agents/.skill-lock.json`, `<project>/skills-lock.json`)
   records the skill from GitHub, or from a local path outside this user's home → external.
5. In git, the commit that first added `SKILL.md` was authored by someone
   other than the user (identities listed under **Me** in `ownership.md`, plus
   the global and repo `git config user.name`/`user.email`) → external.
   Committed by the user does not always mean written by the user — skills
   are often copied into a repo:
   - the commit message has a URL (e.g. "add skills from https://github.com/…") → external (`ext:imported`);
   - the commit added 10 or more skills at once → `unknown:bulk-<commit>` (probably an import; ask);
   - otherwise → mine.
6. The frontmatter has an `author:` that is not the user → external.
7. Otherwise → `unknown`.

Treat `unknown` and `unknown:bulk-…` as external until the user says
otherwise: ask them in one AskUserQuestion (multiSelect, list the unknown
skills; group a bulk commit's skills into one option with the commit's
message) which ones they wrote. Record each answer under **Mine** or
**External** in `ownership.md` so they are not asked again. Entries may be
glob patterns (`*/.claude/skills/flutter-*`), which is the way to cover every
clone of a shared config repo with one line. If the user says a skill marked external is theirs (e.g.
they committed it under another email), add that email under **Me** rather
than overriding the check.

A skill Claude created for the user in this session (the skill-name hook or
`~/.claude/CLAUDE.md` sent you here) is theirs: record its path under **Mine**
without asking.

## The naming convention

```
[<project>-]<type>-<skill-name>
```

Examples: `wf-sync-dropbox-config`, `kn-slack-api`, `pt-wf-seed-db`, `pz-kn-domain-model`.

### `<project>` (optional, first)

A short lowercase code for the project, e.g. Party → `pt`, Pazzy → `pz`.
Known codes are listed in `projects.md` next to this file; read it first.

Add a project code **only when it is needed**: when the skill is specific to
one project *and* it can appear next to other projects' skills — it lives in
`~/.claude/skills`, in a skills folder synced or cloned into several projects,
or another project has a skill with the same remaining name. A skill that sits
only in its own `<project>/.claude/skills` folder gets no code; the folder
already says which project it belongs to.

A project code must never equal a type code (`wf kn tl tp st or`), or names
could not be read unambiguously.

### `<type>` (required)

Every type code has exactly two letters:

| Code | Type                                      | Typical signs in SKILL.md |
|------|-------------------------------------------|---------------------------|
| `wf` | Workflow                                  | Several steps that use tools, edit files, ask the user, branch |
| `kn` | Knowledge / Reference / Integration guide | Facts, API usage, conventions, "how X works"; little or no procedure |
| `tl` | Tool / Script / Checker / Validator       | Mainly runs one script or CLI (often in `scripts/`): checks, validates, converts |
| `tp` | Template / Asset                          | Ships templates, boilerplate or assets to copy or fill in |
| `st` | Style / Persona                           | Tone, writing style, persona, output format rules |
| `or` | Meta / Orchestrator                       | Coordinates other skills, agents or workflows |

Old prefixes are renamed: `kw` → `kn`, `sty` → `st`, `orch` → `or`.

**When a skill could be more than one type**, decide by what it mostly does when it runs:

- Mostly runs one script or CLI, the rest is setup and reading the output → `tl`.
- Several steps that use tools or ask the user, maybe with a script as one step → `wf`.
- Steps mainly call other skills, agents or workflows → `or`.
- Mostly explains, with at most a short example procedure → `kn`.
- Its main value is the files it ships for copying → `tp`, even if it explains how to fill them.
- Changes how Claude writes or speaks rather than what it does → `st`.

### `<skill-name>`

Kebab-case. Its form depends on the type:

| Type        | Form                       | Examples |
|-------------|----------------------------|----------|
| `wf`, `tl`  | verb-object                | `wf-sync-dropbox-config`, `tl-validate-workflow` |
| `kn`        | the topic                  | `kn-slack-api`, `pt-kn-domain-model` |
| `tp`        | the thing it produces      | `tp-pr-description` |
| `st`        | the voice or audience      | `st-terse-reviewer` |
| `or`        | what it coordinates        | `or-release-train` |

- Put the most memorable word first (`wf-sync-…`, not `wf-do-sync-…`); it is
  what the user types after the prefix in the `/` menu.
- Do not repeat the type or project in this part (`wf-workflow-deploy` → `wf-deploy`,
  `pt-wf-party-seed` → `pt-wf-seed`).
- Give skills that do opposite jobs matching verbs (`wf-sync-…` / `wf-apply-…`).
- Whole name: lowercase letters, digits and hyphens, at most 64 characters.

## 1. Collect the skills

Run the audit script (it only reads files):

```bash
python3 -I ~/.claude/skills/wf-refactor-skill-names/scripts/audit_skill_names.py [dirs...]
```

With no arguments it scans `~/.claude/skills` and `./.claude/skills`. Pass
project directories (or their `.claude/skills` folders) to scan others. It
prints each skill's owner, folder, `name:` value, symlink status, whether the
name already follows the pattern, and how many other files mention it.

Resolve every `unknown` owner with the user now (see above). From here on,
work only with skills whose owner is `mine`. Leave these alone and say so in
the report:
- every skill whose owner is `ext:...`, with the reason the script gave;
- folders without a `SKILL.md` (e.g. `synced`, `.trash`);
- this skill itself (already follows the pattern).

If the user's own skill is a symlink to a folder that is external (e.g. into
`~/.agents/skills` from an installer), it is external too.

## 2. Classify each skill

Read every `SKILL.md` you are renaming in full (and glance at `scripts/`, `templates/`,
`assets/`). Pick the type with the table and the tie-break rules above, and
write a one-line reason for each choice; it goes into the proposal table.
Then shape the `<skill-name>` part by the form rules.

## 3. Decide the project code

First decide whether the skill is project-specific: hard-coded repo paths, that
project's services or domain terms, or it lives in `<project>/.claude/skills`.
Then apply the "only when needed" rule above. If a code is needed and the
project is missing from `projects.md`, propose a code (Party → `pt`, Pazzy →
`pz` style) and ask the user to confirm it. Never reuse a code that already
belongs to another project, and never use a type code.

## 4. Propose the new names

Show one table and wait for approval:

| Current | Proposed | Type | Project | Why | References |
|---------|----------|------|---------|-----|------------|

- Mark skills that already follow the pattern as "keep".
- Flag collisions (two skills → same name, or a name already taken by an existing folder).
- Flag anything you were unsure about and ask instead of guessing.

Use AskUserQuestion only for real choices (unsure type, new project code). The
user may edit rows; apply their edits. With `--check`, stop here.

## 5. Rename

Use `scripts/rename_skill.py`; don't write a rename script of your own. Write
the approved rows to a plan file in the scratchpad (a JSON list of
`{"skills_dir", "old", "new", "project"}`; set `project` for global skills so
that project's references are searched too), then dry-run it:

```bash
python3 -I ~/.claude/skills/wf-refactor-skill-names/scripts/rename_skill.py --plan <plan.json>
```

It checks every row before anything moves (source exists, target doesn't, new
name passes, owner is `mine`, not a symlink) and exits 1 if any row is
refused. Read every `edit` hit: a common word (e.g. `run`, `init`) can match
unrelated text, so add such files to the row's `skip_files`. Then run it again
with `--apply`. It does items 1–4 below; items 5 and 6 come from its `jobs`
and `workflow` lists, and its `external` list goes in the report. The steps
below say what it does and what stays manual.

For each approved row, in this order:

1. **Folder.** If the skills folder is in git, `git mv old new`; otherwise `mv`.
   If the skill folder is a symlink, rename the link *and* ask whether the
   target folder should be renamed too (it may be shared, e.g. via Dropbox —
   see the `synced` folder). Never overwrite an existing folder.
2. **This skill's records, right after the move.** Change the old path to the
   new one under **Mine** in `ownership.md`; if the skill was not listed there
   (its owner came from git), add the new path anyway: until the rename is
   committed the audit shows the moved skill as `unknown`, and the skill-name
   hook would treat it as a new skill. Add new project codes to `projects.md`.
3. **Frontmatter.** Set `name: <new>` in `SKILL.md`. Change nothing else in the
   frontmatter except a mention of `/old-name` inside `description` or
   `argument-hint`.
4. **References.** Search for the old name as a whole word and update each hit:

   ```bash
   grep -rnwI --exclude-dir={node_modules,.git,.trash} -e 'old-name' \
     ~/.claude/skills ~/.claude/commands ~/.claude/agents ~/.claude/CLAUDE.md \
     ~/.claude/settings.json ~/.claude/settings.local.json \
     <project>/.claude <project>/CLAUDE.md <project>/.vscode/workflows 2>/dev/null
   ```

   Typical hits: `/old-name` in other skills or CLAUDE.md, "the `old-name`
   skill", `Skill(old-name)` permission rules in settings, and CC Workflow
   Studio files made by `wf-convert-skills-to-workflows` (don't hand-edit
   those; see 6 below). Read each hit before changing
   it — a common word (e.g. `run`, `init`) can match unrelated text. Do not
   edit session transcripts, `.trash`, or `projects/*/memory` unless asked.
   Do not edit files inside external skills either: if one mentions the old
   name, list it in the report so the user can tell its owner.
   If the skill is in a repo shared with other people, warn that the rename
   changes the `/` command for everyone who pulls it.
5. **Jobs that run the skill by path.** The name search above misses
   schedulers that call the skill's scripts by their full path. Search for
   the folder path too:

   ```bash
   grep -rlF -e '/skills/old-name/' ~/Library/LaunchAgents <project>/scripts 2>/dev/null
   crontab -l 2>/dev/null | grep -F '/skills/old-name/'
   ```

   A hit is a job that would silently stop after the rename: tell the user,
   update the path, and reload the job (`launchctl bootout` / `bootstrap`).
6. **Workflow copies.** If `<project>/.vscode/workflows/old-name.json` exists,
   regenerate it with `wf-convert-skills-to-workflows new-name` (ask the user
   to close that tab first), then move `old-name.json` to
   `backups/old-name.<YYYY-MM-DD>.json.bak` and `originals/old-name.SKILL.md`
   to `backups/`, the converter's backup naming.

If a hook blocks a command or edit in this step (e.g. a project's
`require-cursor-agent` hook blocks `mv`, `sed -i` and edits to `.json` and
`.sh` files), follow the route the hook's message names; never work around it.

## 6. Verify and report

- Rerun the audit script: every renamed skill should show owner `mine` and
  status `ok`.
- Rerun the step 5 searches (name and path) for each old name. The audit does
  not look for old names. No hit may remain outside transcripts, `.trash`,
  `projects/*/memory`, external skills and the hits you chose to leave (list
  those in the report).
- Report a table `old → new`, the files whose references were updated, the
  external skills left alone (with their owner), and anything else skipped and why.
- Remind the user that a running Claude Code session keeps the old skill list
  until it is restarted, and that `/old-name` no longer works.
- If a skills folder is a git repo, show `git status` and offer to commit; do
  not commit or push without being asked.
