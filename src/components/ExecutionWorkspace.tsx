import { Check, CheckCircle2, ChevronDown, Circle, CircleAlert, Clock3, FileText, LoaderCircle, ShieldCheck, XCircle } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { ClaimDecision, GateDecisionInput, RunSnapshot, RunSummary, StepAttempt, WorkflowDefinition, WorkflowStage } from '../runtime/types'

interface ExecutionWorkspaceProps {
  definition: WorkflowDefinition
  run: RunSnapshot | null
  runs: RunSummary[]
  busy: boolean
  busyLabel: string
  onSelectRun: (runId: string) => void
  onNewResearch: () => void
  onGateDecision: (gate: string, input: GateDecisionInput) => Promise<void>
  onSubmitRevision: (gate: 'H1' | 'H2', revision: unknown, comment: string) => Promise<void>
}

const statusText = {
  created: '待启动', running: '运行中', waiting_human: '等待人工审核', blocked: '已阻塞',
  failed: '执行失败', completed: '已完成', stopped: '已终止', cancelled: '已取消',
}

function JsonBlock({ value, empty = '本步骤尚未产生内容。' }: { value: unknown; empty?: string }) {
  if (value === null || value === undefined || value === '') return <p className="technical-empty">{empty}</p>
  return <pre>{typeof value === 'string' ? value : JSON.stringify(value, null, 2)}</pre>
}

function formatTime(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
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
  const source = [...run.steps].reverse().find((step) => step.nodeId === nodeId && step.status === 'waiting_human')?.input
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
  const [comment, setComment] = useState('')
  const [decisions, setDecisions] = useState<Record<string, ClaimDecision>>({})
  const [finalTexts, setFinalTexts] = useState<Record<string, string>>({})
  const [showRevision, setShowRevision] = useState(returnedForRevision)
  const [revisionText, setRevisionText] = useState(() => gate === 'H1' || gate === 'H2' ? revisionSeed(run, gate) : '')
  const [revisionError, setRevisionError] = useState<string | null>(null)
  if (!gate || (run.status !== 'waiting_human' && !returnedForRevision)) return null
  const fixtureH3 = gate === 'H3' && (run.mode === 'fixture' || run.planOnly)
  const allClaimsReady = gate !== 'H3' || (Boolean(run.claims.length) && run.claims.every((claim) => {
    const decision = decisions[claim.id] ?? claim.decision
    return Boolean(decision) && (decision !== 'downgrade' || Boolean(finalTexts[claim.id]?.trim()))
  }))

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
      <header><ShieldCheck size={22} /><div><strong>{gate} · {returnedForRevision ? '请继续提交修订' : gate === 'H1' ? '请确认研究边界' : gate === 'H2' ? '请审核并冻结分析计划' : '请逐条授权结论'}</strong><p>{returnedForRevision ? '上一次“退回”已经记录在服务端。请修改结构化内容并重新提交，刷新页面后也可以从这里继续。' : gate === 'H1' ? '批准后系统才会拆解假设并设计方法。' : gate === 'H2' ? '批准后样本、变量、模型和诊断将进入不可静默修改的研究合同。' : 'Writer 只能读取本次明确授权的结论。'}</p></div></header>
      {gate === 'H3' && <div className="claim-review-list">{run.claims.map((claim) => <article key={claim.id}><p>{claim.text}</p><small>允许强度：{claim.allowedStrength ?? '未指定'}</small><div>{(fixtureH3 ? ['reject', 'hold'] : ['approve', 'downgrade', 'reject', 'hold']).map((decision) => <button type="button" key={decision} aria-pressed={(decisions[claim.id] ?? claim.decision) === decision} className={(decisions[claim.id] ?? claim.decision) === decision ? 'is-selected' : ''} onClick={() => setDecisions((current) => ({ ...current, [claim.id]: decision as ClaimDecision }))}>{{ approve: '批准', downgrade: '降级', reject: '拒绝', hold: '暂缓' }[decision]}</button>)}</div>{(decisions[claim.id] ?? claim.decision) === 'downgrade' && <textarea value={finalTexts[claim.id] ?? ''} onChange={(event) => setFinalTexts((current) => ({ ...current, [claim.id]: event.target.value }))} placeholder="填写降级后的审慎表述" />}</article>)}</div>}
      {fixtureH3 && <p className="fixture-warning">本次没有真实实证结果，每条 Claim 只能拒绝或暂缓；提交后仅生成研究计划。</p>}
      {!returnedForRevision && <label>审核说明<textarea rows={3} value={comment} onChange={(event) => setComment(event.target.value)} placeholder="记录批准或拒绝理由（选填）" /></label>}
      {showRevision && (gate === 'H1' || gate === 'H2') && <section className="revision-editor"><header><div><strong>{gate} 结构化修订</strong><p>{gate === 'H1' ? '修改 CaseSubmission 后，系统会重新执行 Intake 与输入校验，再回到 H1。' : '修改 AnalysisPlan 后，系统会重新执行四类 Critic；plan_version 已自动加一。'}</p></div></header><textarea aria-label={`${gate} 结构化修订 JSON`} rows={18} spellCheck={false} value={revisionText} onChange={(event) => setRevisionText(event.target.value)} />{revisionError && <p className="revision-error" role="alert">{revisionError}</p>}<footer>{!returnedForRevision && <button type="button" className="secondary-button" disabled={busy} onClick={() => setShowRevision(false)}>取消修订</button>}<button type="button" className="primary-button" disabled={busy} onClick={submitRevision}>提交修订并重新校验</button></footer></section>}
      {!returnedForRevision && <footer><button type="button" className="danger-button" disabled={busy} onClick={() => onDecision(gate, { action: 'reject', comment })}>拒绝并终止</button>{gate !== 'H3' && <button type="button" className="secondary-button" disabled={busy} onClick={openRevision}>退回并编辑</button>}{gate === 'H3' ? <button type="button" className="primary-button" disabled={busy || !allClaimsReady} onClick={submitH3}>{fixtureH3 ? '生成 plan-only 成果' : '提交结论授权'}</button> : <button type="button" className="primary-button" disabled={busy} onClick={() => onDecision(gate, { action: 'approve', comment })}>批准并继续</button>}</footer>}
    </section>
  )
}

