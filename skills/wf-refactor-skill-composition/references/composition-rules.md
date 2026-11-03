# Skill composition rules

Condensed from the Obsidian note
`~/workplace/project/Obsidian_doc/30_references/tools/skill-vs-subagent-integration.md`
(checked against Claude Code v2.1.309 on 2026-10-23). If the note is newer
than this file, read the note and prefer it.

Keep in sync with `~/.claude/skills/kn-skill-subagent-composition/` (SKILL.md and
`references/`), a copy of the same note. When you change a shared rule here,
change it there too.

A **skill** is *what* to do (instructions, knowledge). A **sub-agent** is *where*
it runs (a separate context). Calling a skill does not make it a sub-agent.

Principle: skills define reusable capabilities; the orchestrator controls
workflow and decisions; agents provide optional execution isolation; stage
contracts (`stage-contracts.md`) preserve correctness across boundaries.
Isolation is a cost-benefit decision that needs measurements (`measuring.md`);
the contract is needed either way.

Keep each SKILL.md under ~500 lines; move detail needed only sometimes into
supporting files that load on demand. Splitting a long skill this way is
cheaper than isolating it in an agent and should be tried first.

## Three questions per helper

For each helper B that orchestrator A uses, decide separately:

1. Should B exist as a **separate skill** at all?
2. Should B run in the **same conversation** as A?
3. If not, which **execution model** should it use?

## Execution models

| # | Pattern | Context the work sees | Mechanism | Best for |
|---|---|---|---|---|
| 1 | A → Skill B (inline) | Shared with the main conversation | Skill tool | Reusing instructions and knowledge |
| 2 | A → forked skill B | Fresh, with the skill's text as its task | `context: fork` (+ `agent:`) in B's frontmatter | A standalone task that should always run isolated |
| 3 | A → custom agent with skills | Fresh, with skills preloaded | `.claude/agents/<name>.md` with `skills:` and `tools:` | A reusable specialist with controlled tools |
| 4 | A → conversation fork | Inherits the whole conversation | `/subtask <task>` (or fork mode) | A side task that needs earlier context but whose output shouldn't clutter the main thread |

Facts to keep in mind:

- Inline use is the model following instructions, not a function call. Critical steps need verification.
- `context: fork` starts a **fresh** sub-agent with no chat history. If the task depends on history, use `/subtask`.
- Custom and general-purpose agents also load CLAUDE.md and git status (unless `omitClaudeMd`). Explore and Plan skip both.
- `skills: [...]` in an agent **preloads** full text (first 32 names). It does not run them. The agent body must say how to use them. Skills with `disable-model-invocation: true` can't be preloaded.

## Separate skill or fold into A?

Default: a helper that only A uses belongs inside A. A separate skill adds
coupling (A refers to it by name in prose, so a rename can break A silently).
Keep it separate only for a concrete benefit: independent invocation,
separate testing, separate ownership, a large procedure clearer on its own,
or execution isolation.

| Helper kind | Only A uses it | Shared |
|---|---|---|
| Rules / knowledge | `references/<topic>.md` in A | Independent skill, invoked inline |
| Deterministic file processing | Script in A's `scripts/` | Shared script or skill |
| Procedure | Steps in A's `SKILL.md` | Independent skill |

## Where should the work run?

| Workload | Preferred implementation |
|---|---|
| Short knowledge or rules, only used by A | A's `references/` |
| Shared knowledge used by several workflows | Independent skill, inline |
| Deterministic file processing (search, list, rename by pattern) | Script |
| Small sequential reasoning task | Inline steps in A |
| Large independent research or analysis that floods the context | Forked skill or custom sub-agent |
| Reusable specialist with controlled tools (e.g. read-only) | Custom sub-agent with preloaded skills |
| Side task that needs a lot of earlier conversation | `/subtask` |
| Task needing frequent user decisions | Main conversation |
| Several independent reasoning tasks | Parallel sub-agents |
| Dependent reasoning stages | Sequential sub-agents with explicit handoffs |

A sub-agent needs an **input contract**, **one responsibility** and a bounded
**output contract**. Detailed findings can go to a file; only a summary and
file paths come back. If you can't define these, keep the work inline.
Parallelism is an advantage, not a requirement.

## Coordination

- **Waiting.** A `context: fork` skill runs in the background by default; set
  `background: false` to wait in the same turn (it also waits in `-p`/SDK mode
  and with `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`). Spawned sub-agents also
  start in the background. If D depends on C, A must say explicitly to wait.
- **Approvals.** Sub-agents can ask for permissions, so keeping the apply step
  in the main conversation is not technically required. It belongs there
  because applying changes needs the shared, approved plan and a controlled order.
- **Nesting.** Up to 3 layers below the main conversation by default
  (`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`). Keep orchestrator A itself in the
  main conversation (no `context: fork`).
- **Filesystem.** Sub-agents edit the same working directory. `isolation: worktree`
  branches from the **default branch**, not current `HEAD`.

## Template: read-only scanner agent

```markdown
---
name: skill-scanner
description: Scan skill definitions and report naming issues
tools:
  - Read
  - Glob
  - Grep
skills:
  - scan-skills
  - naming-rules
---

You are a read-only repository skill scanner.

Execute the scanning procedure described in the preloaded `scan-skills` skill.
Apply the conventions from `naming-rules`.
Do not edit files.

Return a table with: file path, current name, proposed name, reason, evidence.
Report any incomplete scans or uncertainties.
```

Matching workflow lines in A:

```markdown
2. Run the skill-scanner agent in the **foreground** and **wait for its report**.
   Do not continue until the report has arrived.
3. Validate the scanner's findings.
4. Present the proposed changes and get approval before modifying files.
5. Apply the approved changes in the main conversation.
```

Forked-skill variant: `context: fork`, `agent: Explore`, `background: false`.

## Verification after the first real run

- [ ] The intended skills were available and actually loaded.
- [ ] The right agent was launched; for `context: fork`, a sub-agent call really appears.
- [ ] Dependency order held: A waited for C before D.
- [ ] Sub-agent output is complete and matches its output contract.
- [ ] No unauthorized changes: read-only agents edited nothing; D applied only approved changes.
