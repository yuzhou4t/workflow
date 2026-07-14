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
import type { WorkflowDefinition } from '../domain/types'
import { WorkflowNodeCard, type WorkflowNodeData } from './WorkflowNodeCard'

interface WorkflowCanvasProps {
  workflow: WorkflowDefinition
  selectedNodeId: string | null
  activeStageId: string
  onSelectNode: (nodeId: string) => void
}

const nodeTypes = { workflow: WorkflowNodeCard }

export function WorkflowCanvas({
  workflow,
  selectedNodeId,
  activeStageId,
  onSelectNode,
}: WorkflowCanvasProps) {
  const [instance, setInstance] = useState<ReactFlowInstance<Node<WorkflowNodeData>, Edge> | null>(null)

  const stageTitleById = useMemo(
    () => new Map(workflow.stages.map((stage) => [stage.id, stage.title])),
    [workflow.stages],
  )

  const nodes = useMemo<Node<WorkflowNodeData>[]>(
    () =>
      workflow.nodes.map((node) => ({
        id: node.id,
        type: 'workflow',
        position: node.position,
        selected: node.id === selectedNodeId,
        data: {
          node,
          stageTitle: stageTitleById.get(node.stageId) ?? node.stageId,
          onSelect: onSelectNode,
        },
      })),
    [onSelectNode, selectedNodeId, stageTitleById, workflow.nodes],
  )

  const edges = useMemo<Edge[]>(
    () =>
      workflow.edges.map((edge) => ({
        id: edge.id,
        source: edge.source,
        target: edge.target,
        sourceHandle: edge.sourceHandle,
        targetHandle: edge.targetHandle,
        type: 'smoothstep',
        animated: edge.animated,
        label: edge.label,
        markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
        className: edge.animated ? 'flow-edge flow-edge--parallel' : 'flow-edge',
        style: { stroke: edge.animated ? '#0f9388' : '#a8b4bd', strokeWidth: edge.animated ? 1.8 : 1.25 },
        labelStyle: { fill: '#4b5f6e', fontSize: 10, fontWeight: 600 },
        labelBgStyle: { fill: '#ffffff', fillOpacity: 0.95 },
        labelBgPadding: [5, 3] as [number, number],
        labelBgBorderRadius: 4,
      })),
    [workflow.edges],
  )

  const activeStage = workflow.stages.find((stage) => stage.id === activeStageId) ?? workflow.stages[0]

  useEffect(() => {
    if (!instance || !activeStage) return
    const timer = window.setTimeout(() => {
      instance.fitView({
        nodes: activeStage.nodeIds.map((id) => ({ id })),
        padding: 0.22,
        duration: 420,
        maxZoom: 1.15,
      })
    }, 0)
    return () => window.clearTimeout(timer)
  }, [activeStage, instance, workflow.id])

  return (
    <section className="workflow-canvas" aria-label="工作流画布">
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
        <Panel position="bottom-center" className="canvas-legend" aria-label="画布图例">
          <span><i className="legend-dot legend-dot--llm" />LLM</span>
          <span><i className="legend-dot legend-dot--code" />确定性节点</span>
          <span><i className="legend-dot legend-dot--gate" />人类闸门</span>
          <span><i className="legend-line legend-line--parallel" />并行</span>
          <span><i className="legend-line" />分支 / 汇合</span>
        </Panel>
      </ReactFlow>
    </section>
  )
}
