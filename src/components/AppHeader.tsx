import { AlertTriangle, Menu, PanelRight, Workflow } from 'lucide-react'
import type { WorkflowDefinition } from '../domain/types'

interface AppHeaderProps {
  workflows: WorkflowDefinition[]
  activeWorkflowId: WorkflowDefinition['id']
  issueCount: number
  onChangeWorkflow: (id: WorkflowDefinition['id']) => void
  onOpenStages: () => void
  onOpenInspector: () => void
  onOpenIssues: () => void
}

export function AppHeader({
  workflows,
  activeWorkflowId,
  issueCount,
  onChangeWorkflow,
  onOpenStages,
  onOpenInspector,
  onOpenIssues,
}: AppHeaderProps) {
  return (
    <header className="app-header">
      <div className="brand">
        <span className="brand__mark" aria-hidden="true"><Workflow size={19} /></span>
        <div>
          <strong>HypoWeaver-Qwen</strong>
          <span>工作流实验台</span>
        </div>
      </div>

      <nav className="workflow-switch" aria-label="选择工作流">
        {workflows.map((workflow) => (
          <button
            key={workflow.id}
            type="button"
            className={workflow.id === activeWorkflowId ? 'is-active' : ''}
            onClick={() => onChangeWorkflow(workflow.id)}
          >
            {workflow.id === 'app-a' ? '主研究 App A' : '盲测 App B'}
          </button>
        ))}
      </nav>

      <div className="app-header__actions">
        <button type="button" className="mobile-action" onClick={onOpenStages}>
          <Menu size={16} />阶段
        </button>
        <button type="button" className="mobile-action" onClick={onOpenInspector}>
          <PanelRight size={16} />详情
        </button>
        <button type="button" className="audit-action" onClick={onOpenIssues}>
          <AlertTriangle size={15} />
          落地检查
          <span>{issueCount}</span>
        </button>
      </div>
    </header>
  )
}
