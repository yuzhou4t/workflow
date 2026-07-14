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
import type { StepAttempt, WorkflowNode } from '../runtime/types'

export interface WorkflowNodeData extends Record<string, unknown> {
  node: WorkflowNode
  stageTitle: string
  attempt?: StepAttempt
  current: boolean
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

const statusLabel = {
  pending: '未开始',
  running: '运行中',
  waiting_human: '待审批',
  succeeded: '已完成',
  failed: '失败',
  blocked: '阻塞',
  skipped: '跳过',
} as const

export function WorkflowNodeCard({ data, selected }: NodeProps) {
  const { node, stageTitle, attempt, current, onSelect } = data as WorkflowNodeData
  const Icon = iconByKind[node.kind]
  const status = attempt?.status ?? 'pending'

  return (
    <article
      className={`workflow-node workflow-node--${node.kind} workflow-node--status-${status}${selected ? ' is-selected' : ''}${current ? ' is-current' : ''}`}
      aria-label={`${node.title}，${statusLabel[status]}`}
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
        <span className="workflow-node__icon" aria-hidden="true"><Icon size={15} /></span>
        <span className="workflow-node__stage">{stageTitle}</span>
        <i className={`step-dot step-dot--${status}`} title={statusLabel[status]} />
      </div>
      <strong>{node.title}</strong>
      <div className="workflow-node__runtime">
        <span>{statusLabel[status]}</span>
        {attempt && <small>Attempt {attempt.attempt}</small>}
      </div>
      {node.kind !== 'end' && <Handle id="source" type="source" position={Position.Right} />}
    </article>
  )
}
