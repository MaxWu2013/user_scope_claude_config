# Router v2

Opus subagents are denied until a foreground `opus-judge` decides. Workflows may run under an Opus main session only when the process pins every workflow worker to Sonnet or Haiku and caps how many can run. User `settings.json` carries those env lines and the guard hooks. `hooks/data/opus-arbitration-settings-fragment.json` is the source of the entries. Rollback is in Activation and rollback.

## Architecture

The main session is the only place that may call Opus. A PreToolUse gate (`hooks/opus-arbitration-gate.py`) sees `Agent`, `Task`, `SendMessage`, `Workflow`, and `Skill`. A PostToolUse hook (`hooks/opus-arbitration-post.py`) records results and tells the main session the next step. A separate guard (`hooks/opus-workflow-guard.py`) watches workflow workers. The ledger is `~/.claude/logs/opus-arbitration/ledger.db`. Policy is `hooks/data/opus-arbitration-policy.json`. The switch file is `~/.claude/opus-arbitration-mode`.

`session-cost` reads the same ledger and the session transcripts, including `subagents/workflows/<runId>/agent-*.jsonl`, and prices them with ccusage.

## State machine

```
CANDIDATE → JUDGE_PENDING → JUDGE_RUNNING → JUDGED_CONTINUE
                                         → JUDGED_RETRY
                                         → JUDGED_STOP
                                         → JUDGED_ESCALATE → GRANT_READY → GRANT_CONSUMED
                                                                         → WORKER_COMPLETED
                                                                         → WORKER_FAILED
                                                                         → WORKER_UNKNOWN
```

`EXPIRED`, `CANCELLED`, and `FAILED_CLOSED` are terminal from any state. A pending judge expires at `judge_pending_ttl_seconds`. A running judge fail-closes at `judge_running_ttl_seconds` (900s in the shipped policy). A running judge with no completing PostToolUse for 120 seconds may be retried by the same session: the new call resets `judge_tool_use_id` and stays `JUDGE_RUNNING`.

## Triggers

| Trigger | When |
| --- | --- |
| T02 `UNAPPROVED_OPUS` | An Agent/Task call can run on Opus and has no grant |
| Footer `WORKER_REQUEST` / `CONFLICTING_REQUIREMENTS` | A finished worker asks for judgment |
| `WORKFLOW_WORKER_REQUEST` | A workflow worker stops with `NEEDS_JUDGMENT` |
| Cursor-run distinct failures | Repeated cursor-run failures on one task |
| Reasoning failure | `BLOCKED` or `FAILED` that is not environment or budget |

Environment and budget failures are not candidates.

## Judge protocol

Other permission modes tell the main session to launch `opus-judge` in the foreground with the short prompt `ARBITRATION-PACKET request_id=…`. The gate expands that reference into the stored packet (`updatedInput`) and marks `JUDGE_RUNNING`. An exact copy of the packet, with or without `<<<PACKET` / `PACKET>>>` markers, is accepted and is not rewritten.

The judge returns only `CONTINUE`, `RETRY`, `STOP`, or `ESCALATE`.

- `CONTINUE` stays on the current model.
- `RETRY` is one materially different attempt on a non-Opus model.
- `STOP` is reported and the task moves on.
- `ESCALATE` is one grant. A call that already had an invocation digest is re-issued unchanged. A composed grant is launched with `ARBITRATION-GRANT request_id=…`, and the gate fills in the approved prompt.

A `request_id` correlates the row. It is not permission to call Opus.

## Auto mode

When `permission_mode` is `auto`, the gate does not emit `updatedInput`. Auto mode refuses a call whose input a hook changed after review.

- `ARBITRATION_REQUIRED` and `ARBITRATION_CANDIDATE` include the full packet between `<<<PACKET` and `PACKET>>>` and tell the main session to copy that prompt exactly.
- A short `ARBITRATION-PACKET` reference is denied `JUDGE_REFERENCE_UNSUPPORTED_IN_AUTO` with the packet in the reason. The row stays `JUDGE_PENDING`.
- A short `ARBITRATION-GRANT` reference is denied `GRANT_REFERENCE_UNSUPPORTED_IN_AUTO` with the canonical invocation JSON in the reason. The grant is not consumed.
- A composed `ESCALATE` result gives that same invocation JSON for verbatim use.

