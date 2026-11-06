import { update, type EngineInterface, type Register } from 'claude-code'
import type { Counters, SpawnRecord } from '../types/index.js'

const MOD_VERSION = '1.0.0'
const COUNTERS = { plugin: 'opus-workflow-spawn', key: 'counters' } as const
const AGENTS = { plugin: 'opus-workflow-spawn', key: 'workflowAgents' } as const
const SPAWNS = { plugin: 'opus-workflow-spawn', key: 'spawnLog' } as const

type Policy = {
  workerModel: string
  maxPerRun: number
  maxPerSession: number
  sha256: string
}

type SpawnEvent = {
  workflow?: { runId: string; agentIndex: number }
  resume?: { agentId: string }
  model?: string
  subagentType?: string
}

const CONTROL_AGENTS = new Set(['opus-planner', 'opus-judge'])

function spawnAgentType(event: SpawnEvent): string | null {
  const value = event.subagentType
  return typeof value === 'string' && value !== '' ? value : null
}

function controlDenyReason(agentType: string): string {
  return (
    `WORKFLOW_CONTROL_AGENT_DENIED agent_type=${agentType}: ` +
    'planner and judge run only through arbitration from the main session.'
  )
}

const trackedIds = new Set<string>()
let tail: Promise<unknown> = Promise.resolve()

function isTrackedWorkflowResume(event: SpawnEvent): boolean {
  const agentId = event.resume?.agentId
  return typeof agentId === 'string' && agentId !== '' && trackedIds.has(agentId)
}

function exclusive<T>(work: () => Promise<T>): Promise<T> {
  const run = tail.then(work, work)
  tail = run.then(
    () => undefined,
    () => undefined,
  )
  return run
}

function safeId(value: string): string | null {
  if (value === '' || value.includes('/') || value.includes('\\') || value.includes('..') || value.startsWith('.'))
    return null
  return value
}

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
}

function randomMarker(): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
}

function familyOf(model: string): string {
  const low = model.trim().toLowerCase()
  if (low === '') return 'unknown'
  if (low.includes('opus')) return 'opus'
  if (low === 'inherit') return 'inherit'
  if (low.includes('sonnet')) return 'sonnet'
  if (low.includes('haiku')) return 'haiku'
  return 'unknown'
}

function positiveInt(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 1) return null
  return value
}

function parsePolicy(text: string): Omit<Policy, 'sha256'> | null {
  let body: unknown
  try {
    body = JSON.parse(text)
  } catch {
    return null
  }
  if (body === null || typeof body !== 'object') return null
  const workflow = (body as { workflow?: unknown }).workflow
  if (workflow === null || typeof workflow !== 'object') return null
  const raw = workflow as {
    worker_model?: unknown
    max_workers_per_workflow?: unknown
    max_workers_per_session?: unknown
    allowed_worker_families?: unknown
  }
  if (typeof raw.worker_model !== 'string' || raw.worker_model.trim() === '') return null
  const maxPerRun = positiveInt(raw.max_workers_per_workflow)
  const maxPerSession = positiveInt(raw.max_workers_per_session)
  if (maxPerRun === null || maxPerSession === null) return null
  const allowed = raw.allowed_worker_families
  if (!Array.isArray(allowed) || allowed.length === 0 || allowed.some(item => typeof item !== 'string' || item === ''))
    return null
  const workerModel = raw.worker_model.trim()
  if (!allowed.includes(familyOf(workerModel))) return null
  return { workerModel, maxPerRun, maxPerSession }
}

async function homeOf($: EngineInterface): Promise<string | null> {
  const home = await $.env.get('HOME')
  if (typeof home !== 'string' || home === '') return null
  return home.replace(/\/+$/, '')
}

async function loadPolicy($: EngineInterface): Promise<Policy | null> {
  try {
    const home = await homeOf($)
    if (home === null) return null
    const text = await $.fs.read(`${home}/.claude/hooks/data/opus-arbitration-policy.json`)
    if (typeof text !== 'string') return null
    const parsed = parsePolicy(text)
    if (parsed === null) return null
    return { ...parsed, sha256: await sha256Hex(text) }
  } catch {
    return null
  }
}

async function refreshTracked($: EngineInterface): Promise<void> {
  const { value } = await $.state.get(AGENTS)
  for (const id of value?.ids ?? []) trackedIds.add(id)
}

async function writeHeartbeat($: EngineInterface): Promise<void> {
  const home = await homeOf($)
  const sessionId = safeId(await $.session.id())
  if (home === null || sessionId === null) return
  const policy = await loadPolicy($)
  const body = {
    session_id: sessionId,
    mod_version: MOD_VERSION,
    policy_sha256: policy?.sha256 ?? '',
    started_at: new Date().toISOString(),
    pid_marker: randomMarker(),
  }
  await $.fs.write(`${home}/.claude/logs/opus-arbitration/mod/${sessionId}.json`, JSON.stringify(body))
}

async function writeCallAttest($: EngineInterface, toolUseId: string): Promise<void> {
  const home = await homeOf($)
  const sessionId = safeId(await $.session.id())
  const callId = safeId(toolUseId)
  if (home === null || sessionId === null || callId === null) return
  const policy = await loadPolicy($)
  const body = {
    session_id: sessionId,
    tool_use_id: callId,
    mod_version: MOD_VERSION,
    policy_sha256: policy?.sha256 ?? '',
    ts: new Date().toISOString(),
  }
  await $.fs.write(
    `${home}/.claude/logs/opus-arbitration/mod/attest-${sessionId}-${callId}.json`,
    JSON.stringify(body),
  )
}

