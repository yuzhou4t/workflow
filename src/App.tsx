import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AppHeader, type AppView } from './components/AppHeader'
import { ExecutionWorkspace } from './components/ExecutionWorkspace'
import { PreflightPanel } from './components/PreflightPanel'
import { ResearchBenchLauncher } from './components/ResearchBenchLauncher'
import { ResearchInputForm } from './components/ResearchInputForm'
import { SystemConfigPanel } from './components/SystemConfigPanel'
import { demoResearchDraft, emptyResearchDraft, preflightResearch, type ResearchDraft } from './data/researchDraft'
import { normalizeCaseSubmission, workflowApi } from './runtime/api'
import { selectCaseFolder, type CaseFolderSelection } from './runtime/caseFolder'
import type { BaselineRun, CaseImportReport, CaseSubmissionInput, ConnectionTestResult, GateDecisionInput, RunSnapshot, RunSummary, RuntimeConfigStatus, RuntimeConfigUpdate, WorkflowDefinition } from './runtime/types'

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
  const [importReport, setImportReport] = useState<CaseImportReport | null>(null)
  const [showPreflight, setShowPreflight] = useState(false)
  const [showAdvancedInput, setShowAdvancedInput] = useState(false)
  const [compareOpen, setCompareOpen] = useState(false)
  const [baselineRun, setBaselineRun] = useState<BaselineRun | null>(null)
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
          setBaselineRun(null)
          return
        }
        const nextRun = await workflowApi.getRun(preferredId)
        if (cancelled || requestId !== runRequestRef.current) return
        const nextBaselines = await workflowApi.listAgentLaboratoryRuns(nextRun.caseId)
        if (cancelled || requestId !== runRequestRef.current) return
        runIdRef.current = nextRun.id
        setRun(nextRun)
        setBaselineRun(nextBaselines[0] ?? null)
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

  useEffect(() => {
    if (!baselineRun || !['queued', 'running'].includes(baselineRun.status)) return
    let cancelled = false
    const timer = window.setTimeout(() => {
      workflowApi.getAgentLaboratoryRun(baselineRun.id)
        .then((nextRun) => { if (!cancelled) setBaselineRun(nextRun) })
        .catch((reason) => { if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason)) })
    }, 1500)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [baselineRun])

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

  async function startHypoweaver() {
    const blockers = preflightResearch(draft, config, accessTokenVerified).filter((item) => item.level === 'blocker')
    if (blockers.length) {
      setShowPreflight(true)
      changeView('new')
      return
    }
    await startResearch()
  }

  async function startBaseline(caseInput: CaseSubmissionInput = draft.case) {
    if (!caseInput.datasetRefs.length) {
      setError('当前页面没有可复用的数据文件，请重新选择 CSV。')
      return
    }
    if (!config?.qwenApiKey.configured) {
      setError('请先在“配置”中保存并测试千问 API。')
      return
    }
    const approved = window.confirm('Agent Laboratory 会在本机执行其生成的 Python 代码。仅应使用可信数据；是否启动本次基线运行？')
    if (!approved) return
    const nextRun = await withBusy('正在启动 Agent Laboratory…', () => workflowApi.startAgentLaboratory(caseInput))
    if (!nextRun) return
    setBaselineRun(nextRun)
    setCompareOpen(true)
    changeView('runs')
  }

  async function importCaseFile(file: File, target: 'hypoweaver' | 'agent-laboratory' = 'hypoweaver', folder?: CaseFolderSelection) {
    const imported = await withBusy(`正在上传并分析 ${file.name}…`, () => workflowApi.uploadCaseFile(file))
    if (!imported) return
    if (folder) {
      const supplementaryRefs = folder.supplementaryData.length
        ? await withBusy('正在登记空间权重矩阵…', () => Promise.all(folder.supplementaryData.map((asset) => workflowApi.uploadCaseAsset(asset))))
        : []
      if (supplementaryRefs === null) return
      const datasetRefs = [...imported.case.datasetRefs, ...supplementaryRefs]
      imported.report.hiddenFileCount = folder.hiddenFileCount
      imported.report.excludedFileCount = folder.excludedFileCount
      if (folder.caseProfile) {
        let profilePayload: unknown
        try {
          profilePayload = JSON.parse(await folder.caseProfile.text())
        } catch {
          setError('case_profile.json 不是有效的 JSON，已停止启动以避免使用错误研究定义。')
          return
        }
        const profile = normalizeCaseSubmission(profilePayload)
        imported.case = { ...profile, datasetRefs }
        imported.report.reviewItems.unshift('已读取 case_profile.json；数据引用由本次上传结果重新绑定。')
      } else {
        imported.case = { ...imported.case, datasetRefs }
      }
    }
    const importedDraft: ResearchDraft = { mode: 'research', case: imported.case }
    setDraft(importedDraft)
    setImportReport(imported.report)
    if (target === 'agent-laboratory') {
      await startBaseline(imported.case)
      return
    }
    const blockers = preflightResearch(importedDraft, config, accessTokenVerified)
      .filter((item) => item.level === 'blocker')
    if (blockers.length) {
      setShowPreflight(true)
      return
    }

    const nextRun = await withBusy('案例已安全导入，正在启动工作流并进入 H1…', () => workflowApi.createRun({ mode: importedDraft.mode, case: importedDraft.case }))
    if (!nextRun) return
    runIdRef.current = nextRun.id
    setRun(nextRun)
    await refreshRuns()
    setShowPreflight(false)
    changeView('runs')
  }

  async function importCaseFolder(files: File[], target: 'hypoweaver' | 'agent-laboratory') {
    try {
      const selection = selectCaseFolder(files)
      await importCaseFile(selection.mainData, target, selection)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  async function selectRun(runId: string) {
    const requestId = ++runRequestRef.current
    const restored = await withBusy('正在恢复持久化运行状态…', async () => {
      const nextRun = await workflowApi.getRun(runId)
      const nextBaselines = await workflowApi.listAgentLaboratoryRuns(nextRun.caseId)
      return { nextRun, baselineRun: nextBaselines[0] ?? null }
    })
    if (!restored || requestId !== runRequestRef.current) return
    runIdRef.current = restored.nextRun.id
    setRun(restored.nextRun)
    setBaselineRun(restored.baselineRun)
  }

  async function deleteRun() {
    if (!run) return
    const confirmed = window.confirm(`确定删除“${run.caseName}”这条运行记录吗？此操作不会删除案例数据文件。`)
    if (!confirmed) return
    const restored = await withBusy('正在删除运行记录…', async () => {
      await workflowApi.deleteRun(run.id)
      const nextRuns = await workflowApi.listRuns()
      const nextRun = nextRuns[0] ? await workflowApi.getRun(nextRuns[0].id) : null
      const nextBaselines = nextRun
        ? await workflowApi.listAgentLaboratoryRuns(nextRun.caseId)
        : []
      return { nextRuns, nextRun, baselineRun: nextBaselines[0] ?? null }
    })
    if (!restored) return
    setRuns(restored.nextRuns)
    runIdRef.current = restored.nextRun?.id ?? null
    setRun(restored.nextRun)
    setBaselineRun(restored.baselineRun)
  }

  async function decideGate(gate: string, input: GateDecisionInput) {
    if (!run) return
    const label = gate === 'H1'
      ? '正在确认研究边界并生成候选方案…'
      : gate === 'H2'
        ? '正在冻结所选研究合同、执行并独立复现…'
        : gate === 'H3'
          ? '正在按授权结论生成完整论文初稿…'
          : '正在封存 H4 已批准的最终初稿…'
    const nextRun = await withBusy(label, () => workflowApi.decideGate(run, gate, input))
    if (!nextRun) return
    runIdRef.current = nextRun.id
    setRun(nextRun)
    await refreshRuns()
  }

  async function retryWriting() {
    if (!run) return
    const nextRun = await withBusy('正在使用已封存的回归结果重新生成完整论文初稿…', () => workflowApi.retryWriting(run.id))
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
    if (nextView === 'new') setShowAdvancedInput(false)
    if (nextView !== 'new') setShowPreflight(false)
  }

  if (loading) return <main className="load-state"><span className="loading-mark" /><strong>正在连接代码工作流</strong><p>读取流程定义、配置状态与持久化运行记录…</p></main>
  if (!definition) return <main className="load-state load-state--error"><strong>无法连接工作流后端</strong><p>{error ?? '后端没有返回有效流程定义。'}</p><button type="button" onClick={() => window.location.reload()}>重新连接</button></main>

  return (
    <div className="app-shell">
      <AppHeader view={view} config={config} onChangeView={changeView} />
      {error && <div className="error-banner" role="alert"><span>{error}</span><button type="button" onClick={() => setError(null)}>关闭</button></div>}
      {view === 'settings' && <SystemConfigPanel status={config} accessTokenPresent={accessTokenPresent} accessTokenVerified={accessTokenVerified} busy={busy} onRefresh={() => withBusy('正在重新读取配置状态…', refreshConfig).then(() => undefined)} onSetAccessToken={(token) => { workflowApi.setAccessToken(token); setAccessTokenPresent(workflowApi.hasAccessToken()); setAccessTokenVerified(false) }} onSave={saveConfig} onTest={testConnection} />}
      {view === 'new' && !showPreflight && !showAdvancedInput && <ResearchBenchLauncher config={config} importReport={importReport} busy={busy} busyLabel={busyLabel} compareOpen={compareOpen} onToggleCompare={() => setCompareOpen((current) => !current)} onImportCaseFolder={importCaseFolder} onOpenAdvanced={() => setShowAdvancedInput(true)} onOpenSettings={() => changeView('settings')} />}
      {view === 'new' && !showPreflight && showAdvancedInput && <ResearchInputForm draft={draft} config={config} importReport={importReport} busy={busy} onChange={setDraft} onLoadDemo={() => { setDraft(demoResearchDraft()); setImportReport(null) }} onImportCaseFile={(file) => importCaseFile(file, 'hypoweaver')} onOpenSettings={() => changeView('settings')} onCheck={() => setShowPreflight(true)} />}
      {view === 'new' && showPreflight && <PreflightPanel draft={draft} items={preflightItems} importReport={importReport} busy={busy} onBack={() => setShowPreflight(false)} onStart={startResearch} />}
      {view === 'runs' && <ExecutionWorkspace definition={definition} run={run} runs={runs} baselineRun={baselineRun} compareOpen={compareOpen} caseReady={Boolean(draft.case.datasetRefs.length && (!run || draft.case.caseId === run.caseId) && (!baselineRun || draft.case.caseId === baselineRun.caseId))} busy={busy} busyLabel={busyLabel} onSelectRun={selectRun} onDeleteRun={() => void deleteRun()} onNewResearch={() => { setShowAdvancedInput(false); changeView('new') }} onToggleCompare={() => setCompareOpen((current) => !current)} onStartHypoweaver={startHypoweaver} onStartBaseline={() => startBaseline()} onOpenSettings={() => changeView('settings')} onGateDecision={decideGate} onSubmitRevision={submitGateRevision} onRetryWriting={retryWriting} />}
    </div>
  )
}
