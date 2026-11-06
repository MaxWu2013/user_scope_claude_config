import { expect, mock, test } from 'claude-code/testing'
import type { AgentSpawnInput, AgentSpawnResult } from 'claude-code'

const HOME = '/tmp/tester'

function policyText(maxPerRun = 12, maxPerSession = 30): string {
  return JSON.stringify({
    schema_version: 1,
    enabled: true,
    workflow: {
      opus_main_mode: 'pinned_env',
      worker_model: 'sonnet',
      allowed_worker_families: ['sonnet', 'haiku'],
      max_concurrent_agents_required: 4,
      max_workflows_per_session: 3,
      max_workers_per_workflow: maxPerRun,
      max_workers_per_session: maxPerSession,
      require_mod_call_attestation: true,
    },
  })
}

function spawn(over: Partial<AgentSpawnInput> = {}): AgentSpawnInput {
  return {
    tool_use_id: 'tu-1',
    prompt: 'do the step',
    description: 'step',
    subagentType: 'general-purpose',
    provider: { plugin: 'engine', tier: 'core' },
    parentModel: 'claude-opus-5-5',
    background: false,
    fork: false,
    ...over,
  }
}

function workflow(runId: string, agentIndex: number, model?: string): AgentSpawnInput {
  const input = spawn({
    tool_use_id: `tu-${runId}-${agentIndex}`,
    workflow: { runId, agentIndex },
  })
  if (model !== undefined) input.model = model
  return input
}

test('non-workflow spawn is unchanged', async ($, on) => {
  mock.env(on, { HOME })
  on('session.id', async () => ({ value: 'sess-plain' }))
  on('fs.read', async () => ({ value: policyText() }))
  on('fs.write', async () => ({ value: undefined }))
  const seen: AgentSpawnInput[] = []
  on('agent.spawn', async (_$, event) => {
    seen.push(event)
    return { model: event.model ?? 'inherit', agentId: 'plain-1' }
  })
  const input = spawn({ model: 'opus', subagentType: 'file-reader', isTeammate: true })
  const result = await $.agent.spawn(input)
  expect(seen).toHaveLength(1)
  expect(seen[0]?.model).toBe('opus')
  expect(seen[0]?.prompt).toBe(input.prompt)
  expect(seen[0]?.subagentType).toBe('file-reader')
  expect(seen[0]?.workflow).toBeUndefined()
  expect(seen[0]?.isTeammate).toBe(true)
  expect(result.agentId).toBe('plain-1')
})

test('workflow spawn forces sonnet', async ($, on) => {
  mock.env(on, { HOME })
  on('session.id', async () => ({ value: 'sess-model' }))
  on('fs.read', async () => ({ value: policyText() }))
  on('fs.write', async () => ({ value: undefined }))
  const seen: AgentSpawnInput[] = []
  on('agent.spawn', async (_$, event) => {
    seen.push(event)
    return { model: event.model ?? 'missing', agentId: 'worker-' + seen.length }
  })
  const requested = ['opus', 'op' + 'us', undefined] as const
  for (const model of requested) {
    const input = workflow('wf_models', seen.length + 1, model)
    if (model === undefined) delete input.model
    const result = await $.agent.spawn(input)
    expect(result.agentId).toBe('worker-' + seen.length)
  }
  expect(seen).toHaveLength(3)
  for (const event of seen) expect(event.model).toBe('sonnet')
})

test('13th spawn in a run is denied', async ($, on) => {
  mock.env(on, { HOME })
  on('session.id', async () => ({ value: 'sess-run-cap' }))
  on('fs.read', async () => ({ value: policyText() }))
  on('fs.write', async () => ({ value: undefined }))
  let nexts = 0
  on('agent.spawn', async (_$, event) => {
    nexts += 1
    return { model: event.model ?? 'sonnet', agentId: 'run-' + nexts }
  })
  let last: AgentSpawnResult | undefined
  for (let index = 1; index <= 13; index++) last = await $.agent.spawn(workflow('wf_cap', index, 'opus'))
  expect(nexts).toBe(12)
  expect(last?.deny).toBe('WORKFLOW_SPAWN_CAP run=13/12 session=13/30')
})

test('31st spawn in a session is denied', { timeoutMs: 20000 }, async ($, on) => {
  mock.env(on, { HOME })
  on('session.id', async () => ({ value: 'sess-session-cap' }))
  on('fs.read', async () => ({ value: policyText(40, 30) }))
  on('fs.write', async () => ({ value: undefined }))
  let nexts = 0
  on('agent.spawn', async (_$, event) => {
    nexts += 1
    return { model: event.model ?? 'sonnet', agentId: 'sess-' + nexts }
  })
  let last: AgentSpawnResult | undefined
  for (let index = 1; index <= 31; index++) last = await $.agent.spawn(workflow('wf_session', index))
  expect(nexts).toBe(30)
  expect(last?.deny).toBe('WORKFLOW_SPAWN_CAP run=31/40 session=31/30')
})

