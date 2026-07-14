import type {
  CaseSubmissionInput,
  ClaimDecision,
  ConnectionTestResult,
  CreateRunInput,
  GateDecisionInput,
  LocalCaseImportResult,
  NodeKind,
  PromptContent,
  RunEvent,
  RunSnapshot,
  RunStatus,
  RunSummary,
  RuntimeConfigStatus,
  RuntimeConfigUpdate,
  StepAttempt,
  StepStatus,
  WorkflowDefinition,
  WorkflowEdge,
  WorkflowNode,
  WorkflowStage,
} from './types'

type UnknownRecord = Record<string, unknown>

const API_BASE = '/api/v1'
const API_TOKEN_KEY = 'hypoweaver.workflow-api-token'

function accessToken(): string | null {
  return typeof window === 'undefined' ? null : window.sessionStorage.getItem(API_TOKEN_KEY)
}

const stageDefinitions = [
  { id: 'intake-stage', order: 1, title: '案例入口', description: '解析案例材料并在 H1 确认研究边界。', nodeIds: ['intake', 'await_h1'] },
  { id: 'understanding', order: 2, title: '研究理解', description: '拆解假设、形成数据画像并完成方法路由。', nodeIds: ['decompose', 'profile', 'route'] },
  { id: 'method-design', order: 3, title: '方法设计', description: '设计、批判并有限修订分析计划，在 H2 冻结合同。', nodeIds: ['design', 'critique', 'revise_plan', 'await_h2', 'freeze'] },
  { id: 'execution', order: 4, title: '模型执行', description: '通过 Fixture 或隔离的外部执行器生成 ResearchRun。', nodeIds: ['execute'] },
  { id: 'result-audit', order: 5, title: '结果审计', description: '解释结果、审查科学有效性并在 H3 授权 ClaimLedger。', nodeIds: ['interpret', 'scientific_audit', 'build_claims', 'await_h3', 'seal'] },
  { id: 'writing', order: 6, title: '结论与写作', description: '仅使用获批结论写作并完成一致性审计。', nodeIds: ['write', 'audit_draft', 'complete'] },
] satisfies WorkflowStage[]

const deterministicSteps: Array<Pick<WorkflowNode, 'id' | 'title' | 'type' | 'kind' | 'description'>> = [
  { id: 'await_h1', title: 'H1 研究边界确认', type: 'gate', kind: 'gate', description: '批准、退回或停止；决定前下游不会启动。' },
  { id: 'await_h2', title: 'H2 冻结分析计划', type: 'gate', kind: 'gate', description: '批准后冻结版本化 FormalResearchContract。' },
  { id: 'freeze', title: '冻结研究合同', type: 'code', kind: 'code', description: '确定性生成合同哈希和版本。' },
  { id: 'execute', title: '执行研究计划', type: 'executor', kind: 'http', description: 'Fixture 或隔离 Python 执行器，执行状态与科学状态分离。' },
  { id: 'await_h3', title: 'H3 结论授权', type: 'gate', kind: 'gate', description: '逐条授权 Claim；Fixture 只能拒绝或暂缓。' },
  { id: 'seal', title: '封存 ClaimLedger', type: 'code', kind: 'code', description: '封存人工决定，形成不可变审计产物。' },
  { id: 'complete', title: '成果包完成', type: 'end', kind: 'end', description: '输出研究计划或获批论文成果包。' },
]

function stageIdForNode(nodeId: string): string {
  return stageDefinitions.find((stage) => stage.nodeIds.includes(nodeId))?.id ?? 'writing'
}

function asRecord(value: unknown): UnknownRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as UnknownRecord) : {}
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function asString(value: unknown, fallback = ''): string {
  return value === undefined || value === null ? fallback : String(value)
}

function first(record: UnknownRecord, ...keys: string[]): unknown {
  for (const key of keys) {
    if (record[key] !== undefined && record[key] !== null) return record[key]
  }
  return undefined
}

