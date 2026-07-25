import { ArrowRight, Clock3, Play } from 'lucide-react'
import type { RunStatus, RunSummary } from '../runtime/types'

interface RunHistoryListProps {
  runs: RunSummary[]
  busy: boolean
  onSelectRun: (runId: string) => void
  onNewResearch: () => void
}

const statusText: Record<RunStatus, string> = {
  created: '待启动',
  running: '运行中',
  waiting_human: '等待人工审核',
  blocked: '已阻塞',
  failed: '执行失败',
  completed: '已完成',
  stopped: '已终止',
  cancelled: '已取消',
}

function statusTone(status: RunStatus): string {
  if (status === 'completed') return 'is-done'
  if (status === 'waiting_human') return 'is-wait'
  if (status === 'failed' || status === 'blocked') return 'is-problem'
  return ''
}

function formatTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  const now = new Date()
  const sameDay = date.toDateString() === now.toDateString()
  const time = date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  if (sameDay) return `今天 ${time}`
  return `${date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })} ${time}`
}

export function RunHistoryList({ runs, busy, onSelectRun, onNewResearch }: RunHistoryListProps) {
  return (
    <main className="history">
      <header className="history__hero">
        <div>
          <p className="exec-eyebrow">运行记录</p>
          <h1 className="history__title">历史记录</h1>
          <p className="exec-sub">{runs.length ? `共 ${runs.length} 条 · 点击任一条查看它的完整执行流程` : '还没有运行记录。'}</p>
        </div>
        <button type="button" className="primary-button" onClick={onNewResearch}><Play size={15} />进行新任务</button>
      </header>

      {runs.length === 0 ? (
        <div className="history__empty">
          <Clock3 size={26} />
          <h2>暂无运行记录</h2>
          <p className="exec-sub">回到首页选择一个案例并开始，运行过的任务会在这里留档。</p>
          <button type="button" className="primary-button" onClick={onNewResearch}><Play size={15} />进行任务</button>
        </div>
      ) : (
        <div className="history__list" role="list">
          {runs.map((item) => {
            const width = item.status === 'completed' ? '100%' : item.status === 'created' ? '4%' : '60%'
            return (
              <button
                type="button"
                role="listitem"
                className="history-row"
                key={item.id}
                disabled={busy}
                onClick={() => onSelectRun(item.id)}
              >
                <span className="hr-main">
                  <strong>{item.caseName}</strong>
                  <small>{item.mode === 'fixture' ? '流程演示' : '真实研究'} · {formatTime(item.updatedAt)}</small>
                </span>
                {item.currentGate && <span className="hr-gate">{item.currentGate}</span>}
                <span className={`hr-status ${statusTone(item.status)}`}>{statusText[item.status]}</span>
                <span className="hr-bar"><i style={{ width }} /></span>
                <ArrowRight className="hr-arrow" size={16} aria-hidden="true" />
              </button>
            )
          })}
        </div>
      )}
    </main>
  )
}
