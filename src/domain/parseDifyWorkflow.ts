import { parse } from 'yaml'
import type {
  ContractField,
  NodeKind,
  PromptPart,
  WorkflowDefinition,
  WorkflowEdge,
  WorkflowNode,
} from './types'
import type { WorkflowSourceConfig } from '../data/workflowConfig'

type UnknownRecord = Record<string, unknown>

const reservedInputNamespaces = new Set(['env', 'sys', 'conversation'])

const edgeLabels: Record<string, string> = {
  case_policy: '政策因果',
  case_panel: '面板 / 机制',
  case_event: '事件研究',
  case_spatial: '空间计量',
  case_measure: '指标 / 效率',
  case_fixture: 'Fixture',
}

function edgeLabel(source: string, sourceHandle?: string): string | undefined {
  if (!sourceHandle) return undefined
  if (source === '18' && sourceHandle === 'false') return '结构模型 / 兜底'
  if (source === '25' && sourceHandle === 'false') return 'External API'
  return edgeLabels[sourceHandle]
}

function asRecord(value: unknown): UnknownRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as UnknownRecord) : {}
}

function toKind(type: string): NodeKind {
  const kinds: Record<string, NodeKind> = {
    start: 'start',
    'document-extractor': 'document',
    llm: 'llm',
    code: 'code',
    'human-input': 'gate',
    'if-else': 'router',
    'variable-aggregator': 'merge',
    'http-request': 'http',
    end: 'end',
  }
  return kinds[type] ?? 'other'
}

