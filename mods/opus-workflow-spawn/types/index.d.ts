export type Counters = {
  perRun: { [runId: string]: number }
  perSession: number
}

export type WorkflowAgents = {
  ids: string[]
}

export type SpawnRecord = {
  session_id: string
  runId: string
  agentIndex: number
  decision: 'allow' | 'deny'
  ts: string
  agentId?: string | null
  model_requested?: string | null
  model_forced?: string
  reason?: string
}

export type SpawnLog = {
  lines: SpawnRecord[]
}

declare module 'claude-code' {
  interface PluginState {
    'opus-workflow-spawn': {
      counters: Counters
      workflowAgents: WorkflowAgents
      spawnLog: SpawnLog
    }
  }
}
