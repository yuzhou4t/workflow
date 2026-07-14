import type { WorkflowIssue, WorkflowStage } from '../domain/types'

export interface WorkflowSourceConfig {
  id: 'app-a' | 'app-b'
  label: string
  shortLabel: string
  file: string
  stages: Omit<WorkflowStage, 'nodeIds'>[]
  stageByNode: Record<string, string>
  issues: WorkflowIssue[]
  forbiddenInputNames?: string[]
}

const appAStages: Omit<WorkflowStage, 'nodeIds'>[] = [
  {
    id: 'intake',
    order: 1,
    title: '案例入口',
    description: '并行读取三份说明材料，汇合为 ResearchPackage，并完成人类边界确认。',
  },
  {
    id: 'understanding',
    order: 2,
    title: '研究理解',
    description: '依次拆解假设、形成数据画像，再选择可辩护的方法家族。',
  },
  {
    id: 'method-design',
    order: 3,
    title: '方法设计',
    description: '六类方法互斥路由后汇合，经 Critic 修订并冻结分析计划。',
  },
  {
    id: 'execution',
    order: 4,
    title: '模型执行',
    description: 'Fixture 与外部 Python 执行器二选一，统一为 ResearchRun。',
  },
  {
    id: 'result-audit',
    order: 5,
    title: '结果审计',
    description: '解释真实运行结果，检查科学有效性，再构造并授权 ClaimLedger。',
  },
  {
    id: 'writing',
    order: 6,
    title: '结论与写作',
    description: '仅使用获批结论写作，随后进行引用与一致性审计。',
  },
]

const appAStageByNode: Record<string, string> = {}

for (const id of ['10', '111', '112', '113', '11', '12', '13']) appAStageByNode[id] = 'intake'
for (const id of ['14', '15', '16', '17']) appAStageByNode[id] = 'understanding'
for (const id of ['18', '201', '202', '203', '204', '205', '206', '21', '22', '23', '24h', '24']) {
  appAStageByNode[id] = 'method-design'
}
for (const id of ['25', '261', '262', '27']) appAStageByNode[id] = 'execution'
for (const id of ['28', '29', '30', '31h', '31']) appAStageByNode[id] = 'result-audit'
for (const id of ['32', '33', '99']) appAStageByNode[id] = 'writing'

const appAIssues: WorkflowIssue[] = [
  {
    id: 'missing-main-data',
    title: '缺少主数据入口',
    detail: 'A00 只有案例说明、变量字典和数据说明文件，没有 CSV/XLSX/DTA 或 data_asset_id。',
    severity: 'blocker',
    nodeIds: ['10'],
  },
  {
    id: 'empty-http-executor',
    title: 'Python 执行器尚未接通',
    detail: 'A14B 仍使用占位 URL，且没有认证、请求 Body 或上游变量映射。',
    severity: 'blocker',
    nodeIds: ['262'],
  },
  {
    id: 'gates-do-not-block',
    title: '三个人类闸门不会阻断流程',
    detail: 'revise、hold、stop 或 reject 仍会进入下游节点；正式代码必须显式建模状态转移。',
    severity: 'blocker',
    nodeIds: ['13', '24h', '31h'],
  },
  {
    id: 'gate-review-context-missing',
    title: '人类闸门缺少审核材料',
    detail: 'H1、H2、H3 表单只显示决定与说明，没有把 ResearchPackage、AnalysisPlan/CriticReport、ClaimLedger/ResearchRun 呈现给审批者。',
    severity: 'blocker',
    nodeIds: ['13', '24h', '31h'],
  },
  {
    id: 'blocked-falls-through',
    title: 'blocked 状态仍会继续',
    detail: '输入校验、方法路由与 Critic 的 blocked 结果没有终止或退回路径。',
    severity: 'blocker',
    nodeIds: ['12', '17', '23'],
  },
  {
    id: 'raw-json-only',
    title: 'LLM 输出缺少 Schema 校验',
    detail: '当前依赖提示词要求 JSON，实际仍是 raw text；下游 json.loads 遇到 fenced JSON 会失败。',
    severity: 'warning',
    nodeIds: ['11', '16', '22', '28', '29', '30'],
  },
  {
    id: 'run-mode-unused',
    title: 'run_mode 尚未生效',
    detail: 'research 与 preset_demo 目前走完全相同的路径，没有预设案例加载逻辑。',
    severity: 'warning',
    nodeIds: ['10'],
  },
  {
    id: 'critic-single-pass',
    title: 'Critic 只有固定一轮',
    detail: 'A10 → A11 是线性单轮，不是设计文档中的“最多两轮有限修复”。',
    severity: 'design',
    nodeIds: ['22', '23'],
  },
  {
    id: 'contract-not-frozen',
    title: '合同未冻结解析后的计划',
    detail: 'data_version 为空，批准模型与稳健性列表为空，analysis_plan 仍是嵌套字符串。',
    severity: 'warning',
    nodeIds: ['24'],
  },
  {
    id: 'writer-no-references',
    title: 'Writer 没有已核验文献',
    detail: 'A20 的 Verified References 固定为“未提供”，当前不能生成可定位引用。',
    severity: 'warning',
    nodeIds: ['32', '33'],
  },
  {
    id: 'audit-no-revision-loop',
    title: '写作审计没有修复回路',
    detail: 'A21 即使返回 revise，原稿仍直接进入最终成果包。',
    severity: 'design',
    nodeIds: ['33', '99'],
  },
]

