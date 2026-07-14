export type NodeKind =
  | 'start'
  | 'document'
  | 'llm'
  | 'code'
  | 'gate'
  | 'router'
  | 'merge'
  | 'http'
  | 'end'
  | 'other'

export type RunStatus =
  | 'created'
  | 'running'
  | 'waiting_human'
  | 'blocked'
  | 'failed'
  | 'completed'
  | 'stopped'
  | 'cancelled'

export type StepStatus =
  | 'pending'
  | 'running'
  | 'waiting_human'
  | 'succeeded'
  | 'failed'
  | 'blocked'
  | 'skipped'

export type GateAction = 'approve' | 'revise' | 'reject' | 'generate_plan_only'
export type ClaimDecision = 'approve' | 'downgrade' | 'reject' | 'hold'

export interface PromptContent {
  id: string
  role: string
  template: string
  rendered?: string
}

export interface WorkflowStage {
  id: string
  order: number
  title: string
  description: string
  nodeIds: string[]
}

export interface WorkflowNode {
  id: string
  title: string
  type: string
  kind: NodeKind
  stageId: string
  description: string
  position: { x: number; y: number }
  prompts: PromptContent[]
  inputSchema: unknown
  outputSchema: unknown
}

export interface WorkflowEdge {
  id: string
  source: string
  target: string
  sourceHandle?: string
  targetHandle?: string
  label?: string
}

export interface WorkflowDefinition {
  id: string
  version: string
  name: string
  description: string
  stages: WorkflowStage[]
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
  gates: Record<string, { state: string; decisions: string[] }>
}

export interface StepAttempt {
  id: string
  nodeId: string
  attempt: number
  status: StepStatus
  startedAt?: string
  endedAt?: string
  prompts: PromptContent[]
  input: unknown
  output: unknown
  logs: string[]
  error?: string
}

export interface RunEvent {
  seq: number
  type: string
  message: string
  timestamp: string
  nodeId?: string
  stepStatus?: StepStatus
}

export interface ClaimRecord {
  id: string
  text: string
  allowedStrength?: string
  supportingRuns: string[]
  decision?: ClaimDecision
}

export interface RunSnapshot {
  id: string
  version: number
  definitionId: string
  definitionVersion: string
  caseId: string
  caseName: string
  mode: 'fixture' | 'research'
  status: RunStatus
  currentNodeId?: string
  currentGate?: 'H1' | 'H2' | 'H3'
  executionStatus: string
  scientificStatus: string
  planOnly: boolean
  createdAt: string
  updatedAt: string
  steps: StepAttempt[]
  events: RunEvent[]
  claims: ClaimRecord[]
  allowedActions: string[]
}

export interface RunSummary {
  id: string
  caseName: string
  mode: 'fixture' | 'research'
  status: RunStatus
  currentGate?: 'H1' | 'H2' | 'H3'
  updatedAt: string
}

export interface CreateRunInput {
  presetId: string
  mode: 'fixture' | 'research'
}

export interface GateDecisionInput {
  action: GateAction
  comment?: string
  claims?: Array<{ claimId: string; decision: ClaimDecision; finalText?: string; reason?: string }>
}

export interface PresetCase {
  id: string
  title: string
  description: string
  method: string
}
