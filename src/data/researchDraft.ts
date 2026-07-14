import type { CaseSubmissionInput, RuntimeConfigStatus } from '../runtime/types'

export interface ResearchDraft {
  mode: 'fixture' | 'research'
  case: CaseSubmissionInput
}

export interface PreflightItem {
  id: string
  level: 'pass' | 'warning' | 'blocker'
  title: string
  detail: string
}

function caseId(): string {
  return `case-${new Date().toISOString().slice(0, 10).replaceAll('-', '')}-${Date.now().toString().slice(-5)}`
}

export function emptyResearchDraft(): ResearchDraft {
  return {
    mode: 'fixture',
    case: {
      caseId: caseId(),
      title: '',
      researchQuestion: '',
      hypotheses: [{ hypothesisId: 'H1', statement: '', expectedDirection: 'unspecified', mechanism: '' }],
      unitOfAnalysis: '',
      samplePeriod: '',
      dataStructureHint: 'panel',
      variables: [
        { name: '', label: '', role: 'outcome', definition: '', source: '' },
        { name: '', label: '', role: 'treatment', definition: '', source: '' },
        { name: '', label: '', role: 'id', definition: '', source: '' },
        { name: '', label: '', role: 'time', definition: '', source: '' },
      ],
      datasetRefs: [],
      knownPolicyFacts: [''],
      constraints: ['原论文结论、回归结果和隐藏参考材料不得进入主工作流。'],
    },
  }
}

export function demoResearchDraft(): ResearchDraft {
  return {
    mode: 'fixture',
    case: {
      caseId: 'green-finance-demo-001',
      title: '绿色金融试验区政策评估',
      researchQuestion: '绿色金融改革创新试验区政策是否促进企业绿色创新？',
      hypotheses: [{
        hypothesisId: 'H1',
        statement: '绿色金融改革创新试验区政策促进企业绿色创新。',
        expectedDirection: 'positive',
        mechanism: '政策通过缓解绿色项目融资约束并强化创新激励发挥作用。',
      }],
      unitOfAnalysis: '企业—年度',
      samplePeriod: '政策前后若干年度（由数据组最终确认）',
      dataStructureHint: 'panel',
      variables: [
        { name: 'firm_id', label: '企业代码', role: 'id', definition: 'A 股上市公司唯一代码', source: 'CSMAR' },
        { name: 'year', label: '年份', role: 'time', definition: '会计年度', source: 'CSMAR' },
        { name: 'green_patent', label: '绿色专利', role: 'outcome', definition: '绿色发明专利申请数量', source: 'CNRDS/国家知识产权局' },
        { name: 'treat_post', label: '政策处理变量', role: 'treatment', definition: '试点企业或地区 × 政策实施后', source: '政策名单' },
        { name: 'firm_size', label: '企业规模', role: 'control', definition: '总资产自然对数', source: 'CSMAR' },
        { name: 'leverage', label: '资产负债率', role: 'control', definition: '总负债/总资产', source: 'CSMAR' },
      ],
      datasetRefs: [],
      knownPolicyFacts: ['政策存在明确实施时间和试点地区；具体名单由案例包提供。'],
      constraints: ['原论文结论、回归结果和隐藏参考材料不得进入主工作流。'],
    },
  }
}

export function preflightResearch(
  draft: ResearchDraft,
  config: RuntimeConfigStatus | null,
  accessTokenConfigured = false,
): PreflightItem[] {
  const { case: researchCase } = draft
  const items: PreflightItem[] = []
  const duplicateNames = researchCase.variables
    .map((variable) => variable.name.trim())
    .filter((name, index, names) => name && names.indexOf(name) !== index)

  items.push({
    id: 'research-input',
    level: researchCase.caseId.trim() && researchCase.title.trim() && researchCase.researchQuestion.trim()
      && researchCase.hypotheses.length > 0
      && researchCase.hypotheses.every((item) => item.hypothesisId.trim() && item.statement.trim())
      ? 'pass' : 'blocker',
    title: '研究问题与假设',
    detail: '需要案例编号、案例名称、研究问题，并为每条假设填写编号和陈述。',
  })
  items.push({
    id: 'variables',
    level: researchCase.variables.some((variable) => variable.role === 'outcome' && variable.name.trim()) && !duplicateNames.length
      ? 'pass' : 'blocker',
    title: '变量角色与命名',
    detail: duplicateNames.length ? `发现重复变量名：${[...new Set(duplicateNames)].join('、')}` : '至少需要一个已命名的结果变量，变量名不能重复。',
  })
  const panelKeysReady = researchCase.dataStructureHint !== 'panel'
    || (researchCase.variables.some((item) => item.role === 'id' && item.name.trim())
      && researchCase.variables.some((item) => item.role === 'time' && item.name.trim()))
  items.push({
    id: 'data-shape',
    level: panelKeysReady ? 'pass' : 'warning',
    title: '数据结构',
    detail: panelKeysReady ? `已声明为 ${researchCase.dataStructureHint}。` : '面板数据建议同时提供个体主键与时间变量。',
  })

  if (draft.mode === 'fixture') {
    items.push({
      id: 'runtime',
      level: 'pass',
      title: '运行方式',
      detail: '流程演示不调用千问与统计执行器；不会产生系数、显著性或实证结论。',
    })
  } else {
    items.push({
      id: 'qwen',
      level: config?.qwenApiKey.configured ? 'pass' : 'blocker',
      title: '千问模型',
      detail: config?.qwenApiKey.configured ? `将使用 ${config.qwenModel.value ?? '已配置模型'}。` : '尚未配置 DASHSCOPE_API_KEY。',
    })
    items.push({
      id: 'executor',
      level: config?.researchEngineUrl.value ? 'pass' : 'blocker',
      title: 'Python 研究执行器',
      detail: config?.researchEngineUrl.value ?? '尚未配置执行器地址。',
    })
    const datasetsReady = researchCase.datasetRefs.length > 0 && researchCase.datasetRefs.every((dataset) => (
      Boolean(dataset.datasetId.trim())
      && Boolean(dataset.filename.trim())
      && /^[a-f0-9]{64}$/i.test(dataset.sha256.trim())
      && dataset.sizeBytes > 0
    ))
    items.push({
      id: 'dataset',
      level: datasetsReady ? 'pass' : 'blocker',
      title: '可执行数据资产',
      detail: datasetsReady
        ? `已登记 ${researchCase.datasetRefs.length} 个数据资产引用。`
        : '尚未登记可执行数据。请返回使用“一键导入标准案例包”，或手动填写执行器已登记资产的 dataset_id、文件名、64 位 SHA256 和文件字节数。',
    })
  }
  if (config?.workflowApiTokenRequired) {
    items.push({
      id: 'workflow-access',
      level: accessTokenConfigured ? 'pass' : 'blocker',
      title: '工作流访问令牌',
      detail: accessTokenConfigured
        ? '当前标签页已持有工作流访问令牌。'
        : '后端要求 HYPOWEAVER_API_TOKEN；请先在“系统设置”中为当前标签页填写访问令牌。',
    })
  }
  return items
}
