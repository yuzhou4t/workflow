import { Check, CheckCircle2, ChevronDown, Circle, CircleAlert, Clock3, FileText, LoaderCircle, RotateCcw, Settings2, ShieldCheck, Trash2, XCircle } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { BaselineRun, ClaimDecision, GateDecisionInput, RunSnapshot, RunSummary, StepAttempt, WorkflowDefinition, WorkflowStage } from '../runtime/types'

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
    complete: run.manuscript?.mode === 'full_manuscript'
      && run.manuscript.auditResult === 'pass_with_no_critical_issues'
      && requiredManuscriptSections.every((sectionId) => sectionIds.has(sectionId))
      && characterCount >= 3200,
    characterCount,
  }
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
    return Boolean(decision) && (decision !== 'downgrade' || Boolean(finalTexts[claim.id]?.trim()))
  }))
  const selectedCandidateReady = gate !== 'H2'
    || !run.designArena
    || run.designArena.recommendedCandidateIds.includes(selectedCandidateId)

  async function submitH3() {
    await onDecision('H3', {
      action: fixtureH3 ? 'generate_plan_only' : 'approve',
      comment,
      claims: run.claims.map((claim) => ({
        claimId: claim.id,
        decision: decisions[claim.id] ?? claim.decision ?? (fixtureH3 ? 'hold' : 'reject'),
        finalText: finalTexts[claim.id]?.trim() || undefined,
        reason: comment,
      })),
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
      {gate === 'H3' && <div className="claim-review-list">{run.claims.map((claim) => <article key={claim.id}><p>{claim.text}</p><small>允许强度：{claim.allowedStrength ?? '未指定'}</small><div>{(fixtureH3 ? ['reject', 'hold'] : ['approve', 'downgrade', 'reject', 'hold']).map((decision) => <button type="button" key={decision} aria-pressed={(decisions[claim.id] ?? claim.decision) === decision} className={(decisions[claim.id] ?? claim.decision) === decision ? 'is-selected' : ''} onClick={() => setDecisions((current) => ({ ...current, [claim.id]: decision as ClaimDecision }))}>{{ approve: '批准', downgrade: '降级', reject: '拒绝', hold: '暂缓' }[decision]}</button>)}</div>{(decisions[claim.id] ?? claim.decision) === 'downgrade' && <textarea value={finalTexts[claim.id] ?? ''} onChange={(event) => setFinalTexts((current) => ({ ...current, [claim.id]: event.target.value }))} placeholder="填写降级后的审慎表述" />}</article>)}</div>}
      {gate === 'H4' && run.manuscript && <section className="h4-manuscript-review"><p><strong>论文初稿 v{run.manuscript.version}</strong> · {run.manuscript.sections.length} 节 · {run.manuscript.auditResult === 'pass_with_no_critical_issues' ? '一致性审计通过' : '需要修订'}</p>{run.manuscript.sections.map((section) => <details key={section.id}><summary>{section.title}</summary><div className="manuscript-copy">{section.content}</div></details>)}</section>}
      {fixtureH3 && <p className="fixture-warning">本次没有真实实证结果，每条 Claim 只能拒绝或暂缓；提交后仅生成研究计划。</p>}
      {!returnedForRevision && <label>审核说明<textarea rows={3} value={comment} onChange={(event) => setComment(event.target.value)} placeholder={gate === 'H4' ? '退回重写时，请写明需要修改的章节和具体问题' : '记录批准或拒绝理由（选填）'} /></label>}
      {showRevision && (gate === 'H1' || gate === 'H2') && <section className="revision-editor"><header><div><strong>{gate} 结构化修订</strong><p>{gate === 'H1' ? '修改 CaseSubmission 后，系统会重新执行 Intake 与输入校验，再回到 H1。' : '修改 AnalysisPlan 后，系统会重新执行四类 Critic；plan_version 已自动加一。'}</p></div></header><textarea aria-label={`${gate} 结构化修订 JSON`} rows={18} spellCheck={false} value={revisionText} onChange={(event) => setRevisionText(event.target.value)} />{revisionError && <p className="revision-error" role="alert">{revisionError}</p>}<footer>{!returnedForRevision && <button type="button" className="secondary-button" disabled={busy} onClick={() => setShowRevision(false)}>取消修订</button>}<button type="button" className="primary-button" disabled={busy} onClick={submitRevision}>提交修订并重新校验</button></footer></section>}
      {!returnedForRevision && <footer><button type="button" className="danger-button" disabled={busy} onClick={() => onDecision(gate, { action: 'reject', comment })}>拒绝并终止</button>{gate !== 'H3' && <button type="button" className="secondary-button" disabled={busy || (gate === 'H4' && !comment.trim())} onClick={() => gate === 'H4' ? onDecision('H4', { action: 'revise', comment }) : openRevision()}>{gate === 'H4' ? '退回重写' : '退回并编辑'}</button>}{gate === 'H3' ? <button type="button" className="primary-button" disabled={busy || !allClaimsReady} onClick={submitH3}>{fixtureH3 ? '生成 plan-only 成果' : '提交结论授权'}</button> : <button type="button" className="primary-button" disabled={busy || !selectedCandidateReady} onClick={() => onDecision(gate, { action: 'approve', comment, ...(gate === 'H2' && selectedCandidateId ? { selectedCandidateId } : {}) })}>{gate === 'H4' ? '批准并封存' : '批准并继续'}</button>}</footer>}
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
  const writingFailed = run?.status === 'failed' && run.currentNodeId === 'scientific_writer'
  const preservedDraft = Boolean(writingFailed && run?.manuscript)

  return (
    <main className="bench-page bench-page--run">
      <header className="run-toolbar">
        <div>
          <p className="eyebrow">当前案例</p>
          <h1>{caseName}</h1>
          <p>{busy ? busyLabel : currentNode ? `HypoWeaver 当前：${currentNode.title}` : '两条流程可以分别启动。'}</p>
        </div>
        <div className="run-toolbar__actions">
          {runs.length > 0 && (
            <select aria-label="切换运行记录" value={run?.id ?? ''} disabled={busy} onChange={(event) => onSelectRun(event.target.value)}>
              {!run && <option value="">选择运行记录</option>}
              {runs.map((item) => <option value={item.id} key={item.id}>{item.caseName} · {statusText[item.status]}</option>)}
            </select>
          )}
          {run && <button type="button" className="quiet-button delete-run-button" disabled={busy} onClick={onDeleteRun}><Trash2 size={14} />删除记录</button>}
          <button type="button" className="quiet-button" onClick={onToggleCompare}>{compareOpen ? '收起基线' : '展开基线'}</button>
          <button type="button" className="quiet-button" onClick={onOpenSettings}><Settings2 size={14} />配置</button>
          <button type="button" className="primary-button" onClick={onNewResearch}>选择新案例</button>
        </div>
      </header>

      <section className="shared-input-strip shared-input-strip--run">
        <div><span>01</span><strong>案例一致</strong><small>{caseName}</small></div>
        <div><span>02</span><strong>模型一致</strong><small>使用同一套千问配置</small></div>
        <div><span>03</span><strong>结果隔离</strong><small>隐藏参考仅供最终评测</small></div>
      </section>

      {busy && <div className="bench-operation"><LoaderCircle size={16} className="spin" /><strong>{busyLabel}</strong></div>}

      <div className={`bench-grid ${compareOpen ? 'is-comparing' : ''}`}>
        <section className="bench-lane">
          <header className="bench-lane__header">
            <div><span className="bench-badge">HW</span><h2>HypoWeaver-Qwen</h2></div>
            <span className={`plain-status plain-status--${run?.status ?? 'idle'}`}>{run ? statusText[run.status] : '尚未启动'}</span>
          </header>
          {!run && <button type="button" className="primary-button lane-start" disabled={busy || !caseReady} onClick={() => void onStartHypoweaver()}>{caseReady ? '启动 HypoWeaver' : '请重新选择案例'}</button>}
          {run?.mode === 'fixture' && <p className="fixture-banner"><CircleAlert size={16} />流程演示不会生成实证结论。</p>}
          {run?.status === 'failed' && run.lastError && <div className="run-error-summary"><CircleAlert size={16} /><span><strong>失败原因</strong>{run.lastError}</span>{writingFailed && <button type="button" className="secondary-button" disabled={busy} onClick={() => void onRetryWriting()}><RotateCcw size={14} />调整后重试论文写作</button>}</div>}

          <div className="stage-flow stage-flow--compact">
            {definition.stages.map((stage) => {
              const state = run ? stageState(stage, definition, run) : 'pending'
              const attempts = attemptsByStage.get(stage.id) ?? []
              const StageIcon = state === 'complete' ? CheckCircle2 : state === 'active' ? LoaderCircle : state === 'problem' ? XCircle : Circle
              const stateLabel = !run ? '尚未开始' : state === 'problem'
                ? run.status === 'failed' ? '执行失败' : '需要处理'
                : { complete: '已完成', active: run.status === 'waiting_human' ? '等待审核' : '正在执行', pending: '尚未开始' }[state]
              return (
                <section className={`stage-card stage-card--${state}`} key={stage.id}>
                  <div className="stage-rail"><StageIcon size={18} className={state === 'active' && busy ? 'spin' : ''} /><span /></div>
                  <div className="stage-card__content">
                    <header><div><small>阶段 {stage.order}</small><h3>{stage.title}</h3></div><span>{stateLabel}</span></header>
                    {attempts.length > 0 && <div className="attempt-list">{attempts.map((attempt) => <StepDetails key={attempt.id} step={attempt} title={definition.nodes.find((node) => node.id === attempt.nodeId)?.title ?? attempt.nodeId} />)}</div>}
                    {!attempts.length && state === 'pending' && <p className="stage-pending-copy"><Clock3 size={14} />等待上游阶段</p>}
                    {run && (state === 'active' || state === 'problem') && <HumanReview key={`${run.id}:${run.version}:${run.currentGate}`} run={run} busy={busy} onDecision={onGateDecision} onSubmitRevision={onSubmitRevision} />}
                  </div>
                </section>
              )
            })}
          </div>

          {run && (run.status === 'completed' || preservedDraft) && <section className="result-summary">
            <div className="result-summary__heading">{preservedDraft ? <CircleAlert size={20} /> : <Check size={20} />}<div><h2>{preservedDraft ? '本次重生成失败，上一版已保留' : run.planOnly ? '研究计划已生成' : manuscriptState.complete ? '完整论文初稿已生成' : '论文初稿不完整'}</h2><p>{preservedDraft ? `仍显示论文初稿 v${run.manuscript?.version ?? '—'}；失败原因与本次尝试记录保留在上方。` : run.planOnly ? '本次没有统计结论。' : manuscriptState.complete ? `共 ${run.manuscript?.sections.length ?? 0} 节、${manuscriptState.characterCount.toLocaleString()} 字；实证表述受 H3 授权结论约束。` : '当前成果没有达到完整论文门槛，不将其冒充为已完成。'}</p></div>{!run.planOnly && <button type="button" className={manuscriptState.complete ? 'quiet-button' : 'primary-button'} disabled={busy} onClick={() => void onRetryWriting()}><RotateCcw size={14} />{preservedDraft ? '调整后重试论文写作' : manuscriptState.complete ? '重新生成论文' : '生成完整论文'}</button>}</div>
            {!run.planOnly && <>
              <div className="result-summary__meta"><span>执行状态 · {run.executionStatus}</span><span>科学状态 · {run.scientificStatus}</span></div>
              <div className="result-summary__claims">
                {run.claims.filter((claim) => claim.decision === 'approve' || claim.decision === 'downgrade').map((claim) => <article key={claim.id}>
                  <header><strong>{claimDecisionText[claim.decision!]}</strong><small>{claim.allowedStrength ?? '未指定结论强度'}</small></header>
                  <p>{claim.finalText ?? claim.text}</p>
                  <footer>证据：{claim.evidenceStatus ?? '未标注'} · 稳健性：{claim.robustnessStatus ?? '未标注'} · 支撑运行：{claim.supportingRuns.length}</footer>
                </article>)}
                {!run.claims.some((claim) => claim.decision === 'approve' || claim.decision === 'downgrade') && <p>本次没有获得 H3 授权的实证结论。</p>}
              </div>
              {run.manuscript && <article className="manuscript-draft">
                <header><FileText size={18} /><div><strong>论文初稿 · v{run.manuscript.version}</strong><small>{run.manuscript.auditResult === 'pass_with_no_critical_issues' ? '一致性审计通过' : '尚未通过一致性审计'}</small></div></header>
                <div className="manuscript-sections">
                  {run.manuscript.sections.filter((section) => section.status === 'generated').map((section, index) => <section key={section.id} id={`manuscript-${section.id}`}>
                    <p className="manuscript-section-index">{String(index + 1).padStart(2, '0')} · {section.id}</p>
                    <h3>{section.title}</h3>
                    <div className="manuscript-copy">{section.content}</div>
                    {(section.claimIds.length > 0 || section.runIds.length > 0) && <footer>Claim {section.claimIds.join('、') || '—'} · Run {section.runIds.join('、') || '—'}</footer>}
                  </section>)}
                </div>
                {run.manuscript.disclosures.length > 0 && <aside><strong>写作披露</strong><ul>{run.manuscript.disclosures.map((item) => <li key={item}>{item}</li>)}</ul></aside>}
              </article>}
            </>}
          </section>}
        </section>

        {compareOpen && <BaselineLane run={baselineRun} busy={busy} caseReady={caseReady} onStart={onStartBaseline} />}
      </div>

      {compareOpen && (
        <section className="comparison-panel">
          <header><div><p className="eyebrow">统一结果</p><h2>流程对比</h2></div><small>同一案例 · 同一模型 · 独立运行</small></header>
          <div className="comparison-table-wrap">
            <table className="comparison-table">
              <thead><tr><th>指标</th><th>HypoWeaver-Qwen</th><th>Agent Laboratory</th></tr></thead>
              <tbody>
                <tr><th>进度</th><td>{run ? `${completedStages}/${definition.stages.length} 阶段` : '未启动'}</td><td>{baselineRun ? baselineStatusText[baselineRun.status] : '未启动'}</td></tr>
                <tr><th>方法</th><td>{findMethodFamily(run)}</td><td>{baselineRun?.methodFamily ?? '待规划'}</td></tr>
                <tr><th>执行状态</th><td>{run?.executionStatus ?? 'not_started'}</td><td>{baselineRun?.executionStatus ?? 'not_started'}</td></tr>
                <tr><th>科学状态</th><td>{run?.scientificStatus ?? 'not_assessed'}</td><td>{baselineRun?.scientificStatus ?? 'not_assessed'}</td></tr>
                <tr><th>结论约束</th><td>{run ? `${run.claims.length} 条 Claim` : '尚未生成'}</td><td>无 ClaimLedger</td></tr>
                <tr><th>模型用量</th><td>未记录</td><td>{baselineRun ? `${baselineRun.llmCalls} 次 · ${(baselineRun.inputTokens + baselineRun.outputTokens).toLocaleString()} tokens` : '—'}</td></tr>
                <tr><th>运行时间</th><td>{run ? elapsedSeconds(run.createdAt, run.updatedAt) : '—'}</td><td>{baselineRun ? `${baselineRun.wallTimeSeconds.toFixed(1)} 秒` : '—'}</td></tr>
              </tbody>
            </table>
          </div>
        </section>
      )}
    </main>
  )
}
