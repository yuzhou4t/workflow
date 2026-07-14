import { Clock3, Menu, PanelRight, Play, Plus, Workflow } from 'lucide-react'
import type { RunStatus, RunSummary } from '../runtime/types'

interface AppHeaderProps {
  runs: RunSummary[]
  activeRunId: string | null
  runStatus?: RunStatus
  allowedActions: string[]
  actionBusy: boolean
  onChangeRun: (runId: string) => void
  onCreateRun: () => void
  onAdvance: () => void
  onOpenStages: () => void
  onOpenInspector: () => void
  onOpenTimeline: () => void
}

const statusLabel: Record<RunStatus, string> = {
  created: '待启动',
  running: '运行中',
  waiting_human: '等待人工',
  blocked: '已阻塞',
  failed: '失败',
  completed: '已完成',
  stopped: '已停止',
  cancelled: '已取消',
}

export function AppHeader({
  runs,
  activeRunId,
  runStatus,
  allowedActions,
  actionBusy,
  onChangeRun,
  onCreateRun,
  onAdvance,
  onOpenStages,
  onOpenInspector,
  onOpenTimeline,
}: AppHeaderProps) {
  const canAdvance = allowedActions.includes('advance') || runStatus === 'created'

  return (
    <header className="app-header">
      <div className="brand">
        <span className="brand__mark" aria-hidden="true"><Workflow size={19} /></span>
        <div>
          <strong>HypoWeaver-Qwen</strong>
          <span>代码工作流运行台</span>
        </div>
      </div>

      <div className="run-switcher">
        <label htmlFor="run-select">当前 Run</label>
        <select
          id="run-select"
          value={activeRunId ?? ''}
          onChange={(event) => event.target.value && onChangeRun(event.target.value)}
          disabled={!runs.length}
        >
          {!runs.length && <option value="">尚未创建</option>}
          {runs.map((run) => (
            <option key={run.id} value={run.id}>
              {run.caseName} · {run.id.slice(0, 8)}
            </option>
          ))}
        </select>
        {runStatus && <span className={`run-status run-status--${runStatus}`}>{statusLabel[runStatus]}</span>}
      </div>

      <div className="app-header__actions">
        <button type="button" className="mobile-action" onClick={onOpenStages}>
          <Menu size={16} />阶段
        </button>
        <button type="button" className="mobile-action" onClick={onOpenInspector}>
          <PanelRight size={16} />详情
        </button>
        <button type="button" className="timeline-action" onClick={onOpenTimeline}>
          <Clock3 size={15} />时间线
        </button>
        {canAdvance && (
          <button type="button" className="run-action" onClick={onAdvance} disabled={actionBusy}>
            <Play size={15} />{runStatus === 'created' || allowedActions.includes('advance') ? '执行到下一闸门' : '继续运行'}
          </button>
        )}
        <button type="button" className="create-run-action" onClick={onCreateRun}>
          <Plus size={15} />新建 Run
        </button>
      </div>
    </header>
  )
}
