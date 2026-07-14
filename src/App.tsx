import { useEffect, useMemo, useRef, useState } from 'react'
import { ReactFlowProvider } from '@xyflow/react'
import { AppHeader } from './components/AppHeader'
import { IssuePanel } from './components/IssuePanel'
import { NodeInspector } from './components/NodeInspector'
import { StageNavigator } from './components/StageNavigator'
import { WorkflowCanvas } from './components/WorkflowCanvas'
import { WORKFLOW_SOURCES } from './data/workflowConfig'
import { loadWorkflow } from './domain/parseDifyWorkflow'
import type { WorkflowDefinition } from './domain/types'

type SidePanel = 'inspector' | 'issues'
type MobilePanel = 'none' | 'stages' | 'side'

export function App() {
  const [workflows, setWorkflows] = useState<WorkflowDefinition[]>([])
  const [activeWorkflowId, setActiveWorkflowId] = useState<WorkflowDefinition['id']>('app-a')
  const [activeStageId, setActiveStageId] = useState('intake')
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>('16')
  const [sidePanel, setSidePanel] = useState<SidePanel>('inspector')
  const [mobilePanel, setMobilePanel] = useState<MobilePanel>('none')
  const [error, setError] = useState<string | null>(null)
  const [loadWarning, setLoadWarning] = useState<string | null>(null)
  const mobileOpenerRef = useRef<HTMLElement | null>(null)
  const mobileDrawerWasOpenRef = useRef(false)

  useEffect(() => {
    let cancelled = false
    Promise.allSettled(WORKFLOW_SOURCES.map(loadWorkflow)).then((results) => {
      if (cancelled) return
      const loaded = results.flatMap((result) => (result.status === 'fulfilled' ? [result.value] : []))
      const failures = results.flatMap((result, index) =>
        result.status === 'rejected'
          ? [`${WORKFLOW_SOURCES[index].shortLabel}：${result.reason instanceof Error ? result.reason.message : String(result.reason)}`]
          : [],
      )
      setWorkflows(loaded)
      if (!loaded.length) setError(failures.join('；') || '没有可用的工作流')
      else if (failures.length) setLoadWarning(failures.join('；'))
    })
    return () => {
      cancelled = true
    }
  }, [])

  const workflow = workflows.find((candidate) => candidate.id === activeWorkflowId) ?? workflows[0]
  const selectedNode = workflow?.nodes.find((node) => node.id === selectedNodeId) ?? null

  useEffect(() => {
    if (!workflow) return
    const defaultNodeId = workflow.id === 'app-a' ? '16' : '51'
    const defaultStageId = workflow.id === 'app-a' ? 'understanding' : 'blind-evaluation'
    setSelectedNodeId(defaultNodeId)
    setActiveStageId(defaultStageId)
    setSidePanel('inspector')
    setMobilePanel('none')
  }, [workflow?.id])

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
    () => workflow?.stages.find((stage) => stage.id === activeStageId) ?? workflow?.stages[0],
    [activeStageId, workflow],
  )

  function selectNode(nodeId: string) {
    const node = workflow?.nodes.find((candidate) => candidate.id === nodeId)
    if (node) setActiveStageId(node.stageId)
    setSelectedNodeId(nodeId)
    setSidePanel('inspector')
    setMobilePanel('side')
  }

  function selectStage(stageId: string) {
    const stage = workflow?.stages.find((candidate) => candidate.id === stageId)
    setActiveStageId(stageId)
    setSelectedNodeId(stage?.nodeIds[0] ?? null)
    setSidePanel('inspector')
    setMobilePanel('none')
  }

  if (error) {
    return (
      <main className="load-state load-state--error">
        <strong>工作流读取失败</strong>
        <p>{error}</p>
      </main>
    )
  }

  if (!workflow || !activeStage) {
    return (
      <main className="load-state">
        <span className="loading-mark" />
        <strong>正在解析 Dify 工作流</strong>
        <p>读取节点、连线、提示词与输入输出契约…</p>
      </main>
    )
  }

  return (
    <ReactFlowProvider>
      <div className="app-shell">
        <AppHeader
          workflows={workflows}
          activeWorkflowId={workflow.id}
          issueCount={workflow.issues.length}
          onChangeWorkflow={setActiveWorkflowId}
          onOpenStages={() => setMobilePanel('stages')}
          onOpenInspector={() => {
            setSidePanel('inspector')
            setMobilePanel('side')
          }}
          onOpenIssues={() => {
            setSidePanel('issues')
            setMobilePanel('side')
          }}
        />

        {loadWarning && <div className="load-warning" role="status">部分工作流未加载：{loadWarning}</div>}

        <div className="app-workspace">
          <StageNavigator
            workflow={workflow}
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
                <span>{workflow.name}</span>
                <strong>{workflow.sourceFile}</strong>
              </div>
              <dl>
                <div><dt>LLM</dt><dd>{workflow.stats.llm}</dd></div>
                <div><dt>闸门</dt><dd>{workflow.stats.gates}</dd></div>
                <div><dt>互斥路由</dt><dd>{workflow.stats.routers}</dd></div>
                <div><dt>汇合</dt><dd>{workflow.stats.merges}</dd></div>
              </dl>
            </div>
            <WorkflowCanvas
              key={workflow.id}
              workflow={workflow}
              selectedNodeId={selectedNodeId}
              activeStageId={activeStage.id}
              onSelectNode={selectNode}
            />
          </main>

          {sidePanel === 'issues' ? (
            <IssuePanel
              workflow={workflow}
              mobileOpen={mobilePanel === 'side'}
              onClose={() => {
                setSidePanel('inspector')
                setMobilePanel('none')
              }}
              onSelectNode={selectNode}
            />
          ) : (
            <NodeInspector
              workflow={workflow}
              node={selectedNode}
              mobileOpen={mobilePanel === 'side'}
              onCloseMobile={() => setMobilePanel('none')}
              onShowIssues={() => {
                setSidePanel('issues')
                setMobilePanel('side')
              }}
            />
          )}
        </div>
        {mobilePanel !== 'none' && <button className="mobile-scrim" type="button" tabIndex={-1} onClick={() => setMobilePanel('none')} aria-label="关闭面板" />}
      </div>
    </ReactFlowProvider>
  )
}
