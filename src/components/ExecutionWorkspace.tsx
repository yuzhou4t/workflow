import { ArrowLeft, Check, ChevronDown, Circle, CircleAlert, Clock3, Download, FileText, GitCompare, Image as ImageIcon, Layers, LoaderCircle, RotateCcw, Settings2, ShieldCheck, Trash2, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { BaselineRun, ClaimDecision, ClaimRecord, GateDecisionInput, ManuscriptStatementSourceView, ModelCallGroup, ModelUsageView, RunSnapshot, RunSummary, StepAttempt, WorkflowDefinition, WorkflowStage } from '../runtime/types'

interface ExecutionWorkspaceProps {
  definition: WorkflowDefinition
  run: RunSnapshot | null
  runs: RunSummary[]
  baselineRun: BaselineRun | null
  compareOpen: boolean
  caseReady: boolean
  busy: boolean
  busyLabel: string
  onSelectRun: (runId: string) => void
  onBackToHistory: () => void
  onDeleteRun: () => void
  onNewResearch: () => void
  onToggleCompare: () => void
  onStartHypoweaver: () => Promise<void>
  onStartBaseline: () => Promise<void>
  onOpenSettings: () => void
  onGateDecision: (gate: string, input: GateDecisionInput) => Promise<void>
  onSubmitRevision: (gate: 'H1' | 'H2', revision: unknown, comment: string) => Promise<void>
  onRetryWriting: () => Promise<void>
}

export const MODEL_CALL_PROTOCOL = {
  humanGateCount: 4,
  logicalCallCount: 9,
  maxProviderAttempts: 20,
} as const

const modelCallGroups: ModelCallGroup[] = ['h1_h2', 'h3', 'h4']
const modelCallStageLabels: Record<ModelCallGroup, string> = {
  h1_h2: '设计与审查阶段',
  h3: '证据与结论审计阶段',
  h4: '论文写作与复核阶段',
}

export function modelCallStageUsage(modelUsage: ModelUsageView) {
  return modelCallGroups.map((group) => ({
    label: modelCallStageLabels[group],
    attempts: modelUsage.groupUsage[group],
  }))
}

function usesSharedRetryPool(modelUsage: ModelUsageView): boolean {
  return modelUsage.retryPolicy === 'shared_bounded'
    || modelUsage.retryPolicy === 'shared-retry-v1'
    || modelUsage.retryMode === 'global_shared_retry_pool'
}

export function ModelCallContract({ modelUsage }: { modelUsage?: ModelUsageView }) {
  return (
    <section className="model-call-contract" aria-label="人工确认与模型调用结构">
      <header>
        <div><strong>确认与模型调用结构</strong><small>人工 Gate、逻辑任务和真实请求分别计数</small></div>
        {modelUsage && <span>当前 {modelUsage.logicalCalls}/{modelUsage.requiredLogicalCalls} 个逻辑调用 · {modelUsage.providerAttempts}/{modelUsage.maxCalls} 次 Provider Attempt</span>}
      </header>
      <div className="model-call-contract__totals">
        <article><strong>{MODEL_CALL_PROTOCOL.humanGateCount}</strong><span>个人工 Gate</span><small>H1–H4 决策点</small></article>
        <article><strong>{MODEL_CALL_PROTOCOL.logicalCallCount}</strong><span>个逻辑模型调用</span><small>完整首轮调用图</small></article>
        <article><strong>{MODEL_CALL_PROTOCOL.maxProviderAttempts}</strong><span>次 Provider Attempt 上限</span><small>包含首轮、网络重试与格式修复</small></article>
      </div>
      {modelUsage && (
        <div className="model-call-contract__usage">
          <p>{usesSharedRetryPool(modelUsage)
            ? <>共享重试池剩余 <strong>{modelUsage.sharedRetryRemaining}</strong> / {modelUsage.sharedRetrySlots} 次</>
            : '阶段调用用量'}</p>
          <ul aria-label="模型调用阶段用量">
            {modelCallStageUsage(modelUsage).map((stage) => (
              <li key={stage.label}><span>{stage.label}</span><strong>{stage.attempts} 次 attempt</strong></li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

const requiredManuscriptSections = [
  'abstract',
  'introduction',
  'theory_hypotheses',
  'data_variables',
  'research_design',
  'empirical_results',
  'discussion_limitations',
  'conclusion',
]

export function manuscriptQuality(run: RunSnapshot): { complete: boolean; characterCount: number } {
  const generated = run.manuscript?.sections.filter((section) => section.status === 'generated') ?? []
  const sectionIds = new Set(generated.map((section) => section.id))
  const characterCount = generated.reduce((total, section) => total + section.content.trim().length, 0)
  return {
    complete: run.manuscript?.auditResult === 'pass_with_no_critical_issues'
      && (run.manuscript.mode === 'identification_failure_report'
        || (run.manuscript.mode === 'full_manuscript'
          && requiredManuscriptSections.every((sectionId) => sectionIds.has(sectionId))
          && characterCount >= 3200)),
    characterCount,
  }
}

const statementKindText: Record<ManuscriptStatementSourceView['kind'], string> = {
  authorized_claim: '获批结论',
  estimate_fact: '估计事实',
  sample_fact: '样本事实',
  diagnostic_fact: '诊断事实',
  citation: '核验引文',
}

function StatementProvenance({ statements }: { statements: ManuscriptStatementSourceView[] }) {
  if (!statements.length) return null
  return <details className="statement-provenance">
    <summary>语句来源 · {statements.length} 条</summary>
    <ul>{statements.map((statement) => <li key={statement.id}>
      <strong>{statementKindText[statement.kind] ?? statement.kind}</strong>
      <span>{statement.id}</span>
      {statement.sources.length > 0 && <small>{statement.sources.map((source) => `${source.kind} · ${source.id} · ${source.path}`).join('；')}</small>}
    </li>)}</ul>
  </details>
}

function FigureGallery({ run }: { run: RunSnapshot }) {
  const visibleBundles = run.figureBundles.filter((bundle) => bundle.status !== 'not_generated')
  if (!visibleBundles.length) return null
  const fileUrl = (figureId: string, format: string) => (
    `/api/v1/runs/${encodeURIComponent(run.id)}/figures/${encodeURIComponent(figureId)}/${encodeURIComponent(format)}`
  )
  return <section className="figure-gallery">
    <header><ImageIcon size={18} /><div><strong>HypoWeaver 科研绘图</strong><small>确定性绘图模块生成图片；HypoWeaver Writer 负责正文</small></div></header>
    {visibleBundles.map((bundle) => <section key={bundle.stage} className="figure-bundle">
      <div className="figure-bundle__heading"><strong>{bundle.stage === 'evidence' ? 'H3 前证据图' : 'H3 后论文图'}</strong><span>{bundle.status === 'succeeded' ? `${bundle.figures.length} 张` : '生成失败'}</span></div>
      {bundle.figures.length > 0 && <div className="figure-grid">{bundle.figures.map((figure) => {
        const png = figure.files.find((file) => file.format === 'png')
        return <article key={figure.id}>
          {png && <img src={fileUrl(figure.id, 'png')} alt={figure.altText} loading="lazy" />}
          <div className="figure-copy"><p className="figure-recipe">{figure.recipeId} · v{figure.recipeVersion}</p><h3>{figure.title}</h3><p>{figure.caption}</p>
            <div className="figure-downloads">{figure.files.map((file) => <a key={file.format} href={fileUrl(figure.id, file.format)} target="_blank" rel="noreferrer"><Download size={12} />{file.format.toUpperCase()}</a>)}</div>
            <small>Execution {figure.executionIds.join('、') || '冻结源数据聚合（未绑定估计样本）'} · Claim {figure.claimIds.join('、') || 'H3 前未授权'}</small>
          </div>
        </article>
      })}</div>}
      {bundle.warnings.length > 0 && <ul className="figure-warnings">{bundle.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>}
    </section>)}
  </section>
}

const statusText = {
  created: '待启动', running: '运行中', waiting_human: '等待人工审核', blocked: '已阻塞',
  failed: '执行失败', completed: '已完成', stopped: '已终止', cancelled: '已取消',
}

const claimDecisionText: Record<ClaimDecision, string> = {
  approve: 'H3 已批准',
  downgrade: 'H3 已降级授权',
  reject: 'H3 已拒绝',
  hold: 'H3 已暂缓',
}

const allClaimDecisions: ClaimDecision[] = ['approve', 'downgrade', 'reject', 'hold']
const claimAdmissionText: Record<string, string> = {
  unassessed: '旧版未评估',
  admitted: '已准入',
  downgrade_required: '必须降级',
  prohibited: '禁止使用',
  rejected: '已拒绝准入',
}

export function permittedClaimDecisions(claim: ClaimRecord, fixture: boolean): ClaimDecision[] {
  if (fixture) return ['reject', 'hold']
  if (claim.admissionStatus === 'admitted' && !['insufficient', 'prohibited'].includes(claim.allowedStrength ?? '')) {
    return ['approve', 'downgrade', 'reject', 'hold']
  }
  if (claim.admissionStatus === 'downgrade_required' && !['insufficient', 'prohibited'].includes(claim.allowedStrength ?? '')) {
    return ['downgrade', 'reject', 'hold']
  }
  if (claim.admissionStatus === 'prohibited' || claim.admissionStatus === 'rejected' || claim.allowedStrength === 'prohibited') {
    return ['reject', 'hold']
  }
  return ['approve', 'downgrade', 'reject', 'hold']
}

const designStrategyLabels = {
  direct_baseline: '直接基准',
  identification_first: '识别优先',
  measurement_robustness: '测量稳健性优先',
} as const

function JsonBlock({ value, empty = '本步骤尚未产生内容。' }: { value: unknown; empty?: string }) {
  if (value === null || value === undefined || value === '') return <p className="technical-empty">{empty}</p>
  return <pre>{typeof value === 'string' ? value : JSON.stringify(value, null, 2)}</pre>
}

function stageState(stage: WorkflowStage, definition: WorkflowDefinition, run: RunSnapshot) {
  const currentNode = definition.nodes.find((node) => node.id === run.currentNodeId)
  const currentOrder = definition.stages.find((item) => item.id === currentNode?.stageId)?.order ?? 0
  if (run.status === 'completed') return 'complete'
  if (stage.id === currentNode?.stageId) return ['failed', 'blocked', 'stopped', 'cancelled'].includes(run.status) ? 'problem' : 'active'
  if (stage.order < currentOrder) return 'complete'
  return 'pending'
}

function StepDetails({ step, title }: { step: StepAttempt; title: string }) {
  const [tab, setTab] = useState<'prompt' | 'input' | 'output' | 'log'>('output')
  const tabPrefix = `step-${step.id.replaceAll(/[^a-zA-Z0-9_-]/g, '-')}`
  return (
    <article className="step-attempt">
      <header><span className={`step-status step-status--${step.status}`} /> <strong>{title}</strong><small>Attempt {step.attempt} · {step.status}</small></header>
      <details>
        <summary>查看本阶段提示词与输入输出 <ChevronDown size={15} /></summary>
        <div className="technical-tabs" role="tablist">
          {([['prompt', '提示词'], ['input', '实际输入'], ['output', '实际输出'], ['log', '运行日志']] as const).map(([id, label]) => <button type="button" role="tab" id={`${tabPrefix}-${id}-tab`} aria-controls={`${tabPrefix}-${id}-panel`} aria-selected={tab === id} tabIndex={tab === id ? 0 : -1} className={tab === id ? 'is-active' : ''} key={id} onClick={() => setTab(id)}>{label}</button>)}
        </div>
        <div className="technical-content" role="tabpanel" id={`${tabPrefix}-${tab}-panel`} aria-labelledby={`${tabPrefix}-${tab}-tab`}>
          {tab === 'prompt' && (step.prompts.length ? step.prompts.map((prompt) => <section className="prompt-entry" key={prompt.id}><strong>{prompt.role} · {prompt.rendered ? '本次渲染' : '模板'}</strong><pre>{prompt.rendered ?? prompt.template}</pre></section>) : <p className="technical-empty">这是确定性代码步骤，没有 LLM 提示词。</p>)}
          {tab === 'input' && <JsonBlock value={step.input} />}
          {tab === 'output' && <JsonBlock value={step.output} />}
          {tab === 'log' && <JsonBlock value={step.error ? [...step.logs, `ERROR: ${step.error}`].join('\n') : step.logs.join('\n')} empty="没有运行日志。" />}
        </div>
      </details>
    </article>
  )
}

export function revisionSeed(run: RunSnapshot, gate: 'H1' | 'H2'): string {
  const nodeId = gate === 'H1' ? 'h1_gate' : 'h2_gate'
  const waitingSource = [...run.steps].reverse().find((step) => step.nodeId === nodeId && step.status === 'waiting_human')?.input
  const blockedPlan = gate === 'H2'
    ? [...run.steps].reverse().find((step) => ['plan_revision', 'analysis_plan_merge'].includes(step.nodeId) && ['succeeded', 'blocked'].includes(step.status))?.output
    : undefined
  const source = waitingSource ?? blockedPlan
  const sourceRecord = source && typeof source === 'object' && !Array.isArray(source)
    ? source as Record<string, unknown>
    : {}
  const editable = gate === 'H2' && sourceRecord.analysis_plan && typeof sourceRecord.analysis_plan === 'object' && !Array.isArray(sourceRecord.analysis_plan)
    ? sourceRecord.analysis_plan
    : sourceRecord
  const value = editable && typeof editable === 'object' && !Array.isArray(editable)
    ? JSON.parse(JSON.stringify(editable)) as Record<string, unknown>
    : {}
  if (gate === 'H1') {
    delete value.input_conflicts
    delete value.missing_required_information
  } else {
    value.plan_version = Number(value.plan_version ?? 0) + 1
  }
  return JSON.stringify(value, null, 2)
}

export function returnedRevisionGate(run: RunSnapshot): 'H1' | 'H2' | undefined {
  const blockedByCritic = [...run.steps].reverse().find((step) => step.nodeId === run.currentNodeId)
  if (['critic_merge', 'design_arena_merge'].includes(run.currentNodeId ?? '') && blockedByCritic?.status === 'blocked') return 'H2'
  const latestDecision = [...run.steps].reverse().find((step) => (
    step.status === 'succeeded' && ['h1_gate', 'h2_gate'].includes(step.nodeId)
  ))
  const action = latestDecision?.output && typeof latestDecision.output === 'object' && !Array.isArray(latestDecision.output)
    ? (latestDecision.output as Record<string, unknown>).action
    : undefined
  if (action !== 'revise') return undefined
  if (latestDecision?.nodeId === 'h1_gate' && run.currentNodeId === 'input_validation') return 'H1'
  if (latestDecision?.nodeId === 'h2_gate' && run.currentNodeId === 'analysis_plan_merge') return 'H2'
  return undefined
}

function HumanReview({ run, busy, onDecision, onSubmitRevision }: {
  run: RunSnapshot
  busy: boolean
  onDecision: (gate: string, input: GateDecisionInput) => Promise<void>
  onSubmitRevision: (gate: 'H1' | 'H2', revision: unknown, comment: string) => Promise<void>
}) {
  const returnedGate = run.status === 'blocked' ? returnedRevisionGate(run) : undefined
  const gate = run.currentGate ?? returnedGate
  const returnedForRevision = Boolean(returnedGate)
  const blockedByCritic = returnedGate === 'H2' && ['critic_merge', 'design_arena_merge'].includes(run.currentNodeId ?? '')
  const criticOutput = blockedByCritic
    ? [...run.steps].reverse().find((step) => step.nodeId === run.currentNodeId)?.output
    : undefined
  const criticIssues = criticOutput && typeof criticOutput === 'object' && !Array.isArray(criticOutput)
    ? (criticOutput as Record<string, unknown>).issues
    : undefined
  const [comment, setComment] = useState('')
  const [decisions, setDecisions] = useState<Record<string, ClaimDecision>>({})
  const [finalTexts, setFinalTexts] = useState<Record<string, string>>({})
  const [showRevision, setShowRevision] = useState(returnedForRevision)
  const [revisionText, setRevisionText] = useState(() => gate === 'H1' || gate === 'H2' ? revisionSeed(run, gate) : '')
  const [revisionError, setRevisionError] = useState<string | null>(null)
  const [selectedCandidateId, setSelectedCandidateId] = useState(run.designArena?.provisionalCandidateId ?? '')
  if (!gate || (run.status !== 'waiting_human' && !returnedForRevision)) return null
  const fixtureH3 = gate === 'H3' && (run.mode === 'fixture' || run.planOnly)
  const allClaimsReady = gate !== 'H3' || (Boolean(run.claims.length) && run.claims.every((claim) => {
    const decision = decisions[claim.id] ?? claim.decision
    return Boolean(decision)
      && permittedClaimDecisions(claim, fixtureH3).includes(decision!)
      && (decision !== 'downgrade' || Boolean(finalTexts[claim.id]?.trim()))
      && (!(decision === 'approve' && /\d/.test(claim.text)) || Boolean(finalTexts[claim.id]?.trim()))
  }))
  const selectedCandidateReady = gate !== 'H2'
    || !run.designArena
    || run.designArena.recommendedCandidateIds.includes(selectedCandidateId)
  const willGenerateFailureReport = gate === 'H3' && !fixtureH3 && !run.claims.some((claim) => {
    const decision = decisions[claim.id] ?? claim.decision ?? 'reject'
    return decision === 'approve' || decision === 'downgrade'
  })

  async function submitH3() {
    const claimDecisions = run.claims.map((claim) => ({
      claimId: claim.id,
      decision: decisions[claim.id] ?? claim.decision ?? (fixtureH3 ? 'hold' : 'reject'),
      finalText: finalTexts[claim.id]?.trim() || undefined,
      reason: comment,
    }))
    const hasAdmittedClaim = claimDecisions.some(({ decision }) => decision === 'approve' || decision === 'downgrade')
    await onDecision('H3', {
      action: fixtureH3
        ? 'generate_plan_only'
        : hasAdmittedClaim
          ? 'approve'
          : 'generate_identification_failure_report',
      comment,
      claims: claimDecisions,
    })
  }

  function openRevision() {
    if (gate !== 'H1' && gate !== 'H2') return
    setRevisionText(revisionSeed(run, gate))
    setRevisionError(null)
    setShowRevision(true)
  }

  async function submitRevision() {
    if (gate !== 'H1' && gate !== 'H2') return
    try {
      const revision = JSON.parse(revisionText) as unknown
      if (!revision || typeof revision !== 'object' || Array.isArray(revision)) throw new Error('修订内容必须是一个 JSON 对象。')
      setRevisionError(null)
      await onSubmitRevision(gate, revision, comment)
    } catch (reason) {
      setRevisionError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  return (
    <section className="human-review-card">
      <header><ShieldCheck size={22} /><div><strong>{gate} · {blockedByCritic ? '请处理关键审查问题' : returnedForRevision ? '请继续提交修订' : gate === 'H1' ? '请确认研究边界' : gate === 'H2' ? '请选择并冻结分析计划' : gate === 'H3' ? '请逐条授权结论' : '请审核最终论文初稿'}</strong><p>{blockedByCritic ? '当前不能直接批准。请按 Reviewer 意见修改分析计划并重新审查；通过后系统会开放 H2。' : returnedForRevision ? '上一次“退回”已经记录在服务端。请修改结构化内容并重新提交，刷新页面后也可以从这里继续。' : gate === 'H1' ? '批准后系统才会拆解假设并设计方法。' : gate === 'H2' ? 'Reviewer 只淘汰硬失败方案；请从可行候选中明确选择一个，批准后才冻结合同。' : gate === 'H3' ? 'Writer 只能读取本次明确授权的结论。' : '一致性审计已经通过，但只有你批准后成果才会封存并进入盲测比较。'}</p></div></header>
      {blockedByCritic && Array.isArray(criticIssues) && <ul className="review-issue-list">{criticIssues.map((issue, index) => {
        const item = issue && typeof issue === 'object' && !Array.isArray(issue) ? issue as Record<string, unknown> : {}
        return <li key={`${String(item.issue_id ?? 'issue')}-${index}`}><strong>{String(item.severity ?? 'issue')}</strong><span>{String(item.evidence ?? item.why_it_matters ?? '请查看 CriticReport 输出。')}</span><small>需要修改：{String(item.required_fix ?? '请根据审查意见补充研究设计。')}</small></li>
      })}</ul>}
      {gate === 'H2' && run.designArena && <section className="design-candidate-list" aria-label="可行研究设计候选">
        {run.designArena.candidates.map((candidate) => {
          const recommended = run.designArena?.recommendedCandidateIds.includes(candidate.id)
          return <label key={candidate.id} className={`design-candidate ${selectedCandidateId === candidate.id ? 'is-selected' : ''} ${recommended ? '' : 'is-unavailable'}`}>
            <input type="radio" name="design-candidate" value={candidate.id} checked={selectedCandidateId === candidate.id} disabled={!recommended} onChange={() => setSelectedCandidateId(candidate.id)} />
            <span><strong>{designStrategyLabels[candidate.strategy]}</strong><small>{candidate.methodFamily} · {candidate.estimator || '估计器待确认'}</small></span>
            <em>Probe {candidate.probeVerdict} · Reviewer 问题 {candidate.reviewIssueCount}</em>
            <p>{candidate.rationale}</p>
            {candidate.formula && <code>{candidate.formula}</code>}
            <details><summary>查看 Probe 检查</summary><ul>{candidate.probeChecks.map((check) => <li key={check.id}><strong>{check.status}</strong> {check.evidence}</li>)}</ul></details>
          </label>
        })}
        <p className="design-arena-note">不按总分或多数票自动选“赢家”；不可执行、目标错配或存在 critical 问题的候选不能冻结。</p>
      </section>}
      {gate === 'H3' && <div className="claim-review-list">{run.claims.map((claim) => {
        const permitted = permittedClaimDecisions(claim, fixtureH3)
        const selected = decisions[claim.id] ?? claim.decision
        return <article key={claim.id}>
          <p>{claim.text}</p>
          <div className="claim-gate-summary">
            <small>Gate：{claimAdmissionText[claim.admissionStatus ?? 'unassessed'] ?? claim.admissionStatus}</small>
            <small>代码上限：{claim.maxAllowedStrength ?? claim.allowedStrength ?? '未指定'}</small>
            <small>候选强度：{claim.allowedStrength ?? '未指定'}</small>
          </div>
          {claim.requiredCheckIds.length > 0 && <details><summary>必做检查 · {claim.requiredCheckIds.length}</summary><ul>{claim.requiredCheckIds.map((checkId) => <li key={checkId}>{checkId}</li>)}</ul></details>}
          {claim.gateReasons.length > 0 && <details open><summary>Gate 理由 · {claim.gateReasons.length}</summary><ul>{claim.gateReasons.map((reason, index) => <li key={`${claim.id}-reason-${index}`}>{reason}</li>)}</ul></details>}
          <div>{allClaimDecisions.map((decision) => <button type="button" key={decision} disabled={!permitted.includes(decision)} aria-pressed={selected === decision} className={selected === decision ? 'is-selected' : ''} onClick={() => setDecisions((current) => ({ ...current, [claim.id]: decision }))}>{{ approve: '批准', downgrade: '降级', reject: '拒绝', hold: '暂缓' }[decision]}</button>)}</div>
          {(selected === 'approve' || selected === 'downgrade') && <textarea value={finalTexts[claim.id] ?? ''} onChange={(event) => setFinalTexts((current) => ({ ...current, [claim.id]: event.target.value }))} placeholder={selected === 'downgrade' ? '填写降级后的审慎表述' : /\d/.test(claim.text) ? '候选含裸数字，必须填写不含数字的安全表述' : '可选：填写最终授权表述'} />}
        </article>
      })}</div>}
      {gate === 'H4' && run.manuscript && <section className="h4-manuscript-review"><p><strong>{run.manuscript.mode === 'identification_failure_report' ? '识别失败报告' : '论文初稿'} v{run.manuscript.version}</strong> · IR {run.manuscript.irVersion} · {run.manuscript.sections.length} 节 · {run.manuscript.auditResult === 'pass_with_no_critical_issues' ? '一致性审计通过' : '需要修订'}</p>{run.manuscript.sections.map((section) => <details key={section.id}><summary>{section.title}</summary><div className="manuscript-copy">{section.content}</div><StatementProvenance statements={section.statements} /></details>)}</section>}
      {fixtureH3 && <p className="fixture-warning">本次没有真实实证结果，每条 Claim 只能拒绝或暂缓；提交后仅生成研究计划。</p>}
      {!returnedForRevision && <label>审核说明<textarea rows={3} value={comment} onChange={(event) => setComment(event.target.value)} placeholder={gate === 'H4' ? '退回重写时，请写明需要修改的章节和具体问题' : '记录批准或拒绝理由（选填）'} /></label>}
      {showRevision && (gate === 'H1' || gate === 'H2') && <section className="revision-editor"><header><div><strong>{gate} 结构化修订</strong><p>{gate === 'H1' ? '修改 CaseSubmission 后，系统会重新执行 Intake 与输入校验，再回到 H1。' : '修改 AnalysisPlan 后，系统会重新执行四类 Critic；plan_version 已自动加一。'}</p></div></header><textarea aria-label={`${gate} 结构化修订 JSON`} rows={18} spellCheck={false} value={revisionText} onChange={(event) => setRevisionText(event.target.value)} />{revisionError && <p className="revision-error" role="alert">{revisionError}</p>}<footer>{!returnedForRevision && <button type="button" className="secondary-button" disabled={busy} onClick={() => setShowRevision(false)}>取消修订</button>}<button type="button" className="primary-button" disabled={busy} onClick={submitRevision}>提交修订并重新校验</button></footer></section>}
      {!returnedForRevision && <footer><button type="button" className="danger-button" disabled={busy} onClick={() => onDecision(gate, { action: 'reject', comment })}>拒绝并终止</button>{gate !== 'H3' && <button type="button" className="secondary-button" disabled={busy || (gate === 'H4' && !comment.trim())} onClick={() => gate === 'H4' ? onDecision('H4', { action: 'revise', comment }) : openRevision()}>{gate === 'H4' ? '退回重写' : '退回并编辑'}</button>}{gate === 'H3' ? <button type="button" className="primary-button" disabled={busy || !allClaimsReady} onClick={submitH3}>{fixtureH3 ? '生成 plan-only 成果' : willGenerateFailureReport ? '生成识别失败报告' : '提交结论授权'}</button> : <button type="button" className="primary-button" disabled={busy || !selectedCandidateReady} onClick={() => onDecision(gate, { action: 'approve', comment, ...(gate === 'H2' && selectedCandidateId ? { selectedCandidateId } : {}) })}>{gate === 'H4' ? '批准并封存' : '批准并继续'}</button>}</footer>}
    </section>
  )
}

const baselineStatusText: Record<BaselineRun['status'], string> = {
  queued: '排队中',
  running: '运行中',
  completed: '已完成',
  failed: '执行失败',
}

const baselineFallbackPhases: BaselineRun['phases'] = [
  { id: 'plan', title: '形成研究计划', status: 'pending' },
  { id: 'data', title: '准备分析数据', status: 'pending' },
  { id: 'execute', title: '运行实验', status: 'pending' },
  { id: 'interpret', title: '解释结果', status: 'pending' },
  { id: 'write', title: '生成研究报告', status: 'pending' },
]

function elapsedSeconds(start?: string, end?: string): string {
  if (!start || !end) return '—'
  const value = (new Date(end).getTime() - new Date(start).getTime()) / 1000
  if (!Number.isFinite(value) || value < 0) return '—'
  return value < 60 ? `${value.toFixed(1)} 秒` : `${(value / 60).toFixed(1)} 分钟`
}

function findMethodFamily(run: RunSnapshot | null): string {
  if (!run) return '—'
  for (const step of [...run.steps].reverse()) {
    const output = step.output
    if (!output || typeof output !== 'object' || Array.isArray(output)) continue
    const record = output as Record<string, unknown>
    const direct = record.method_family ?? record.primary_route ?? record.method
    if (typeof direct === 'string' && direct) return direct
    const route = record.method_route
    if (route && typeof route === 'object' && !Array.isArray(route)) {
      const nested = (route as Record<string, unknown>).method_family
      if (typeof nested === 'string' && nested) return nested
    }
  }
  return '待路由'
}

function BaselineLane({ run, busy, caseReady, onStart }: {
  run: BaselineRun | null
  busy: boolean
  caseReady: boolean
  onStart: () => Promise<void>
}) {
  const phases = run?.phases.length ? run.phases : baselineFallbackPhases
  return (
    <section className="bench-lane">
      <header className="bench-lane__header">
        <div><span className="bench-badge">AL</span><h2>Agent Laboratory</h2></div>
        <span className={`plain-status plain-status--${run?.status ?? 'idle'}`}>{run ? baselineStatusText[run.status] : '尚未启动'}</span>
      </header>
      {!run && <button type="button" className="secondary-button lane-start" disabled={busy || !caseReady} onClick={() => void onStart()}>{caseReady ? '启动基线' : '请重新选择案例'}</button>}
      <ol className="compact-flow compact-flow--runtime">
        {phases.map((phase, index) => (
          <li className={`is-${phase.status}`} key={phase.id}>
            <span>{phase.status === 'succeeded' ? <Check size={13} /> : index + 1}</span>
            <div><strong>{phase.title}</strong><small>{phase.status === 'succeeded' ? '已完成' : phase.status === 'running' ? '正在执行' : phase.status === 'failed' ? '执行失败' : '尚未开始'}</small></div>
          </li>
        ))}
      </ol>
      {run?.error && <p className="lane-error">{run.error}</p>}
      <p className="lane-note">基线保留原生调度；科学状态默认不判定。</p>
    </section>
  )
}

export function ExecutionWorkspace({
  definition,
  run,
  runs,
  baselineRun,
  compareOpen,
  caseReady,
  busy,
  busyLabel,
  onSelectRun,
  onBackToHistory,
  onDeleteRun,
  onNewResearch,
  onToggleCompare,
  onStartHypoweaver,
  onStartBaseline,
  onOpenSettings,
  onGateDecision,
  onSubmitRevision,
  onRetryWriting,
}: ExecutionWorkspaceProps) {
  const attemptsByStage = useMemo(
    () => new Map(definition.stages.map((stage) => [stage.id, run?.steps.filter((step) => stage.nodeIds.includes(step.nodeId)) ?? []])),
    [definition.stages, run?.steps],
  )
  const caseName = run?.caseName ?? baselineRun?.caseName ?? '已导入案例'
  const currentNode = definition.nodes.find((node) => node.id === run?.currentNodeId)
  const completedStages = run ? definition.stages.filter((stage) => stageState(stage, definition, run) === 'complete').length : 0
  const manuscriptState = run ? manuscriptQuality(run) : { complete: false, characterCount: 0 }
  const identificationFailure = run?.manuscript?.mode === 'identification_failure_report'
  const writingFailed = run?.status === 'failed' && run.currentNodeId === 'scientific_writer'
  const preservedDraft = Boolean(writingFailed && run?.manuscript)
  const totalStages = definition.stages.length
  const progressPct = totalStages ? Math.round((completedStages / totalStages) * 100) : 0
  const modelUsage = run?.modelUsage
  const approvedClaims = run ? run.claims.filter((claim) => claim.decision === 'approve' || claim.decision === 'downgrade') : []
  const latestClaim = approvedClaims[approvedClaims.length - 1]
  const statusLabel = run ? statusText[run.status] : ''
  const hasArtifacts = Boolean(run && (run.manuscript || run.figureBundles.some((bundle) => bundle.status !== 'not_generated')))
  const visibleClaims = approvedClaims.slice(0, 3)
  const hiddenClaimCount = approvedClaims.length - visibleClaims.length
  const [drawer, setDrawer] = useState<'steps' | 'artifacts' | 'compare' | null>(null)
  const gateByStage = useMemo(() => {
    const map = new Map<string, string>()
    for (const node of definition.nodes) {
      if (node.kind !== 'gate') continue
      const match = /H[1-4]/.exec(node.title) ?? /h([1-4])_gate/i.exec(node.id)
      if (match) map.set(node.stageId, match[0].toUpperCase().startsWith('H') ? match[0].toUpperCase() : `H${match[1]}`)
    }
    return map
  }, [definition.nodes])

  return (
    <main className="exec">
      <section className="exec__stage">
        <header className="exec-hero">
          <div className="exec-hero__top">
            <p className="exec-eyebrow">当前案例{run ? ` · ${run.mode === 'fixture' ? '流程演示' : '真实研究'}` : ''}</p>
            <div className="exec-hero__actions">
              <button type="button" className="quiet-button" onClick={onBackToHistory}><ArrowLeft size={14} />历史记录</button>
              <button type="button" className="quiet-button" onClick={onToggleCompare}>{compareOpen ? '收起基线' : '展开基线'}</button>
              <button type="button" className="quiet-button" onClick={onOpenSettings}><Settings2 size={14} />配置</button>
              {run && <button type="button" className="quiet-button delete-run-button" disabled={busy} onClick={onDeleteRun}><Trash2 size={14} />删除</button>}
              <button type="button" className="primary-button" onClick={onNewResearch}>选择新案例</button>
            </div>
          </div>
          <h1 className="exec-title" title={caseName}>{caseName}</h1>
          <p className="exec-sub">{busy ? busyLabel : currentNode ? `当前：${currentNode.title}` : run ? statusLabel : '选择或启动一个研究。'}</p>
          {run?.mode === 'fixture' && <p className="exec-note exec-note--fixture"><CircleAlert size={15} />流程演示不会生成实证结论。</p>}
          {run?.status === 'failed' && run.lastError && <div className="exec-note exec-note--error"><CircleAlert size={15} /><div><strong>失败原因</strong>{run.lastError}</div>{writingFailed && <button type="button" className="secondary-button" disabled={busy} onClick={() => void onRetryWriting()}><RotateCcw size={13} />重试写作</button>}</div>}
        </header>

        <nav className="exec-progress" aria-label="研究进度">
          <div className="track" />
          <div className="fill" style={{ width: `${progressPct}%` }} />
          <div className="steps">
            {definition.stages.map((stage) => {
              const state = run ? stageState(stage, definition, run) : 'pending'
              const cls = state === 'complete' ? 'is-complete' : state === 'active' ? 'is-active' : state === 'problem' ? 'is-problem' : ''
              const gate = gateByStage.get(stage.id)
              return (
                <div className={`exec-step ${cls}`} key={stage.id} title={stage.title}>
                  <span className="dot" />
                  <span className="cap">{stage.title}</span>
                  {gate && <span className="g">{gate}</span>}
                </div>
              )
            })}
          </div>
        </nav>

        <div className="exec-focus">
          {!run && (
            <div className="exec-empty">
              <h2>{caseReady ? '案例已就绪' : '尚未选择案例'}</h2>
              <p className="exec-sub">{caseReady ? '启动 HypoWeaver，进入 H1 研究边界确认。' : '请回到“新建”选择案例文件夹。'}</p>
              <button type="button" className="primary-button" disabled={busy || !caseReady} onClick={() => void onStartHypoweaver()}>{caseReady ? '启动 HypoWeaver' : '选择案例'}</button>
            </div>
          )}
          {run && (
            <>
              <HumanReview key={`${run.id}:${run.version}:${run.currentGate}`} run={run} busy={busy} onDecision={onGateDecision} onSubmitRevision={onSubmitRevision} />
              {run.status === 'running' && (
                <div className="exec-running"><LoaderCircle size={18} className="spin" /><div><b>正在执行 · {currentNode?.title ?? '处理中'}</b><span>{busyLabel || '模型与执行器运行中，可在“阶段明细”查看过程。'}</span></div></div>
              )}
              {(run.status === 'completed' || preservedDraft) && (
                <div className="exec-result">
                  <div className="exec-focus__title"><h2>{preservedDraft ? '上一版初稿已保留' : run.planOnly ? '研究计划已生成' : identificationFailure ? '识别失败报告已生成' : manuscriptState.complete ? '完整论文初稿已生成' : '论文初稿不完整'}</h2></div>
                  <div className="exec-result__meta"><span>执行 · {run.executionStatus}</span><span>科学 · {run.scientificStatus}</span>{run.manuscript && <span>{run.manuscript.sections.filter((section) => section.status === 'generated').length} 节 · {manuscriptState.characterCount.toLocaleString()} 字</span>}</div>
                  <div className="exec-result__claims">
                    {visibleClaims.map((claim) => (
                      <button type="button" className={`exec-claim ${claim.decision === 'downgrade' ? 'is-hold' : ''} ${hasArtifacts ? '' : 'exec-claim--static'}`} key={claim.id} onClick={() => hasArtifacts && setDrawer('artifacts')}>
                        <div className="ch"><b>{claimDecisionText[claim.decision!]}</b><small>{claim.allowedStrength ?? '未指定'}</small></div>
                        <p>{claim.finalText ?? claim.text}</p>
                      </button>
                    ))}
                    {hiddenClaimCount > 0 && <button type="button" className="exec-claim-more" onClick={() => setDrawer('artifacts')}>+{hiddenClaimCount} 条结论 · 查看全部</button>}
                  </div>
                  {!approvedClaims.length && !run.planOnly && <p className="exec-sub">本次没有获得 H3 授权的实证结论。</p>}
                  <div className="exec-actions">
                    {hasArtifacts && <button type="button" className="secondary-button" onClick={() => setDrawer('artifacts')}><FileText size={14} />论文与图表</button>}
                    {!run.planOnly && !identificationFailure && <button type="button" className={manuscriptState.complete ? 'quiet-button' : 'primary-button'} disabled={busy} onClick={() => void onRetryWriting()}><RotateCcw size={14} />{manuscriptState.complete ? '重新生成论文' : '生成完整论文'}</button>}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </section>

      <aside className="exec__aside">
        <div className="exec-blk">
          <p className="exec-eyebrow">研究进度</p>
          <div className="exec-hero-stat"><div className="num">{completedStages}<small>/{totalStages}</small></div><div className="meta"><div>阶段已完成</div><div>{run ? (run.currentGate ? `当前 ${run.currentGate}` : statusLabel) : '未启动'}</div></div></div>
          <div className="exec-ring"><i style={{ width: `${progressPct}%` }} /></div>
        </div>
        <div className="exec-blk">
          <p className="exec-eyebrow">实时用量</p>
          <div className="exec-mrow"><span>逻辑模型调用</span><b>{modelUsage?.logicalCalls ?? 0} <em>/ {modelUsage?.requiredLogicalCalls ?? MODEL_CALL_PROTOCOL.logicalCallCount}</em></b></div>
          <div className="exec-mrow"><span>Provider Attempt</span><b>{modelUsage?.providerAttempts ?? 0} <em>/ {modelUsage?.maxCalls ?? MODEL_CALL_PROTOCOL.maxProviderAttempts}</em></b></div>
          <div className="exec-mrow"><span>受约束结论</span><b>{run?.claims.length ?? 0} <em>条</em></b></div>
        </div>
        <div className="exec-blk">
          <p className="exec-eyebrow">并行运行</p>
          {runs.length === 0 && <p className="exec-sub">暂无运行记录。</p>}
          {runs.map((item) => {
            const cls = item.status === 'waiting_human' ? 'is-wait' : item.status === 'completed' ? 'is-done' : ''
            const width = item.status === 'completed' ? '100%' : item.status === 'created' ? '0%' : '60%'
            return (
              <div className={`exec-run ${item.id === run?.id ? 'is-active' : ''} ${cls}`} key={item.id} onClick={() => { if (!busy) onSelectRun(item.id) }}>
                <span className="rn">{item.caseName}</span>
                <span className="stt">{item.currentGate ? `${item.currentGate} · ` : ''}{statusText[item.status]}</span>
                <span className="pl"><i style={{ width }} /></span>
              </div>
            )
          })}
        </div>
        {latestClaim && (
          <div className="exec-blk">
            <p className="exec-eyebrow">最新授权结论</p>
            <p className="exec-claimline"><b>{claimDecisionText[latestClaim.decision!]} · </b>{latestClaim.finalText ?? latestClaim.text}</p>
          </div>
        )}
        <div className="exec-aside__tools">
          {run && <button type="button" className="secondary-button" onClick={() => setDrawer('steps')}><Layers size={14} />阶段明细与日志</button>}
          {hasArtifacts && <button type="button" className="secondary-button" onClick={() => setDrawer('artifacts')}><FileText size={14} />论文与图表</button>}
          {compareOpen && <button type="button" className="secondary-button" onClick={() => setDrawer('compare')}><GitCompare size={14} />流程对比</button>}
        </div>
      </aside>

      {drawer && (
        <div className="exec-drawer" role="dialog" aria-modal="true">
          <div className="exec-drawer__scrim" onClick={() => setDrawer(null)} />
          <div className="exec-drawer__panel">
            <div className="exec-drawer__head">
              <div><h2>{drawer === 'steps' ? '阶段明细与日志' : drawer === 'artifacts' ? '论文与图表' : '流程对比'}</h2><small>{caseName}</small></div>
              <button type="button" className="exec-drawer__close" aria-label="关闭" onClick={() => setDrawer(null)}><X size={16} /></button>
            </div>
            <div className="exec-drawer__body">
              {drawer === 'steps' && run && (
                <>
                  <ModelCallContract modelUsage={run.modelUsage} />
                  <div className="stage-flow">
                    {definition.stages.map((stage) => {
                      const state = stageState(stage, definition, run)
                      const attempts = attemptsByStage.get(stage.id) ?? []
                      return (
                        <section className={`stage-card stage-card--${state}`} key={stage.id}>
                          <div className="stage-rail"><Circle size={16} /><span /></div>
                          <div className="stage-card__content">
                            <header><div><small>阶段 {stage.order}</small><h2>{stage.title}</h2></div></header>
                            {attempts.length > 0 ? <div className="attempt-list">{attempts.map((attempt) => <StepDetails key={attempt.id} step={attempt} title={definition.nodes.find((node) => node.id === attempt.nodeId)?.title ?? attempt.nodeId} />)}</div> : <p className="stage-pending-copy"><Clock3 size={14} />尚无记录</p>}
                          </div>
                        </section>
                      )
                    })}
                  </div>
                </>
              )}
              {drawer === 'artifacts' && run && (
                <>
                  <FigureGallery run={run} />
                  {run.manuscript && <article className="manuscript-draft">
                    <header><FileText size={18} /><div><strong>{identificationFailure ? '识别失败报告' : '论文初稿'} · v{run.manuscript.version}</strong><small>{run.manuscript.auditResult === 'pass_with_no_critical_issues' ? '一致性审计通过' : '尚未通过一致性审计'}</small></div></header>
                    <div className="manuscript-sections">
                      {run.manuscript.sections.filter((section) => section.status === 'generated').map((section, index) => (
                        <section key={section.id} id={`manuscript-${section.id}`}>
                          <p className="manuscript-section-index">{String(index + 1).padStart(2, '0')} · {section.id}</p>
                          <h3>{section.title}</h3>
                          <div className="manuscript-copy">{section.content}</div>
                          <StatementProvenance statements={section.statements} />
                        </section>
                      ))}
                    </div>
                    {run.manuscript.disclosures.length > 0 && <aside><strong>写作披露</strong><ul>{run.manuscript.disclosures.map((item) => <li key={item}>{item}</li>)}</ul></aside>}
                  </article>}
                  {!hasArtifacts && <p className="exec-sub">尚未生成论文或图表。</p>}
                </>
              )}
              {drawer === 'compare' && (
                <>
                  <div className="bench-grid is-comparing" style={{ marginBottom: 16 }}>
                    <BaselineLane run={baselineRun} busy={busy} caseReady={caseReady} onStart={onStartBaseline} />
                  </div>
                  <div className="comparison-table-wrap">
                    <table className="comparison-table">
                      <thead><tr><th>指标</th><th>HypoWeaver-Qwen</th><th>Agent Laboratory</th></tr></thead>
                      <tbody>
                        <tr><th>进度</th><td>{run ? `${completedStages}/${totalStages} 阶段` : '未启动'}</td><td>{baselineRun ? baselineStatusText[baselineRun.status] : '未启动'}</td></tr>
                        <tr><th>方法</th><td>{findMethodFamily(run)}</td><td>{baselineRun?.methodFamily ?? '待规划'}</td></tr>
                        <tr><th>执行状态</th><td>{run?.executionStatus ?? 'not_started'}</td><td>{baselineRun?.executionStatus ?? 'not_started'}</td></tr>
                        <tr><th>科学状态</th><td>{run?.scientificStatus ?? 'not_assessed'}</td><td>{baselineRun?.scientificStatus ?? 'not_assessed'}</td></tr>
                        <tr><th>结论约束</th><td>{run ? `${run.claims.length} 条 Claim` : '尚未生成'}</td><td>无 ClaimLedger</td></tr>
                        <tr><th>运行时间</th><td>{run ? elapsedSeconds(run.createdAt, run.updatedAt) : '—'}</td><td>{baselineRun ? `${baselineRun.wallTimeSeconds.toFixed(1)} 秒` : '—'}</td></tr>
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </main>
  )
}