function asNodeKind(value: unknown): NodeKind {
  const normalized = asString(value, 'other').toLowerCase().replaceAll('_', '-')
  const aliases: Record<string, NodeKind> = {
    start: 'start',
    input: 'start',
    document: 'document',
    'document-extractor': 'document',
    llm: 'llm',
    model: 'llm',
    code: 'code',
    validator: 'code',
    gate: 'gate',
    'human-input': 'gate',
    router: 'router',
    'if-else': 'router',
    merge: 'merge',
    aggregator: 'merge',
    http: 'http',
    executor: 'http',
    end: 'end',
    output: 'end',
  }
  return aliases[normalized] ?? 'other'
}

function normalizePrompts(value: unknown): PromptContent[] {
  if (typeof value === 'string') {
    return [{ id: 'prompt-1', role: 'user', template: value, rendered: value }]
  }
  return asArray(value).map((item, index) => {
    const prompt = asRecord(item)
    const template = asString(first(prompt, 'template', 'text', 'content'))
    return {
      id: asString(prompt.id, `prompt-${index + 1}`),
      role: asString(prompt.role, 'prompt'),
      template,
      rendered: asString(first(prompt, 'rendered', 'resolved'), '') || undefined,
    }
  })
}

function normalizeStage(value: unknown, index: number): WorkflowStage {
  const stage = asRecord(value)
  return {
    id: asString(first(stage, 'id', 'key'), `stage-${index + 1}`),
    order: Number(first(stage, 'order', 'index') ?? index + 1),
    title: asString(first(stage, 'title', 'name'), `阶段 ${index + 1}`),
    description: asString(stage.description),
    nodeIds: asArray(first(stage, 'node_ids', 'nodeIds', 'steps')).map(String),
  }
}

function normalizeNode(value: unknown, index: number, stages: WorkflowStage[]): WorkflowNode {
  const node = asRecord(value)
  const position = asRecord(node.position)
  const nodeId = asString(first(node, 'id', 'key'), `node-${index + 1}`)
  const stageId = asString(first(node, 'stage_id', 'stageId')) || stageIdForNode(nodeId) || stages[0]?.id || 'stage-1'
  const stageIndex = Math.max(0, stages.findIndex((stage) => stage.id === stageId))
  const nodeIndexInStage = Math.max(
    0,
    stages[stageIndex]?.nodeIds.findIndex((candidate) => candidate === nodeId) ?? index,
  )
  const type = asString(first(node, 'type', 'kind'), 'other')
  const prompts = normalizePrompts(first(node, 'prompts', 'prompt_templates', 'prompt'))

  if (!prompts.length) {
    const systemPrompt = asString(first(node, 'system_prompt', 'systemPrompt'))
    const userPrompt = asString(first(node, 'user_prompt', 'userPrompt', 'user_template'))
    if (systemPrompt) prompts.push({ id: 'system', role: 'system', template: systemPrompt })
    if (userPrompt) prompts.push({ id: 'user', role: 'user', template: userPrompt })
  }

  return {
    id: nodeId,
    title: asString(first(node, 'title', 'name'), `节点 ${index + 1}`),
    type,
    kind: asNodeKind(type),
    stageId,
    description: asString(node.description),
    position: {
      x: Number(position.x ?? 260 + stageIndex * 360),
      y: Number(position.y ?? 170 + nodeIndexInStage * 150),
    },
    prompts,
    inputSchema: first(node, 'input_schema', 'inputSchema') ?? null,
    outputSchema: first(node, 'output_schema', 'outputSchema') ?? null,
  }
}

function normalizeEdge(value: unknown, index: number): WorkflowEdge {
  const edge = asRecord(value)
  const source = asString(edge.source)
  const target = asString(edge.target)
  return {
    id: asString(edge.id, `${source}-${target}-${index}`),
    source,
    target,
    sourceHandle: asString(first(edge, 'source_handle', 'sourceHandle'), '') || undefined,
    targetHandle: asString(first(edge, 'target_handle', 'targetHandle'), '') || undefined,
    label: asString(first(edge, 'label', 'condition'), '') || undefined,
  }
}

