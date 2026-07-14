import { Clock3, X } from 'lucide-react'
import type { RunSnapshot, WorkflowDefinition } from '../runtime/types'

interface RunTimelineProps {
  definition: WorkflowDefinition
  run: RunSnapshot | null
  mobileOpen: boolean
  onClose: () => void
  onSelectNode: (nodeId: string) => void
}

function formatTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value || '—'
  return date.toLocaleTimeString('zh-CN', { hour12: false })
}

export function RunTimeline({ definition, run, mobileOpen, onClose, onSelectNode }: RunTimelineProps) {
  return (
    <aside className={`issue-panel run-timeline${mobileOpen ? ' is-mobile-open' : ''}`} aria-label="运行时间线">
      <button type="button" className="mobile-close" onClick={onClose} aria-label="关闭运行时间线">
        <X size={18} />
      </button>
      <header>
        <span>持久化 Run Events</span>
        <h2>运行时间线</h2>
        <p>事件来自服务端快照；页面刷新后仍可恢复。</p>
      </header>
      <div className="issue-panel__body timeline-list">
        {!run?.events.length ? (
          <div className="timeline-empty"><Clock3 size={22} /><strong>暂无运行事件</strong><span>创建并启动 Run 后，阶段变化会显示在这里。</span></div>
        ) : [...run.events].reverse().map((event) => {
          const node = definition.nodes.find((candidate) => candidate.id === event.nodeId)
          return (
            <article key={event.seq} className="timeline-event">
              <i className={`step-dot step-dot--${event.stepStatus ?? 'pending'}`} />
              <div>
                <header><strong>{event.type}</strong><time>{formatTime(event.timestamp)}</time></header>
                <p>{event.message || node?.title || '状态已更新'}</p>
                {node && <button type="button" onClick={() => { onSelectNode(node.id); onClose() }}>查看 {node.title}</button>}
              </div>
              <span>#{event.seq}</span>
            </article>
          )
        })}
      </div>
    </aside>
  )
}
