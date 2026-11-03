# Stage contracts

The target design for any multi-stage orchestrator. Principle:

> Skills define reusable capabilities. The orchestrator controls workflow and decisions.
> Agents provide optional execution isolation. Stage contracts preserve correctness
> across boundaries.

A contract is what makes a stage safe to run anywhere (inline, forked, agent, a new
session after compaction). Without one, isolating a stage only hides what it lost.

## How much contract — pick the lowest level that is safe

A contract costs writing, hashing and checking. Choose per skill, and say why in the report.

| Level | What | Use when **all** of these hold |
|---|---|---|
| **L0 — none** | Nothing beyond the skill's steps | One stage; nothing is handed to another context (no fork, agent or external CLI); every output is a file in git, so `git diff` is the record; redoing it from scratch is cheap |
| **L1 — hand-off file** | One spec/brief file written before delegating, plus checks the orchestrator reruns itself | Work crosses a context boundary (sub-agent, fork, `cursor-agent`), **or** extracted data (design values, decisions) must survive compaction; but there is still one stage, outputs are files in git, and a retry from the start is cheap and repeats no side effects |
| **L2 — stage records** | The full record format below: fingerprints, upstream digests, transitive invalidation, retry rules | Any of: two or more stages where a later stage consumes an earlier stage's outputs; a stage is expensive or slow to redo; a stage has side effects outside git (messages sent, servers started, uploads, commits); the work is expected to span sessions or compactions; human decisions made mid-run must be kept |

Notes:

- The level can differ per boundary: an orchestrator can use L2 between its stages and L1
  for one stage's delegation.
- A hand-off file (L1) must still be complete: everything the delegate needs, the files it
  may touch, the files it must not touch, and the verify commands. It lives outside the repo
  or in a gitignored folder.
- Going from L1 to L2 is justified by evidence: a run that lost a decision after compaction,
  a retry that broke something, an upstream change that went unnoticed. Without such
  evidence, a proposal for L2 on a single-stage skill is over-engineering.

## One record per stage (L2)

Written by the orchestrator (not by the stage's sub-agent) at the end of the stage,
to a run folder **outside the repo** (e.g. `<scratchpad or ~/.cache>/<skill>/<run-id>/stage-<n>.json`):

```json
{
  "stage": 3,
  "name": "proto-to-dart",
  "status": "passed",
  "attempt": 1,
  "depends_on": [2],
  "inputs": {
    "files": {"banban_base/proto_def/lib/group/room_level.proto": "sha256:…"},
    "upstream": {"2": "sha256:<stage-2 record digest>"},
    "params": {"api_prefix": "go/party/roomInfo"}
  },
  "decisions": ["URL derived from filename; approved by user 10:42"],
  "outputs": {
    "files": {"banban_base/proto_def/lib/api/group__room_level.api.dart": "sha256:…"}
  },
  "checks": [
    {"cmd": "grep -n 'System.api' …", "exit": 0, "evidence": "stage-3/grep.txt"}
  ],
  "unresolved": [],
  "digest": "sha256:<hash of inputs + outputs + decisions>"
}
```

## Rules

1. **Fingerprint by content.** `inputs.files` and `outputs.files` hold SHA-256 of file
   content (not mtime, not git status). Directories are hashed as the sorted list of
   `path:hash` of their files. Human decisions are inputs too: put them in `decisions`
   so a changed decision changes the digest.
2. **Upstream by digest.** `inputs.upstream` records each dependency's `digest`. A stage
   is **valid** only if (a) its record says `passed`, (b) every input file still has the
   recorded hash, (c) every output file still has the recorded hash, and (d) every
   upstream record is itself valid and has the recorded digest.
3. **Invalidation is transitive.** If stage k becomes invalid, every stage that depends
   on k (directly or not) is invalid. Resume from the **earliest invalid** stage, not
   from the last passed one.
4. **Outputs changed by hand.** If (c) fails but (b) holds, someone edited an output:
   ask whether to adopt the edit (re-run only the checks, record new hashes) or
   regenerate. Never silently overwrite.
5. **Safe retry.** Before re-running a stage, compare the working tree with the stage's
   recorded outputs: files it created may be replaced; files it did not list must not
   be touched (check `git status --porcelain` before and after). A stage whose effects
   are not files (a sent chat message, a started server) records them under
   `side_effects` with how to check or undo them, and the orchestrator asks before
   repeating one.
6. **Checks carry evidence.** Each check stores its command, exit code and an evidence
   file. A sub-agent's report is not a check; the orchestrator reruns the cheap
   command (exit code) and reads the evidence, it does not redo the work.
7. **Unresolved items travel.** Anything a stage could not settle goes in `unresolved`
   with an owner; later stages read it instead of relying on chat history.
8. **The record is the hand-off.** A later stage (or a sub-agent) gets the records it
   depends on as its input contract. If it needs something that is only in the chat,
   that is a missing field in an upstream contract — fix the contract.
9. **Change manifest = union of outputs.** The final verify/commit stage takes the
   union of every stage's `outputs.files`, cross-checked with `git status --porcelain`
   (which also shows untracked, staged and renamed files). A file in git status that no
   stage claims is a finding, not something to commit.

## Run state — the control plane

Stage records say what each stage produced. **Run state** says where the run is and what
comes next: needed whenever A has a loop, a retry, an approval gate (especially one that
may be skipped), a delegate whose result decides the next step, or failure handling.

Keep it in one file in the run folder, owned by a deterministic script — never in prompt
text. Use `tl-route-run-state` (`~/.claude/skills/tl-route-run-state/scripts/run_state.py`):

| Field | Meaning |
|---|---|
| `phase` | spec · approval · execute · verify · failed · done · stopped |
| `spec_version` / `approved_spec_version` | approval is valid only while they are equal |
| `executor_status` | last delegate result: success · partial · error |
| `executor_retries` / `verification_retries` + `limits` | retry budgets; over the limit → `escalate` |
| `last_error`, `history` | for reports and resume |

Interface: `init`, `record <event>`, `route` (one token: write-spec, approve, execute,
retry-execute, verify, revise-spec, handle-failure, escalate, done, stopped), `get`.
Every decision that depends on a count, a limit, a version or a delegate's result reads
`route` (or a `get` field). Prompts record events; they don't count or decide.

## Diagnosing an orchestrator against this

For each stage boundary, ask and record the answer with evidence:

- What does stage n+1 read from stage n? Is it a file, a record, or only chat history?
- Can each gate be checked again later from files alone?
- If an upstream file changes after its gate passed, would the orchestrator notice?
- If the stage is re-run, which effects repeat (edits, generated files, messages, commits)?
- After compaction or in a new session, can the orchestrator tell where it is?
