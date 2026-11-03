# Plan: create the `sync-slack-info` skill in InfoTank

## Context

`InfoTank` (`/Users/wujincheng/workplace/OlaChat/InfoTank`) is a **brand-new repo** — branch
`master`, **no commits, no remote**, and only two empty directories: `.claude/` and `data/`.
There is no code to integrate with yet; this skill is the repo's first real content.

The need: the same Slack story/event channels that feed the `pt-prd` PRD repo
(`#story-*`, `#event-*`) also need to be pulled into InfoTank as a per-channel
information store, so channel history can be read from disk instead of re-fetched from
Slack on every question. The reference implementation
(`PRD/pt-prd/.claude/skills/sync-story-event-from-slack/`) does this well but is coupled to
PRD feature directories and a feature-matching engine — InfoTank has no PRD tree, so most of
that machinery is dead weight here.

Outcome: a self-contained skill that lists Slack channels, keeps only the `story-` / `event-`
ones, archives each into `data/<channel-name>/`, and commits — with re-runs reading only what
is new since last time.

**Confirmed decisions** (from the user):
- Channel dirs live under `data/<channel-name>/`.
- Each dir holds a Chinese markdown archive `消息-YYYYMMDD.md` + `source.json` bookkeeping.
- Repeat runs are incremental and resumable via `resume_after_ts`; quiet channels are recorded, not rewritten.

Prefix note: the user's first message said `story-pz-`, the corrected re-send said `story-`.
Using **`story-` / `event-`** (a superset that still covers `story-pz-*`), as instructed last.

## Files to create

```
InfoTank/.claude/skills/sync-slack-info/
├── SKILL.md
└── scripts/
    ├── plan_channels.py
    ├── test_plan_channels.py
    └── fixtures/
        ├── channels-story.json
        └── channels-event.json
```

Then the synced result itself: `data/<channel-name>/{source.json, 消息-YYYYMMDD.md}`.

## 1. `scripts/plan_channels.py` — Step 1+2 engine

Adapted from the reference `match_channels.py`. **Carry over almost verbatim** (these are the
parts that encode the real, hard-won Slack listing format):

- `unwrap()` — unwrap the MCP `{"result": "..."}` JSON wrapper.
- `parse_channels()` — split on `^### #`, read `**ID:**`, `**Topic:**` (multi-line, stop at the
  next `- **`), `**Archived:**`.
- `check_format()` — compare the parsed count against Slack's `showing N of M total` header and
  flag truncation. **Exit 2 on drift** — a Slack-side format change must fail loudly, not
  silently match nothing.
- `--json` plan output and the `FORMAT:` stderr convention.

**Drop** (no PRD tree to match against): `normalize`, `core`, `name_score`, `load_features`,
`match`, `ISSUE_RE`/`DOC_RE` matching, `NAME_THRESHOLD`, `STOPWORDS`.

**Change:**
- `PREFIXES = ("story-", "event-")`.
- `target_dir(name) -> "data/<name>"` — the directory *is* the channel name; no matching step.
- New `state(repo, name)`: `new` (dir absent) / `resume` (dir with parseable `source.json`) /
  `orphan` (dir exists, no usable `source.json`). Drives incremental sync in Step 3.
- Add a `slug` guard: reject channel names containing `/` or path traversal before using them
  as directory names (they come from Slack, i.e. untrusted input).
- Output table `| # | channel | id | dir | state |` listing **every** matched channel.

## 2. `scripts/fixtures/` + `scripts/test_plan_channels.py`

Fixtures: trimmed copies of real `slack_list_user_channels` output (the `{"result": …}`
wrapper, `\u`-escaped), modelled on the reference fixtures — a story set, an event set, and a
decoy non-matching channel (`eng-…` / `general`) to prove filtering.

`test_plan_channels.py` (stdlib `unittest`, run in-place like the reference — no pytest dep):
parsing the real format (multi-line topic, channel without topic, bare markdown), format-drift
detection (renamed `**ID:**`, changed block marker, `showing N of M` mismatch, missing header),
prefix filtering excludes non-`story-`/`event-`, `target_dir` mapping, `state()` for
new/resume/orphan, path-traversal guard, and CLI end-to-end incl. exit 2.

## 3. `SKILL.md` — the 4 steps