export function normalizeDefinition(payload: unknown): WorkflowDefinition {
  const envelope = asRecord(payload)
  const raw = asRecord(first(envelope, 'definition', 'workflow') ?? payload)
  const stages = (asArray(raw.stages).length ? asArray(raw.stages).map(normalizeStage) : stageDefinitions)
    .sort((left, right) => left.order - right.order)
  const hasStructuredGraph = asArray(raw.stages).length > 0 && asArray(raw.nodes).length > 0
  const apiNodes = asArray(first(raw, 'nodes', 'steps')).map((node, index) => normalizeNode(node, index, stages))
  const apiNodeIds = new Set(apiNodes.map((node) => node.id))
  const nodes = hasStructuredGraph
    ? apiNodes
    : [
        ...apiNodes,
        ...deterministicSteps
          .filter((node) => !apiNodeIds.has(node.id))
          .map((node, index) => normalizeNode({ ...node, stage_id: stageIdForNode(node.id) }, apiNodes.length + index, stages)),
      ]

  if (!nodes.length) throw new Error('工作流定义缺少 steps')

  const orderedNodeIds = stages.flatMap((stage) => stage.nodeIds).filter((nodeId) => nodes.some((node) => node.id === nodeId))
  const fallbackEdges = orderedNodeIds.slice(0, -1).map((source, index) => ({
    id: `${source}-${orderedNodeIds[index + 1]}`,
    source,
    target: orderedNodeIds[index + 1],
  }))

  const nodeIds = new Set(nodes.map((node) => node.id))
  const edges = (asArray(raw.edges).length ? asArray(raw.edges) : fallbackEdges).map(normalizeEdge)
  for (const edge of edges) {
    if (!nodeIds.has(edge.source) || !nodeIds.has(edge.target)) {
      throw new Error(`工作流连线 ${edge.id} 指向不存在的节点`)
    }
  }

  return {
    id: asString(raw.id, 'app-a'),
    version: asString(raw.version, 'unversioned'),
    name: asString(first(raw, 'name', 'title'), 'HypoWeaver-Qwen'),
    description: asString(raw.description),
    stages: stages.map((stage) => ({
      ...stage,
      nodeIds: stage.nodeIds.length ? stage.nodeIds : nodes.filter((node) => node.stageId === stage.id).map((node) => node.id),
    })),
    nodes,
    edges,
    gates: Object.fromEntries(
      Object.entries(asRecord(raw.gates)).map(([gate, value]) => {
        const gateDefinition = asRecord(value)
        return [gate, {
          state: asString(gateDefinition.state),
          decisions: asArray(gateDefinition.decisions).map(String),
        }]
      }),
    ),
  }
}

function normalizeStepStatus(value: unknown): StepStatus {
  const status = asString(value, 'pending').toLowerCase()
  const allowed: StepStatus[] = ['pending', 'running', 'waiting_human', 'succeeded', 'failed', 'blocked', 'skipped']
  if (status === 'completed' || status === 'success') return 'succeeded'
  if (status === 'waiting') return 'waiting_human'
  return allowed.includes(status as StepStatus) ? (status as StepStatus) : 'pending'
}

function normalizeRunStatus(value: unknown): RunStatus {
  const status = asString(value, 'created').toLowerCase()
  const allowed: RunStatus[] = ['created', 'running', 'waiting_human', 'blocked', 'failed', 'completed', 'stopped', 'cancelled']
  if (status === 'waiting') return 'waiting_human'
  if (status === 'success' || status === 'succeeded') return 'completed'
  return allowed.includes(status as RunStatus) ? (status as RunStatus) : 'created'
}

function normalizeStep(value: unknown, index: number): StepAttempt {
  const step = asRecord(value)
  const nodeId = asString(first(step, 'node_id', 'nodeId', 'step_key', 'stepKey', 'stage'))
  const logsValue = first(step, 'logs', 'log')
  return {
    id: asString(first(step, 'id', 'step_run_id', 'attempt_id'), `${nodeId}-${index + 1}`),
    nodeId,
    attempt: Number(step.attempt ?? 1),
    status: normalizeStepStatus(step.status),
    startedAt: asString(first(step, 'started_at', 'startedAt'), '') || undefined,
    endedAt: asString(first(step, 'ended_at', 'endedAt', 'completed_at'), '') || undefined,
    prompts: normalizePrompts(first(step, 'prompts', 'rendered_prompts', 'prompt')),
    input: first(step, 'input', 'inputs') ?? null,
    output: first(step, 'output', 'outputs', 'result') ?? null,
    logs: Array.isArray(logsValue) ? logsValue.map(String) : logsValue ? [String(logsValue)] : [],
    error: asString(first(step, 'error', 'error_message'), '') || undefined,
  }
}

