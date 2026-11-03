---
name: wf-apply-dropbox-config
description: Restore a tool's config on this machine by symlinking its original location to the copy already stored in Dropbox (e.g. create ~/.zshrc -> ~/Dropbox/bash_cofig/.zshrc). The reverse of wf-sync-dropbox-config — use when setting up a new machine, re-applying synced dotfiles, "apply/restore/link my config from Dropbox", or when a tool lost its config but Dropbox still has it.
---

# Apply Tool Configuration from Dropbox

This is the **inverse** of the `wf-sync-dropbox-config` skill. That skill moves the real file
into Dropbox and leaves a symlink behind. This one assumes the real file is **already** in
Dropbox and only needs the symlink created at the location the tool expects.

## Goal
For each target config file:
1. Confirm the real file exists in the Dropbox config directory.
2. Create a symlink at the original location pointing to it.

Example:
```
~/.zshrc  ->  /Users/wujincheng/Dropbox/bash_cofig/.zshrc
```
`~/.zshrc` is a symlink; the actual saved content lives in `~/Dropbox/bash_cofig/.zshrc`.

Nothing is moved out of Dropbox and nothing is copied — Dropbox stays the single source of truth.

## Source directory — read the shared layout first
Folder names, legacy folders and every Dropbox path → local path pair are in
`~/.claude/skills/wf-sync-dropbox-config/references/layout.md`, the only source for
them (shared with `wf-sync-dropbox-config`). Read it before planning.

When applying on a new machine, a per-device folder from a *different* device is usually
the wrong thing to link — list what's in Dropbox and ask the user which device's folder
to apply rather than guessing.

## Discovering what's available
When the user names a tool but not specific files, list the folder first and apply what's there:

```bash
ls -la ~/Dropbox/<tool>_config/
```

Map each entry back to its local path with the layout table. Only link files that
actually exist in Dropbox — never invent a file, and never create an empty placeholder.
An entry with no row in the table (keys, backups, `.DS_Store`, anything new) is not
linked: list it as `unmapped` in the plan and ask the user for its local path if they
want it. For kind `dir` and `folder`, symlink the **directory** in one shot rather than
linking each file inside it.

GUI-app paths (Cursor, VS Code) vary by version/platform — verify the actual destination path on
disk before linking.

## Procedure (per file)
1. **Verify the Dropbox source exists and is complete.** `test -s "<dropbox_path>"` for a file
   (`ls -A` must list something for a directory). Missing → stop and report — this skill never
   creates the source file; suggest `wf-sync-dropbox-config` if the user actually wanted to push a
   local file up instead. Zero-byte or empty → stop: Dropbox may not have finished syncing.
2. **Inspect the destination.**
   - **Nothing there** → nothing to back up; continue with step 3.
   - **Already a symlink to the same Dropbox file** (`readlink` matches) → already applied; report and skip.
   - **Symlink pointing elsewhere** → stop and ask. Don't silently relink.
   - **A real file/directory exists** → this is a conflict. `diff` it against the Dropbox copy:
     - Identical content → safe to replace after the plan is approved (step 3); back it up (step 4).
     - Different content → **stop and ask the user** which version wins. The local file may hold
       machine-specific changes that were never synced. Never clobber it on your own judgement.
3. **Show the plan and wait for a yes** before changing anything, on every run: every
   source → destination pair, which destinations hold a real file that will be backed up, and
   every `unmapped` entry. These are files the shell and tools depend on at startup. No yes →
   change nothing and report every file as `not linked (not approved)`.
4. **Back up before replacing.** When replacing an existing real file, move it aside rather than
   deleting: `mv "<dest>" "<dest>.bak.$(date +%Y%m%d%H%M%S)"`. Tell the user the backup path.
   Never `rm` a user config file.
5. **Create parent dirs.** `mkdir -p "$(dirname "<dest>")"` — needed for paths like
   `~/.ssh/config`, `~/.config/fish`, or the Cursor User dir on a fresh machine.
6. **Symlink.** `ln -s "<dropbox_abs_path>" "<dest>"` — always an absolute Dropbox path, never relative.
   For directories, make sure `<dest>` does not already exist as a directory, otherwise `ln -s`
   nests the link *inside* it (`~/.vim/.vim`). Handle the existing directory via steps 2–4 first.
7. **Verify.** `test -e "<dest>"` must succeed (proves the link resolves) and
   `readlink "<dest>"` must print the expected Dropbox path.
8. **ssh special case.** `chmod 600` the real file in Dropbox (`~/Dropbox/ssh_config/config`) and
   `chmod 700 ~/.ssh` — ssh refuses a config that is group/world readable, and Dropbox sync often
   loosens permissions. Only ever link `config`; never keys.

## Reporting
Summarize per file: `linked`, `already linked (skipped)`, `linked (existing file backed up to <path>)`,
or `needs user decision (conflict)`. Never report success for a file that was skipped or left in conflict.

## Safety notes
- Never delete a local config — back it up by renaming.
- Never create or modify the file inside Dropbox; this skill only reads it and links to it.
- If Dropbox hasn't finished syncing (file missing or zero-byte on a fresh machine), stop and tell
  the user to let Dropbox finish rather than linking to a partial file.
- After applying shell rc files, tell the user to open a new terminal tab/window to confirm the
  shell still loads correctly.