## Workflows

Workflow workers inherit a model the main session does not pass on the tool call. Four layers limit that. A settings hook still cannot rewrite the spawn; the spawn mod can, and it fails closed.

### 1. Gate preconditions and runtime attestations

Every Workflow call is denied when `name` or `resumeFromRunId` is present, when `scriptPath` cannot be read from a regular file under the home directory, or when the script text contains `opus`.

A Sonnet or Haiku main session may launch anything else only when the guard attestation is present. The gate records a `workflows` row (`status=launched`, `expected_family` of the main model). PostToolUse on `Workflow` fills `runId`, `taskId`, and `workflowName`.

An Opus or unknown main session is denied with the old `OPUS_WORKFLOW_DENIED` text when `workflow.opus_main_mode` is missing, invalid, or `deny`. `pinned_env` allows the call only when all of these hold:

1. `CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL` is set and its family is in `allowed_worker_families`. A value containing `opus`, or an unknown family such as `best`, fails.
2. `CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS` is an integer from 1 through `max_concurrent_agents_required`.
3. This session has fewer than `max_workflows_per_session` recorded workflows.
4. The script contains at most `max_workers_per_workflow` matches of `\bagent\s*\(`. `CLAUDE_ROUTING_WORKFLOW_MAX_WORKERS` may only lower that cap.
5. Runtime attestations, below, are present.

The required lines for the shipped policy are:

```
CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL=sonnet
CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS=4
CLAUDE_CODE_PLUGIN_DIRS=<home>/.claude/mods/opus-workflow-spawn
```

`expected_family` on that row is the family of the env var. A missing line is `OPUS_WORKFLOW_DENIED reason=env` and names both model lines. A bad concurrency value is `reason=concurrency`. Too many workflows is `reason=session`. Too many static `agent(` calls is `reason=agents`.

On a main-thread PreToolUse for `Workflow`, `opus-workflow-guard.py` writes `~/.claude/logs/opus-arbitration/guard/attest-<session_id>-<tool_use_id>` mode 0600 before it returns. The gate polls for that file for up to 1.5 seconds, in 50 ms steps, and stops early if its own 6 second deadline is reached. Missing means `WORKFLOW_GUARD_NOT_ACTIVE`. Sonnet and Haiku mains need this file too: worker visibility is required for every Workflow.

An Opus or unknown main also needs the spawn mod:

- `mod/<session_id>.json` exists, `mod_version` is `1.0.0`, and `policy_sha256` equals the sha256 of the current policy file. Missing, stale, or a hash mismatch is `WORKFLOW_SPAWN_MOD_NOT_ACTIVE`.
- When `workflow.require_mod_call_attestation` is true (the default), the gate polls up to 1.5 seconds for `mod/attest-<session_id>-<tool_use_id>.json`. Missing is `WORKFLOW_SPAWN_MOD_NOT_ACTIVE call`.

The call attestation is written by the mod's `tool.call` hook for `Workflow`, using the event's `tool_use_id`. Hook order decides whether that file exists before the gate looks. The policy stays true until a live run shows the ordering.

### 2. Spawn mod

`mods/opus-workflow-spawn` loads from user-level `CLAUDE_CODE_PLUGIN_DIRS` (or `--plugin-dir`). The settings fragment records the env lines and the hook entries. See Activation and rollback.

`session.start` writes `mod/<session_id>.json` with `session_id`, `mod_version` `1.0.0`, `policy_sha256`, `started_at`, and a random `pid_marker`.

`agent.spawn` leaves ordinary subagents, teammates, and resumes of agents it did not start completely unchanged. For `e.workflow` it reads `hooks/data/opus-arbitration-policy.json` through `$.fs` and `$.env.get("HOME")`. An unreadable or invalid policy denies `OPUS_SPAWN_POLICY_UNAVAILABLE`. A valid policy uses `workflow.worker_model` (shipped as `sonnet`), `max_workers_per_workflow`, `max_workers_per_session`, and `allowed_worker_families`.