Frontmatter mirroring the reference (`name`, `description` with trigger phrases, `version: 1.0.0`,
`scope`, `triggers`). Body, in Chinese where it describes content, English for instructions:

- **Step 1 — List channels.** Load the Slack tools in one ToolSearch call
  (`mcp__plugin_slack_slack__{slack_list_user_channels,slack_read_channel,slack_read_thread,slack_search_channels}`),
  then list once per prefix — `name_prefix="story-"` and `name_prefix="event-"`,
  `limit=200, exclude_archived=true` (prefix scans every page internally, no cursor loop).
  Save each raw result **verbatim** to scratch. These are private channels, so
  `slack_search_channels` is not a substitute.
- **Step 2 — Filter and show.** Run `plan_channels.py` over the scratch files; show the user
  the **full table** of matched channels (this is step 2's deliverable). Channels that don't
  match the prefix are silently dropped.
- **Step 3 — Sync into `data/<channel-name>/`.**
  - 3.1 read window: `source.json → slack.resume_after_ts` (`oldest=` that ts, **plus** re-read
    every thread named in `resume_note` — thread replies never show up in an `oldest`-filtered
    top-level list); else `read_through`; else whole channel from creation.
  - 3.2 pre-check `slack_read_channel(id, oldest=ts, limit=1)` → if empty and threads unchanged,
    the channel is **quiet**; bump `archived_on`/`read_through` only, write no archive.
  - 3.3 read channel to exhausted `next_cursor`, and **`slack_read_thread` for every message
    with replies** — decisions are usually in replies; skipping threads is the main failure mode.
    Note file/image messages; open only when the text depends on them.
  - 3.4 write `消息-YYYYMMDD.md` in Chinese: header (来源/覆盖区间/参与方/本轮性质), 〇 先看这里,
    一 决策表 with thread permalinks (`https://<workspace>/archives/<id>/p<ts without dot>`),
    then 链接与素材 / 未决项 as applicable. **Quote, don't infer** — anything derived is marked
    `按定案推算（不是群内原话）`.
  - 3.5 update `source.json` (merge, never replace): `channel`, `channel_id`, `workspace`,
    `topic`, `archived_to[]`, `archived_on`, `coverage`, `resume_after_ts` (newest top-level
    message read), `resume_note`.
  - **>3 channels with new activity → fan out one `Agent` per channel**; each prompt must carry
    the channel id + window, target path, the format rules, and the attribution constraint
    (sub-agents don't inherit it). Sub-agents write files only; the main session commits.
- **Doctor** — compact read-only liveness table (offline test passes, the 4 Slack tools load,
  both prefixes list ≥1 channel, `slack_read_channel` returns a message, `git` target reachable).

## 4. `SKILL.md` — Step 4: save and commit

```bash
git status --short          # expect only data/<channel-name>/ dirs
git add -- data/<name>/ ...  # only the dirs touched; never `git add -A`
```
Chinese commit message matching the repo's style, one line per channel, e.g.
`同步 Slack 频道信息：归档 3 个 story 频道、1 个 event 频道`.

Repo state caveat to document in the skill: InfoTank currently has **no remote and no commits**,
so Step 4 commits locally on `master` and **skips the push**; if a remote is added later, the
step pushes to it.

**Attribution:** the reference repo has `.claude/rules/no_ai_attribution.md`, which overrides the
harness default. InfoTank has **no such rule**, so the skill will state: follow `no_ai_attribution.md`
if present, otherwise use the standard `Co-Authored-By: Claude Code <noreply@anthropic.com>` trailer.
(Flag for you — say the word and I'll make it drop attribution unconditionally instead.)

## Verification

1. `python3 .claude/skills/sync-slack-info/scripts/test_plan_channels.py` → all tests pass.
2. Doctor run with the real Slack MCP: both prefixes list ≥1 channel and `plan_channels.py`
   exits 0 (no `FORMAT:` line). Exit 2 means a Slack format change — fix `parse_channels` and
   extend `fixtures/`, never hand-patch.
3. End-to-end on live Slack: sync one `story-` and one `event-` channel; confirm
   `data/<name>/消息-<today>.md` + `source.json` exist, thread replies are present, and a
   second run reports the channel quiet without rewriting the archive.
4. `git log --oneline` + `git status` show the expected commit and a clean tree.
