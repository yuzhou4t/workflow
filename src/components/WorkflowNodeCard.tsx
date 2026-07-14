import {
  Bot,
  Braces,
  CircleStop,
  Code2,
  FileInput,
  GitBranch,
  GitMerge,
  Globe2,
  Play,
  ShieldCheck,
  type LucideIcon,
} from 'lucide-react'
import { Handle, Position, type NodeProps } from '@xyflow/react'
import type { WorkflowNode } from '../domain/types'

export interface WorkflowNodeData extends Record<string, unknown> {
  node: WorkflowNode
  stageTitle: string
  onSelect: (nodeId: string) => void
}

const iconByKind: Record<WorkflowNode['kind'], LucideIcon> = {
  start: Play,
  document: FileInput,
  llm: Bot,
  code: Code2,
  gate: ShieldCheck,
  router: GitBranch,
  merge: GitMerge,
  http: Globe2,
  end: CircleStop,
  other: Braces,
}

function sourceHandles(node: WorkflowNode): string[] {
  if (node.kind === 'router') {
    const cases = Array.isArray(node.rawData.cases) ? node.rawData.cases : []
    const handles = cases
      .map((item) => (item && typeof item === 'object' ? String((item as Record<string, unknown>).case_id ?? '') : ''))
      .filter(Boolean)
    return [...handles, 'false']
  }
  if (node.kind === 'gate') return ['submit']
  return ['source']
}

export function WorkflowNodeCard({ data, selected }: NodeProps) {
  const { node, stageTitle, onSelect } = data as WorkflowNodeData
  const Icon = iconByKind[node.kind]
  const handles = sourceHandles(node)

  return (
    <article
      className={`workflow-node workflow-node--${node.kind}${selected ? ' is-selected' : ''}`}
      aria-label={`${node.title}，${node.type}`}
      aria-pressed={selected}
      role="button"
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onSelect(node.id)
        }
      }}
    >
      {node.kind !== 'start' && <Handle id="target" type="target" position={Position.Left} />}
      <div className="workflow-node__topline">
        <span className="workflow-node__icon" aria-hidden="true">
          <Icon size={15} />
        </span>
        <span className="workflow-node__stage">{stageTitle}</span>
        {node.issueIds.length > 0 && (
          <span className="workflow-node__issue" title={`${node.issueIds.length} 项落地检查`}>
            {node.issueIds.length}
          </span>
        )}
      </div>
      <strong>{node.title}</strong>
      <span className="workflow-node__type">{node.type}</span>
      {node.kind !== 'end' &&
        handles.map((handle, index) => (
          <Handle
            key={handle}
            id={handle}
            type="source"
            position={Position.Right}
            style={{ top: `${((index + 1) / (handles.length + 1)) * 100}%` }}
          />
        ))}
    </article>
  )
}