Before `next`, `update()` on `$.state` increments the run and the session together. Either count past its cap denies `WORKFLOW_SPAWN_CAP run=<n>/<cap> session=<m>/<cap>`. The reservation stays consumed. The model passed to `next` is `worker_model`, whatever the script asked for. The result is appended to `mod/spawns-<session_id>.jsonl` with `decision` `allow`, and the `agentId` is kept in `$.state` `workflowAgents`.

A resume whose `agentId` is in that set is `WORKFLOW_RESUME_DENIED`. It is not counted as a spawn. Any other resume is passed through; the Python gate still denies an Opus resume.

Each deny (`WORKFLOW_SPAWN_CAP`, `OPUS_SPAWN_POLICY_UNAVAILABLE`, `OPUS_SPAWN_GUARD_ERROR`, `WORKFLOW_RESUME_DENIED`) appends `{session_id, runId, agentIndex, decision: deny, reason, ts}` to that same log. A failure to append the line leaves the deny in place.

Fail closed: a throw or a timeout skips a hook and the spawn would proceed. The workflow registration's `.catch` denies `OPUS_SPAWN_GUARD_ERROR` when `next` was not called, for a workflow spawn or a tracked resume. A throw on any other spawn calls `next(e)`. The `tool.call` attestation uses `.catch` to call `next(e)`, so a broken attestation does not hide the call from the gate; the missing file denies it.

### 3. Worker guard

`opus-workflow-guard.py` is registered for every PreToolUse (`onFailure: block`, timeout 5), and for SubagentStart and SubagentStop (`onFailure: continue`, timeout 5). Ordinary tool calls return immediately. The fast path imports only `json`, `sys`, and `os`. Non-Workflow, non-worker payloads stay on that path.

On SubagentStart for `agent_type=workflow-subagent`, the guard links the worker to the newest `launched` workflow with the same `session_id` and `prompt_id`, inserts `agent_runs` (`kind=workflow_worker`), and marks `over_cap` when the worker count exceeds `max_workers_per_workflow` or `max_workers_per_session`. It does not block the spawn. If `agent_id` is absent from `mod/spawns-<session_id>.jsonl` after one retry (1 second), it records `unarbitrated_workflow_spawn`.

On a worker PreToolUse:

- `Agent`, `Task`, `SendMessage`, `Workflow`, and `Skill` are denied `WORKFLOW_WORKER_NO_DELEGATION`. The worker reports `NEEDS_JUDGMENT` in its status footer; the main session arbitrates.
- `over_cap` is `WORKFLOW_FANOUT_CAP`. No linked workflow row is `WORKFLOW_UNTRACKED`.
- An `agent_id` missing from the spawn log, after the same one-second retry, denies `WORKFLOW_WORKER_NOT_MOD_SPAWNED` and records `unarbitrated_workflow_spawn`.
- The model is read from `<dirname(transcript_path)>/<session_id>/subagents/workflows/*/agent-<agent_id>.jsonl` (at most 64 run directories, regular file, first 256 KiB, `O_NONBLOCK`). The first attachment of type `model` supplies `identity.modelId`. Otherwise the text `The exact model ID is <id>` is accepted. The family is cached on `agent_runs`. A family outside `allowed_worker_families`, or an unreadable transcript, is `WORKFLOW_WORKER_MODEL_DENIED`. Opus also records `unarbitrated_opus_run`.

The gate denies `Agent`, `Task`, `SendMessage`, and `Workflow` from `workflow-subagent` with the same delegation text, including calls that are not Opus. The transcript check, the caps, and the no-delegation rule stay as defense in depth behind the mod.

### 4. Stop audit

SubagentStop reads `agent_transcript_path` with the same parser, stores the model, and records `unarbitrated_opus_run` when the worker is Opus. A footer parsed by `parse_footer` becomes a candidate when it is `NEEDS_JUDGMENT` with a non-empty `unresolved` or `attempts`, or when `reason_code` is `CONFLICTING_REQUIREMENTS`. The request trigger is `WORKFLOW_WORKER_REQUEST`, the requester is `general-purpose`, and the observed model is the verified worker model. The guard then writes `~/.claude/logs/opus-arbitration/surface/<session_id>` (mode 0600). The next main-thread PreToolUse emits one `ARBITRATION_CANDIDATE` (packet form follows `permission_mode`) and deletes the flag. The judge runs as a normal Agent call in the main session.

