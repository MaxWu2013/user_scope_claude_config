---
name: wf-sync-claude-config-repos
description: Find every project `.claude` directory that is its own git repo (shared Claude config cloned into many projects), check the skills in each against the naming convention of wf-refactor-skill-names and rename the user's own non-conforming skills (once per remote, after approval), then fast-forward each repo to its latest remote commit, asking before stashing, merging, rebasing or pushing. Never renames teammates' or installed skills. Use when asked to sync, update, pull or refresh the `.claude` config repos across projects ("sync my .claude repos", "pull the latest claude config everywhere"), to check or fix skill names in those repos, or to audit which `.claude` directories are repos and whether they are behind.
argument-hint: "[search root, default ~] [--check to only report] [--no-rename to skip the skill-name step]"
---

# Sync .claude Config Repos

Arguments: `$ARGUMENTS`. The first path is the search root (default `~`).
`--check` means report only: run steps 1–3 in report mode plus a fetch (see
"Check mode"), change nothing. `--no-rename` skips step 3.

Several projects keep their `.claude` directory as a separate git repo, often
cloned from the same remote. The goal is to fix the user's own skill names
first and then bring every repo to the latest remote commit without losing
local edits.

## 1. Find candidate `.claude` directories

```bash
ROOT="${ROOT:-$HOME}"
find "$ROOT" -maxdepth 6 \
  \( -name node_modules -o -name .cache -o -name .local -o -name .git \
     -o -name snap -o -name .Trash -o -path "$HOME/Library" \) -prune \
  -o -type d -name .claude -print 2>/dev/null
```

Depth 6 reaches `~/workplace/<group>/<project>/.claude` without crawling the
whole disk. The pruned directories are huge (dependencies, caches, app data,
trash) and never hold project config, so skipping them keeps the search fast
and avoids picking up copies inside packages or deleted projects.

## 2. Keep only independent repos

For each candidate, keep it only if both hold:

- it has its own `.git` (`test -e "$d/.git"`; `-e` because it may be a file
  for worktrees), and
