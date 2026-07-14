import { Check, Clipboard, FileCode2, ShieldCheck, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import type {
  ClaimDecision,
  GateAction,
  GateDecisionInput,
  RunSnapshot,
  StepAttempt,
  WorkflowDefinition,
  WorkflowNode,
} from '../runtime/types'

type InspectorTab = 'overview' | 'prompt' | 'input' | 'output' | 'log'

interface NodeInspectorProps {
  definition: WorkflowDefinition
  run: RunSnapshot | null
  node: WorkflowNode | null
  attempt: StepAttempt | null
  mobileOpen: boolean
  actionBusy: boolean
  onCloseMobile: () => void
  onGateDecision: (gate: string, input: GateDecisionInput) => Promise<void>
}

function JsonView({ value, emptyText }: { value: unknown; emptyText: string }) {
  if (value === null || value === undefined || value === '') return <p className="inspector-empty">{emptyText}</p>
  return <pre className="runtime-json">{typeof value === 'string' ? value : JSON.stringify(value, null, 2)}</pre>
}

function formatTime(value?: string): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

function gateForNode(node: WorkflowNode | null): 'H1' | 'H2' | 'H3' | null {
  if (!node || node.kind !== 'gate') return null
  const match = node.title.toUpperCase().match(/H[123]/)?.[0]
  return match === 'H1' || match === 'H2' || match === 'H3' ? match : null
}

export function NodeInspector({
  definition,
  run,
  node,
  attempt,
  mobileOpen,
  actionBusy,
  onCloseMobile,
  onGateDecision,
}: NodeInspectorProps) {
  const [tab, setTab] = useState<InspectorTab>('overview')
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const [comment, setComment] = useState('')
  const [claimDecisions, setClaimDecisions] = useState<Record<string, ClaimDecision>>({})
  const [claimTexts, setClaimTexts] = useState<Record<string, string>>({})
  const stage = definition.stages.find((candidate) => candidate.id === node?.stageId)
  const gate = gateForNode(node)
  const isActiveGate = Boolean(gate && run?.status === 'waiting_human' && run.currentGate === gate)
  const h3Gate = isActiveGate && gate === 'H3'
  const planOnlyH3 = h3Gate && Boolean(run?.planOnly || run?.mode === 'fixture')
  const prompts = attempt?.prompts.length ? attempt.prompts : node?.prompts ?? []

  useEffect(() => {
    setTab('overview')
    setComment('')
    setClaimDecisions({})
    setClaimTexts({})
  }, [attempt?.id, node?.id])

  const allClaimsDecided = useMemo(
    () => Boolean(run?.claims.length) && run?.claims.every((claim) => {
      const decision = claimDecisions[claim.id] ?? claim.decision
      return Boolean(decision) && (decision !== 'downgrade' || Boolean(claimTexts[claim.id]?.trim()))
    }),
    [claimDecisions, claimTexts, run?.claims],
  )

  async function copyPrompt(id: string, text: string) {
    await navigator.clipboard.writeText(text)
    setCopiedId(id)
    window.setTimeout(() => setCopiedId(null), 1400)
  }

  async function decide(action: GateAction) {
    if (!gate) return
    await onGateDecision(gate, { action, comment })
    setComment('')
  }

  async function submitClaimDecisions() {
    if (!gate || !run) return
    await onGateDecision(gate, {
      action: planOnlyH3 ? 'generate_plan_only' : 'approve',
      comment,
      claims: run.claims.map((claim) => ({
        claimId: claim.id,
        decision: claimDecisions[claim.id] ?? claim.decision ?? (planOnlyH3 ? 'hold' : 'reject'),
        finalText: claimTexts[claim.id]?.trim() || undefined,
        reason: comment,
      })),
    })
  }

  return (
    <aside className={`node-inspector${mobileOpen ? ' is-mobile-open' : ''}`} aria-label="步骤详情">
      <button type="button" className="mobile-close" onClick={onCloseMobile} aria-label="关闭步骤详情">
        <X size={18} />
      </button>
      {node ? (
        <>
          <header className="node-inspector__header">
            <div>
              <span>{stage?.title ?? node.stageId}</span>
              <h2>{node.title}</h2>
            </div>
            <div className="node-inspector__source">
              <FileCode2 size={14} aria-hidden="true" />
              <span>{definition.id} · {definition.version}</span>
            </div>
            <dl>
              <div><dt>节点类型</dt><dd>{node.type}</dd></div>
              <div><dt>运行状态</dt><dd>{attempt?.status ?? 'pending'}</dd></div>
              <div><dt>Attempt</dt><dd>{attempt?.attempt ?? '—'}</dd></div>
            </dl>
          </header>

          <div className="inspector-tabs" role="tablist" aria-label="步骤内容">
            {([
              ['overview', '概览'],
              ['prompt', '提示词'],
              ['input', '输入'],
              ['output', '输出'],
              ['log', '日志'],
            ] as Array<[InspectorTab, string]>).map(([value, label]) => (
              <button
                key={value}
                type="button"
                role="tab"
                aria-selected={tab === value}
                className={tab === value ? 'is-active' : ''}
                onClick={() => setTab(value)}
              >
                {label}
              </button>
            ))}
          </div>

          <div className="node-inspector__body">
            {tab === 'overview' && (
              <section className="step-overview">
                <p>{node.description || '该步骤由版本化代码工作流执行。'}</p>
                <dl>
                  <div><dt>开始时间</dt><dd>{formatTime(attempt?.startedAt)}</dd></div>
                  <div><dt>完成时间</dt><dd>{formatTime(attempt?.endedAt)}</dd></div>
                  <div><dt>当前 Run</dt><dd>{run?.id ?? '尚未创建'}</dd></div>
                </dl>
                {attempt?.error && <div className="step-error"><strong>执行错误</strong><p>{attempt.error}</p></div>}
                <div className="schema-pair">
                  <section><span>Input Schema</span><JsonView value={node.inputSchema} emptyText="未声明输入 Schema。" /></section>
                  <section><span>Output Schema</span><JsonView value={node.outputSchema} emptyText="未声明输出 Schema。" /></section>
                </div>
              </section>
            )}

            {tab === 'prompt' && (
              prompts.length ? (
                <div className="prompt-stack">
                  {prompts.map((prompt) => {
                    const text = prompt.rendered ?? prompt.template
                    return (
                      <section className="prompt-block" key={prompt.id}>
                        <header>
                          <span>{prompt.role}{prompt.rendered ? ' · 本次渲染' : ' · 模板'}</span>
                          <button type="button" onClick={() => copyPrompt(prompt.id, text)}>
                            {copiedId === prompt.id ? <Check size={14} /> : <Clipboard size={14} />}
                            {copiedId === prompt.id ? '已复制' : '复制'}
                          </button>
                        </header>
                        <pre>{text}</pre>
                      </section>
                    )
                  })}
                </div>
              ) : <p className="inspector-empty">该确定性步骤没有 LLM 提示词。</p>
            )}

            {tab === 'input' && <JsonView value={attempt?.input} emptyText="该步骤尚未执行，因此没有实际输入快照。" />}
            {tab === 'output' && <JsonView value={attempt?.output} emptyText="该步骤尚未产生输出。" />}
            {tab === 'log' && (
              attempt?.logs.length ? <pre className="runtime-log">{attempt.logs.join('\n')}</pre> : <p className="inspector-empty">该步骤没有运行日志。</p>
            )}
          </div>

          {isActiveGate && (
            <footer className="gate-decision-bar">
              <div className="gate-decision-bar__title"><ShieldCheck size={16} /><strong>{gate} 等待人工决定</strong></div>
              {h3Gate ? (
                <>
                  <p>{planOnlyH3
                    ? '该 Run 没有可授权的真实实证结果。每条 Claim 只能拒绝或暂缓，随后生成 plan-only 成果。'
                    : '逐条审查 Claim。所有 Claim 都作出决定后，才能提交 H3。'}</p>
                  <div className="claim-decision-list">
                    {run?.claims.map((claim) => (
                      <article key={claim.id}>
                        <span>{claim.text}</span>
                        <div>
                          {(planOnlyH3
                            ? (['reject', 'hold'] as ClaimDecision[])
                            : (['approve', 'downgrade', 'reject', 'hold'] as ClaimDecision[])
                          ).map((decision) => (
                            <button
                              key={decision}
                              type="button"
                              className={(claimDecisions[claim.id] ?? claim.decision) === decision ? 'is-selected' : ''}
                              onClick={() => setClaimDecisions((current) => ({ ...current, [claim.id]: decision }))}
                            >
                              {{ approve: '批准', downgrade: '降级', reject: '拒绝', hold: '暂缓' }[decision]}
                            </button>
                          ))}
                        </div>
                        {(claimDecisions[claim.id] ?? claim.decision) === 'downgrade' && (
                          <textarea
                            value={claimTexts[claim.id] ?? ''}
                            onChange={(event) => setClaimTexts((current) => ({ ...current, [claim.id]: event.target.value }))}
                            placeholder="填写降级后的审慎表述（必填）"
                          />
                        )}
                      </article>
                    ))}
                  </div>
                  <textarea value={comment} onChange={(event) => setComment(event.target.value)} placeholder="H3 审核说明（选填）" />
                  <button type="button" className="gate-primary" disabled={!allClaimsDecided || actionBusy} onClick={submitClaimDecisions}>
                    {planOnlyH3 ? '生成 plan-only 成果' : '提交逐条结论授权'}
                  </button>
                  <div className="gate-actions">
                    <button type="button" disabled={actionBusy} onClick={() => decide('revise')}>退回 ClaimLedger</button>
                    <button type="button" disabled={actionBusy} onClick={() => decide('reject')}>拒绝并终止</button>
                  </div>
                </>
              ) : (
                <>
                  <textarea value={comment} onChange={(event) => setComment(event.target.value)} placeholder="填写审批或退回说明" />
                  <div className="gate-actions">
                    <button type="button" disabled={actionBusy} onClick={() => decide('revise')}>退回修订</button>
                    <button type="button" disabled={actionBusy} onClick={() => decide('reject')}>拒绝终止</button>
                    <button type="button" className="gate-primary" disabled={actionBusy} onClick={() => decide('approve')}>批准并继续</button>
                  </div>
                </>
              )}
            </footer>
          )}
        </>
      ) : (
        <div className="inspector-placeholder">
          <FileCode2 size={24} />
          <h2>选择一个步骤</h2>
          <p>查看该次 Attempt 的实际提示词、输入、输出和日志。</p>
        </div>
      )}
    </aside>
  )
}
