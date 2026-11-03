# Worked example: A/B/C/D

A is "refactor skill names":

| Part | What it does | Decision |
|---|---|---|
| B = naming rules | Knowledge, shared by other skills | **Separate skill, inline.** A needs the rules while it works. |
| C = scan skills | Reads many skill files, judges them against B | **Script** if only a deterministic search. **Custom sub-agent** if it needs semantic judgement (classifying skills, proposing names). |
| D = rename and fix references | Edits files after approval | **Steps in A, main conversation.** |

Real-world note: the naming convention lives in `wf-refactor-skill-names` and is
also used by `wf-sync-claude-config-repos` and the global CLAUDE.md, so it stays shared.

```
Main conversation
 └─ Skill A (orchestrator, inline)
      ├─ Skill B → inline (naming rules, shared context)
      ├─ skill-scanner agent (foreground, preloads C + B, read-only)
      │     └─ returns findings to A
      ├─ A validates findings, presents proposed renames, gets approval
      └─ D → inline: apply changes, verify references, report
```

## `.claude/agents/skill-scanner.md`

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

Return a table with:
- File path
- Current skill name
- Proposed skill name
- Reason for the change
- Evidence

Report any incomplete scans or uncertainties.
```

- No edit tools and no Bash → read-only.
- Neither preloaded skill may set `disable-model-invocation: true`.

## Workflow section in A's `SKILL.md`

```markdown
## Workflow

1. Use the naming-rules skill to understand the naming convention.
2. Run the skill-scanner agent in the **foreground** and **wait for its report**.
   Do not continue until the report has arrived.
3. Validate the scanner's findings.
4. Present the proposed renames to the user and get approval before modifying files.
5. Apply the approved changes in the main conversation.
6. Verify that all renamed references resolve correctly, then report the outcome.
```

## Alternative: C as a forked skill

Add `background: false` so A waits:

```yaml
---
name: scan-skills
description: Scan repository skill definitions
context: fork
agent: Explore
background: false
---
```
