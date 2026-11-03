# Measuring a skill run

`scripts/measure_skill_run.py <transcript | session id> [--stage-regex REGEX]` reads one
Claude Code transcript. Transcripts live in `~/.claude/projects/<project-slug>/<session>.jsonl`;
sub-agent transcripts in `<session>/subagents/agent-*.jsonl` (the script reads those too).
Test: `python3 -I scripts/tests/test_measure_skill_run.py` (fixture with known answers).

## What each number is

| Field | Kind | Source |
|---|---|---|
| `requests` | observed | Assistant messages, deduplicated by `message.id` (one message is split over several rows that repeat its usage) |
| `peak_ctx`, `final_ctx`, per-request context | observed | `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` of the API usage of that request = the real context size sent |
| `output_tokens` | observed | API usage, once per message |
| `compactions` | observed | `system` rows with `subtype: compact_boundary` and `compactMetadata.preTokens/postTokens` |
| `delegations` | observed | `Skill runner:*`, Bash calls containing `cursor-agent`, `Agent`/`Task` calls, with `run_in_background` |
| `tool_errors` | observed | `tool_result` with `is_error: true` |
| `duration_min` | observed | first to last request timestamp (wall clock, includes waiting for the user) |
| `skills[].est_tokens` | **estimated** | characters of the loaded skill text / 4 |
| `skills[].observed_ctx_delta` | observed, but **not skill-only** | context growth across the load; also includes the assistant's own output and attachments in that step |
| `stages_heuristic` | **heuristic** | first match of `--stage-regex` in assistant text; only as good as the markers the orchestrator prints |

How skills load (verified on transcripts, Claude Code v2.1.309):

- **Skill tool**: a `tool_result` "Launching skill: X", then a meta user row
  (`isMeta: true`, `sourceToolUseID` = the tool call) holding the full SKILL.md text.
- **Typed `/command`**: a user row with `<command-name>/X</command-name>`, then a meta
  row starting "Base directory for this skill".
- Only invoked skills load their body. A skill that is merely *mentioned* costs nothing
  until something invokes it. So "A references 15 skills" says nothing about context;
  count the loads.

## Caveats (state them in every report that uses these numbers)

- Context numbers are only trustworthy for Claude models. Requests from other backends
  (e.g. `cursor:*`, `deepseek-*` via a proxy) are listed in `non_claude_models` and
  excluded from `peak_ctx`. Their usage is often zero or not comparable.
- `preTokens` in a compaction record has been seen far above any context window
  (3.9M); report it as given, do not use it as context size.
- `cursor-agent` CLI work runs outside Claude Code: its tokens are not in any transcript.
  Only the call, its duration and its output size are visible.
- `api-request-blob` rows replay earlier messages; both scripts skip every row whose `type`
  is not `user` or `assistant`, or replays would count as extra requests and extra runs.
- The script measures the whole transcript, and one transcript can hold several runs
  and other work (seen: 9 invocations of two skills in one session). For one run, pass
  `--from-request N --to-request M` (from the skill's load to the next skill load or the
  task's end) and say so. Compare runs only on the same task, commit and models.
- `delegations[].background` is `true` when the call passed `run_in_background: true` or
  a `<task-notification>` names it, `false` when it passed `false`, and `"not set"`
  otherwise. An `Agent` call left "not set" may still have run in the background
  (`composition-rules.md` → Coordination); check its result arrived inline.
- `cursor-agent CLI` counts only commands that run it (line start or after `;`, `&`,
  `|`, outside heredocs); a wrapper script that calls it is not counted.

## Baseline protocol (Phase 0)

1. Pin the code: a git worktree of the project at a recorded commit, plus its `.claude`
   at a recorded commit if `.claude` is a separate repo.
2. Pin the task: one real, small ticket (inputs written down: Figma node, PRD section,
   target package). Never invent a ticket.
3. Run the orchestrator in a **new** session started in the worktree, so the transcript
   contains only that run. Ask it to print `STAGE <n> START` / `GATE <n> PASS|FAIL`.
4. Measure with `--stage-regex "STAGE (\d+) START"` and record: per-stage requests,
   context at start and peak, skills loaded with est tokens, delegations (and whether
   any ran in the background), compactions, tool errors, gate results, duration.
5. Keep the JSON next to the run notes; rerun the same ticket after each change set.
