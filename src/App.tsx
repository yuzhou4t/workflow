import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AppHeader, type AppView } from './components/AppHeader'
import { ExecutionWorkspace } from './components/ExecutionWorkspace'
import { PreflightPanel } from './components/PreflightPanel'
import { ResearchInputForm } from './components/ResearchInputForm'
import { SystemConfigPanel } from './components/SystemConfigPanel'
import { demoResearchDraft, emptyResearchDraft, preflightResearch, type ResearchDraft } from './data/researchDraft'
import { workflowApi } from './runtime/api'
import type { ConnectionTestResult, GateDecisionInput, RunSnapshot, RunSummary, RuntimeConfigStatus, RuntimeConfigUpdate, WorkflowDefinition } from './runtime/types'

function viewFromHash(): AppView {
  const value = window.location.hash.replace('#', '')
  return value === 'runs' || value === 'settings' ? value : 'new'
}

export function App() {
  const [view, setView] = useState<AppView>(() => viewFromHash())
  const [definition, setDefinition] = useState<WorkflowDefinition | null>(null)
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [run, setRun] = useState<RunSnapshot | null>(null)
  const [config, setConfig] = useState<RuntimeConfigStatus | null>(null)
  const [accessTokenPresent, setAccessTokenPresent] = useState(() => workflowApi.hasAccessToken())
  const [accessTokenVerified, setAccessTokenVerified] = useState(false)
  const [draft, setDraft] = useState<ResearchDraft>(() => emptyResearchDraft())
  const [showPreflight, setShowPreflight] = useState(false)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [busyLabel, setBusyLabel] = useState('')
  const [error, setError] = useState<string | null>(null)
  const runRequestRef = useRef(0)
  const runIdRef = useRef<string | null>(null)

  const refreshRuns = useCallback(async () => {
    const nextRuns = await workflowApi.listRuns()
    setRuns(nextRuns)
    return nextRuns
  }, [])

  const refreshConfig = useCallback(async () => {
    const nextConfig = await workflowApi.getRuntimeConfig()
    setConfig(nextConfig)
  }, [])

  useEffect(() => {
    let cancelled = false
    Promise.all([workflowApi.getDefinition(), workflowApi.getRuntimeConfig()])
      .then(([nextDefinition, nextConfig]) => {
        if (cancelled) return
        setDefinition(nextDefinition)
        setConfig(nextConfig)
      })
      .catch((reason) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    const syncHash = () => setView(viewFromHash())
    window.addEventListener('hashchange', syncHash)
    return () => window.removeEventListener('hashchange', syncHash)
  }, [])

  useEffect(() => {
    if (view !== 'runs') return
    let cancelled = false
    const requestId = ++runRequestRef.current
    setBusy(true)
    setBusyLabel('正在读取运行记录…')
    setError(null)
    workflowApi.listRuns()
      .then(async (nextRuns) => {
        if (cancelled || requestId !== runRequestRef.current) return
        setRuns(nextRuns)
        const preferredId = runIdRef.current && nextRuns.some((item) => item.id === runIdRef.current)
          ? runIdRef.current
          : nextRuns[0]?.id
        if (!preferredId) {
          setRun(null)
          return
        }
        const nextRun = await workflowApi.getRun(preferredId)
        if (cancelled || requestId !== runRequestRef.current) return
        runIdRef.current = nextRun.id
        setRun(nextRun)
      })
      .catch((reason) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason))
      })
      .finally(() => {
        if (!cancelled && requestId === runRequestRef.current) {
          setBusy(false)
          setBusyLabel('')
        }
      })
    return () => { cancelled = true }
  }, [view])

  const preflightItems = useMemo(
    () => preflightResearch(draft, config, accessTokenVerified),
    [accessTokenVerified, config, draft],
  )

  async function withBusy<T>(label: string, operation: () => Promise<T>): Promise<T | null> {
    setBusy(true)
    setBusyLabel(label)
    setError(null)
    try {
      return await operation()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
      return null
    } finally {
      setBusy(false)
      setBusyLabel('')
    }
  }

  async function startResearch() {
    const nextRun = await withBusy('正在接收研究任务并执行输入校验…', () => workflowApi.createRun({ mode: draft.mode, case: draft.case }))
    if (!nextRun) return
    runIdRef.current = nextRun.id
    setRun(nextRun)
    await refreshRuns()
    setShowPreflight(false)
    changeView('runs')
  }

  async function selectRun(runId: string) {
    const requestId = ++runRequestRef.current
    const nextRun = await withBusy('正在恢复持久化运行状态…', () => workflowApi.getRun(runId))
    if (nextRun && requestId === runRequestRef.current) {
      runIdRef.current = nextRun.id
      setRun(nextRun)
    }
  }

  async function decideGate(gate: string, input: GateDecisionInput) {
    if (!run) return
    const label = gate === 'H1' ? '正在确认研究边界并设计方法…' : gate === 'H2' ? '正在冻结研究合同并执行计划…' : '正在封存结论并生成成果…'
    const nextRun = await withBusy(label, () => workflowApi.decideGate(run, gate, input))
    if (!nextRun) return
    runIdRef.current = nextRun.id
    setRun(nextRun)
    await refreshRuns()
  }

  async function submitGateRevision(gate: 'H1' | 'H2', revision: unknown, comment: string) {
    if (!run) return
    setBusy(true)
    setBusyLabel(`正在提交 ${gate} 修订并重新校验…`)
    setError(null)
    let returnedRun = run
    try {
      if (run.status === 'waiting_human') {
        returnedRun = await workflowApi.decideGate(run, gate, { action: 'revise', comment })
        runIdRef.current = returnedRun.id
        setRun(returnedRun)
      }
      const nextRun = await workflowApi.submitRevision(returnedRun, gate, revision)
      runIdRef.current = nextRun.id
      setRun(nextRun)
      await refreshRuns()
    } catch (reason) {
      runIdRef.current = returnedRun.id
      setRun(returnedRun)
      setError(reason instanceof Error ? reason.message : String(reason))
      await refreshRuns().catch(() => undefined)
    } finally {
      setBusy(false)
      setBusyLabel('')
    }
  }

  async function saveConfig(input: RuntimeConfigUpdate): Promise<boolean> {
    const nextConfig = await withBusy('正在安全保存后端配置…', () => workflowApi.updateRuntimeConfig(input))
    if (!nextConfig) {
      setAccessTokenVerified(false)
      return false
    }
    setConfig(nextConfig)
    setAccessTokenVerified(workflowApi.hasAccessToken())
    return true
  }

  async function testConnection(target: ConnectionTestResult['target']) {
    const result = await withBusy(`正在测试${target === 'qwen' ? '千问模型' : '研究执行器'}连接…`, () => workflowApi.testRuntimeConnection(target))
    return result ?? { target, success: false, message: '连接测试未完成。' }
  }

  function changeView(nextView: AppView) {
    setView(nextView)
    window.history.replaceState(null, '', `#${nextView}`)
    if (nextView !== 'new') setShowPreflight(false)
  }

  if (loading) return <main className="load-state"><span className="loading-mark" /><strong>正在连接代码工作流</strong><p>读取流程定义、配置状态与持久化运行记录…</p></main>
  if (!definition) return <main className="load-state load-state--error"><strong>无法连接工作流后端</strong><p>{error ?? '后端没有返回有效流程定义。'}</p><button type="button" onClick={() => window.location.reload()}>重新连接</button></main>

  return (
    <div className="app-shell">
      <AppHeader view={view} config={config} onChangeView={changeView} />
      {error && <div className="error-banner" role="alert"><span>{error}</span><button type="button" onClick={() => setError(null)}>关闭</button></div>}
      {view === 'settings' && <SystemConfigPanel status={config} accessTokenPresent={accessTokenPresent} accessTokenVerified={accessTokenVerified} busy={busy} onRefresh={() => withBusy('正在重新读取配置状态…', refreshConfig).then(() => undefined)} onSetAccessToken={(token) => { workflowApi.setAccessToken(token); setAccessTokenPresent(workflowApi.hasAccessToken()); setAccessTokenVerified(false) }} onSave={saveConfig} onTest={testConnection} />}
      {view === 'new' && !showPreflight && <ResearchInputForm draft={draft} config={config} onChange={setDraft} onLoadDemo={() => setDraft(demoResearchDraft())} onOpenSettings={() => changeView('settings')} onCheck={() => setShowPreflight(true)} />}
      {view === 'new' && showPreflight && <PreflightPanel draft={draft} items={preflightItems} busy={busy} onBack={() => setShowPreflight(false)} onStart={startResearch} />}
      {view === 'runs' && <ExecutionWorkspace definition={definition} run={run} runs={runs} busy={busy} busyLabel={busyLabel} onSelectRun={selectRun} onNewResearch={() => changeView('new')} onGateDecision={decideGate} onSubmitRevision={submitGateRevision} />}
    </div>
  )
}