function promptReferences(prompts: PromptPart[]): Array<[string, string]> {
  const references: Array<[string, string]> = []
  const pattern = /\{\{#([^.}]+)\.([^#}]+)#\}\}/g

  for (const prompt of prompts) {
    for (const match of prompt.text.matchAll(pattern)) {
      if (match[1] !== '$output') references.push([match[1], match[2]])
    }
  }

  return references
}

function collectSelectors(value: unknown, selectors: Array<[string, string]>): void {
  if (Array.isArray(value)) {
    for (const item of value) collectSelectors(item, selectors)
    return
  }

  if (!value || typeof value !== 'object') return
  const record = value as UnknownRecord

  for (const key of ['variable_selector', 'value_selector']) {
    const selector = record[key]
    if (Array.isArray(selector) && selector.length >= 2) {
      selectors.push([String(selector[0]), String(selector[1])])
    }
  }

  for (const child of Object.values(record)) collectSelectors(child, selectors)
}

function dedupeFields(fields: ContractField[]): ContractField[] {
  const seen = new Set<string>()
  return fields.filter((field) => {
    const key = `${field.sourceNodeId ?? 'local'}:${field.name}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function extractJsonSchema(text: string): unknown | null {
  const candidates: Array<{ json: string; start: number }> = []
  let start = -1
  let depth = 0
  let inString = false
  let escaped = false

  for (let index = 0; index < text.length; index += 1) {
    const char = text[index]
    if (inString) {
      if (escaped) escaped = false
      else if (char === '\\') escaped = true
      else if (char === '"') inString = false
      continue
    }
    if (char === '"') {
      inString = true
      continue
    }
    if (char === '{') {
      if (depth === 0) start = index
      depth += 1
    } else if (char === '}' && depth > 0) {
      depth -= 1
      if (depth === 0 && start >= 0) candidates.push({ json: text.slice(start, index + 1), start })
    }
  }

  return candidates
    .filter((candidate) => {
      let previous = candidate.start - 1
      while (previous >= 0 && /\s/.test(text[previous])) previous -= 1
      return text[previous] !== '['
    })
    .map((candidate) => {
      try {
        return JSON.parse(candidate.json) as unknown
      } catch {
        return null
      }
    })
    .filter((candidate): candidate is unknown => candidate !== null)
    .sort((left, right) => JSON.stringify(right).length - JSON.stringify(left).length)[0] ?? null
}

function parsePrompts(data: UnknownRecord): PromptPart[] {
  const rawPrompts = Array.isArray(data.prompt_template) ? data.prompt_template : []
  const prompts = rawPrompts.map((item, index) => {
    const prompt = asRecord(item)
    return {
      id: String(prompt.id ?? `prompt-${index}`),
      role: String(prompt.role ?? 'prompt'),
      text: String(prompt.text ?? ''),
    }
  })

  if (typeof data.code === 'string') {
    prompts.push({ id: 'code', role: String(data.code_language ?? 'python'), text: data.code })
  }
  if (typeof data.form_content === 'string') {
    prompts.push({ id: 'human-form', role: 'human form', text: data.form_content })
  }
  if (String(data.type) === 'http-request') {
    const httpDetails = {
      method: data.method ?? 'POST',
      url: data.url ?? '',
      headers: data.headers ?? '',
      body_type: data.body_type ?? '',
      body: data.body ?? '',
    }
    prompts.push({ id: 'http-request', role: 'http', text: JSON.stringify(httpDetails, null, 2) })
  }

  return prompts
}

function parseStartFields(data: UnknownRecord): ContractField[] {
  if (!Array.isArray(data.variables)) return []
  return data.variables.map((value) => {
    const variable = asRecord(value)
    return {
      name: String(variable.variable ?? 'unknown'),
      label: String(variable.label ?? variable.variable ?? '未命名字段'),
      type: String(variable.type ?? 'unknown'),
      required: Boolean(variable.required),
    }
  })
}

function parseHumanFields(data: UnknownRecord): ContractField[] {
  if (!Array.isArray(data.inputs)) return []
  return data.inputs.map((value, index) => {
    const input = asRecord(value)
    const name = String(input.output_variable_name ?? `input_${index + 1}`)
    return {
      name,
      label: name === 'decision' ? '人工决定' : name === 'comment' ? '说明 / 补充意见' : name,
      type: String(input.type ?? 'human input'),
    }
  })
}

function parseVariableBindings(
  data: UnknownRecord,
  titleById: Map<string, string>,
): { fields: ContractField[]; selectorKeys: Set<string> } {
  const fields: ContractField[] = []
  const selectorKeys = new Set<string>()
  if (!Array.isArray(data.variables)) return { fields, selectorKeys }

  for (const value of data.variables) {
    const variable = asRecord(value)
    const selector = variable.value_selector
    if (!variable.variable || !Array.isArray(selector) || selector.length < 2) continue
    const sourceNodeId = String(selector[0])
    const sourceVariable = String(selector[1])
    const localName = String(variable.variable)
    selectorKeys.add(`${sourceNodeId}:${sourceVariable}`)
    fields.push({
      name: localName,
      label: `${localName} ← ${titleById.get(sourceNodeId) ?? sourceNodeId}.${sourceVariable}`,
      type: String(variable.value_type ?? 'upstream reference'),
      sourceNodeId,
      sourceNodeTitle: titleById.get(sourceNodeId) ?? sourceNodeId,
    })
  }

  return { fields, selectorKeys }
}

function parseInputs(
  data: UnknownRecord,
  prompts: PromptPart[],
  titleById: Map<string, string>,
): ContractField[] {
  if (String(data.type) === 'start') return parseStartFields(data)

  const localFields = String(data.type) === 'human-input' ? parseHumanFields(data) : []
  const selectors: Array<[string, string]> = []
  const bindings = parseVariableBindings(data, titleById)
  collectSelectors(data, selectors)
  selectors.push(...promptReferences(prompts))

  if (String(data.type) === 'variable-aggregator' && Array.isArray(data.variables)) {
    for (const selector of data.variables) {
      if (Array.isArray(selector) && selector.length >= 2) {
        selectors.push([String(selector[0]), String(selector[1])])
      }
    }
  }

  const referencedFields = selectors
    .filter(([nodeId, variable]) => !bindings.selectorKeys.has(`${nodeId}:${variable}`))
    .map(([nodeId, variable]) => ({
      name: variable,
      label: `${titleById.get(nodeId) ?? nodeId}.${variable}`,
      type: 'upstream reference',
      sourceNodeId: nodeId,
      sourceNodeTitle: titleById.get(nodeId) ?? nodeId,
    }))

  return dedupeFields([...localFields, ...bindings.fields, ...referencedFields])
}

function parseExplicitOutputs(outputs: unknown): ContractField[] {
  if (Array.isArray(outputs)) {
    return outputs.map((value, index) => {
      const output = asRecord(value)
      return {
        name: String(output.variable ?? `output_${index + 1}`),
        label: String(output.variable ?? `输出 ${index + 1}`),
        type: String(output.type ?? 'output'),
      }
    })
  }

  const record = asRecord(outputs)
  return Object.entries(record).map(([name, value]) => {
    const output = asRecord(value)
    return { name, label: name, type: String(output.type ?? 'output') }
  })
}

function parseOutputs(data: UnknownRecord, outputSchema: unknown | null): ContractField[] {
  const explicit = parseExplicitOutputs(data.outputs)
  if (explicit.length) return explicit

  const type = String(data.type ?? '')
  if (type === 'start') return parseStartFields(data)
  if (type === 'document-extractor') return [{ name: 'text', label: '提取文本', type: 'string' }]
  if (type === 'human-input') return parseHumanFields(data)
  if (type === 'if-else') return [{ name: 'branch', label: '命中的互斥分支', type: 'route' }]
  if (type === 'variable-aggregator') return [{ name: 'output', label: '聚合结果', type: 'string' }]
  if (type === 'http-request') {
    return [
      { name: 'body', label: '响应 Body', type: 'string' },
      { name: 'status_code', label: '状态码', type: 'number' },
      { name: 'headers', label: '响应头', type: 'object' },
    ]
  }
  if (type === 'llm') {
    if (outputSchema && typeof outputSchema === 'object') {
      return Object.keys(outputSchema as UnknownRecord).map((name) => ({
        name,
        label: name,
        type: 'JSON field',
      }))
    }
    return [{ name: 'text', label: '模型文本输出', type: 'string' }]
  }
  return []
}

function isParallelEdge(source: string, target: string, workflowId: string): boolean {
  if (workflowId === 'app-a') return source === '10' && ['111', '112', '113'].includes(target)
  return source === '50' && ['511', '512'].includes(target)
}

export function parseDifyWorkflow(
  yamlText: string,
  config: WorkflowSourceConfig,
): WorkflowDefinition {
  const document = asRecord(parse(yamlText))
  const app = asRecord(document.app)
  const workflow = asRecord(document.workflow)
  const graph = asRecord(workflow.graph)
  const rawNodes = Array.isArray(graph.nodes) ? graph.nodes.map(asRecord) : []
  const rawEdges = Array.isArray(graph.edges) ? graph.edges.map(asRecord) : []

  const appName = String(app.name ?? '')
  const expectedAppName = config.id === 'app-a' ? 'HypoWeaver-Research' : 'HypoWeaver-Blind-Evaluator'
  if (!appName.includes(expectedAppName)) {
    throw new Error(`${config.label} 文件身份不匹配：读取到“${appName || '未命名应用'}”`)
  }
  if (!rawNodes.length) throw new Error(`${config.label} 不包含任何工作流节点`)

  const nodeIds = rawNodes.map((node) => String(node.id ?? ''))
  if (nodeIds.some((id) => !id)) throw new Error(`${config.label} 存在缺少 id 的节点`)
  if (new Set(nodeIds).size !== nodeIds.length) throw new Error(`${config.label} 存在重复节点 id`)

  const configuredStageIds = new Set(config.stages.map((stage) => stage.id))
  for (const nodeId of nodeIds) {
    const stageId = config.stageByNode[nodeId]
    if (!stageId) throw new Error(`${config.label} 的节点 ${nodeId} 未映射到阶段`)
    if (!configuredStageIds.has(stageId)) throw new Error(`${config.label} 的节点 ${nodeId} 映射到未知阶段 ${stageId}`)
  }

  const nodeIdSet = new Set(nodeIds)
  const edgeIds = new Set<string>()
  for (const [index, edge] of rawEdges.entries()) {
    const source = String(edge.source ?? '')
    const target = String(edge.target ?? '')
    const edgeId = String(edge.id ?? `${source}-${target}-${index}`)
    if (edgeIds.has(edgeId)) throw new Error(`${config.label} 存在重复连线 id ${edgeId}`)
    edgeIds.add(edgeId)
    if (!nodeIdSet.has(source) || !nodeIdSet.has(target)) {
      throw new Error(`${config.label} 的连线 ${edgeId} 指向不存在的节点`)
    }
  }

  const titleById = new Map(
    rawNodes.map((node) => {
      const data = asRecord(node.data)
      return [String(node.id), String(data.title ?? node.id)] as const
    }),
  )

  const nodes: WorkflowNode[] = rawNodes.map((node) => {
    const id = String(node.id)
    const data = asRecord(node.data)
    const type = String(data.type ?? 'unknown')
    const position = asRecord(node.positionAbsolute ?? node.position)
    const prompts = parsePrompts(data)
    const systemText = prompts
      .filter((prompt) => prompt.role === 'system')
      .map((prompt) => prompt.text)
      .join('\n')
    const outputSchema = extractJsonSchema(systemText)

    return {
      id,
      title: String(data.title ?? id),
      type,
      kind: toKind(type),
      stageId: config.stageByNode[id],
      description: String(data.desc ?? ''),
      position: {
        x: Number(position.x ?? 0) * 0.9,
        y: Number(position.y ?? 0) * 0.9,
      },
      prompts,
      inputs: parseInputs(data, prompts, titleById),
      outputs: parseOutputs(data, outputSchema),
      outputSchema,
      issueIds: config.issues.filter((issue) => issue.nodeIds.includes(id)).map((issue) => issue.id),
      rawData: data,
    }
  })

  const edges: WorkflowEdge[] = rawEdges.map((edge, index) => {
    const source = String(edge.source ?? '')
    const target = String(edge.target ?? '')
    const sourceHandle = edge.sourceHandle ? String(edge.sourceHandle) : undefined
    return {
      id: String(edge.id ?? `${source}-${target}-${index}`),
      source,
      target,
      sourceHandle,
      targetHandle: edge.targetHandle ? String(edge.targetHandle) : undefined,
      label: edgeLabel(source, sourceHandle),
      animated: isParallelEdge(source, target, config.id),
    }
  })

  const stages = config.stages.map((stage) => ({
    ...stage,
    nodeIds: nodes.filter((node) => node.stageId === stage.id).map((node) => node.id),
  }))

  for (const node of nodes) {
    for (const input of node.inputs) {
      if (
        input.sourceNodeId &&
        !nodeIdSet.has(input.sourceNodeId) &&
        !reservedInputNamespaces.has(input.sourceNodeId)
      ) {
        throw new Error(`${config.label} 的节点 ${node.id} 引用了不存在的上游节点 ${input.sourceNodeId}`)
      }
      if (config.forbiddenInputNames?.includes(input.name)) {
        throw new Error(`${config.label} 的节点 ${node.id} 使用了禁止输入 ${input.name}`)
      }
    }
  }

  return {
    id: config.id,
    name: appName,
    description: String(app.description ?? ''),
    sourceFile: config.file.split('/').at(-1) ?? config.file,
    nodes,
    edges,
    stages,
    issues: config.issues,
    stats: {
      nodes: nodes.length,
      edges: edges.length,
      llm: nodes.filter((node) => node.kind === 'llm').length,
      gates: nodes.filter((node) => node.kind === 'gate').length,
      routers: nodes.filter((node) => node.kind === 'router').length,
      merges: nodes.filter((node) => node.kind === 'merge').length,
    },
  }
}

export async function loadWorkflow(config: WorkflowSourceConfig): Promise<WorkflowDefinition> {
  const response = await fetch(config.file)
  if (!response.ok) throw new Error(`无法读取 ${config.file}: HTTP ${response.status}`)
  return parseDifyWorkflow(await response.text(), config)
}