test('20 concurrent spawns admit 12', { timeoutMs: 20000 }, async ($, on) => {
  mock.env(on, { HOME })
  on('session.id', async () => ({ value: 'sess-parallel' }))
  on('fs.read', async () => ({ value: policyText() }))
  on('fs.write', async () => ({ value: undefined }))
  let nexts = 0
  on('agent.spawn', async (_$, event) => {
    nexts += 1
    return { model: event.model ?? 'sonnet', agentId: 'par-' + nexts }
  })
  const results = await Promise.all(
    Array.from({ length: 20 }, (_item, index) => $.agent.spawn(workflow('wf_parallel', index + 1, 'opus'))),
  )
  const denied = results.filter(result => result.deny !== undefined)
  expect(nexts).toBe(12)
  expect(denied).toHaveLength(8)
  for (const result of denied) expect(result.deny).toMatch(/^WORKFLOW_SPAWN_CAP run=\d+\/12 session=\d+\/30$/)
})

test('unreadable policy denies the workflow spawn', async ($, on) => {
  mock.env(on, { HOME })
  on('session.id', async () => ({ value: 'sess-nopolicy' }))
  on('fs.read', async () => {
    throw new Error('unreadable')
  })
  on('fs.write', async () => ({ value: undefined }))
  let nexts = 0
  on('agent.spawn', async () => {
    nexts += 1
    return { model: 'sonnet', agentId: 'should-not' }
  })
  const result = await $.agent.spawn(workflow('wf_nopolicy', 1, 'opus'))
  expect(nexts).toBe(0)
  expect(result.deny).toBe('OPUS_SPAWN_POLICY_UNAVAILABLE')
})

test('a throw before next denies the workflow spawn', async ($, on) => {
  mock.env(on, { HOME })
  on('session.id', async () => ({ value: 'sess-throw' }))
  on('fs.read', async () => ({ value: policyText() }))
  on('fs.write', async () => ({ value: undefined }))
  on('state.set', async () => ({ deny: 'state exploded' }))
  let nexts = 0
  on('agent.spawn', async () => {
    nexts += 1
    return { model: 'sonnet', agentId: 'should-not' }
  })
  const result = await $.agent.spawn(workflow('wf_throw', 1, 'opus'))
  expect(nexts).toBe(0)
  expect(result.deny).toBe('OPUS_SPAWN_GUARD_ERROR')
})

test('resume of a recorded workflow agent is denied and not counted', async ($, on) => {
  mock.env(on, { HOME })
  on('session.id', async () => ({ value: 'sess-resume' }))
  on('fs.read', async () => ({ value: policyText() }))
  on('fs.write', async () => ({ value: undefined }))
  let nexts = 0
  on('agent.spawn', async (_$, event) => {
    nexts += 1
    return { model: event.model ?? 'sonnet', agentId: 'worker-recorded' }
  })
  const started = await $.agent.spawn(workflow('wf_resume', 1, 'opus'))
  expect(started.agentId).toBe('worker-recorded')
  const resumed = await $.agent.spawn(spawn({ resume: { agentId: 'worker-recorded' }, prompt: 'continue' }))
  expect(nexts).toBe(1)
  expect(resumed.deny).toBe('WORKFLOW_RESUME_DENIED')
})

test('resume of another agent is unchanged', async ($, on) => {
  mock.env(on, { HOME })
  on('session.id', async () => ({ value: 'sess-other-resume' }))
  on('fs.read', async () => ({ value: policyText() }))
  on('fs.write', async () => ({ value: undefined }))
  const seen: AgentSpawnInput[] = []
  on('agent.spawn', async (_$, event) => {
    seen.push(event)
    return { model: event.model ?? 'inherit', agentId: event.resume?.agentId ?? 'new' }
  })
  const input = spawn({ resume: { agentId: 'file-reader-1' }, prompt: 'continue', model: 'opus' })
  const result = await $.agent.spawn(input)
  expect(seen).toHaveLength(1)
  expect(seen[0]?.model).toBe('opus')
  expect(seen[0]?.resume?.agentId).toBe('file-reader-1')
  expect(seen[0]?.workflow).toBeUndefined()
  expect(result.agentId).toBe('file-reader-1')
})

function captureWrites(on: Parameters<Parameters<typeof test>[1]>[1]): { path: string; text: string }[] {
  const writes: { path: string; text: string }[] = []
  on('fs.write', async (_$, event) => {
    writes.push({ path: event.path, text: event.text })
    return { value: undefined }
  })
  return writes
}

