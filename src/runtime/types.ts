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

export type GateAction =
  | 'approve'
  | 'revise'
  | 'reject'
  | 'generate_plan_only'
  | 'generate_identification_failure_report'
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
  finalText?: string
  claimType?: string
  allowedStrength?: string
  maxAllowedStrength?: string
  admissionStatus?: 'unassessed' | 'admitted' | 'downgrade_required' | 'prohibited' | 'rejected'
  evidenceStatus?: string
  robustnessStatus?: string
  requiredCheckIds: string[]
  gateReasons: string[]
  supportingRuns: string[]
  decision?: ClaimDecision
}

export interface ManuscriptSectionView {
  id: string
  title: string
  content: string
  status: 'generated' | 'not_generated'
  claimIds: string[]
  runIds: string[]
  figureIds: string[]
  statements: ManuscriptStatementSourceView[]
}

export interface ManuscriptStatementSourceView {
  id: string
  kind: 'authorized_claim' | 'estimate_fact' | 'sample_fact' | 'diagnostic_fact' | 'citation'
  claimIds: string[]
  executionIds: string[]
  sources: Array<{
    kind: 'claim' | 'execution' | 'passage'
    id: string
    path: string
  }>
}

export interface ManuscriptPackageView {
  version: number
  irVersion: number
  mode: 'research_plan_only' | 'full_manuscript' | 'identification_failure_report'
  status: 'draft' | 'needs_revision' | 'ready_for_human_review' | 'not_generated'
  researchPlan: string
  sections: ManuscriptSectionView[]
  figureIds: string[]
  disclosures: string[]
  unresolvedIssues: string[]
  auditResult: 'not_run' | 'pass_with_no_critical_issues' | 'revise'
}

export interface FigureFileView {
  format: 'svg' | 'png' | 'pdf' | 'csv'
  mimeType: string
  sha256: string
}

export interface FigureView {
  id: string
  recipeId: string
  recipeVersion: string
  title: string
  caption: string
  altText: string
  executionIds: string[]
  claimIds: string[]
  files: FigureFileView[]
  warnings: string[]
}

export interface FigureBundleView {
  stage: 'evidence' | 'publication'
  status: 'succeeded' | 'not_generated' | 'failed'
  figures: FigureView[]
  warnings: string[]
}

export interface DesignCandidateView {
  id: string
  strategy: 'direct_baseline' | 'identification_first' | 'measurement_robustness'
  rationale: string
  methodFamily: string
  estimator: string
  formula?: string
  probeVerdict: 'pass' | 'warn' | 'fail'
  executorReady: boolean
  probeChecks: Array<{ id: string; status: 'pass' | 'warn' | 'fail'; evidence: string }>
  reviewIssueCount: number
  reviewerRejected: boolean
}

export interface DesignArenaView {
  id: string
  candidates: DesignCandidateView[]
  recommendedCandidateIds: string[]
  provisionalCandidateId?: string
  selectionRationale: string[]
}

export type ModelCallGroup = 'h1_h2' | 'h3' | 'h4'

export interface ModelUsageView {
  maxCalls: number
  llmCalls: number
  logicalCalls: number
  providerAttempts: number
  requiredLogicalCalls: number
  retryPolicy?: string
  retryMode?: string
  sharedRetrySlots: number
  sharedRetryRemaining: number
  groupUsage: Record<ModelCallGroup, number>
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
  currentGate?: 'H1' | 'H2' | 'H3' | 'H4'
  lastError?: string
  executionStatus: string
  scientificStatus: string
  planOnly: boolean
  createdAt: string
  updatedAt: string
  steps: StepAttempt[]
  events: RunEvent[]
  claims: ClaimRecord[]
  figureBundles: FigureBundleView[]
  manuscript?: ManuscriptPackageView
  designArena?: DesignArenaView
  modelUsage?: ModelUsageView
  allowedActions: string[]
}

export interface RunSummary {
  id: string
  caseName: string
  mode: 'fixture' | 'research'
  status: RunStatus
  currentGate?: 'H1' | 'H2' | 'H3' | 'H4'
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
  | 'fixed_effect'
  | 'cluster'
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
  designEnvelope?: {
    benchmarkTrack: 'strict_blind' | 'reproduction_aligned'
    researchGoal: 'causal' | 'associational' | 'mechanism' | 'prediction' | 'measurement' | 'structural' | 'mixed'
    targetEstimands: string[]
    designConstraints: string[]
    requiredDiagnostics: string[]
    allowedClaimStrength: 'causal' | 'associational' | 'descriptive' | 'not_prespecified'
  }
  policyDesign?: {
    policyDate: string
    groupField: string
    timeField: string
    policyStartWeight?: number
    postStartWeight: number
    exposureName: string
    fixedEffects: string[]
    clusterFields: string[]
    clusterComposition: 'interaction'
    eventReferenceYear?: number
    eventYears: number[]
    eventRemotePreYears: number[]
    eventTermScaling: 'binary_group_year_contrast'
    placeboStartYear?: number
    placeboRepetitions?: number
    permutationScheme: 'assignment_unit_label' | 'rowwise_exposure'
    permutationUnitField?: string
    randomSeed?: number
  }
  knownPolicyFacts: string[]
  constraints: string[]
}

export interface CaseImportReport {
  datasetFilename: string
  rowCount: number
  columnCount: number
  samplePeriod?: string
  hiddenFileCount: number
  excludedFileCount: number
  reviewItems: string[]
}

export interface LocalCaseImportResult {
  case: CaseSubmissionInput
  report: CaseImportReport
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
  selectedCandidateId?: string
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

export interface BaselinePhase {
  id: string
  title: string
  status: 'pending' | 'running' | 'succeeded' | 'failed'
}

export interface BaselineRun {
  id: string
  systemId: 'agent_laboratory_social_science_adapted' | 'agent_laboratory_upstream_original'
  caseId: string
  caseName: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  phases: BaselinePhase[]
  executionStatus: string
  scientificStatus: string
  methodFamily?: string
  llmCalls: number
  inputTokens: number
  outputTokens: number
  wallTimeSeconds: number
  error?: string
  createdAt: string
  updatedAt: string
}