function normalizeEvent(value: unknown, index: number): RunEvent {
  const event = asRecord(value)
  const payload = asRecord(event.payload)
  return {
    seq: Number(first(event, 'seq', 'event_seq') ?? index + 1),
    type: asString(first(event, 'type', 'event_type'), 'run.updated'),
    message: asString(event.message ?? payload.message ?? payload.reason),
    timestamp: asString(first(event, 'timestamp', 'created_at', 'createdAt')),
    nodeId: asString(first(event, 'node_id', 'nodeId', 'step_key') ?? payload.stage ?? event.to_state, '') || undefined,
    stepStatus: event.status || payload.status ? normalizeStepStatus(event.status ?? payload.status) : undefined,
  }
}

function normalizeClaim(value: unknown, index: number) {
  const claim = asRecord(value)
  const decision = asString(first(claim, 'decision', 'approval_status')) as ClaimDecision
  const decisionAliases: Record<string, ClaimDecision> = {
    approved: 'approve',
    downgraded: 'downgrade',
    rejected: 'reject',
  }
  const normalizedDecision = decisionAliases[decision] ?? decision
  return {
    id: asString(first(claim, 'id', 'claim_id'), `claim-${index + 1}`),
    text: asString(first(claim, 'text', 'claim_text')),
    allowedStrength: asString(first(claim, 'allowed_strength', 'allowedStrength'), '') || undefined,
    supportingRuns: asArray(first(claim, 'supporting_runs', 'supportingRuns')).map(String),
    decision: ['approve', 'downgrade', 'reject', 'hold'].includes(normalizedDecision)
      ? normalizedDecision
      : undefined,
  }
}

const artifactKindByStep: Record<string, string[]> = {
  intake: ['research_package'],
  decompose: ['testable_hypotheses'],
  profile: ['data_profile'],
  route: ['method_route'],
  design: ['analysis_plan'],
  critique: ['critic_report'],
  revise_plan: ['analysis_plan'],
  freeze: ['formal_research_contract'],
  execute: ['research_run'],
  interpret: ['evidence_assessment'],
  scientific_audit: ['scientific_audit'],
  build_claims: ['claim_ledger'],
  seal: ['approved_claim_ledger'],
  write: ['manuscript_package'],
  audit_draft: ['draft_audit'],
}