- `git -C "$d" rev-parse --show-toplevel` equals the directory itself
  (compare against `cd "$d" && pwd -P` so symlinks don't cause a mismatch).

A `.claude` that is just tracked files inside a parent project fails the
second check. Leave it alone: running `git pull` there would pull the parent
project, not the config.

For each kept repo record:

```bash
git -C "$d" branch --show-current
git -C "$d" remote get-url origin
git -C "$d" status --porcelain | wc -l     # dirty-file count
```

Skip (and report) repos with no `origin` or a detached HEAD — there is
nothing to pull into.

## 3. Check and fix skill names (before syncing)

Skip this step with `--no-rename`. The naming rules, the ownership check and
the rename procedure all come from the `wf-refactor-skill-names` skill
(`~/.claude/skills/wf-refactor-skill-names/SKILL.md`). Read it before this
step and follow its rules; this section only adds what is special about
shared config repos.

### 3a. Fetch and audit

For every kept repo that has a `skills/` folder:

```bash
GIT_TERMINAL_PROMPT=0 perl -e 'alarm shift; exec @ARGV' 60 git -C "$d" fetch --quiet
python3 -I ~/.claude/skills/wf-refactor-skill-names/scripts/audit_skill_names.py "$d/skills"
```

The `owner` column decides what may be renamed:

- `mine` → a candidate if its status is not `ok`.
- `ext:<someone>` (a teammate added it in git, or it was installed) → **never
  rename**, even if the name breaks the convention. List it in the report.
- `unknown` → ask the user, as `wf-refactor-skill-names` describes, and
  record the answer in its `ownership.md`.

### 3b. One rename per remote, not per clone

Group the repos by `origin` URL. Clones of the same remote must not each make
their own rename commit — the commits would duplicate each other and conflict
on the next sync. For each remote pick **one primary clone** to rename in:
prefer a clean one (no dirty files) that is not behind; otherwise the
cleanest. The other clones of that remote receive the rename by pulling
after it is pushed.

### 3c. Skip skills the remote is changing

For each candidate, check whether the remote changed it since this clone's
HEAD, or already has a folder with the proposed name:

```bash
git -C "$d" diff --name-status HEAD...@{u} -- "skills/<old>" "skills/<new>"
```

Any output → do not rename it now; note "rename after sync" in the
proposal. Renaming a folder that upstream just edited makes the sync conflict.

### 3d. Propose, then rename

Show one table across all remotes, as in `wf-refactor-skill-names` step 4,
with extra columns for the remote and the primary clone. Say plainly that a
rename in a shared repo changes the `/` command for everyone who pulls it.
Wait for approval; with `--check`, stop after the table.

For each approved row, in the primary clone only:

1. Write the approved rows for this clone to a plan file in the scratchpad
   (`[{"skills_dir": "<d>/skills", "old": ..., "new": ...}, ...]`) and run
   `python3 -I ~/.claude/skills/wf-refactor-skill-names/scripts/rename_skill.py --plan <plan.json>`
   as in `wf-refactor-skill-names` step 5: dry run first, read the `edit`
   hits, then `--apply`. It uses `git mv` in a repo, sets `name:`, records
   the new path in `ownership.md` and updates references **inside this repo**
   (other `skills/*/SKILL.md`, `settings*.json` `Skill(...)` rules,
   `commands/`, `agents/`). It never edits other people's skills; it lists
   them under `external`.
2. If it refuses a row (exit 1), nothing was moved: report the reason and
   drop or fix that row.
3. References outside the repo (the parent project's `CLAUDE.md`,
   `.vscode/workflows`, other projects) belong to other repos: list them and
   ask before changing any. Workflow copies (`.vscode/workflows/<old>.json`)
   are regenerated, not edited, as in `wf-refactor-skill-names` step 5; offer
   that per project after the pulls (step 8).

Then show `git -C "$d" status --short` and **ask** before committing. Commit
only the rename and its reference updates, with a message like
`Rename skills to naming convention: <old> → <new>, ...`. If the clone had
unrelated local edits, stage only the rename paths (`git add` the exact
paths) so those edits stay uncommitted.

## 4. Pull with fast-forward only

```bash
before=$(git -C "$d" rev-parse --short HEAD)
GIT_TERMINAL_PROMPT=0 perl -e 'alarm shift; exec @ARGV' 60 git -C "$d" pull --ff-only --no-rebase --no-autostash
after=$(git -C "$d" rev-parse --short HEAD)
```

- `--ff-only`: clean repos that are simply behind update; anything unusual
  (local changes in the way, diverged history) stops with the repo untouched.
- `--no-rebase --no-autostash`: the user's global git config sets
  `pull.rebase=true` and `rebase.autostash=true`. Without these flags a pull
  stashes local edits and re-applies them without asking, instead of stopping.
- `GIT_TERMINAL_PROMPT=0`: a remote needing credentials fails instead of
  hanging on a password prompt.
- `perl -e 'alarm shift; exec @ARGV' 60`: an unreachable remote can't stall
  the whole run. macOS has no `timeout` command; perl is always there.

Record the output of each pull, and the commit before and after.

A primary clone that got a rename commit in step 3 and whose remote has new
commits is now diverged; it is handled in step 5 like any other diverged repo
(rebase keeps the history linear and is usually the right choice for a
one-commit rename, but ask).

## 5. Report, then ask about blocked repos

Show which repos updated, which were already up to date, and which failed.
For each repo that did not pull, show what blocks it:

- **Local changes would be overwritten** — list the files from the error
  and `git -C "$d" status --short`.
- **Diverged** — `git -C "$d" log --oneline HEAD..@{u}` and `@{u}..HEAD`,
  so both sides' commits are visible.
- **Network / auth / timeout** — the error text; nothing to decide.

Then **ask** how to handle each blocked repo: stash and pull, merge,
rebase, or skip. Don't choose for the user unless the original request
already said what to do; these repos hold hand-edited local config, and
the right call differs per repo.

## 6. Carry out the chosen action — one step at a time

Don't chain `git stash && git pull && git stash pop`. Check the result of
each step before running the next, because each can stop halfway:

**Stash and pull**
1. `git -C "$d" stash push -m "wf-sync-claude-config-repos <date>"`
2. `git -C "$d" pull --ff-only --no-rebase --no-autostash` (or
   `--no-rebase --no-autostash --no-edit` if also diverged).
   If it stops on a merge conflict, resolve it (step 7) and commit the merge
   **before** popping — popping into a half-finished merge mixes two conflicts.
3. `git -C "$d" stash pop`. If the pop conflicts, git keeps the stash; leave
   it there until the merged file is verified (step 7).

**Merge** (diverged repo): `git -C "$d" pull --no-rebase --no-autostash --no-edit`. This
makes an ordinary merge commit with the default message and doesn't open an
editor.

**Rebase**: `git -C "$d" pull --rebase --no-autostash` (a repo with local
edits then stops; stash first as above); on conflict, resolve each commit
per step 7 and `git rebase --continue`.

## 7. Resolve conflicts

List conflicted files with `git -C "$d" diff --name-only --diff-filter=U`.

Keep both sides **only when both sides purely added content**:

- **`settings.local.json`** — union of `permissions.allow`: the entries
  before the conflict, then upstream's new entries, then local's new
  entries, each in original order, exact duplicates dropped. Keep anything
  upstream added after the array (e.g. a `hooks` block) as upstream has it.
- **Markdown logs / knowledge bases** — keep both appended sections, ordered
  by date, separated with the file's existing section separator.

If either side changed or removed existing content, don't guess: show the
conflict and ask. A rename/modify conflict on a skill folder (upstream
edited a skill this run renamed) also goes to the user: usually the fix is
to abort, sync first, and redo the rename on top.

**Verify before finishing**

- `git -C "$d" diff --numstat HEAD -- <file>` and
  `git -C "$d" diff --numstat MERGE_HEAD -- <file>` must both show **zero
  deletions** (for a stash pop, compare against `HEAD` and `stash@{0}`).
  A deletion means content from one side was dropped.
- For JSON, load the result and both source versions in Python
  (`git show HEAD:<path>`, `git show MERGE_HEAD:<path>` / `stash@{0}:<path>`)
  and confirm: it parses; every `permissions.allow` entry from either side is
  present; nothing appears that neither side had; every other key equals
  upstream's value.

**Finish**

- Merge: `git add <file>` then `git commit --no-edit` — the default merge
  message, no AI attribution trailers, since this is a routine sync commit
  in a shared repo.
- Stash-pop conflict: `git restore --staged <file>` so it is an ordinary
  unstaged local edit again (as it was before the stash), then
  `git stash drop`. Report the dropped stash's commit hash (printed by
  `drop`) so it stays recoverable with `git stash apply <hash>`.

