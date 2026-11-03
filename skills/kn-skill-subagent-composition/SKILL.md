---
name: kn-skill-subagent-composition
description: "How to compose Claude Code skills with other skills and sub-agents - when a helper (B, C, D used by skill A) should stay inline, become a references/ file or script, a separate skill, a `context: fork` skill, a custom agent with preloaded `skills:`, or a `/subtask` conversation fork. Covers the four execution models, the decision table, input/output contracts, background-vs-wait (`background: false`), approvals, nesting depth, worktree isolation and a post-run verification checklist. Use when deciding \"should this skill be a sub-agent\", \"should B be its own skill\", \"inline or fork\", \"how do I preload skills into an agent\", why a forked skill didn't wait or didn't see the chat, or when designing/refactoring an orchestrator skill."
---

# Skill vs. Sub-Agent Composition

Checked against Claude Code **v2.1.309** and the docs
([Skills](https://code.claude.com/docs/en/skills), [Sub-agents](https://code.claude.com/docs/en/sub-agents))
on 2026-10-23. Re-check version-sensitive facts with `claude --version` and the docs.

Source: `~/workplace/project/Obsidian_doc/30_references/tools/skill-vs-subagent-integration.md`.
Keep in sync with `~/.claude/skills/wf-refactor-skill-composition/references/composition-rules.md`,
a condensed copy of the same note. When you change a shared rule here, change it there too.

**Core idea:** a **skill** is reusable instructions or knowledge (*what* to do).
A **sub-agent** is a separate execution environment (*where* it runs). They
solve different problems and can be combined. Calling a skill ≠ making it a sub-agent.

Wording: "skill composition" = skill A uses skill B. The docs use "nested
skills" for something else (skills in `<subdir>/.claude/skills/`).

## How to decide, per helper

When skill A uses B, C and D, do **not** turn them all into sub-agents by
default. Decide for **each** helper separately:

1. Should it exist as a **separate skill** at all? (section 2)
2. Should it run in the **same conversation** as A? (section 3)
3. If not, which **execution model**? (section 1)

## 1. Execution models

| # | Pattern | Context the work sees | Mechanism | Best for |
|---|---|---|---|---|
| 1 | A → Skill B (inline) | Shared with the main conversation | Skill tool | Reusing instructions and knowledge |
| 2 | A → forked Skill B | **Fresh**, skill text is the task | `context: fork` (+ `agent:`) in B's frontmatter | A standalone task that should always run isolated |
| 3 | A → custom agent with skills | **Fresh**, skills preloaded | `.claude/agents/<name>.md` with `skills:` and `tools:` | Reusable specialist with controlled tools |
| 4 | A → conversation fork | **Inherits** the whole conversation | `/subtask <task>` (or fork mode) | Side task needing lots of earlier context, output kept out of the main thread |

Gotchas:

- **Inline is instruction-driven, not a function call.** "Use B, then C, then D"
  in A's SKILL.md means Claude *can* invoke them; it is not guaranteed. Verify critical steps.
- **`context: fork` is NOT a conversation fork.** It starts a fresh sub-agent
  with no chat history. The skill text must stand alone. Needs history → `/subtask`.
- **A fresh sub-agent gets more than its prompt.** Custom and general-purpose
  agents also load CLAUDE.md and git status (unless `omitClaudeMd`). **Explore**
  and **Plan** skip both.
- **Preloading ≠ invoking.** `skills: [C, B]` injects their full text at startup
  (first 32 names only); it does not run them. The agent body must say what to
  do, e.g. "perform C's procedure using B's rules". Skills with
  `disable-model-invocation: true` **cannot** be preloaded.

## 2. Separate skill or fold into A?

**Default:** if only A uses a helper, keep it inside A. A separate skill adds
coupling: A refers to it by name in prose, so a rename can silently break A.
Keep it separate with a single user only for a concrete benefit: independent
invocation, separate testing, separate ownership, a large procedure clearer on
its own, or execution isolation.

| Helper kind | Only A uses it | Shared |
|---|---|---|
| Rules / knowledge | `references/<topic>.md` in A's folder | Independent skill, invoked inline |
| Deterministic file processing | Script in A's `scripts/` | Shared script or skill |
| Procedure | Steps in A's `SKILL.md` | Independent skill |

## 3. Where should the work run?

| Workload | Preferred implementation |
|---|---|
| Short knowledge/rules, only used by A | A's `references/` |
| Shared knowledge used by several workflows | Independent skill, inline |
| Deterministic file processing (search, list, rename by pattern) | **Script** |
| Small sequential reasoning task | Inline steps in A |
| Large independent research/analysis that floods the context | Forked skill or custom sub-agent |
| Reusable specialist with controlled tools (e.g. read-only) | Custom sub-agent with preloaded skills |
| Side task needing a lot of earlier conversation | Conversation fork (`/subtask`) |
| Task needing frequent user decisions | Main conversation |
| Several independent reasoning tasks | Parallel sub-agents |
| Dependent reasoning stages | Sequential sub-agents with explicit handoffs |

**When forking, define the contract:** an explicit **input contract**, one clear
**responsibility**, a bounded **output contract**. Detailed findings can go to a
file; only a concise summary plus file references return to the orchestrator.
If you can't define these, keep the work inline.

**Parallelism is an advantage, not a requirement.** Sequential sub-agents
(research → analysis → back to A) are a documented pattern.

## 4. Coordination essentials

- **Background by default.** `context: fork` skills and sub-agents Claude spawns
  run in the background unless configured. If D depends on C, set
  `background: false` on a forked skill and/or tell A explicitly to wait for C's
  report. Details: [references/runtime-details.md](references/runtime-details.md).
- **Keep orchestrator A in the main conversation** (no `context: fork`) - for
  approvals and a controlled order of changes, not because sub-agents can't ask
  for permissions (they can).
- **Context isolation ≠ filesystem isolation.** Sub-agents edit the same working
  directory unless `isolation: worktree`.

## 5. Verify after the first real run

- [ ] Intended skills were available and actually loaded (invocation shows in the session).
- [ ] The right agent launched; for `context: fork`, a sub-agent call really appears (some older versions ran it inline).
- [ ] Dependency order held: A waited for C before D.
- [ ] Sub-agent output is complete and matches its output contract.
- [ ] No unauthorized changes: read-only agents edited nothing; D applied only approved changes.

## References

- [references/worked-example.md](references/worked-example.md) - A/B/C/D "refactor
  skill names" example with a read-only `skill-scanner` agent, A's workflow
  section and a forked-skill frontmatter. Read when writing the actual files.
- [references/runtime-details.md](references/runtime-details.md) - background vs.
  wait, approvals, nesting depth and version history, worktree isolation.

## Summary

1. Skill = *what*; sub-agent = *where*.
2. Single-use helper → fold into A (reference, script or steps) unless separation gives a concrete benefit.
3. Deterministic → script. Heavy independent reasoning → sub-agent. Needs history → `/subtask`. Frequent decisions → main conversation.
4. Prefer a custom agent with explicit `skills:` and `tools:`, and tell it how to use the preloaded skills.
5. Forks and spawned agents run in the background by default: `background: false` or tell A to wait.
6. Keep A in the main conversation for approvals and sequencing.
7. Test once; verify loading, order, output and edits.

An orchestrating A is type `or-` under the naming convention in
`wf-refactor-skill-names`, not `wf-`; check the type table there before renaming.
