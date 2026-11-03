---
name: wf-refactor-skill-composition
description: Diagnose and refactor one of the user's workflow or orchestrator skills - how it uses other skills, sub-agents and external agents. Gathers evidence first (dependencies, what helpers change, commands it runs, real runs from transcripts, hook and delegation policy, what each derived copy's platform can execute, contracts and run state, and every file that refers to or copies the skill), labels findings observed, inferred or assumed, and dry-runs the skill to catch edits in the wrong place, stops that don't stop, control logic in prose and steps with no failure path. Proposes reviewable change sets with exact diffs and acceptance criteria, applies one after approval, regenerates derived workflows and verifies. Never edits other people's skills. Use when asked to diagnose, review or refactor a workflow skill, "should B be a sub-agent", split or merge skills, fix how a skill calls others, add stage contracts or run state, or /wf-refactor-skill-composition <skill> [--check].
argument-hint: "<skill name | skill folder> [extra skills dir ...] [--check to only diagnose]"
---

# Refactor Skill Composition

Target: `$ARGUMENTS`. The first word is the skill to diagnose (call it **A**);
more paths add skills folders or project roots to search. With `--check`, stop
after step 6 (report only).

The architecture this skill works toward:

> **Skills** define reusable capabilities. **The orchestrator** controls workflow
> and decisions. **Agents** provide optional execution isolation. **Stage
> contracts** preserve correctness across boundaries.

The goal is a composition that is reliable, measurable, maintainable and
recoverable — not only a smaller context.

