import { afterEach, describe, expect, it, vi } from 'vitest'
import { normalizeDefinition, normalizeLocalCaseImport, normalizeRun, normalizeRunList, workflowApi } from '../src/runtime/api'

const definitionPayload = {
  id: 'app-a',
  version: '1.0.0',
  title: 'HypoWeaver App A',
  steps: [
    {
      id: 'intake',
      title: '案例解析',
      kind: 'llm',
      prompt_version: '1.0.0',
      system_prompt: '只输出 JSON',
      user_template: '输入：{payload}',
      input_schema: { type: 'object' },
      output_schema: { type: 'object', required: ['case_id'] },
    },
    {
      id: 'decompose',
      title: '假设拆解',
      kind: 'llm',
      system_prompt: '拆成可证伪预测',
      user_template: '输入：{payload}',
    },
  ],
  gates: {
    H1: { state: 'await_h1', decisions: ['approve', 'revise', 'stop'] },
  },
}

const runPayload = {
  id: 'run-001',
  case_id: 'green-finance-did',
  definition: 'app-a',
  version: 7,
  current_step: 'await_h3',
  status: 'waiting',
  allowed_actions: ['gate:H3'],
  revision_round: 1,
  step_attempts: [
    {
      id: 'attempt-1',
      stage: 'intake',
      status: 'success',
      attempt: 1,
      prompt_version: '1.0.0',
      provider: 'fixture',
      started_at: '2026-07-14T10:00:00Z',
      completed_at: '2026-07-14T10:00:01Z',
    },
    {
      id: 'attempt-2',
      stage: 'build_claims',
      status: 'success',
      attempt: 1,
      provider: 'fixture',
      started_at: '2026-07-14T10:00:02Z',
      completed_at: '2026-07-14T10:00:03Z',
    },
  ],
  artifacts: [
    {
      id: 'case-artifact',
      kind: 'case_submission',
      version: 1,
      sha256: 'abc',
      payload: {
        case_title: '绿色金融试验区政策评估',
        run_mode: 'preset_demo',
        execution_mode: 'fixture',
      },
      created_at: '2026-07-14T10:00:00Z',
    },
    {
      id: 'claim-artifact',
      kind: 'claim_ledger',
      version: 1,
      sha256: 'def',
      payload: {
        claims: [
          {
            claim_id: 'claim-1',
            claim_text: 'Fixture 不得形成实证结论',
            allowed_strength: 'prohibited',
            supporting_runs: [],
          },
        ],
      },
      created_at: '2026-07-14T10:00:03Z',
    },
  ],
  events: [
    {
      seq: 1,
      event_type: 'run.created',
      from_state: null,
      to_state: 'intake',
      payload: { message: 'Run 已创建' },
      created_at: '2026-07-14T10:00:00Z',
    },
    {
      seq: 2,
      event_type: 'gate.waiting',
      from_state: 'build_claims',
      to_state: 'await_h3',
      payload: { message: '等待 H3' },
      created_at: '2026-07-14T10:00:04Z',
    },
  ],
}

const actualBackendRunPayload = {
  id: 'run-code-native',
  version: 3,
  definition_id: 'app-a',
  definition_version: '1.0.0',
  case_id: 'green-finance-did',
  case_name: '绿色金融试验区政策评估',
  mode: 'fixture',
  status: 'waiting_human',
  current_node_id: 'h3_gate',
  current_gate: 'H3',
  execution_status: 'fixture_only',
  scientific_status: 'not_evaluated',
  plan_only: true,
  created_at: '2026-07-14T10:00:00Z',
  updated_at: '2026-07-14T10:01:00Z',
  steps: [{
    id: 'step-h3',
    node_id: 'h3_gate',
    attempt: 1,
    status: 'waiting_human',
    prompts: [],
    input: { ledger_id: 'ledger-1' },
    output: null,
    logs: ['H3 已暂停'],
  }],
  events: [{ seq: 1, type: 'gate.waiting', message: 'H3 等待人工决定。', timestamp: '2026-07-14T10:01:00Z', node_id: 'h3_gate', status: 'waiting_human' }],
  claims: [{ claim_id: 'claim-H1', claim_text: '尚未检验', allowed_strength: 'prohibited', supporting_runs: [], opposing_runs: [], evidence_status: 'not_tested', scope: '', robustness_status: 'not_executed', unresolved_risks: [], approval_status: 'pending' }],
  artifacts: {
    claim_ledger: { artifact_id: 'run:claim_ledger', kind: 'claim_ledger', sha256: 'abc', payload: { claims: [] } },
  },
}

