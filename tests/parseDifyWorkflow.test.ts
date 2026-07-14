import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { parse as parseYaml, stringify as stringifyYaml } from 'yaml'
import { WORKFLOW_SOURCES } from '../src/data/workflowConfig'
import { parseDifyWorkflow } from '../src/domain/parseDifyWorkflow'

interface DifyFixture {
  workflow: {
    graph: {
      nodes: Array<{ id: string; data: Record<string, unknown> }>
    }
  }
}

function parseFixture(index: number) {
  const config = WORKFLOW_SOURCES[index]
  const path = resolve(process.cwd(), 'public', config.file.replace('/workflows/', 'workflows/'))
  return parseDifyWorkflow(readFileSync(path, 'utf8'), config)
}

function readFixture(index: number) {
  const config = WORKFLOW_SOURCES[index]
  const path = resolve(process.cwd(), 'public', config.file.replace('/workflows/', 'workflows/'))
  return readFileSync(path, 'utf8')
}

describe('Dify workflow parser', () => {
  it('preserves App A nodes, edges and node kinds', () => {
    const workflow = parseFixture(0)

    expect(workflow.stats).toMatchObject({
      nodes: 35,
      edges: 42,
      llm: 17,
      gates: 3,
      routers: 2,
      merges: 2,
    })
    expect(workflow.edges.filter((edge) => edge.animated)).toHaveLength(3)
    expect(workflow.edges.filter((edge) => edge.source === '18')).toHaveLength(6)
    expect(workflow.edges.find((edge) => edge.source === '18' && edge.sourceHandle === 'false')?.label).toBe('结构模型 / 兜底')
    expect(workflow.edges.find((edge) => edge.source === '25' && edge.sourceHandle === 'false')?.label).toBe('External API')
  })

  it('extracts prompt references and target JSON schema from Method Router', () => {
    const workflow = parseFixture(0)
    const router = workflow.nodes.find((node) => node.id === '16')

    expect(router?.inputs.map((input) => `${input.sourceNodeId}.${input.name}`)).toEqual([
      '14.text',
      '15.text',
      '11.text',
    ])
    expect(router?.outputSchema).toMatchObject({
      route_status: 'routed|blocked|needs_human_review',
      primary_route: 'string|null',
    })
    expect(router?.issueIds).toContain('raw-json-only')
  })

  it('preserves code aliases and does not invent a complete schema from an inner object', () => {
    const workflow = parseFixture(0)
    const validator = workflow.nodes.find((node) => node.id === '12')
    const reviser = workflow.nodes.find((node) => node.id === '23')

    expect(validator?.inputs).toEqual([
      expect.objectContaining({
        name: 'research_package_text',
        sourceNodeId: '11',
        type: 'string',
      }),
    ])
    expect(reviser?.outputSchema).toBeNull()
    expect(reviser?.outputs).toEqual([
      expect.objectContaining({ name: 'text', type: 'string' }),
    ])
  })

  it('treats human form controls as local inputs and filters the $output namespace', () => {
    const workflow = parseFixture(0)
    const gate = workflow.nodes.find((node) => node.id === '13')

    expect(gate?.inputs.map((input) => input.name)).toEqual(['decision', 'comment'])
    expect(gate?.inputs.every((input) => input.sourceNodeId === undefined)).toBe(true)
    expect(gate?.outputs.map((output) => output.name)).toEqual(['decision', 'comment'])
    expect(gate?.issueIds).toContain('gate-review-context-missing')
  })

  it('keeps upstream review materials when a human form is connected to them', () => {
    const document = parseYaml(readFixture(0)) as DifyFixture
    const gate = document.workflow.graph.nodes.find((node) => node.id === '13')
    if (!gate) throw new Error('missing H1 fixture')
    gate.data.form_content = `${String(gate.data.form_content)}\n审核材料：{{#11.text#}}`

    const workflow = parseDifyWorkflow(stringifyYaml(document), WORKFLOW_SOURCES[0])
    expect(workflow.nodes.find((node) => node.id === '13')?.inputs).toEqual([
      expect.objectContaining({ name: 'decision' }),
      expect.objectContaining({ name: 'comment' }),
      expect.objectContaining({ name: 'text', sourceNodeId: '11' }),
    ])
  })

  it('keeps App B isolated and exposes its two parallel extractors', () => {
    const workflow = parseFixture(1)

    expect(workflow.stats).toMatchObject({ nodes: 5, edges: 5, llm: 1, gates: 0 })
    expect(workflow.edges.filter((edge) => edge.animated)).toHaveLength(2)
    expect(workflow.nodes.find((node) => node.id === '51')?.outputSchema).toMatchObject({
      case_id: 'string',
      overall_score: 0,
    })
    const nodeIds = new Set(workflow.nodes.map((node) => node.id))
    const reserved = new Set(['env', 'sys', 'conversation'])
    expect(workflow.nodes.flatMap((node) => node.inputs).every((input) =>
      !input.sourceNodeId || nodeIds.has(input.sourceNodeId) || reserved.has(input.sourceNodeId),
    )).toBe(true)
  })

  it('rejects a workflow paired with the wrong app config', () => {
    expect(() => parseDifyWorkflow(readFixture(1), WORKFLOW_SOURCES[0])).toThrow('文件身份不匹配')
  })

  it('rejects an empty graph instead of presenting it as a valid workflow', () => {
    const emptyWorkflow = `app:\n  name: HypoWeaver-Research\nworkflow:\n  graph:\n    nodes: []\n    edges: []\n`
    expect(() => parseDifyWorkflow(emptyWorkflow, WORKFLOW_SOURCES[0])).toThrow('不包含任何工作流节点')
  })

  it('rejects hidden reference inputs in the main research workflow', () => {
    const document = parseYaml(readFixture(0)) as DifyFixture
    const start = document.workflow.graph.nodes.find((node) => node.id === '10')
    if (!start) throw new Error('missing App A start fixture')
    const variables = start.data.variables as Array<Record<string, unknown>>
    variables.push({
      label: '禁止的隐藏论文',
      required: false,
      type: 'file',
      variable: 'reference_paper_file',
    })

    expect(() => parseDifyWorkflow(stringifyYaml(document), WORKFLOW_SOURCES[0])).toThrow('使用了禁止输入 reference_paper_file')
  })
})