Read before step 5: `references/composition-rules.md` (execution models and
decision tables), `references/stage-contracts.md` (contract format and
invalidation rules, and run state — the control plane), `references/measuring.md`
(what the measuring script reports and how far to trust it) and
`references/platforms.md` (what a derived copy's platform can actually execute). `composition-rules.md` summarizes an Obsidian
note; if the note has a newer `updated:` date, read the note and prefer it.

This skill runs in the main conversation: judging needs the user, and applying
needs their approval. Do not hand the apply step to a sub-agent.

If you hand read-only work to a sub-agent (diagnosing other skills in parallel,
an independent review), call the Agent tool with `model: "sonnet"`. Without it,
the agent inherits the session's model.

## Evidence labels

Every claim in the report carries one label and its source:

- **Observed** — seen in a file, a transcript or a command's output; cite it
  (`file:line`, transcript id + request number, command + output).
- **Inferred** — follows from observed facts by a stated step ("A invokes 6
  skills in stage 1, each 300+ lines, so stage 1 likely loads >8k tokens").
- **Assumed** — not checked (no run exists, a file could not be read, behaviour
  of a tool not verified). Say what would turn it into an observation.

Never present a line count as context usage, a mention as a call, or a
recommendation as a fact. If the evidence for a finding is only assumed, the
fix for it is "measure", not "change".

## 1. Resolve the target and check ownership

```bash
python3 ~/.claude/skills/wf-refactor-skill-names/scripts/audit_skill_names.py [extra dirs]
```

- A must be owner `mine`. `ext:...` → stop and say why. `unknown` → ask.
- Helpers not owned by the user may be diagnosed, never edited, renamed or
  moved. Changes involving them are notes for their owner, or changes in A only.
- Note whether A's folder is a git repo shared with others (`git remote -v`);
  every later change set then needs its own reviewable diff.

If no target is given, list the user's `wf-`/`or-` skills and ask which one.

## 2. Static map (what A *mentions*)

```bash
python3 ~/.claude/skills/wf-refactor-skill-composition/scripts/map_skill_deps.py <A> [extra dirs]
```

JSON with A's frontmatter, files, signal lines; each mentioned skill and agent
with frontmatter and `used_by` (`single_use: true` = only A mentions it); agent
preloads that don't exist or can't be preloaded; `mentioned_by_only` (skills that
name A while A never names them); `mcp_tools` (each `mcp__<server>__<tool>`
A names, `NOT CONFIGURED` when no user or project MCP config has that server);
per helper, `effects` (write hints, whether it mentions delegation, file-changing
commands in its code); `commands` (shell commands in A's own text with their
SKILL.md line, `mutating` or `read-only`, `negated` when the line says "do not");
and `searched` (the skills folders it looked in — A's own project is always
included, so check this list rather than the current directory).
The `effects` and `commands` fields are heuristics: they point at lines to read.
`commands` marks lines it can't judge (`python3`, `npx`, unknown shell lines) as
`unclassified`: read those. Line numbers are SKILL.md line numbers. For "who
uses this skill", run `map_skill_deps.py --consumers <name>`: it also searches
hook scripts, settings, project `.claude` folders and `.vscode/workflows`.
These are **mentions**: read each hit to tell a call from a "see also". Also find
what the script can't see: built-in agents in prose, plugin skills
(`plugin:skill`), `/commands`, external CLIs (`cursor-agent`, `codex`, ...),
Workflow scripts, scripts in other folders.

### Helpers A should use but doesn't

The map only shows what A names. Check what A *ought* to name:

- **Reverse links.** Read each `mentioned_by_only` skill: one that calls A its
  consumer, or owns a step A does by hand (e.g. "open the Figma file", "add a
  tracking event"), is a missing helper. One that merely lists A is not.
- **Routing.** Read the project's task routers and rules — CLAUDE.md, `rules/`
  ("Related Skills"), navigator skills with task → skill tables — for the kind of
  work each of A's steps does. A step that does work a router assigns to another
  skill is a missing helper.
- **Required routes.** A step whose work a policy or hook routes elsewhere (e.g.
  all coding through `cursor-agent`) must say so; if it doesn't, that is a
  missing delegation, not just a style issue.
- **Tools.** Every `NOT CONFIGURED` MCP tool, and every CLI A calls that isn't
  installed (`command -v`), means the step can't work as written; find the
  skill or tool that does that job now.

Also record A's **consumers**, for compatibility: grep the old name (whole
word, plus `/<name>` and `Skill(<name>)`) in `~/.claude`, the project's
`.claude` (skills, agents, commands, workflows, rules, settings, CLAUDE.md),
`.vscode/workflows`, and docs. Each hit is a consumer a change must keep working.

Separate the **derived copies** among them: files generated from A's content,
such as a CC Workflow Studio workflow (`.vscode/workflows/<A>.json`, made by
`wf-convert-skills-to-workflows`) and its `originals/` backup. A copy can
carry A's defects in its own form (a skill node that executes, a branch that
merges back), and people may run the copy instead of A. Diagnose each copy
with the same checks in step 5, and plan to regenerate it after any change
(step 7).

## 3. Runtime evidence (what A *does*)

Find real runs of A in transcripts, under its current **and former** names:

```bash
python3 ~/.claude/skills/wf-refactor-skill-composition/scripts/find_skill_runs.py <A's folder>
```

It takes former names from git (every folder name and `name:` the SKILL.md had),
counts only real invocations (Skill tool calls and typed `/name` commands, in
main and sub-agent transcripts, ignoring replayed `api-request-blob` rows), and
reports transcripts that only list A as `transcripts_listing_only`. If
`git_history` is false, former names are unknown: say so, and ask the user
whether A had another name. For each real run, measure it:

```bash
python3 ~/.claude/skills/wf-refactor-skill-composition/scripts/measure_skill_run.py <session id> \
  --stage-regex '<pattern for A's stage markers>'
```

From the output, record per run: which skills **actually loaded** (and their
estimated tokens), context at each stage start and peak, delegations and
whether any ran in the background, compactions, tool errors, duration. Follow
the caveats in `references/measuring.md` (non-Claude backends, external CLI
work not in the transcript).

**If no run exists**, say so plainly. Every runtime claim (context pressure,
slow stages, flaky gates) is then **assumed**, and the first change set must be
a baseline run (Phase 0 in `references/measuring.md`) before any change that is
justified by runtime behaviour.

## 4. Policy and delegation (what A is *allowed* to do)

Read, and cite:

- **Hooks** in `~/.claude/settings.json`, the project's `.claude/settings*.json`
  and the hook scripts they run: what each blocks or rewrites, for whom (this
  Claude session only? sub-agents? external CLIs?), and the approved route the
  hook names. A proposal that moves blocked work to another agent must follow
  that approved route, never work around it.
- **Delegation config**: where each delegate's model, permissions and working
  directory come from (`.claude/agents/*.md`, `.agents/*.md`, plugin skill
  files such as `runner:sub-agents`, env vars the hook reads). There should be
  one source per setting; a copy hard-coded in A is a drift risk.
- **Completion behaviour** of each delegation: foreground or background, how A
  learns it finished or failed, what A checks afterwards (diff, exit codes).
  Read the delegate's **actual result format** in its own definition and cite
  it (e.g. the `runner:sub-agents` skill returns JSON with `status`
  `success | partial | error`, `exit_code` and a default 600 s timeout; a
  `partial` result means "timed out, not done"). A's delegation step must say
  what it checks in that result before it moves on.
- CLAUDE.md and `rules/` that A restates: duplicated rules drift.

Distinguish a **provider change** (different engine, model, billing or
permissions) from a **configuration fix** (same engine, setting read from its
single source). Never propose the first as if it were the second.

### 4b. Platform of each derived copy

For each derived copy, establish what its platform can execute — from
`references/platforms.md` if it covers the platform and version, otherwise from
the platform's schema or docs (and add it there). Record: who executes the copy
(for CC Workflow Studio: Claude, reading the exported SKILL.md — there is no
engine), which node types run what, which have failure ports, and whether it
has state, counters, limits or timeouts.

Every fix you propose later carries one platform-fit label: **native**, **needs
a deterministic script** (a script the copy calls, e.g. `tl-route-run-state`),
or **not possible here**. Never propose engine-owned state, engine-enforced
limits or an executor node as native on a platform that has none — including
when a reviewer recommends them.

## 5. Diagnose

Write A's stages in order; for each, what it reads, decides, edits, delegates
and passes on — and whether what it passes on is a **file** or only **chat
history**.

### Dry run

Then walk through one imagined run of A, step by step, as the main session
would follow it — and, for each derived copy, the way that copy would run
(e.g. a workflow's skill nodes in `execute` mode run the helper *there*).
For a workflow copy, first run the checker — it finds the control-flow
defects that follow fixed rules:

```bash
python3 ~/.claude/skills/wf-refactor-skill-composition/scripts/check_workflow_copy.py <copy.json>
```

Each finding is a heuristic pointing at a node; read the node before reporting
it. Then walk the copy node by node anyway: read the text **inside** every
prompt node, because a stop or condition written in prose inside a prompt with
a single way out does not stop or branch anything (D24).

For every step write one row:

| Step | Who acts (main session / helper / delegate) | What it reads | What changes (files, outside effects) | Before or after approval? | Allowed? (policy, hook, A's own division of work) | What it passes on (file / record / chat) | Decision input from (state file / record / chat) |
|---|---|---|---|---|---|---|---|

Fill "what changes" from the facts, not from A's intent: a step that says
"follow helper X" or invokes X does whatever X does (`effects` in the map, then
X's text); a command does what `commands` says. Fill "allowed" from step 4. A
row where the main session changes files that the policy, a hook or A itself
assigns to someone else is a finding (D22, D23). A row that changes the repo
(or has outside effects) **before** the user's approval gate is a finding even
when the actor is allowed to make that change (D26). Also note:

- every place the run has to stop or choose — check that A really stops or
  branches there (D24);
- every rule A says must hold ("DO NOT deviate", "must match", "never") —
  check that some step can **fail** on it and that the failure has a path; a
  branch that only *classifies* (which family, which type) does not validate
  values within the class (D24);
- every cycle — check it has a limit, separates failure kinds (fix the work vs.
  fix the plan) and has an exit; a cycle through a user question is paced by
  the user and is fine (D27);
- every approval gate — check it names what is approved (a file path) and
  collects the requested changes before looping back (D9);
- every sample, template or table — check it against A's rules, and check it
  would work as written (names it uses are defined, APIs exist) (D25);
- every decision — where its input comes from. A count, a limit, a version or a
  delegate's result that only a prompt keeps (or chat remembers) is control
  logic in prose (D28); so is a prompt that counts or decides on its own;
- every step that executes something (a helper in `execute` mode, an MCP call,
  an agent, a delegation) — check that its outcome is recorded and a decision
  right after it has a failure branch (D29);
- the shape of the graph — straight runs of 3+ nodes that only carry content
  inflate the control graph (D30).

For **each helper**, answer the three questions in `composition-rules.md`
(separate skill? same conversation? which execution model?) using the observed
evidence. Prefer, in this order: put knowledge where its owner is → enforce in
tooling what can be enforced → progressive disclosure (split long skills into
loaded-on-demand files) → isolation (fork/agent) only when a measured cost or
an isolation need justifies it.

Then check A for these defects. Each finding needs a label and a citation.

| # | Defect | Signs | Usual fix |
|---|---|---|---|
| D1 | Single-use separate skill | `single_use: true`, no concrete reason to separate | Fold into A as `references/`, `scripts/` or steps |
| D2 | Reasoning used for deterministic work | Steps that list/search/rename/verify by pattern "by hand" | Script, written by the route hooks allow |
| D3 | Measured context pressure | **Observed** high context or compactions at a stage (not line counts) | Progressive disclosure first; then fork/agent with a contract |
| D4 | Missing wait or unchecked result | A later step uses a fork's, agent's or CLI's result; nothing says wait, nothing checks the delegate's actual result (its status / exit code / timeout, from its definition), or a partial or timed-out result is treated as done | `background: false`, or an explicit wait + a check of the delegate's real result fields; record what changed (`git status`) against the hand-off's allowed files |
| D5 | Fork without history | `context: fork` skill relies on earlier chat | Make it stand alone with a contract, or `/subtask` |
| D6 | Preload not used | Agent `skills:` never referenced in its body | "perform X's procedure using Y's rules" |
| D7 | Blocked preload | `preloads_blocked` / `preloads_unknown` not empty | Fix the flag/name or invoke inline |
| D8 | Too many tools | Read-only role with Edit/Write/Bash or no `tools:` | Restrict `tools:` |
| D9 | Contract below the needed level | Data or decisions cross a boundary (stage, fork, agent, external CLI) only through chat; a gate can't be rechecked from files; an approval gate doesn't name what it approves (no path) or loops on "needs changes" without asking what to change; a gate may be skipped ("already approved") but only prose says when | Raise to the lowest safe level in `references/stage-contracts.md` (L1 hand-off file, or L2 stage records) and state why that level; gates name the file and collect the change request; a skippable gate is a decision on approval validity (approved version = current version, from run state); if a parent orchestrator calls A, say whether its approval carries over |
| D10 | No recovery / unsafe retry (L2 skills only) | Multi-stage or side effects: no record of passed gates, resume relies on memory, no fingerprints, re-running repeats side effects | Fingerprinted records, transitive invalidation, retry rules |
| D11 | Apply in a sub-agent | Edits needing the approved plan happen in a fork/agent | Move apply into A's main conversation |
| D12 | Orchestrator forked | A itself has `context: fork` | Remove it |
| D13 | Fake parallelism | Dependent stages in parallel, or independent heavy work strictly serial | Sequential hand-offs / parallel agents |
| D14 | Worktree surprise | `isolation: worktree` where the current branch matters | Remove, or name the branch |
| D15 | Fragile reference | Helper named vaguely or by a stale name; an MCP tool or CLI that isn't configured/installed; config value copied from its source | Exact name; the current tool or skill; read config from its single source |
| D16 | Knowledge in the wrong home | A holds rules that belong to a helper or to repo-wide rules, so the helper used alone lacks them | Move to the owner; leave a one-line pointer |
| D17 | Prompt-only enforcement | A rule that a tool, generator, config or CI could enforce is only written as instructions | Fix the tool / add a check; keep the rule as explanation |
| D18 | Oversized skill | SKILL.md over ~500 lines with detail that's only needed sometimes | Split into `references/` loaded on demand |
| D19 | Delegation config drift | Model/permissions hard-coded in A differ from the delegate's definition | Use the approved route that reads the definition |
| D20 | Naming (informational) | Type prefix may not match the convention in `wf-refactor-skill-names` | Separate migration only; never bundled with behaviour changes |
| D21 | Missing helper | Found by "Helpers A should use but doesn't": A does by hand what another skill owns, or skips a required route | Call that skill (or route) at that step; drop A's duplicate text |
| D22 | Helper acts in the wrong place | Dry run: a helper A runs or "follows" changes files (or has outside effects) in a context where the policy, a hook or A's own split of work says someone else does that — e.g. before the hand-off to the delegate | Use the helper as reference only (in a workflow: skill node `executionMode: "load"`) and put its decisions into the hand-off. If only part of the helper may run: when the helper is the user's own, give **it** a mode for that (e.g. "stage only") — a fix in its owner; only for someone else's helper restrict it per caller (`executionPrompt`), and flag that the restriction contradicts the helper's own steps |
| D23 | Command changes files in a read-only role | `commands`: a `mutating` command (not `negated`) that A tells the main session (or a read-only agent) to run, where the policy allows only read-only commands; worse when no hook catches it | Give it to the delegate; replace with a read-only check (e.g. `dart format --output=none --set-exit-if-changed`); note the hook gap for its owner |
| D24 | Stop, condition or check that doesn't hold | Dry run: "ask / flag / wait / stop" with nothing that blocks; "if / when / only when / for any … it needs" with no branch; a must-hold rule that no step can fail on (a branch that only classifies, with no check of values); in a workflow copy, a stop written inside a prompt node with one way out, a stop option that merges back, a conditional step that always runs, or a "fix it" option that continues without a fix step and a re-check | A blocking question or end, with options for what happens next; an explicit condition (a branch in the copy); a real check with a failure path; "fix" options that do the fix and loop back to the check |
| D25 | Self-contradiction or broken sample | A sample, template or table says something A's rules or branches contradict (a shared code sample hard-coding a value one branch must change, a sample using a style the table forbids), or a sample would not work as written (undefined names, APIs that don't exist) | Make the value a parameter or move it to the branch; one source per rule inside A; fix the sample so it is correct when copied |
| D26 | Changes before approval | Dry run: a step changes the repo or has outside effects before the user's approval gate (e.g. downloads written into source folders) — stopping at the gate leaves unapproved changes | Stage in a gitignored scratch folder (the project's own, if it has one) and apply after approval, by whoever owns the change |
| D27 | Unbounded or blind loop | An automatic cycle (no user question in it) with no limit, or one that retries the same work for a failure that needs a different fix (e.g. a wrong plan retried as an implementation bug), or without an exit | State a limit (e.g. at most 2 automatic retries, counted in the hand-off file); split failure kinds — implementation → retry, plan/spec → back through revision and approval; after the limit, stop with a diagnostic report |
| D28 | Control logic in prose | A decision branches on a count, limit, version or a delegate's result that only prompt text keeps; a prompt counts attempts, compares versions or decides a retry by itself; control is split between edges and prose; a route switch's `default` stands for a real route (e.g. "route is escalate"), so an unexpected route or a rejected event is treated as that route | Move control into run state (`references/stage-contracts.md` → Run state): prompts `record` events, decisions follow `route`. Every route switch has an explicit branch per expected route and `default` → a control-error stop. Platform fit on CC Workflow Studio: **needs a deterministic script** (`tl-route-run-state`) |
| D29 | Executed step with no failure path | A node that executes something (skill in `execute` mode, `mcp`, `subAgent`, a delegation prompt) is followed by a plain step, so a failure has nowhere to go | Record the outcome (`record step-failed` / the delegate's status) and put a decision right after it with a failure branch — ideally to one shared failure handler (`handle-failure` / `escalate`) |
| D30 | Content inflating the control graph | A straight run of 3+ prompt nodes (one way in, one way out) that carry content, not control | Merge them into one node, text word for word; keep the graph about control flow |

If a choice is genuinely open, list the options with their trade-offs and what
evidence would decide it.

## 6. Report and propose

Write the report to `<scratch>/composition/<A>.md` (the session scratchpad or
the project's gitignored scratch folder), show it, and wait for approval. The
approval question names that file and the set ("Apply set 1 from <path>?");
"needs changes" asks what to change, updates the file and asks again. Sections,
in this order:

1. **Evidence summary** — runs found (or "none"), measurements with their
   kind, policies read, consumers found, and what could not be checked.
2. **Current structure** — tree: main conversation → A → helpers, with each
   helper's execution model and what passes between stages (file / record / chat).
3. **Findings** — `| # | Defect | Label | Where | Evidence | Fix |`
4. **Helper decisions** — `| Helper | Owner | Used by | Loaded in runs? | Now | Proposed | Why |`
5. **Change sets** — group the fixes so each is reviewable alone, in this order:

   | Set | Contents | Needs |
   |---|---|---|
   | 0. Baseline | Measured run of A as it is now | A real task from the user |
   | 1. Text fixes | References, moved knowledge, explicit waits, config read from source | — |
   | 2. Contracts & recovery | Raise contracts to the chosen level (L1 hand-off, or L2 records with fingerprints, invalidation, retry rules) | L1: nothing; L2: baseline |
   | 3. Tooling | Generators, scripts, CI/config that enforce rules | Owner approval for repo-wide changes |
   | 4. Isolation | Fork/agent/progressive splits | Measured justification from set 0 |
   | 5. Migration | Renames, removing metadata, moving skills | Consumer list + rollback plan |
   | + Derived copies | Regenerate every derived copy after the set it follows (not a set of its own) | The converter skill; backups of the old copy |

   For each set: the files it touches, its **platform fit** for every derived
   copy (native / needs a deterministic script / not possible here, from 4b),
   the **exact diff** (for set 1 and any set about to be applied), and
   **acceptance criteria** — what must hold on
   the baseline task after the change (same gates pass, no new tool errors,
   consumers still resolve, and the measured metric it targets moves the
   expected way).

Use AskUserQuestion only for real choices. The user may edit rows; apply their
edits.

Offer, in one line, a second opinion that isn't anchored on yours: a fresh
agent that gets only A's files, its derived copies and the policy files (not
your findings) and reports what it would change. Run it only if the user
wants it, then reconcile its findings with yours, labelled as above.
**Recommend** it (still in one line) whenever a proposed change set adds
control flow — gates, loops, branches, delegation steps: new control flow is
where a single reviewer most often misses defects.

With `--check`, stop here.

## 7. Apply — one change set at a time

- Apply only the approved set, as written in the report file the approval
  named — re-read it; don't rebuild the set from chat. If the file is missing
  or changed since the approval, ask again. Never combine a set with another
  set, or with changes to this skill.
- In a shared repo, make the change on a branch (or as an uncommitted diff the
  user reviews); warn that it affects everyone who pulls it. Do not commit or
  push unless asked.
- **New files first** (`references/`, `scripts/`, agent files next to A's
  skills folder: `~/.claude/agents` or `<project>/.claude/agents`). Write code
  by the route the hooks allow. Test every new script on real input.
- **Rewrite A's steps** to point at the new homes. Each delegation says how A
  waits, what it passes in, what record comes back. Keep approvals and apply
  steps in the main conversation. Keep A's style.
- **Folded helpers**: recheck `used_by` and consumers; only if nothing else
  uses it, ask before moving it to `~/.claude/skills/.trash/` (never delete);
  remove it from **Mine** in `wf-refactor-skill-names/ownership.md`.
- **Control plane**: a D28/D29 fix uses run state through `tl-route-run-state`
  (one state file per run, in the project's gitignored scratch folder); the copy's
  decisions read its `route`, its prompts `record` events. Don't build a second,
  per-skill counter mechanism.
- **Derived copies**: after the set is applied, regenerate each derived copy
  from the new A (for a workflow: `wf-convert-skills-to-workflows <A> --root
  <the project that holds A's .claude/skills> --force`,
  which keeps the old JSON in `backups/`). Ask the user to close the copy in
  the IDE first.
- **Names**: new skills/agents follow the convention in
  `~/.claude/skills/wf-refactor-skill-names/SKILL.md`; add new skill paths
  under **Mine** in its `ownership.md`. Renames of existing skills only as set 5,
  through `wf-refactor-skill-names`.

## 8. Verify

- Static: rerun `map_skill_deps.py` (intended helpers, no blocked preloads, no
  folded helper still mentioned, no `mutating` command left in a read-only
  role), the audit script (names `ok`), and the consumer grep from step 2
  (every consumer still resolves).
- Diagnose the **new** version in full, not just the old findings: run all of
  step 5 again (map, `check_workflow_copy.py` on each regenerated copy, dry
  run with every column) on A and its copies, and look hardest at what the
  change set **added** — new gates, loops, branches and delegation steps are
  new places for D4, D9, D24, D26 and D27. Every old finding is gone, and the
  additions bring no new ones; if they do, fix them and check again — at most
  2 rounds, then stop, report what is still open and ask whether to continue,
  revert the set or keep it as is.
- Runtime: rerun the baseline task in a new session, measure it the same way,
  and compare with the baseline against the set's acceptance criteria. Report
  differences with their labels; a set whose criteria are not met is not done.
- Report what changed, what was skipped and why, external helpers left alone,
  and the first-run checklist from `composition-rules.md`. Remind the user
  that a running session keeps the old skill and agent list until restarted.
