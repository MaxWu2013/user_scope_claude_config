---
name: tl-route-run-state
description: Keep a skill or workflow run's control state in a file and decide the next step deterministically - spec and approval versions, delegate status, bounded retries, failures and stops - instead of counting and deciding in prompt text. One script (run_state.py) with init, record <event>, route and get; route prints a single token (write-spec, approve, execute, retry-execute, verify, revise-spec, handle-failure, escalate, done, stopped) that the orchestrator or a workflow decision node follows. Use when an orchestrator or CC Workflow Studio workflow has retries, approval gates, a skippable approval, delegation results or failure handling, when asked to "add run state", "make retries deterministic", "track attempts", or when wf-refactor-skill-composition reports D28 (control logic in prose).
argument-hint: "<init|record|route|get> <state.json> [event] [--limit executor=N] [--detail TEXT]"
---

# Route Run State

The control plane in code. The orchestrator (a skill, or Claude following an exported
workflow) does the work; this script owns the counters and decides what comes next.
Prompts never count attempts or compare versions themselves.

```bash
S=.figma_tmp/<run>/run.json          # any gitignored scratch path of the project
R=~/.claude/skills/tl-route-run-state/scripts/run_state.py
python3 $R init   $S --workflow <name> [--limit executor=2] [--limit verification=2]   # --resume continues an existing run
python3 $R record $S <event> [--detail "<what happened>"] [--event-id ID] [--step ID] [--file SPEC] [--hash SHA256]
                                                            # prints the next route, or exits 2 "rejected: …"
python3 $R route  $S [--json]                              # the next route, again
python3 $R get    $S [field]                               # the whole state, or one field
```

`init` refuses an existing state: `--resume` prints its current route and continues it,
`--force` starts over. Writes are atomic and locked (`<state>.lock`), so two writers can't
lose an update. Limits are retries: `executor=2` allows two re-runs after the first
failure; the third failure routes to `escalate`.

**Only legal events are accepted.** Each event is legal in certain phases (below); `done`
and `stopped` are terminal; while the route is `escalate` only `more-attempts` and `stop`
are accepted. An illegal event exits 2 with `rejected: <why>` and changes nothing — treat
that as a control error: report it and stop, don't retry blindly.

- `--event-id ID` makes an event idempotent: recording the same id again is a no-op (use
  e.g. the delegate's run id, so a replayed result isn't counted twice).
- `--step ID` on `step-failed` names the step; `step-recovered --step ID` must match it,
  and `get <S> failed_step` tells a resumed run exactly where it failed.
- `--file SPEC` on `spec-written` stores the spec's sha256. Approval then covers that
  content: a changed spec needs approval again, and `approval-reused` must give the same
  content (`--hash` or `--file`), or it is rejected.

## Events → what changes

| Event | Record it when | Legal in phase | Effect |
|---|---|---|---|
| `spec-written` | a spec / plan version is written or revised | spec | version +1 (and hash with `--file`); needs approval |
| `approved` | the user approves the current version | approval | approved version (and hash) = current |
| `approval-reused` | a parent orchestrator already approved this exact spec | approval | same as `approved`; hash must match if stored |
| `revise` | the user asks for changes | approval | back to the spec |
| `executor-success` / `executor-partial` / `executor-error` | the delegate returned that status | execute | success → verify; partial/error count an executor retry |
| `verify-pass` | every independent check passed | verify | done |
| `verify-fail-impl` | a check failed because the work is wrong | verify | counts a verification retry |
| `verify-fail-spec` | a check failed because the spec is wrong | verify | back to the spec; counters reset |
| `step-failed` / `step-recovered` | any other executed step failed / was fixed | any working phase / failed | handle the failure / return to where it failed (`--step` names it) |
| `more-attempts` | after `escalate`, the user allows more | only while escalated | counters reset; next is a retry |
| `stop` | the user stops | any non-terminal phase | stopped (terminal) |

## Routes → what to do

| Route | Do |
|---|---|
| `write-spec` / `revise-spec` | write or revise the spec, then `record spec-written` |
| `approve` | show the spec, ask; then `approved`, `revise` or `stop` |
| `execute` / `retry-execute` | run the delegate (same spec on a retry), then record its status |
| `verify` | run the independent checks, then `verify-pass` / `verify-fail-impl` / `verify-fail-spec` |
| `handle-failure` | report `last_error`; fix and `step-recovered`, or `stop` |
| `escalate` | stop and report (`get` shows history and `last_error`); ask: `more-attempts` or `stop` |
| `done` / `stopped` | end |

## In a CC Workflow Studio workflow

After each node that does or decides something, a prompt records the event, and the next
decision node branches on the printed route:

- `switch` with `evaluationTarget: "the route printed by run_state.py"` and one explicit
  branch per route that can follow — including `escalate` — and `default` → a control-error
  stop (an unexpected route or a `rejected:` exit is a bug, not a retry).
- An approval that may be reused is an `ifElse` on `run_state.py get <S> approved_spec_version`
  vs `spec_version`, not prose.
- Loops need no limit text in prompts: the limit lives in the state, and `escalate` is the exit.

Run the tests after changing the script:

```bash
python3 -I ~/.claude/skills/tl-route-run-state/scripts/tests/test_run_state.py
```