function spawnLog(writes: { path: string; text: string }[], sessionId: string): Record<string, unknown>[] {
  const suffix = `/mod/spawns-${sessionId}.jsonl`
  const found = writes.filter(item => item.path.endsWith(suffix))
  const last = found[found.length - 1]
  if (!last || last.text.trim() === '') return []
  return last.text
    .trim()
    .split('\n')
    .map(line => JSON.parse(line) as Record<string, unknown>)
}

test('an admitted workflow spawn is logged as allow', async ($, on) => {
  mock.env(on, { HOME })
  on('session.id', async () => ({ value: 'sess-allow-log' }))
  on('fs.read', async () => ({ value: policyText() }))
  const writes = captureWrites(on)
  on('agent.spawn', async (_$, event) => ({ model: event.model ?? 'sonnet', agentId: 'worker-allow' }))
  const result = await $.agent.spawn(workflow('wf_allow', 2, 'opus'))
  expect(result.agentId).toBe('worker-allow')
  const lines = spawnLog(writes, 'sess-allow-log')
  expect(lines).toHaveLength(1)
  expect(lines[0]).toMatchObject({
    session_id: 'sess-allow-log',
    runId: 'wf_allow',
    agentIndex: 2,
    agentId: 'worker-allow',
    agentType: 'general-purpose',
    model_requested: 'opus',
    model_forced: 'sonnet',
    decision: 'allow',
  })
  expect(typeof lines[0]?.ts).toBe('string')
})

test('workflow spawn of opus-planner or opus-judge is denied and not counted', async ($, on) => {
  mock.env(on, { HOME })
  on('session.id', async () => ({ value: 'sess-control-agent' }))
  on('fs.read', async () => ({ value: policyText(1, 30) }))
  const writes = captureWrites(on)
  let nexts = 0
  on('agent.spawn', async (_$, event) => {
    nexts += 1
    return { model: event.model ?? 'sonnet', agentId: 'worker-after-control' }
  })
  const reason =
    'WORKFLOW_CONTROL_AGENT_DENIED agent_type=%s: planner and judge run only through arbitration from the main session.'
  for (const agentType of ['opus-planner', 'opus-judge'] as const) {
    const input = workflow('wf_control', 1, 'opus')
    input.subagentType = agentType
    const denied = await $.agent.spawn(input)
    expect(denied.deny).toBe(reason.replace('%s', agentType))
  }
  const admitted = await $.agent.spawn(workflow('wf_control', 2))
  expect(admitted.agentId).toBe('worker-after-control')
  expect(nexts).toBe(1)
  const lines = spawnLog(writes, 'sess-control-agent')
  expect(lines.map(line => line.decision)).toEqual(['deny', 'deny', 'allow'])
  expect(lines[0]).toMatchObject({
    agentType: 'opus-planner',
    decision: 'deny',
    reason: reason.replace('%s', 'opus-planner'),
  })
  expect(lines[1]?.agentType).toBe('opus-judge')
  expect(lines[2]).toMatchObject({ agentType: 'general-purpose', decision: 'allow', agentId: 'worker-after-control' })
})

test('a cap denial is logged as deny and the admitted spawn stays allow', async ($, on) => {
  mock.env(on, { HOME })
  on('session.id', async () => ({ value: 'sess-deny-log' }))
  on('fs.read', async () => ({ value: policyText(1, 30) }))
  const writes = captureWrites(on)
  let nexts = 0
  on('agent.spawn', async (_$, event) => {
    nexts += 1
    return { model: event.model ?? 'sonnet', agentId: 'worker-once' }
  })
  const admitted = await $.agent.spawn(workflow('wf_deny_log', 1, 'opus'))
  const capped = await $.agent.spawn(workflow('wf_deny_log', 2, 'opus'))
  expect(admitted.agentId).toBe('worker-once')
  expect(capped.deny).toBe('WORKFLOW_SPAWN_CAP run=2/1 session=2/30')
  expect(nexts).toBe(1)
  const lines = spawnLog(writes, 'sess-deny-log')
  expect(lines.map(line => line.decision)).toEqual(['allow', 'deny'])
  expect(lines[1]).toMatchObject({
    session_id: 'sess-deny-log',
    runId: 'wf_deny_log',
    agentIndex: 2,
    decision: 'deny',
    reason: 'WORKFLOW_SPAWN_CAP run=2/1 session=2/30',
  })
  expect(lines[1]?.agentId).toBeUndefined()
})