export function normalizeRun(payload: unknown): RunSnapshot {
  const envelope = asRecord(payload)
  const run = asRecord(first(envelope, 'run', 'item') ?? payload)
  const artifacts = asArray(run.artifacts).map(asRecord)
  const caseArtifact = artifacts.find((artifact) => asString(artifact.kind) === 'case_submission')
  const casePayload = asRecord(caseArtifact?.payload)
  const researchRunArtifact = artifacts.find((artifact) => asString(artifact.kind) === 'research_run')
  const researchRunPayload = asRecord(researchRunArtifact?.payload)
  const manuscriptArtifact = artifacts.find((artifact) => asString(artifact.kind) === 'manuscript_package')
  const manuscriptPayload = asRecord(manuscriptArtifact?.payload)
  const modeValue = asString(first(run, 'mode', 'run_mode', 'execution_mode') ?? casePayload.execution_mode, 'fixture').toLowerCase()
  const currentStep = asString(first(run, 'current_step', 'current_node_id', 'currentNodeId', 'current_step_key'))
  const explicitGate = asString(first(run, 'current_gate', 'currentGate')).toUpperCase()
  const currentGate = explicitGate || (['await_h1', 'h1_gate'].includes(currentStep)
    ? 'H1'
    : ['await_h2', 'h2_gate'].includes(currentStep)
      ? 'H2'
      : ['await_h3', 'h3_gate'].includes(currentStep)
        ? 'H3'
        : '')
  const eventRecords = asArray(run.events).map(asRecord)
  const attempts = asArray(first(run, 'steps', 'step_runs', 'step_attempts', 'attempts')).map((value, index) => {
    const normalized = normalizeStep(value, index)
    const artifactKinds = artifactKindByStep[normalized.nodeId] ?? []
    const outputArtifact = [...artifacts].reverse().find((artifact) => artifactKinds.includes(asString(artifact.kind)))
    const relatedEvents = eventRecords.filter((event) => {
      const eventPayload = asRecord(event.payload)
      return asString(first(eventPayload, 'stage', 'step', 'step_key')) === normalized.nodeId
        || asString(first(event, 'to_state', 'from_state')) === normalized.nodeId
    })
    const eventInput = relatedEvents.map((event) => asRecord(event.payload).input).find((value) => value !== undefined)
    const eventPrompts = relatedEvents.map((event) => asRecord(event.payload).prompts).find((value) => value !== undefined)
    return {
      ...normalized,
      prompts: normalized.prompts.length ? normalized.prompts : normalizePrompts(eventPrompts),
      input: normalized.input ?? eventInput ?? null,
      output: normalized.output ?? outputArtifact?.payload ?? null,
      logs: normalized.logs.length ? normalized.logs : relatedEvents.map((event) => asString(asRecord(event.payload).message)).filter(Boolean),
    }
  })
  const claimArtifact = [...artifacts].reverse().find((artifact) => ['claim_ledger', 'approved_claim_ledger'].includes(asString(artifact.kind)))
  const claimPayload = asRecord(claimArtifact?.payload)
  const normalizedEvents = eventRecords.map(normalizeEvent).sort((left, right) => left.seq - right.seq)
  const firstTimestamp = normalizedEvents[0]?.timestamp ?? ''
  const lastTimestamp = normalizedEvents.at(-1)?.timestamp ?? firstTimestamp
  return {
    id: asString(first(run, 'id', 'run_id')),
    version: Number(run.version ?? 0),
    definitionId: asString(first(run, 'definition_id', 'definitionId'), 'app-a'),
    definitionVersion: asString(first(run, 'definition_version', 'definitionVersion', 'workflow_version'), 'unversioned'),
    caseId: asString(first(run, 'case_id', 'caseId', 'preset_id')),
    caseName: asString(first(run, 'case_name', 'caseName', 'title') ?? casePayload.case_title, '未命名案例'),
    mode: modeValue === 'research' ? 'research' : 'fixture',
    status: normalizeRunStatus(run.status),
    currentNodeId: currentStep || undefined,
    currentGate: ['H1', 'H2', 'H3'].includes(currentGate) ? (currentGate as 'H1' | 'H2' | 'H3') : undefined,
    executionStatus: asString(first(run, 'execution_status', 'executionStatus') ?? researchRunPayload.execution_status, 'not_started'),
    scientificStatus: asString(first(run, 'scientific_status', 'scientificStatus') ?? researchRunPayload.scientific_status, 'not_assessed'),
    planOnly: Boolean(first(run, 'plan_only', 'planOnly')) || manuscriptPayload.mode === 'research_plan_only',
    createdAt: asString(first(run, 'created_at', 'createdAt'), firstTimestamp),
    updatedAt: asString(first(run, 'updated_at', 'updatedAt'), lastTimestamp),
    steps: attempts,
    events: normalizedEvents,
    claims: asArray(first(run, 'claims', 'claim_ledger') ?? claimPayload.claims).map(normalizeClaim),
    allowedActions: asArray(first(run, 'allowed_actions', 'allowedActions')).map(String),
  }
}

