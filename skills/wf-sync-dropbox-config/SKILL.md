---
name: wf-sync-dropbox-config
description: Move a tool's environment/config file(s) into a synced Dropbox folder and replace the original with a symlink back to it, so config stays in sync across machines. Use when asked to migrate/move a config file to Dropbox, symlink dotfiles, or sync a tool's config (zsh, bash, git, vim, tmux, ssh, npm, etc) the way .zshrc is already handled.
---

# Migrate Tool Configuration to Dropbox

## Goal
For each target config file:
1. Move the real file from its original location into the Dropbox config directory, preserving its filename.
2. Replace the original location with a symlink pointing to the file's new home in Dropbox.

Result: the config is backed up/synced via Dropbox, but every tool still finds it at the path it expects (e.g. `~/.zshrc` still works, it's just a symlink now).

## Destination — read `references/layout.md` first
Folder names, legacy folders and the Dropbox path of every known file are in
`references/layout.md`, shared with `wf-apply-dropbox-config`. It is the only source for
them; don't restate them here.

A tool with no row there gets `~/Dropbox/<tool>_config/<name>`. If a tool doesn't map
cleanly to one name (e.g. multiple related tools at once), ask which folder to use
rather than guessing.

## Per-device folders for machine-dependent configs
Some tool configs aren't meant to be identical across machines — they embed machine-specific paths, hardware, or install-dependent content. Syncing those through one shared `<tool>_config/` folder means each machine's symlink fights over the same file. For those, use a **per-device** folder instead (naming in `references/layout.md`).

Use per-device naming when:
- The user says the config is machine-specific / system-dependent, or asks for it explicitly.
- A destination collision check (step 2 below) turns up an existing file with materially different content — a sign the shared folder already holds a *different* machine's version, not a stale duplicate of this one.

Otherwise default to the shared `<tool>_config/` naming. When in doubt, ask rather than guessing which bucket a given config falls into.

## Which files
When the user just names a tool, take its rows from `references/layout.md` (including
its kind: `file`, `dir` or `folder`). Only touch files that actually exist — check
first, don't invent files. GUI-app config locations (Cursor, VS Code, etc.) vary by
version/platform, so verify the actual path on disk before acting.

## Procedure (per file)
1. **Resolve & check.** Expand `~`, confirm the source exists (`test -e`). If it's already a symlink:
   - Already points into the target Dropbox dir → already migrated; report and skip.
   - Points somewhere else → stop and ask the user how to proceed. Don't silently relink.
2. **Check destination collision.** The destination is the row's Dropbox path (`<dropbox_path>`; for kind `folder`, the folder itself). If anything already exists there — same content or not — stop and ask the user: `mv` would overwrite a file without asking, or move a directory *inside* the existing one.
3. **Show the plan and wait for a yes** before step 4, on every run: each source path, its destination path, and that the original will become a symlink. This touches files the shell/tools depend on and is easy to get subtly wrong. No yes → change nothing and report every file as `not migrated (not approved)`.
4. **Move.** Kind `file`/`dir`: `mkdir -p "<folder>"` then `mv "<source>" "<dropbox_path>"`. Kind `folder`: `mv "<source>" "<dropbox_path>"` (the folder must not exist yet; step 2 checked). Use `mv`, not copy-then-delete, so there's never a moment with two divergent copies.
5. **Symlink back.** `ln -s "<dropbox_path>" "<source>"` — always use the absolute Dropbox path as the link target, not a relative one.
6. **Verify.** `test -e "<source>"` must succeed (proves the symlink resolves) and `readlink "<source>"` must show the expected destination.
7. **ssh special case.** After linking `.ssh/config`, `chmod 600` the real file in Dropbox — ssh refuses configs that are group/world readable, and Dropbox sync sometimes loosens permissions.

## Reporting
Summarize per file at the end: `migrated`, `already migrated (skipped)`, or `needs user decision (conflict)`. Never report success for a file that was actually skipped or left in conflict.

## Safety notes
- Never overwrite an existing destination file without asking.
- Never delete either copy on a conflict — leave both in place and ask the user which to keep.
- After migrating shell rc files specifically, tell the user to open a new terminal tab/window to confirm the shell still loads correctly.