### Activation and rollback

User `~/.claude/settings.json` `env` carries the three lines. **Project-level settings `env` was not applied to hooks or mod loading in a headless probe run**, so user-level `env` is required:

```
CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL=sonnet
CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS=4
CLAUDE_CODE_PLUGIN_DIRS=/Users/wujincheng/.claude/mods/opus-workflow-spawn
```

The same file registers the guard. PreToolUse gains an entry with no matcher, `onFailure: block`, timeout 5, running `opus-workflow-guard.py`. SubagentStart and SubagentStop each gain an entry with `onFailure: continue`, timeout 5, running the same guard. The arbitration PostToolUse matcher is `Agent|SendMessage|Bash|Workflow`. The copy of these entries is `hooks/data/opus-arbitration-settings-fragment.json`.

Restore the settings file from the pre-activation copy:

```
cp ~/.claude/backups/router-v2-20261104/settings.pre-workflow.json ~/.claude/settings.json
```

The ledger, the switch file, and the mod source stay.

Live probes on Claude Code 2.1.318 (2026-11-05), Opus main, process env `sonnet` and concurrency `4`, with the spawn mod loaded except where a row says otherwise:

| Probe | What ran | Result |
|---|---|---|
| L1 | Dynamic `model: 'op' + 'us'` plus a model-less worker, both reading a file | Both transcripts are `claude-sonnet-5-5`. Run `wf_c80a37cf-145`. No arbitration packet. |
| L2 | Script text contains literal `opus` | Gate denies `OPUS_WORKFLOW_DENIED` before the workflow starts. |
| L3 | 16 parallel tool-less `agent()` calls | 12 admitted, 4 denied `WORKFLOW_SPAWN_CAP` before they start. |
| L4 | `session-cost` on that same run | Denied spawns have no transcripts and cost $0. The 12 admitted workers are `claude-sonnet-5-5`. |
| L5 | Worker Agent call, a `.py` write, a `NEEDS_JUDGMENT` footer, then SendMessage to a worker | The worker has no Agent tool. `require-cursor-agent.py` blocks the write. The footer surfaces `WORKFLOW_WORKER_REQUEST` and the main-session judge returns STOP. |
| L6 | Env set, spawn mod not loaded | Gate denies `WORKFLOW_SPAWN_MOD_NOT_ACTIVE`. |
| L7 | Spawn mod loaded, worker guard not registered | Gate denies `WORKFLOW_GUARD_NOT_ACTIVE`. |

**Claude Code refuses to resume Workflow workers from the main session** ("No transcript found"), so the mod's `WORKFLOW_RESUME_DENIED` is defense in depth and is unit-tested only.

## Policy

```json
"workflow": {
  "opus_main_mode": "pinned_env",
  "worker_model": "sonnet",
  "allowed_worker_families": ["sonnet", "haiku"],
  "max_concurrent_agents_required": 4,
  "max_workflows_per_session": 3,
  "max_workers_per_workflow": 12,
  "max_workers_per_session": 30,
  "require_mod_call_attestation": true
}
```

`opus_main_mode` is `deny` or `pinned_env`. Anything else is `deny`.

## Evidence

Live probes on Claude Code 2.1.318 (2026-11-05) showed:

- Workflow `tool_input` is `{"script": "..."}` and may also carry `scriptPath`, `name`, or `resumeFromRunId`. `permission_mode` is present.
- PostToolUse Workflow returns `status: async_launched`, `taskId`, `runId`, `workflowName`, and `transcriptDir`.
- Workers are `agent_type: workflow-subagent`. SubagentStart has no model field. `transcript_path` on the worker is the main transcript. `prompt_id` matches the turn that launched the workflow.
- SubagentStop carries `agent_transcript_path` under `subagents/workflows/<runId>/` and `background_tasks[]` with `type: workflow`.
- The worker transcript's early lines include an attachment of type `model` with `identity.modelId`. Later lines can be enormous, so only the head is read.
- `CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL=haiku` forced both a model-less worker and a worker whose script said `model: 'sonnet'` onto `claude-haiku-5-5`. The env var overrides the script.
- Up to 16 workflow agents run concurrently by default. `CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS` (1–256) sets it. The hard limit is 1000 agents per run. A hook cannot block the spawn.
- A PreToolUse `updatedInput` without `permissionDecision` makes auto mode refuse the call. The short judge reference hit that in a live auto session.

