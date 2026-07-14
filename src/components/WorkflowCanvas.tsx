import { useEffect, useMemo, useState } from 'react'
import {
  Background,
  BackgroundVariant,
  Controls,
  MarkerType,
  Panel,
  ReactFlow,
  type Edge,
  type Node,
  type ReactFlowInstance,
} from '@xyflow/react'
import type { RunSnapshot, StepAttempt, WorkflowDefinition } from '../runtime/types'
import { WorkflowNodeCard, type WorkflowNodeData } from './WorkflowNodeCard'

interface WorkflowCanvasProps {
  definition: WorkflowDefinition
  run: RunSnapshot | null
  selectedNodeId: string | null
  activeStageId: string
  onSelectNode: (nodeId: string) => void
}

const nodeTypes = { workflow: WorkflowNodeCard }

function latestAttempt(steps: StepAttempt[], nodeId: string): StepAttempt | undefined {
  return steps.filter((step) => step.nodeId === nodeId).sort((left, right) => right.attempt - left.attempt)[0]
}

export function WorkflowCanvas({ definition, run, selectedNodeId, activeStageId, onSelectNode }: WorkflowCanvasProps) {
  const [instance, setInstance] = useState<ReactFlowInstance<Node<WorkflowNodeData>, Edge> | null>(null)
  const stageTitleById = useMemo(
    () => new Map(definition.stages.map((stage) => [stage.id, stage.title])),
    [definition.stages],
  )
  const attemptByNode = useMemo(
    () => new Map(definition.nodes.map((node) => [node.id, latestAttempt(run?.steps ?? [], node.id)])),
    [definition.nodes, run?.steps],
  )

  const nodes = useMemo<Node<WorkflowNodeData>[]>(
    () => definition.nodes.map((node) => ({
      id: node.id,
      type: 'workflow',
      position: node.position,
      selected: node.id === selectedNodeId,
      data: {
        node,
        stageTitle: stageTitleById.get(node.stageId) ?? node.stageId,
        attempt: attemptByNode.get(node.id),
        current: run?.currentNodeId === node.id,
        onSelect: onSelectNode,
      },
    })),
    [attemptByNode, definition.nodes, onSelectNode, run?.currentNodeId, selectedNodeId, stageTitleById],
  )

  const edges = useMemo<Edge[]>(
    () => definition.edges.map((edge) => {
      const sourceAttempt = attemptByNode.get(edge.source)
      const targetAttempt = attemptByNode.get(edge.target)
      const skipped = sourceAttempt?.status === 'skipped' || targetAttempt?.status === 'skipped'
      const traversed = Boolean(sourceAttempt && targetAttempt && !skipped)
      return {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        sourceHandle: edge.sourceHandle,
        targetHandle: edge.targetHandle,
        type: 'smoothstep',
        animated: targetAttempt?.status === 'running',
        label: edge.label,
        markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
        className: `flow-edge${traversed ? ' flow-edge--traversed' : ''}${skipped ? ' flow-edge--skipped' : ''}`,
        style: { stroke: traversed ? '#0f9388' : '#a8b4bd', strokeWidth: traversed ? 1.9 : 1.2, opacity: skipped ? 0.35 : 1 },
        labelStyle: { fill: '#4b5f6e', fontSize: 10, fontWeight: 600 },
        labelBgStyle: { fill: '#ffffff', fillOpacity: 0.95 },
        labelBgPadding: [5, 3] as [number, number],
        labelBgBorderRadius: 4,
      }
    }),
    [attemptByNode, definition.edges],
  )

  const activeStage = definition.stages.find((stage) => stage.id === activeStageId) ?? definition.stages[0]

  useEffect(() => {
    if (!instance || !activeStage) return
    const timer = window.setTimeout(() => {
      instance.fitView({ nodes: activeStage.nodeIds.map((id) => ({ id })), padding: 0.22, duration: 420, maxZoom: 1.1 })
    }, 0)
    return () => window.clearTimeout(timer)
  }, [activeStage, definition.id, instance])

  return (
    <section className="workflow-canvas" aria-label="代码工作流画布">
      <ReactFlow<Node<WorkflowNodeData>, Edge>
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onInit={setInstance}
        onNodeClick={(_, node) => onSelectNode(node.id)}
        nodesFocusable={false}
        edgesFocusable={false}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        fitView
        minZoom={0.12}
        maxZoom={1.6}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="#d7dee4" />
        <Controls showInteractive={false} position="bottom-left" />
        <Panel position="top-left" className="canvas-stage-note">
          <span>阶段 {String(activeStage.order).padStart(2, '0')}</span>
          <strong>{activeStage.title}</strong>
          <p>{activeStage.description}</p>
        </Panel>
        <Panel position="bottom-center" className="canvas-legend" aria-label="状态图例">
          <span><i className="step-dot step-dot--running" />运行中</span>
          <span><i className="step-dot step-dot--waiting_human" />待审批</span>
          <span><i className="step-dot step-dot--succeeded" />完成</span>
          <span><i className="step-dot step-dot--skipped" />跳过</span>
        </Panel>
      </ReactFlow>
    </section>
  )
}