export function normalizeRunList(payload: unknown): RunSummary[] {
  const envelope = asRecord(payload)
  const items = Array.isArray(payload) ? payload : asArray(first(envelope, 'items', 'runs'))
  return items.map((item) => {
    const run = normalizeRun(item)
    return {
      id: run.id,
      caseName: run.caseName,
      mode: run.mode,
      status: run.status,
      currentGate: run.currentGate,
      updatedAt: run.updatedAt,
    }
  })
}

async function request(path: string, init?: RequestInit): Promise<unknown> {
  const token = accessToken()
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(token ? { 'X-Hypoweaver-Token': token } : {}),
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    const message = asString(first(asRecord(payload), 'detail', 'message'), `HTTP ${response.status}`)
    throw new Error(message)
  }
  return payload
}

function serializeCase(input: NonNullable<CreateRunInput['case']>) {
  return {
    case_id: input.caseId,
    title: input.title,
    research_question: input.researchQuestion,
    hypotheses: input.hypotheses.filter((hypothesis) => hypothesis.statement.trim()).map((hypothesis) => ({
      hypothesis_id: hypothesis.hypothesisId,
      statement: hypothesis.statement,
      expected_direction: hypothesis.expectedDirection,
      mechanism: hypothesis.mechanism || null,
    })),
    unit_of_analysis: input.unitOfAnalysis || null,
    sample_period: input.samplePeriod || null,
    data_structure_hint: input.dataStructureHint,
    variables: input.variables.filter((variable) => variable.name.trim()).map((variable) => ({
      name: variable.name,
      label: variable.label || null,
      role: variable.role,
      definition: variable.definition || null,
      source: variable.source || null,
    })),
    dataset_refs: input.datasetRefs.filter((dataset) => (
      dataset.datasetId.trim()
      && dataset.filename.trim()
      && /^[a-f0-9]{64}$/i.test(dataset.sha256.trim())
      && dataset.sizeBytes > 0
    )).map((dataset) => ({
      dataset_id: dataset.datasetId,
      role: dataset.role,
      filename: dataset.filename,
      mime_type: dataset.mimeType,
      sha256: dataset.sha256,
      size_bytes: dataset.sizeBytes,
    })),
    known_policy_facts: input.knownPolicyFacts.map((item) => item.trim()).filter(Boolean),
    constraints: input.constraints.map((item) => item.trim()).filter(Boolean),
  }
}

function normalizeConfigStatus(payload: unknown): RuntimeConfigStatus {
  const config = asRecord(payload)
  const secret = (value: unknown) => {
    const item = asRecord(value)
    return { configured: Boolean(item.configured), source: asString(item.source, 'missing') as RuntimeConfigStatus['qwenApiKey']['source'] }
  }
  const visible = (value: unknown) => {
    const item = asRecord(value)
    return {
      value: item.value === null || item.value === undefined ? null : String(item.value),
      source: asString(item.source, 'missing') as RuntimeConfigStatus['qwenModel']['source'],
    }
  }
  return {
    configPath: asString(first(config, 'config_path', 'configPath')),
    environmentPrecedence: Boolean(first(config, 'environment_precedence', 'environmentPrecedence')),
    workflowApiTokenRequired: Boolean(first(config, 'workflow_api_token_required', 'workflowApiTokenRequired')),
    qwenApiKey: secret(first(config, 'qwen_api_key', 'qwenApiKey')),
    qwenModel: visible(first(config, 'qwen_model', 'qwenModel')),
    qwenBaseUrl: visible(first(config, 'qwen_base_url', 'qwenBaseUrl')),
    researchEngineUrl: visible(first(config, 'research_engine_url', 'researchEngineUrl')),
    researchEngineToken: secret(first(config, 'research_engine_token', 'researchEngineToken')),
  }
}

