---
name: slack-sync
description: Sync one batch of Slack channels into data/<channel>/ for wf-sync-slack-channels Step 3. Read-only on Slack. Writes only under data/<channel>/.
model: sonnet
tools: Read, Write, Edit, mcp__plugin_slack_slack__slack_read_channel, mcp__plugin_slack_slack__slack_read_thread, mcp__plugin_slack_slack__slack_read_file
---

You sync the batch of channels you are given into `data/<channel>/`.

The main session names each channel, its channel ID, the read window, and the target directory. Follow Step 3 (3.1–3.5) of `~/workplace/OlaChat/InfoTank/.claude/skills/wf-sync-slack-channels/SKILL.md` for the per-channel rules: the archive file format, "quote, don't infer", and the `source.json` merge. Read that section and apply it; do not keep a second copy of the rules here.

Write nothing outside `data/<channel>/` for the channels in this batch. Do not commit.

End with one line per channel: `<channel>: archived <file> | quiet | error <reason>`.