const appBStages: Omit<WorkflowStage, 'nodeIds'>[] = [
  {
    id: 'blind-input',
    order: 1,
    title: '封存输入',
    description: '读取主系统封存产物，并行提取原论文与参考结果摘要。',
  },
  {
    id: 'blind-evaluation',
    order: 2,
    title: '独立盲测',
    description: '在不修改主系统输出的前提下进行六维比较。',
  },
  {
    id: 'blind-output',
    order: 3,
    title: '评分输出',
    description: '输出总分、逐维度诊断、关键一致与差异。',
  },
]

const appBStageByNode: Record<string, string> = {
  '50': 'blind-input',
  '511': 'blind-input',
  '512': 'blind-input',
  '51': 'blind-evaluation',
  '59': 'blind-output',
}

const appBIssues: WorkflowIssue[] = [
  {
    id: 'manual-handoff',
    title: '主系统产物依赖人工复制',
    detail: 'App A 到 App B 没有封存签名或自动交接，ResearchRun 与 ClaimLedger 可被手动修改。',
    severity: 'blocker',
    nodeIds: ['50'],
  },
  {
    id: 'optional-reference-files',
    title: '参考文件可选但提取无条件执行',
    detail: '两份 reference 文件不是必填，流程也没有缺失校验。',
    severity: 'warning',
    nodeIds: ['50', '511', '512'],
  },
  {
    id: 'score-not-deterministic',
    title: '总分没有确定性校验',
    detail: 'overall_score 由 LLM 直接给出，未检查分项范围、加权总和与 fixture 的 N/A 规则。',
    severity: 'warning',
    nodeIds: ['51', '59'],
  },
  {
    id: 'context-overload',
    title: '单节点上下文可能过载',
    detail: '论文全文、多个 50,000 字符 JSON 和参考摘要一次性进入同一个 LLM。',
    severity: 'design',
    nodeIds: ['51'],
  },
]

export const WORKFLOW_SOURCES: WorkflowSourceConfig[] = [
  {
    id: 'app-a',
    label: '主研究工作流 App A',
    shortLabel: '主研究 App A',
    file: '/workflows/04_AppA_humaninput.yml',
    stages: appAStages,
    stageByNode: appAStageByNode,
    issues: appAIssues,
    forbiddenInputNames: [
      'reference_paper_file',
      'reference_summary_file',
      'reference_results',
      'reference_package',
      'hidden_reference',
      'gold_package',
    ],
  },
  {
    id: 'app-b',
    label: '盲测评估工作流 App B',
    shortLabel: '盲测 App B',
    file: '/workflows/05_AppB_BlindEvaluator.yml',
    stages: appBStages,
    stageByNode: appBStageByNode,
    issues: appBIssues,
  },
]
