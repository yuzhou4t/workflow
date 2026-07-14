import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ReactFlowProvider } from '@xyflow/react'
import { AppHeader } from './components/AppHeader'
import { CreateRunDialog } from './components/CreateRunDialog'
import { NodeInspector } from './components/NodeInspector'
import { RunTimeline } from './components/RunTimeline'
import { StageNavigator } from './components/StageNavigator'
import { WorkflowCanvas } from './components/WorkflowCanvas'
import { workflowApi } from './runtime/api'
import type {
  CreateRunInput,
  GateDecisionInput,
  RunSnapshot,
  RunSummary,
  StepAttempt,
  WorkflowDefinition,
} from './runtime/types'

type SidePanel = 'inspector' | 'timeline'
type MobilePanel = 'none' | 'stages' | 'side'

function latestAttempt(steps: StepAttempt[], nodeId: string | null): StepAttempt | null {
  if (!nodeId) return null
  return steps.filter((step) => step.nodeId === nodeId).sort((left, right) => right.attempt - left.attempt)[0] ?? null
}

export function App() {
  const [definition, setDefinition] = useState<WorkflowDefinition | null>(null)
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [run, setRun] = useState<RunSnapshot | null>(null)
  const [activeStageId, setActiveStageId] = useState('')
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [sidePanel, setSidePanel] = useState<SidePanel>('inspector')
  const [mobilePanel, setMobilePanel] = useState<MobilePanel>('none')
  const [createOpen, setCreateOpen] = useState(false)
  const [actionBusy, setActionBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const activeRunIdRef = useRef<string | null>(null)
  const mobileOpenerRef = useRef<HTMLElement | null>(null)
  const mobileDrawerWasOpenRef = useRef(false)

  const refreshRuns = useCallback(async () => {
    const nextRuns = await workflowApi.listRuns()
    setRuns(nextRuns)
    return nextRuns
  }, [])

  const refreshRun = useCallback(async (runId: string) => {
    const nextRun = await workflowApi.getRun(runId)
    if (activeRunIdRef.current === runId) setRun(nextRun)
    return nextRun
  }, [])

  useEffect(() => {
    activeRunIdRef.current = activeRunId
  }, [activeRunId])

  useEffect(() => {
    let cancelled = false
    Promise.all([workflowApi.getDefinition(), workflowApi.listRuns()])
      .then(([nextDefinition, nextRuns]) => {
        if (cancelled) return
        setDefinition(nextDefinition)
        setRuns(nextRuns)
        setActiveStageId(nextDefinition.stages[0].id)
        setSelectedNodeId(nextDefinition.nodes[0].id)
        if (nextRuns[0]) setActiveRunId(nextRuns[0].id)
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
    if (!activeRunId) {
      setRun(null)
      return
    }
    let cancelled = false
    refreshRun(activeRunId).catch((reason) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason))
    })
    return () => { cancelled = true }
  }, [activeRunId, refreshRun])

  useEffect(() => {
    if (!activeRunId || run?.status !== 'running' || run.allowedActions.includes('advance')) return
    const timer = window.setInterval(() => {
      refreshRun(activeRunId).catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
    }, 1500)
    return () => window.clearInterval(timer)
  }, [activeRunId, refreshRun, run?.allowedActions, run?.status])

  useEffect(() => {
    if (!definition || !run?.currentNodeId) return
    const node = definition.nodes.find((candidate) => candidate.id === run.currentNodeId)
    if (!node) return
    setSelectedNodeId(node.id)
    setActiveStageId(node.stageId)
  }, [definition, run?.currentNodeId])

  useEffect(() => {
    if (mobilePanel === 'none') return
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMobilePanel('none')
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [mobilePanel])

  useEffect(() => {
    const header = document.querySelector<HTMLElement>('.app-header')
    const canvas = document.querySelector<HTMLElement>('.canvas-region')
    const wasOpen = mobileDrawerWasOpenRef.current
    const isOpen = mobilePanel !== 'none' && window.matchMedia('(max-width: 820px)').matches
    if (isOpen && !wasOpen) mobileOpenerRef.current = document.activeElement as HTMLElement | null
    if (header) header.inert = isOpen
    if (canvas) canvas.inert = isOpen
    const focusTimer = window.setTimeout(() => {
      if (isOpen) document.querySelector<HTMLElement>('.is-mobile-open .mobile-close')?.focus()
      else if (wasOpen) mobileOpenerRef.current?.focus()
    }, 0)
    mobileDrawerWasOpenRef.current = isOpen
    return () => window.clearTimeout(focusTimer)
  }, [mobilePanel])

  const activeStage = useMemo(
    () => definition?.stages.find((stage) => stage.id === activeStageId) ?? definition?.stages[0],
    [activeStageId, definition],
  )
  const selectedNode = definition?.nodes.find((node) => node.id === selectedNodeId) ?? null
  const selectedAttempt = latestAttempt(run?.steps ?? [], selectedNodeId)

  function selectNode(nodeId: string) {
    const node = definition?.nodes.find((candidate) => candidate.id === nodeId)
    if (node) setActiveStageId(node.stageId)
    setSelectedNodeId(nodeId)
    setSidePanel('inspector')
    setMobilePanel('side')
  }

  function selectStage(stageId: string) {
    const stage = definition?.stages.find((candidate) => candidate.id === stageId)
    setActiveStageId(stageId)
    setSelectedNodeId(stage?.nodeIds[0] ?? null)
    setSidePanel('inspector')
    setMobilePanel('none')
  }

  async function performAction(action: () => Promise<RunSnapshot>) {
    setActionBusy(true)
    setError(null)
    try {
      const nextRun = await action()
      setRun(nextRun)
      setActiveRunId(nextRun.id)
      await refreshRuns()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setActionBusy(false)
    }
  }

  async function createRun(input: CreateRunInput) {
    await performAction(() => workflowApi.createRun(input))
    setCreateOpen(false)
  }

  async function advanceRun() {
    if (!activeRunId) return
    await performAction(() => workflowApi.advanceRun(activeRunId))
  }

  async function decideGate(gate: string, input: GateDecisionInput) {
    if (!run) return
    await performAction(() => workflowApi.decideGate(run, gate, input))
  }

  if (loading) {
    return (
      <main className="load-state">
        <span className="loading-mark" />
        <strong>正在连接代码工作流</strong>
        <p>读取版本化流程定义与持久化 Run 状态…</p>
      </main>
    )
  }

  if (!definition) {
    return (
      <main className="load-state load-state--error">
        <strong>无法连接工作流后端</strong>
        <p>{error ?? 'GET /api/v1/definitions/app-a 没有返回有效定义。'}</p>
        <button type="button" onClick={() => window.location.reload()}>重新连接</button>
      </main>
    )
  }

  if (!activeStage) return null

  return (
    <ReactFlowProvider>
      <div className="app-shell">
        <AppHeader
          runs={runs}
          activeRunId={activeRunId}
          runStatus={run?.status}
          allowedActions={run?.allowedActions ?? []}
          actionBusy={actionBusy}
          onChangeRun={setActiveRunId}
          onCreateRun={() => setCreateOpen(true)}
          onAdvance={advanceRun}
          onOpenStages={() => setMobilePanel('stages')}
          onOpenInspector={() => { setSidePanel('inspector'); setMobilePanel('side') }}
          onOpenTimeline={() => { setSidePanel('timeline'); setMobilePanel('side') }}
        />

        {error && <div className="load-warning" role="alert">{error}<button type="button" onClick={() => setError(null)}>关闭</button></div>}

        <div className="app-workspace">
          <StageNavigator
            definition={definition}
            run={run}
            activeStageId={activeStage.id}
            selectedNodeId={selectedNodeId}
            mobileOpen={mobilePanel === 'stages'}
            onSelectStage={selectStage}
            onSelectNode={selectNode}
            onCloseMobile={() => setMobilePanel('none')}
          />

          <main className="canvas-region">
            <div className="canvas-region__meta">
              <div>
                <span>{definition.name} · {definition.version}</span>
                <strong>{run ? run.caseName : '创建 Run 后开始执行'}</strong>
              </div>
              <dl>
                <div><dt>Run</dt><dd>{run?.id.slice(0, 8) ?? '—'}</dd></div>
                <div><dt>执行状态</dt><dd>{run?.executionStatus ?? 'not_started'}</dd></div>
                <div><dt>科学状态</dt><dd>{run?.scientificStatus ?? 'not_assessed'}</dd></div>
                <div><dt>模式</dt><dd>{run?.mode ?? '—'}</dd></div>
              </dl>
            </div>
            <WorkflowCanvas
              definition={definition}
              run={run}
              selectedNodeId={selectedNodeId}
              activeStageId={activeStage.id}
              onSelectNode={selectNode}
            />
          </main>

          {sidePanel === 'timeline' ? (
            <RunTimeline
              definition={definition}
              run={run}
              mobileOpen={mobilePanel === 'side'}
              onClose={() => { setSidePanel('inspector'); setMobilePanel('none') }}
              onSelectNode={selectNode}
            />
          ) : (
            <NodeInspector
              definition={definition}
              run={run}
              node={selectedNode}
              attempt={selectedAttempt}
              mobileOpen={mobilePanel === 'side'}
              actionBusy={actionBusy}
              onCloseMobile={() => setMobilePanel('none')}
              onGateDecision={decideGate}
            />
          )}
        </div>

        {mobilePanel !== 'none' && <button className="mobile-scrim" type="button" tabIndex={-1} onClick={() => setMobilePanel('none')} aria-label="关闭面板" />}
        <CreateRunDialog open={createOpen} busy={actionBusy} onClose={() => setCreateOpen(false)} onCreate={createRun} />
      </div>
    </ReactFlowProvider>
  )
}
