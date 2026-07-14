import { describe, expect, it } from 'vitest'
import { demoResearchDraft, preflightResearch } from '../src/data/researchDraft'
import type { RuntimeConfigStatus } from '../src/runtime/types'

const configured: RuntimeConfigStatus = {
  configPath: 'backend/var/runtime-config.json',
  environmentPrecedence: true,
  workflowApiTokenRequired: false,
  qwenApiKey: { configured: true, source: 'file' },
  qwenModel: { value: 'qwen-plus', source: 'default' },
  qwenBaseUrl: { value: 'https://dashscope.aliyuncs.com/compatible-mode/v1', source: 'default' },
  researchEngineUrl: { value: 'http://127.0.0.1:9000', source: 'file' },
  researchEngineToken: { configured: false, source: 'missing' },
}

describe('research preflight', () => {
  it('allows a complete fixture case while preserving the plan-only boundary', () => {
    const items = preflightResearch(demoResearchDraft(), null)
    expect(items.some((item) => item.level === 'blocker')).toBe(false)
    expect(items.find((item) => item.id === 'runtime')?.detail).toMatch(/不会产生系数/)
  })

  it('blocks real research until a registered dataset is provided', () => {
    const draft = { ...demoResearchDraft(), mode: 'research' as const }
    const items = preflightResearch(draft, configured)
    expect(items.find((item) => item.id === 'dataset')).toMatchObject({ level: 'blocker' })
  })

  it('does not treat an empty dataset row as an uploaded asset', () => {
    const draft = { ...demoResearchDraft(), mode: 'research' as const }
    draft.case.datasetRefs = [{ datasetId: '', role: 'main', filename: '', mimeType: 'text/csv', sha256: '', sizeBytes: 0 }]
    expect(preflightResearch(draft, configured).find((item) => item.id === 'dataset'))
      .toMatchObject({ level: 'blocker' })
  })

  it('detects missing outcome variables and duplicate names', () => {
    const draft = demoResearchDraft()
    draft.case.variables = [
      { name: 'same', label: '', role: 'control', definition: '', source: '' },
      { name: 'same', label: '', role: 'treatment', definition: '', source: '' },
    ]
    const item = preflightResearch(draft, null).find((candidate) => candidate.id === 'variables')
    expect(item).toMatchObject({ level: 'blocker' })
    expect(item?.detail).toMatch(/重复变量名/)
  })

  it('blocks mutations when the backend requires a workflow access token', () => {
    const protectedConfig = { ...configured, workflowApiTokenRequired: true }
    expect(preflightResearch(demoResearchDraft(), protectedConfig, false).find((item) => item.id === 'workflow-access'))
      .toMatchObject({ level: 'blocker' })
    expect(preflightResearch(demoResearchDraft(), protectedConfig, true).find((item) => item.id === 'workflow-access'))
      .toMatchObject({ level: 'pass' })
  })
})