function normalizeCaseSubmission(payload: unknown): CaseSubmissionInput {
  const value = asRecord(payload)
  return {
    caseId: asString(first(value, 'case_id', 'caseId')),
    title: asString(value.title),
    researchQuestion: asString(first(value, 'research_question', 'researchQuestion')),
    hypotheses: asArray(value.hypotheses).map((item, index) => {
      const hypothesis = asRecord(item)
      return {
        hypothesisId: asString(first(hypothesis, 'hypothesis_id', 'hypothesisId'), `H${index + 1}`),
        statement: asString(hypothesis.statement),
        expectedDirection: asString(first(hypothesis, 'expected_direction', 'expectedDirection'), 'unspecified') as CaseSubmissionInput['hypotheses'][number]['expectedDirection'],
        mechanism: asString(hypothesis.mechanism),
      }
    }),
    unitOfAnalysis: asString(first(value, 'unit_of_analysis', 'unitOfAnalysis')),
    samplePeriod: asString(first(value, 'sample_period', 'samplePeriod')),
    dataStructureHint: asString(first(value, 'data_structure_hint', 'dataStructureHint'), 'unknown') as CaseSubmissionInput['dataStructureHint'],
    variables: asArray(value.variables).map((item) => {
      const variable = asRecord(item)
      return {
        name: asString(variable.name),
        label: asString(variable.label),
        role: asString(variable.role, 'unknown') as CaseSubmissionInput['variables'][number]['role'],
        definition: asString(variable.definition),
        source: asString(variable.source),
      }
    }),
    datasetRefs: asArray(first(value, 'dataset_refs', 'datasetRefs')).map((item) => {
      const dataset = asRecord(item)
      return {
        datasetId: asString(first(dataset, 'dataset_id', 'datasetId')),
        role: asString(dataset.role, 'main') as CaseSubmissionInput['datasetRefs'][number]['role'],
        filename: asString(dataset.filename),
        mimeType: asString(first(dataset, 'mime_type', 'mimeType'), 'text/csv'),
        sha256: asString(dataset.sha256),
        sizeBytes: Number(first(dataset, 'size_bytes', 'sizeBytes') ?? 0),
      }
    }),
    knownPolicyFacts: asArray(first(value, 'known_policy_facts', 'knownPolicyFacts')).map(String),
    constraints: asArray(value.constraints).map(String),
  }
}

export function normalizeLocalCaseImport(payload: unknown): LocalCaseImportResult {
  const envelope = asRecord(payload)
  const report = asRecord(first(envelope, 'report', 'import_report', 'importReport'))
  const yearMin = first(report, 'year_min', 'yearMin')
  const yearMax = first(report, 'year_max', 'yearMax')
  const explicitSamplePeriod = asString(first(report, 'sample_period', 'samplePeriod'))
  return {
    case: normalizeCaseSubmission(first(envelope, 'case_submission', 'case')),
    report: {
      datasetFilename: asString(first(report, 'dataset_filename', 'main_dataset_filename', 'main_data_filename', 'datasetFilename')),
      rowCount: Number(first(report, 'row_count', 'rowCount') ?? 0),
      columnCount: Number(first(report, 'column_count', 'columnCount') ?? 0),
      samplePeriod: explicitSamplePeriod || (yearMin !== undefined && yearMax !== undefined ? `${yearMin}—${yearMax}` : undefined),
      hiddenFileCount: Number(first(report, 'hidden_file_count', 'hiddenFileCount') ?? 0),
      excludedFileCount: Number(first(report, 'excluded_file_count', 'excludedFileCount') ?? 0),
      reviewItems: asArray(first(report, 'review_items', 'human_review_items', 'reviewItems')).map(String),
    },
  }
}