test('an unreadable policy denial is logged', async ($, on) => {
  mock.env(on, { HOME })
  on('session.id', async () => ({ value: 'sess-nopolicy-log' }))
  on('fs.read', async () => {
    throw new Error('unreadable')
  })
  const writes = captureWrites(on)
  let nexts = 0
  on('agent.spawn', async () => {
    nexts += 1
    return { model: 'sonnet', agentId: 'should-not' }
  })
  const result = await $.agent.spawn(workflow('wf_nopolicy_log', 4, 'opus'))
  expect(nexts).toBe(0)
  expect(result.deny).toBe('OPUS_SPAWN_POLICY_UNAVAILABLE')
  const lines = spawnLog(writes, 'sess-nopolicy-log')
  expect(lines).toHaveLength(1)
  expect(lines[0]).toMatchObject({
    session_id: 'sess-nopolicy-log',
    runId: 'wf_nopolicy_log',
    agentIndex: 4,
    decision: 'deny',
    reason: 'OPUS_SPAWN_POLICY_UNAVAILABLE',
  })
})

test('a guard error denial is logged', async ($, on) => {
  mock.env(on, { HOME })
  on('session.id', async () => ({ value: 'sess-guard-log' }))
  on('fs.read', async () => ({ value: policyText() }))
  const writes = captureWrites(on)
  on('state.set', async (_$, event, next) => {
    if (event.key === 'counters') return { deny: 'state exploded' }
    return next(event)
  })
  let nexts = 0
  on('agent.spawn', async () => {
    nexts += 1
    return { model: 'sonnet', agentId: 'should-not' }
  })
  const result = await $.agent.spawn(workflow('wf_guard_log', 1, 'opus'))
  expect(nexts).toBe(0)
  expect(result.deny).toBe('OPUS_SPAWN_GUARD_ERROR')
  const lines = spawnLog(writes, 'sess-guard-log')
  expect(lines).toHaveLength(1)
  expect(lines[0]).toMatchObject({
    session_id: 'sess-guard-log',
    runId: 'wf_guard_log',
    agentIndex: 1,
    decision: 'deny',
    reason: 'OPUS_SPAWN_GUARD_ERROR',
  })
})

test('a workflow resume denial is logged and not admitted', async ($, on) => {
  mock.env(on, { HOME })
  on('session.id', async () => ({ value: 'sess-resume-log' }))
  on('fs.read', async () => ({ value: policyText() }))
  const writes = captureWrites(on)
  on('agent.spawn', async (_$, event) => ({ model: event.model ?? 'sonnet', agentId: 'worker-recorded' }))
  const started = await $.agent.spawn(workflow('wf_resume_log', 1, 'opus'))
  expect(started.agentId).toBe('worker-recorded')
  const resumed = await $.agent.spawn(spawn({ resume: { agentId: 'worker-recorded' }, prompt: 'continue' }))
  expect(resumed.deny).toBe('WORKFLOW_RESUME_DENIED')
  const lines = spawnLog(writes, 'sess-resume-log')
  expect(lines.map(line => line.decision)).toEqual(['allow', 'deny'])
  expect(lines[1]).toMatchObject({
    session_id: 'sess-resume-log',
    runId: '',
    agentIndex: 0,
    decision: 'deny',
    reason: 'WORKFLOW_RESUME_DENIED',
  })
})

test('a spawn-log write failure still denies', async ($, on) => {
  mock.env(on, { HOME })
  on('session.id', async () => ({ value: 'sess-log-fail' }))
  on('fs.read', async () => {
    throw new Error('unreadable')
  })
  on('fs.write', async () => {
    throw new Error('disk full')
  })
  let nexts = 0
  on('agent.spawn', async () => {
    nexts += 1
    return { model: 'sonnet', agentId: 'should-not' }
  })
  const result = await $.agent.spawn(workflow('wf_log_fail', 1, 'opus'))
  expect(nexts).toBe(0)
  expect(result.deny).toBe('OPUS_SPAWN_POLICY_UNAVAILABLE')
})

test('Workflow tool.call attests with tool_use_id', async ($, on) => {
  mock.env(on, { HOME })
  on('session.id', async () => ({ value: 'sess-attest' }))
  on('fs.read', async () => ({ value: policyText() }))
  const writes: { path: string; text: string }[] = []
  on('fs.write', async (_$, event) => {
    writes.push({ path: event.path, text: event.text })
    return { value: undefined }
  })
  on('tool.call', async () => ({ result: { echoed: true } }))
  await $.tool.call({ tool: 'Workflow', tool_use_id: 'tu-workflow', script: 'echo hi' })
  const attest = writes.find(item => item.path.endsWith('/mod/attest-sess-attest-tu-workflow.json'))
  expect(attest).toBeDefined()
  const body = JSON.parse(attest?.text ?? '{}') as { tool_use_id?: string; mod_version?: string }
  expect(body.tool_use_id).toBe('tu-workflow')
  expect(body.mod_version).toBe('1.0.0')
})
