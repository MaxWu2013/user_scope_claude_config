# What a derived copy's platform can execute

Before proposing a fix for a derived copy, know what its platform can actually do.
Label every proposed fix: **native** (the platform does it), **needs a deterministic
script** (Claude calls a script the copy names), or **not possible here**.

## CC Workflow Studio (checked against extension 3.36.0 and `@cc-wf-studio/cli` export, 2026-10-23)

**Who executes:** nobody but Claude. Export turns the JSON into a SKILL.md (a Mermaid
diagram plus a "Workflow Execution Guide"); Claude reads it and follows it. There is no
engine that runs nodes, holds state or enforces anything at run time.

| Need | Platform support | Use instead |
|---|---|---|
| Workflow state, variables passed between nodes | none (`prompt.variables` are template placeholders only) | a state file + `tl-route-run-state` |
| Counters, loop limits, timeouts | none (no such fields in the schema) | the state file's limits; `route` prints `escalate` |
| Error / failure ports | none: every node has fixed output ports; executing nodes have exactly 1 | a decision node right after each executing node, fed by what the step recorded |
| Branching | `ifElse` (2), `switch` (2–10, last = default), `askUserQuestion` (2–4 options) | — |
| Decision input | `evaluationTarget` (exported as **Evaluation Target**) + branch conditions, judged by Claude | point `evaluationTarget` at a recorded value (e.g. "the route printed by run_state.py") |
| Running a skill | `skill` node: `executionMode` `execute` (runs it) or `load` (reference only); `executionPrompt` ≤ 2000 chars | — |
| Running an external CLI agent (e.g. cursor-agent) | none: `codex` node is "Phase 1, CLI execution not yet implemented"; `subAgent` / `subAgentFlow` are Claude Code sub-agents (`subAgentFlow` has exactly 1 output) | a prompt node that runs the approved route (e.g. `/runner:sub-agents`) and records its JSON result |
| Human joint work mid-run | `branchSession` (Claude Code `/branch` … `/resume`) | — |
| Cycles | allowed by the validator (no limit) | bound them by state (`escalate`) or a user question |

Consequences for the checks:

- "Engine-owned state" or "engine-enforced limits" → **needs a deterministic script**.
- "Executor node for cursor-agent" → **not possible here**; the contract lives in the
  delegation prompt plus the recorded result.
- A failure path is always a decision node after the step; it cannot be a port.

## Other platforms

Record the same table before relying on a feature: who executes, state, limits,
failure handling, branching inputs, external executors. If you cannot verify a feature
from the platform's schema or docs, label the fix **assumed**.