export const workflowApi = {
  hasAccessToken(): boolean {
    return Boolean(accessToken())
  },

  setAccessToken(token: string): void {
    if (typeof window === 'undefined') return
    if (token.trim()) window.sessionStorage.setItem(API_TOKEN_KEY, token.trim())
    else window.sessionStorage.removeItem(API_TOKEN_KEY)
  },

  async getDefinition(): Promise<WorkflowDefinition> {
    return normalizeDefinition(await request('/definitions/app-a'))
  },

  async listRuns(): Promise<RunSummary[]> {
    return normalizeRunList(await request('/runs'))
  },

  async getRun(runId: string): Promise<RunSnapshot> {
    return normalizeRun(await request(`/runs/${encodeURIComponent(runId)}`))
  },

  async createRun(input: CreateRunInput): Promise<RunSnapshot> {
    if (Boolean(input.presetId) === Boolean(input.case)) {
      throw new Error('必须且只能提交预设案例或自定义研究输入。')
    }
    const payload = await request('/runs', {
      method: 'POST',
      body: JSON.stringify({
        definition_id: 'app-a',
        ...(input.presetId ? { preset_case_id: input.presetId } : { case: serializeCase(input.case!) }),
        mode: input.mode,
        model_provider: input.mode === 'research' ? 'qwen' : 'fixture',
        execution_mode: input.mode === 'research' ? 'external' : 'fixture',
      }),
    })
    return normalizeRun(payload)
  },

  async importLocalCase(path: string): Promise<LocalCaseImportResult> {
    if (!path.trim()) throw new Error('请先填写本地案例文件夹路径。')
    return normalizeLocalCaseImport(await request('/case-imports/local', {
      method: 'POST',
      body: JSON.stringify({ path: path.trim() }),
    }))
  },

  async getRuntimeConfig(): Promise<RuntimeConfigStatus> {
    return normalizeConfigStatus(await request('/runtime-config'))
  },

  async updateRuntimeConfig(input: RuntimeConfigUpdate): Promise<RuntimeConfigStatus> {
    return normalizeConfigStatus(await request('/runtime-config', {
      method: 'PUT',
      body: JSON.stringify({
        ...(input.qwenApiKey ? { qwen_api_key: input.qwenApiKey } : {}),
        ...(input.qwenModel ? { qwen_model: input.qwenModel } : {}),
        ...(input.qwenBaseUrl ? { qwen_base_url: input.qwenBaseUrl } : {}),
        ...(input.researchEngineUrl ? { research_engine_url: input.researchEngineUrl } : {}),
        ...(input.researchEngineToken ? { research_engine_token: input.researchEngineToken } : {}),
        clear_qwen_api_key: Boolean(input.clearQwenApiKey),
        clear_research_engine_token: Boolean(input.clearResearchEngineToken),
        clear_research_engine_url: Boolean(input.clearResearchEngineUrl),
      }),
    }))
  },

  async testRuntimeConnection(target: ConnectionTestResult['target']): Promise<ConnectionTestResult> {
    const payload = asRecord(await request('/runtime-config/tests', {
      method: 'POST',
      body: JSON.stringify({ target }),
    }))
    return {
      target: asString(payload.target) as ConnectionTestResult['target'],
      success: Boolean(payload.success),
      message: asString(payload.message),
      statusCode: payload.status_code === null || payload.status_code === undefined ? undefined : Number(payload.status_code),
    }
  },

  async advanceRun(runId: string): Promise<RunSnapshot> {
    return normalizeRun(await request(`/runs/${encodeURIComponent(runId)}/advance`, { method: 'POST' }))
  },

  async decideGate(run: RunSnapshot, gate: string, input: GateDecisionInput): Promise<RunSnapshot> {
    const payload = await request(`/runs/${encodeURIComponent(run.id)}/gates/${encodeURIComponent(gate)}`, {
      method: 'POST',
      body: JSON.stringify({
        action: input.action,
        comment: input.comment ?? '',
        actor: 'local_researcher',
        expected_run_version: run.version,
        idempotency_key: crypto.randomUUID(),
        reviewed_artifact_hashes: {},
        claims: input.claims?.map((claim) => ({
          claim_id: claim.claimId,
          decision: claim.decision,
          final_text: claim.finalText,
          reason: claim.reason ?? input.comment ?? '',
        })) ?? [],
      }),
    })
    return normalizeRun(payload)
  },

  async submitRevision(run: RunSnapshot, gate: 'H1' | 'H2', revision: unknown): Promise<RunSnapshot> {
    const payload = await request(`/runs/${encodeURIComponent(run.id)}/revisions`, {
      method: 'POST',
      body: JSON.stringify({
        gate,
        expected_run_version: run.version,
        idempotency_key: crypto.randomUUID(),
        actor: 'local_researcher',
        ...(gate === 'H1' ? { case: revision } : { analysis_plan: revision }),
      }),
    })
    return normalizeRun(payload)
  },
}