describe('runtime API adapter', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('turns the code definition into the six-stage console graph', () => {
    const definition = normalizeDefinition(definitionPayload)

    expect(definition.id).toBe('app-a')
    expect(definition.name).toBe('HypoWeaver App A')
    expect(definition.stages).toHaveLength(6)
    expect(definition.nodes.find((node) => node.id === 'intake')?.prompts).toHaveLength(2)
    expect(definition.nodes.find((node) => node.id === 'decompose')?.stageId).toBe('understanding')
    expect(definition.nodes.find((node) => node.id === 'await_h1')?.kind).toBe('gate')
    expect(definition.edges).toHaveLength(definition.nodes.length - 1)
    expect(definition.gates.H1.decisions).toContain('approve')
  })

  it('restores a persisted fixture run, attempts, artifacts, claims and events', () => {
    const run = normalizeRun(runPayload)

    expect(run.id).toBe('run-001')
    expect(run.version).toBe(7)
    expect(run.mode).toBe('fixture')
    expect(run.status).toBe('waiting_human')
    expect(run.currentGate).toBe('H3')
    expect(run.caseName).toBe('绿色金融试验区政策评估')
    expect(run.steps[0].nodeId).toBe('intake')
    expect(run.steps[1].output).toMatchObject({ claims: expect.any(Array) })
    expect(run.claims[0].id).toBe('claim-1')
    expect(run.events.map((event) => event.seq)).toEqual([1, 2])
  })

  it('normalizes the exact code-native RunState wire contract', () => {
    const run = normalizeRun(actualBackendRunPayload)
    expect(run).toMatchObject({
      id: 'run-code-native',
      version: 3,
      currentNodeId: 'h3_gate',
      currentGate: 'H3',
      executionStatus: 'fixture_only',
      scientificStatus: 'not_evaluated',
      planOnly: true,
    })
    expect(run.steps[0]).toMatchObject({ nodeId: 'h3_gate', status: 'waiting_human' })
    expect(run.claims[0]).toMatchObject({ id: 'claim-H1', allowedStrength: 'prohibited' })
  })

  it('normalizes list envelopes without leaking wire fields into components', () => {
    const runs = normalizeRunList({ items: [runPayload] })
    expect(runs).toEqual([
      expect.objectContaining({ id: 'run-001', status: 'waiting_human', currentGate: 'H3' }),
    ])
  })

  it('normalizes a safe local case import without exposing a source path', async () => {
    const payload = {
      case_submission: {
        case_id: 'case-esg-sdla-dad550862bf5',
        title: 'ESG 与 SDLA 的企业面板研究',
        research_question: '企业 ESG 表现是否与 SDLA 存在系统性关联？',
        hypotheses: [{ hypothesis_id: 'H1', statement: 'ESG 与 SDLA 存在系统性关联。', expected_direction: 'unspecified', mechanism: null }],
        unit_of_analysis: '企业—年度',
        sample_period: '2009—2021',
        data_structure_hint: 'panel',
        variables: [{ name: 'SDLA', label: 'SDLA（定义待确认）', role: 'outcome', definition: null, source: '案例数据' }],
        dataset_refs: [{ dataset_id: 'dataset-dad550862bf5', role: 'main', filename: 'ESG-SDLA-数据.csv', mime_type: 'text/csv', sha256: 'a'.repeat(64), size_bytes: 41972980 }],
        known_policy_facts: [],
        constraints: ['变量定义待 H1 确认。'],
      },
      import_report: {
        dataset_filename: 'ESG-SDLA-数据.csv',
        row_count: 30311,
        column_count: 132,
        sample_period: '2009—2021',
        hidden_file_count: 3,
        excluded_file_count: 2,
        review_items: ['请确认 SDLA 的定义。'],
      },
    }
    expect(normalizeLocalCaseImport(payload)).toMatchObject({
      case: { caseId: 'case-esg-sdla-dad550862bf5', dataStructureHint: 'panel' },
      report: { rowCount: 30311, hiddenFileCount: 3 },
    })
    expect(JSON.stringify(normalizeLocalCaseImport(payload))).not.toContain('/Users/')

    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => payload })
    vi.stubGlobal('fetch', fetchMock)
    await workflowApi.importLocalCase('/tmp/case-1')
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/case-imports/local', expect.objectContaining({ method: 'POST' }))
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ path: '/tmp/case-1' })
  })

  it('rejects definitions whose explicit edge references a missing node', () => {
    expect(() => normalizeDefinition({ ...definitionPayload, edges: [{ source: 'intake', target: 'missing' }] }))
      .toThrow(/不存在的节点/)
  })

  it('submits preset runs through the backend CreateRunRequest contract', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => runPayload })
    vi.stubGlobal('fetch', fetchMock)

    await workflowApi.createRun({ presetId: 'green-finance-did', mode: 'fixture' })

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/runs', expect.objectContaining({ method: 'POST' }))
    const body = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(body).toEqual({
      definition_id: 'app-a',
      preset_case_id: 'green-finance-did',
      mode: 'fixture',
      model_provider: 'fixture',
      execution_mode: 'fixture',
    })
  })

  it('does not silently downgrade research mode to fixture mode', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => runPayload })
    vi.stubGlobal('fetch', fetchMock)

    await workflowApi.createRun({ presetId: 'green-finance-did', mode: 'research' })

    const body = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(body).toMatchObject({
      mode: 'research',
      model_provider: 'qwen',
      execution_mode: 'external',
    })
  })

  it('submits a detailed custom CaseSubmission instead of forcing a preset', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => runPayload })
    vi.stubGlobal('fetch', fetchMock)

    await workflowApi.createRun({
      mode: 'fixture',
      case: {
        caseId: 'case-custom-001',
        title: '自定义案例',
        researchQuestion: '政策是否影响绿色创新？',
        hypotheses: [{ hypothesisId: 'H1', statement: '政策促进绿色创新。', expectedDirection: 'positive', mechanism: '融资约束' }],
        unitOfAnalysis: '企业—年度',
        samplePeriod: '2015—2024',
        dataStructureHint: 'panel',
        variables: [{ name: 'green_patent', label: '绿色专利', role: 'outcome', definition: '专利数量', source: 'CNRDS' }],
        datasetRefs: [],
        knownPolicyFacts: ['政策于 2017 年实施。'],
        constraints: ['隐藏结果不可见。'],
      },
    })

    const body = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(body.preset_case_id).toBeUndefined()
    expect(body.case).toMatchObject({
      case_id: 'case-custom-001',
      research_question: '政策是否影响绿色创新？',
      data_structure_hint: 'panel',
      hypotheses: [{ hypothesis_id: 'H1', expected_direction: 'positive' }],
      variables: [{ name: 'green_patent', role: 'outcome' }],
    })
  })

  it('reads and updates runtime configuration without exposing secret values', async () => {
    const status = {
      config_path: 'backend/var/runtime-config.json',
      environment_precedence: true,
      workflow_api_token_required: true,
      qwen_api_key: { configured: true, source: 'file' },
      qwen_model: { value: 'qwen-plus', source: 'file' },
      qwen_base_url: { value: 'https://dashscope.example/v1', source: 'file' },
      research_engine_url: { value: null, source: 'missing' },
      research_engine_token: { configured: false, source: 'missing' },
    }
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => status })
    vi.stubGlobal('fetch', fetchMock)

    const result = await workflowApi.updateRuntimeConfig({ qwenApiKey: 'secret-value', qwenModel: 'qwen-plus' })

    expect(result.qwenApiKey).toEqual({ configured: true, source: 'file' })
    expect(result.workflowApiTokenRequired).toBe(true)
    expect(JSON.stringify(result)).not.toContain('secret-value')
    const body = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(body).toMatchObject({ qwen_api_key: 'secret-value', qwen_model: 'qwen-plus' })
  })

  it('sends gate decisions with optimistic versioning and fixture claim restrictions', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => runPayload })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('crypto', { randomUUID: () => 'decision-001' })
    const run = normalizeRun(runPayload)

    await workflowApi.decideGate(run, 'H3', {
      action: 'generate_plan_only',
      claims: [{ claimId: 'claim-1', decision: 'hold' }],
    })

    const body = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(body).toMatchObject({
      action: 'generate_plan_only',
      expected_run_version: 7,
      idempotency_key: 'decision-001',
      claims: [{ claim_id: 'claim-1', decision: 'hold' }],
    })
  })

  it('keeps the workflow access token in session storage and sends it on requests', async () => {
    const values = new Map<string, string>()
    vi.stubGlobal('window', {
      sessionStorage: {
        getItem: (key: string) => values.get(key) ?? null,
        setItem: (key: string, value: string) => values.set(key, value),
        removeItem: (key: string) => values.delete(key),
      },
    })
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => definitionPayload })
    vi.stubGlobal('fetch', fetchMock)

    workflowApi.setAccessToken('browser-session-secret')
    await workflowApi.getDefinition()

    expect(fetchMock.mock.calls[0][1].headers).toMatchObject({ 'X-Hypoweaver-Token': 'browser-session-secret' })
  })

  it('submits H1 revisions against the version returned by the revise decision', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => runPayload })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('crypto', { randomUUID: () => 'revision-001' })
    const run = normalizeRun(runPayload)

    await workflowApi.submitRevision(run, 'H1', { case_id: 'revised-case' })

    const body = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(body).toMatchObject({
      gate: 'H1',
      expected_run_version: 7,
      idempotency_key: 'revision-001',
      case: { case_id: 'revised-case' },
    })
    expect(body.analysis_plan).toBeUndefined()
  })
})