## Limitations

- The spawn mod blocks a workflow `agent()` before it starts, including by rewriting the model to `worker_model`. A hook that throws without `.catch` is skipped and the spawn proceeds; this mod's `.catch` denies instead. A resume of a worker the mod recorded is denied. A resume of any other agent is not.
- The in-memory set used by that `.catch` is empty after a hot reload until `$.state` has been read. The resume check itself reloads the set from `$.state` before it decides.
- `require_mod_call_attestation` stays true. If the gate runs strictly before the mod's `tool.call` hook, the poll expires and the Workflow is denied.
- A worker that calls no tools is audited at SubagentStart (spawn log) and SubagentStop (model). The spawn itself is still blocked by the mod when the mod is loaded.
- Caps are counts, not dollars. `session-cost` is where the tokens show up.
- Loops can launch more `agent()` calls than the static count. The mod's per-run and per-session reservations catch that before `next`, and the guard's caps remain behind them.
- Cost is attributed after the fact. An Opus worker that already ran is `coverage_warning: unarbitrated_opus_agent <id>` plus an `unarbitrated_opus_run` event. A worker missing from the spawn log is `unarbitrated_workflow_spawn` and its tool calls are denied.
- `$.fs` has no append. The spawn log is rewritten from `$.state` after each reservation so two parallel spawns do not drop a line.

## Operations

```
opus-arbitration status
opus-arbitration show <request_id>
opus-arbitration disable
opus-arbitration enable
```

`disable` cancels pending judgments and unused grants. `enable` asks for the word `enable` on a terminal. Workflow rollback is the restore command in Activation and rollback. The ledger and the switch file stay.

Load the spawn mod from user-level `CLAUDE_CODE_PLUGIN_DIRS`. The fragment's `env` block is the document form (`<home>` stands for the account home). The probe settings at `/tmp/router-v2-probe/.claude/settings.json` use the literal home path. In a headless `claude -p` run those project-level `env` lines were not applied to hooks or to mod loading.

Workflow workers:

- Heartbeat: `~/.claude/logs/opus-arbitration/mod/<session_id>.json`.
- Call attestation: `mod/attest-<session_id>-<tool_use_id>.json`.
- Guard attestation: `guard/attest-<session_id>-<tool_use_id>`.
- Spawn log: `mod/spawns-<session_id>.jsonl`.
- These logs sit under `logs/opus-arbitration/` and are excluded from control-plane hashing. The mod source under `~/.claude/mods/**` is control-plane and is hashed.

- `workflows` rows: session, prompt, expected family, `run_id`, `task_id`, name.
- `agent_runs` rows with `kind=workflow_worker`: `workflow_id`, `status` (`launched`, `over_cap`, `stopped`), `verified_family`, `resolved_model`.
- Events `workflow_worker_started`, `unarbitrated_opus_run`, and `unarbitrated_workflow_spawn` in `events.jsonl`.
- Surface flags in `~/.claude/logs/opus-arbitration/surface/`.
- `session-cost <session_id>` lists admitted workers under their `runId`, the `workflow_worker` cost bucket, and a count by actual model. Denied workflow spawns are `gate_denied` workflow reasons plus spawn-log records with `decision` `deny`. Those deny records have no transcript and zero cost. `subagents/workflows/<runId>/journal.jsonl` is the workflow journal and is not listed as an agent.

## session-cost

Workflow transcripts are scanned with the rest of the session tree. `subagents/workflows/<runId>/journal.jsonl` is skipped, so it is not an agent and it does not warn "jsonl without meta.json". Each worker's parent is the workflow `runId`, and that node hangs off the main session. Arbitration cost includes `workflow_worker`. Opus workflow workers are coverage warnings even though they are classified. ccusage entries for those transcripts still join on timestamp, model, and token key; a matching fixture has zero unmatched entries. Denied workflow spawns count spawn-log records with `decision` `deny`, and the report confirms none of those records has a transcript.