async function logDeny($: EngineInterface, event: SpawnEvent, reason: string): Promise<void> {
  try {
    await exclusive(async () => {
      const record: SpawnRecord = {
        session_id: await $.session.id(),
        runId: event.workflow?.runId ?? '',
        agentIndex: event.workflow?.agentIndex ?? 0,
        agentType: spawnAgentType(event),
        decision: 'deny',
        reason,
        ts: new Date().toISOString(),
      }
      await update($, SPAWNS, value => ({ lines: [...(value?.lines ?? []), record] }))
      await flushSpawns($)
    })
  } catch {
    // A logging failure must never turn a deny into an allow.
  }
}

async function flushSpawns($: EngineInterface): Promise<void> {
  const home = await homeOf($)
  const sessionId = safeId(await $.session.id())
  if (home === null || sessionId === null) return
  const path = `${home}/.claude/logs/opus-arbitration/mod/spawns-${sessionId}.jsonl`
  for (let attempt = 0; attempt < 4; attempt++) {
    const held = await $.state.get(SPAWNS)
    const lines = held.value?.lines ?? []
    const text = lines.length === 0 ? '' : lines.map(line => JSON.stringify(line)).join('\n') + '\n'
    await $.fs.write(path, text)
    const again = await $.state.get(SPAWNS)
    if (again.version === held.version) return
  }
}

export const register: Register = on => {
  on('session.start', async ($, event, next) => {
    try {
      await writeHeartbeat($)
    } catch {
      // A missing heartbeat fails closed in the Python gate.
    }
    return next(event)
  })

  on('tool.call', { tool: 'Workflow' }, async ($, event, next) => {
    try {
      await writeCallAttest($, event.tool_use_id)
    } catch {
      // No attestation file: the Python gate denies the call.
    }
    return next(event)
  }).catch(($, event, next) => next(event))

  on('agent.spawn', async ($, event, next) => {
    if (!event.workflow && !(event.resume && isTrackedWorkflowResume(event))) {
      if (event.resume) {
        try {
          await refreshTracked($)
        } catch {
          // State unread: an untracked resume still proceeds. The gate covers Opus resumes.
        }
        if (!isTrackedWorkflowResume(event)) return next(event)
      } else {
        return next(event)
      }
    }
    if (event.resume && isTrackedWorkflowResume(event)) {
      await logDeny($, event, 'WORKFLOW_RESUME_DENIED')
      return { deny: 'WORKFLOW_RESUME_DENIED' }
    }

    const agentType = spawnAgentType(event)
    if (agentType !== null && CONTROL_AGENTS.has(agentType)) {
      const reason = controlDenyReason(agentType)
      await logDeny($, event, reason)
      return { deny: reason }
    }

    const policy = await loadPolicy($)
    if (policy === null) {
      await logDeny($, event, 'OPUS_SPAWN_POLICY_UNAVAILABLE')
      return { deny: 'OPUS_SPAWN_POLICY_UNAVAILABLE' }
    }
    const runId = event.workflow?.runId ?? ''
    const reserved: Counters = await exclusive(() =>
      update($, COUNTERS, value => {
        const current = value ?? { perRun: {}, perSession: 0 }
        const runCount = (current.perRun?.[runId] ?? 0) + 1
        return {
          perRun: { ...current.perRun, [runId]: runCount },
          perSession: current.perSession + 1,
        }
      }),
    )
    const runCount = reserved.perRun[runId] ?? 0
    const sessionCount = reserved.perSession
    if (runCount > policy.maxPerRun || sessionCount > policy.maxPerSession) {
      const reason = `WORKFLOW_SPAWN_CAP run=${runCount}/${policy.maxPerRun} session=${sessionCount}/${policy.maxPerSession}`
      await logDeny($, event, reason)
      return { deny: reason }
    }
    const result = await next({ ...event, model: policy.workerModel })
    try {
      const agentId = result.agentId ?? null
      if (agentId) trackedIds.add(agentId)
      await exclusive(async () => {
        const record: SpawnRecord = {
          session_id: await $.session.id(),
          runId,
          agentIndex: event.workflow?.agentIndex ?? 0,
          agentId,
          agentType: spawnAgentType(event),
          model_requested: event.model ?? null,
          model_forced: policy.workerModel,
          decision: 'allow',
          ts: new Date().toISOString(),
        }
        await update($, SPAWNS, value => ({ lines: [...(value?.lines ?? []), record] }))
        if (agentId) {
          await update($, AGENTS, value => {
            const ids = value?.ids ?? []
            return ids.includes(agentId) ? { ids } : { ids: [...ids, agentId] }
          })
        }
        await flushSpawns($)
      })
    } catch {
      // The spawn already started. A missing log line fails closed in the worker guard.
    }
    return result
  }).catch(async ($, event, next) => {
    if (!(event.workflow || isTrackedWorkflowResume(event))) return next(event)
    if (next.called) return next(event)
    await logDeny($, event, 'OPUS_SPAWN_GUARD_ERROR')
    return { deny: 'OPUS_SPAWN_GUARD_ERROR' }
  })
}