## 8. Push the renames (only if the user agrees)

If step 3 made rename commits, the primary clones are now ahead of their
remotes. **Ask** whether to push each one — shared config is the user's call,
and teammates will get the new skill names on their next pull. Never push
anything else (merge commits, local edits) without being asked.

If the user agrees and the push succeeds, pull the other clones of the same
remote again (step 4) so they get the rename too. If not, report those clones
as "rename not yet received".

After a clone pulls a rename, git leaves the old folder behind when it held
ignored files (`__pycache__`, `.DS_Store`); the audit prints it as
`skip (no SKILL.md)`. For each such folder, show
`git -C "$d" status --short --ignored skills/<old>`; if it holds only ignored
files, ask, then remove it.

If a project has workflow copies of renamed skills
(`<project>/.vscode/workflows/<old>.json`), offer to regenerate them as in
`wf-refactor-skill-names` step 5, and list any left in the final report.

Finally rerun the audit script on each renamed repo's `skills/` folder: every
renamed skill should show `ok`.

## 9. Final report

A table:

| repo path | renamed skills | result | old → new commit |
|-----------|----------------|--------|------------------|

Results: `updated`, `up to date`, `stashed + pulled`, `merged`, `rebased`,
`skipped`, `failed (<reason>)`.

Then:
- for each repo that needed more than a plain pull: what conflicted, how it
  was resolved, and its state now (clean / local edits restored / ahead of
  remote by N);
- skills left alone because someone else made them (with the owner);
- renames postponed because the remote was changing that skill (step 3c);
- references outside the repos that still use an old name.

If a repo is left ahead of its remote, say so.

## Check mode (`--check`)

Run steps 1–2, then for each repo:

```bash
GIT_TERMINAL_PROMPT=0 perl -e 'alarm shift; exec @ARGV' 60 git -C "$d" fetch --quiet
git -C "$d" rev-list --left-right --count HEAD...@{u}   # ahead  behind
python3 -I ~/.claude/skills/wf-refactor-skill-names/scripts/audit_skill_names.py "$d/skills"
```

Report a table: repo path | branch | origin | dirty files | ahead | behind |
own skills to rename. Below it, list the proposed renames (step 3d table,
without renaming). Change nothing else — no rename, pull, stash or merge.

## If an edit-blocking hook is active

Apply this section only if the project this session started in registers a
`require-cursor-agent` hook (`grep -l require-cursor-agent
<session project>/.claude/settings*.json`). The hook lives in a project's
`.claude/hooks/`, not in `~/.claude`, and only acts in sessions started in
that project; a run from `~` (the default root) has none. That PreToolUse
hook blocks direct edits to some files:

- `.md` files (logs, knowledge bases, `SKILL.md`) can be edited directly.
- Config files such as `settings.local.json` must be delegated by the route
  the hook names (`/runner:sub-agents Run cursor-agent on <task>`). Give it a
  spec limited to that one file — the exact merged content or union rule from
  step 7 — and tell it to run no git commands. Wait for its JSON result and
  go on only if `status` is `success` (`partial` means it timed out; `error`
  means it failed: report and stop for that repo). Then do the `git add` /
  commit / stash steps yourself and run the step 7 verification on what it
  wrote.
- Inspect conflicted files with the Read tool and
  `git diff --diff-filter=U`, not by grepping for conflict markers.
