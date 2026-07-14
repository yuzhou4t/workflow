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

export type DataStructure =
  | 'cross_section'
  | 'panel'
  | 'time_series'
  | 'spatial_panel'
  | 'event'
  | 'unknown'

export type VariableRole =
  | 'outcome'
  | 'treatment'
  | 'exposure'
  | 'mediator'
  | 'moderator'
  | 'control'
  | 'id'
  | 'time'
  | 'spatial_id'
  | 'event_date'
  | 'unknown'

export interface HypothesisInput {
  hypothesisId: string
  statement: string
  expectedDirection: 'positive' | 'negative' | 'nonlinear' | 'heterogeneous' | 'unspecified'
  mechanism: string
}

export interface VariableInput {
  name: string
  label: string
  role: VariableRole
  definition: string
  source: string
}

export interface DatasetReferenceInput {
  datasetId: string
  role: 'main' | 'supplementary'
  filename: string
  mimeType: string
  sha256: string
  sizeBytes: number
}

export interface CaseSubmissionInput {
  caseId: string
  title: string
  researchQuestion: string
  hypotheses: HypothesisInput[]
  unitOfAnalysis: string
  samplePeriod: string
  dataStructureHint: DataStructure
  variables: VariableInput[]
  datasetRefs: DatasetReferenceInput[]
  knownPolicyFacts: string[]
  constraints: string[]
}

export interface CreateRunInput {
  mode: 'fixture' | 'research'
  presetId?: string
  case?: CaseSubmissionInput
}

export interface GateDecisionInput {
  action: GateAction
  comment?: string
  claims?: Array<{ claimId: string; decision: ClaimDecision; finalText?: string; reason?: string }>
}

export type ConfigSource = 'environment' | 'file' | 'default' | 'missing'

export interface RuntimeConfigStatus {
  configPath: string
  environmentPrecedence: boolean
  workflowApiTokenRequired: boolean
  qwenApiKey: { configured: boolean; source: ConfigSource }
  qwenModel: { value: string | null; source: ConfigSource }
  qwenBaseUrl: { value: string | null; source: ConfigSource }
  researchEngineUrl: { value: string | null; source: ConfigSource }
  researchEngineToken: { configured: boolean; source: ConfigSource }
}

export interface RuntimeConfigUpdate {
  qwenApiKey?: string
  qwenModel?: string
  qwenBaseUrl?: string
  researchEngineUrl?: string
  researchEngineToken?: string
  clearQwenApiKey?: boolean
  clearResearchEngineToken?: boolean
  clearResearchEngineUrl?: boolean
}

export interface ConnectionTestResult {
  target: 'qwen' | 'research_engine'
  success: boolean
  message: string
  statusCode?: number
}