export function ExecutionWorkspace({ definition, run, runs, busy, busyLabel, onSelectRun, onNewResearch, onGateDecision, onSubmitRevision }: ExecutionWorkspaceProps) {
  const attemptsByStage = useMemo(() => new Map(definition.stages.map((stage) => [stage.id, run?.steps.filter((step) => stage.nodeIds.includes(step.nodeId)) ?? []])), [definition.stages, run?.steps])
  if (!run) return <main className="page empty-runs"><FileText size={32} /><h1>还没有研究运行</h1><p>从详细研究输入开始，完成检查后启动第一条链路。</p><button type="button" className="primary-button" onClick={onNewResearch}>新建研究</button></main>

  const completedStages = definition.stages.filter((stage) => stageState(stage, definition, run) === 'complete').length
  const currentNode = definition.nodes.find((node) => node.id === run.currentNodeId)

  return (
    <main className="runs-layout">
      <aside className="run-history"><header><strong>运行记录</strong><button type="button" onClick={onNewResearch} disabled={busy}>新建</button></header>{runs.map((item) => <button type="button" disabled={busy} className={item.id === run.id ? 'is-active' : ''} key={item.id} onClick={() => onSelectRun(item.id)}><strong>{item.caseName}</strong><span>{statusText[item.status]} · {formatTime(item.updatedAt)}</span></button>)}</aside>
      <section className="execution-page">
        <header className="run-heading"><div><span className={`run-state run-state--${run.status}`}>{statusText[run.status]}</span><h1>{run.caseName}</h1><p>{busy ? busyLabel : currentNode ? `当前步骤：${currentNode.title}` : '运行状态已从服务端恢复。'}</p></div><dl><div><dt>已完成阶段</dt><dd>{completedStages}/{definition.stages.length}</dd></div><div><dt>执行状态</dt><dd>{run.executionStatus}</dd></div><div><dt>科学状态</dt><dd>{run.scientificStatus}</dd></div><div><dt>运行模式</dt><dd>{run.mode === 'fixture' ? '流程演示' : '真实研究'}</dd></div></dl></header>
        {busy && <div className="active-operation"><LoaderCircle size={19} className="spin" /><div><strong>{busyLabel}</strong><span>服务端正在生成结构化产物，完成后页面会自动更新。</span></div></div>}
        {run.mode === 'fixture' && <p className="fixture-banner"><CircleAlert size={18} /><strong>流程演示：</strong>不会产生系数、显著性、样本量或实证结论。</p>}

        <div className="stage-flow">
          {definition.stages.map((stage) => {
            const state = stageState(stage, definition, run)
            const attempts = attemptsByStage.get(stage.id) ?? []
            const StageIcon = state === 'complete' ? CheckCircle2 : state === 'active' ? LoaderCircle : state === 'problem' ? XCircle : Circle
            const stateLabel = state === 'problem'
              ? run.status === 'stopped' || run.status === 'cancelled' ? '已终止' : run.status === 'failed' ? '执行失败' : '需要处理'
              : { complete: '已完成', active: run.status === 'waiting_human' ? '等待你审核' : '正在执行', pending: '尚未开始' }[state]
            return <section className={`stage-card stage-card--${state}`} key={stage.id}><div className="stage-rail"><StageIcon size={21} className={state === 'active' && busy ? 'spin' : ''} /><span /></div><div className="stage-card__content"><header><div><small>阶段 {stage.order}</small><h2>{stage.title}</h2><p>{stage.description}</p></div><span>{stateLabel}</span></header>
              {attempts.length > 0 && <div className="attempt-list">{attempts.map((attempt) => <StepDetails key={attempt.id} step={attempt} title={definition.nodes.find((node) => node.id === attempt.nodeId)?.title ?? attempt.nodeId} />)}</div>}
              {state === 'pending' && <p className="stage-pending-copy"><Clock3 size={15} />上游完成并汇合后才会进入本阶段。</p>}
              {(state === 'active' || state === 'problem') && <HumanReview key={`${run.id}:${run.version}:${run.currentGate}`} run={run} busy={busy} onDecision={onGateDecision} onSubmitRevision={onSubmitRevision} />}
            </div></section>
          })}
        </div>

        {run.status === 'completed' && <section className="result-summary"><Check size={24} /><div><h2>{run.planOnly ? '研究计划已经生成' : '研究成果包已经生成'}</h2><p>{run.planOnly ? '本次没有执行实证模型，成果中不包含统计结论。' : '成果只包含 H3 已授权并可追溯到真实运行的结论。'}</p></div></section>}
      </section>
    </main>
  )
}
