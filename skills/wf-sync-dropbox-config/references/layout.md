# Dropbox config layout

The only source for Dropbox folder names and file paths. Used by
`wf-sync-dropbox-config` (moves files here) and `wf-apply-dropbox-config` (links to
them). Change a path here, not in either SKILL.md.

## Folder names

- One folder per tool: `~/Dropbox/<tool>_config/`. Don't lump unrelated tools together.
- Machine-dependent configs: `~/Dropbox/<device>_<tool>_config/`, with `<device>` from
  `scutil --get ComputerName` (fall back to `hostname -s`).
- Always use the `~/Dropbox/...` path, never `~/Library/CloudStorage/Dropbox/...`.
- Legacy names in use — keep them, don't rename or "fix" them:
  - `bash_cofig/` — zsh, bash and git share it; the typo is the real folder name.
  - `cursor_M4_config/` — Cursor on the M4 machine (device after the tool).
    `cursor_ubuntu_config/` is the Ubuntu machine's (`mcp.json` only), and
    `cursor_config/mcp.json` is an older shared copy; link neither on the M4.

## Paths

Kind: `file` / `dir` — the Dropbox path is `<folder>/<name>` and the local path links
to it. `folder` — the Dropbox folder itself **is** the config directory: move the
local directory to the folder path (which must not exist yet) and link the local
path to the folder.

| Tool | Dropbox path | Local path | Kind |
|---|---|---|---|
| zsh | `bash_cofig/` `.zshrc` `.zshenv` `.zprofile` `.zlogin` | `~/<name>` | file |
| bash | `bash_cofig/` `.bashrc` `.bash_profile` `.bash_login` | `~/<name>` | file |
| git | `bash_cofig/` `.gitconfig` `.gitignore_global` | `~/<name>` | file |
| vim | `vim_config/.vimrc` | `~/.vimrc` | file |
| vim | `vim_config/.vim` | `~/.vim` | dir |
| tmux | `tmux_config/.tmux.conf.local` | `~/.tmux.conf.local` | file |
| ssh | `ssh_config/config` | `~/.ssh/config` | file (never keys, never the whole `.ssh`) |
| npm | `npm_config/.npmrc` | `~/.npmrc` | file |
| cursor | `cursor_M4_config/User/settings.json`, `User/keybindings.json` | `~/Library/Application Support/Cursor/User/<name>` | file |
| cursor | `cursor_M4_config/mcp.json` | `~/.cursor/mcp.json` | file |
| fish | `<device>_fish_config` | `~/.config/fish` | folder (machine-specific) |

**tmux / oh-my-tmux:** if `~/.tmux` is a git clone of oh-my-tmux, don't move the clone
or `~/.tmux.conf` (already a symlink into that repo). Only `~/.tmux.conf.local` holds
personal settings — check `cd ~/.tmux && git status --short` is clean first.
