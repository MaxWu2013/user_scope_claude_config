# Runtime details (as of Claude Code v2.1.309)

## Background vs. waiting (most common mistake)

- A `context: fork` skill runs **in the background by default**: the main
  conversation continues and the result arrives later. Set
  **`background: false`** to wait for the result in the same turn.
  - It also waits automatically in non-interactive mode (`-p`, Agent SDK), when
    `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`, and in a few other cases.
- A sub-agent Claude spawns from the main conversation also **starts in the
  background** if nothing is configured.
- If D depends on C's result, **say explicitly that A must wait** for C.
  Otherwise A may move on before C finishes.

## Approvals

- Sub-agents **can** ask for permissions. Foreground ones pass prompts through to
  the user; background ones show the prompt in the main session, naming the
  sub-agent that asks.
- So keeping D in the main conversation is **not** a technical requirement. The
  real reason: applying changes needs shared understanding of the approved plan
  and a controlled order of changes, which works best where the user and A talk.

## Nesting

- A sub-agent can spawn its own sub-agents, **up to 3 layers** below the main
  conversation by default. Change with `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`.
- Version history: v2.1.172–2.1.216 allowed 5 fixed layers; v2.1.217–2.1.218
  defaulted to 1; v2.1.219 raised it to 3. Check with `claude --version`.
- Still keep orchestrator A in the main conversation (no `context: fork`).

## Context isolation ≠ filesystem isolation

- Sub-agents work in the **same working directory** by default; their edits are real.
- For a separate copy set `isolation: worktree`. It creates a temporary git
  worktree branched from the **default branch**, not the current `HEAD`, so
  watch for branch differences.

## What a fresh sub-agent loads

- Custom and general-purpose agents: their prompt + CLAUDE.md + git status
  (unless `omitClaudeMd` is set).
- Explore and Plan: skip CLAUDE.md and git status to stay fast and cheap.
- `skills:` preload: full text of the first 32 listed skills; skills with
  `disable-model-invocation: true` can't be preloaded.
