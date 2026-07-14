import { AlertTriangle, ArrowRight, CircleAlert, Lightbulb, X, type LucideIcon } from 'lucide-react'
import type { IssueSeverity, WorkflowDefinition } from '../domain/types'

interface IssuePanelProps {
  workflow: WorkflowDefinition
  mobileOpen: boolean
  onClose: () => void
  onSelectNode: (nodeId: string) => void
}

const severityLabel: Record<IssueSeverity, string> = {
  blocker: '正式执行阻塞',
  warning: '需要修正',
  design: '设计差异',
}

const severityIcon: Record<IssueSeverity, LucideIcon> = {
  blocker: CircleAlert,
  warning: AlertTriangle,
  design: Lightbulb,
}

export function IssuePanel({ workflow, mobileOpen, onClose, onSelectNode }: IssuePanelProps) {
  return (
    <aside className={`issue-panel${mobileOpen ? ' is-mobile-open' : ''}`} aria-label="落地检查">
      <button type="button" className="mobile-close" onClick={onClose} aria-label="关闭落地检查">
        <X size={18} />
      </button>
      <header>
        <span>YAML 静态审计</span>
        <h2>落地检查</h2>
        <p>这些问题来自当前导入文件，不代表目标架构已经实现。</p>
      </header>
      <div className="issue-panel__body">
        {(['blocker', 'warning', 'design'] as IssueSeverity[]).map((severity) => {
          const issues = workflow.issues.filter((issue) => issue.severity === severity)
          if (!issues.length) return null
          const Icon = severityIcon[severity]
          return (
            <section className={`issue-group issue-group--${severity}`} key={severity}>
              <div className="issue-group__heading">
                <Icon size={16} />
                <strong>{severityLabel[severity]}</strong>
                <span>{issues.length}</span>
              </div>
              {issues.map((issue) => (
                <article key={issue.id}>
                  <h3>{issue.title}</h3>
                  <p>{issue.detail}</p>
                  <button
                    type="button"
                    onClick={() => {
                      onSelectNode(issue.nodeIds[0])
                      onClose()
                    }}
                  >
                    定位节点 <ArrowRight size={13} />
                  </button>
                </article>
              ))}
            </section>
          )
        })}
      </div>
    </aside>
  )
}
