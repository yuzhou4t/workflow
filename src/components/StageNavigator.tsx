import { X } from 'lucide-react'
import type { RunSnapshot, StepAttempt, StepStatus, WorkflowDefinition, WorkflowStage } from '../runtime/types'

interface StageNavigatorProps {
  definition: WorkflowDefinition
  run: RunSnapshot | null
  activeStageId: string
  selectedNodeId: string | null
  mobileOpen: boolean
  onSelectStage: (stageId: string) => void
  onSelectNode: (nodeId: string) => void
  onCloseMobile: () => void
}

type StageStatus = StepStatus | 'pending'

const statusLabel: Record<StageStatus, string> = {
  pending: '未开始',
  running: '运行中',
  waiting_human: '待审批',
  succeeded: '已完成',
  failed: '失败',
  blocked: '阻塞',
  skipped: '已跳过',
}

function latestAttempt(steps: StepAttempt[], nodeId: string): StepAttempt | undefined {
  return steps
    .filter((step) => step.nodeId === nodeId)
    .sort((left, right) => right.attempt - left.attempt)[0]
}

function stageStatus(stage: WorkflowStage, run: RunSnapshot | null): StageStatus {
  if (!run) return 'pending'
  const steps = stage.nodeIds.map((nodeId) => latestAttempt(run.steps, nodeId)).filter(Boolean) as StepAttempt[]
  if (steps.some((step) => step.status === 'waiting_human')) return 'waiting_human'
  if (steps.some((step) => step.status === 'running')) return 'running'
  if (steps.some((step) => step.status === 'failed')) return 'failed'
  if (steps.some((step) => step.status === 'blocked')) return 'blocked'
  if (!steps.length) return 'pending'
  if (steps.every((step) => step.status === 'skipped')) return 'skipped'
  if (steps.every((step) => step.status === 'succeeded' || step.status === 'skipped')) return 'succeeded'
  return 'pending'
}

export function StageNavigator({
  definition,
  run,
  activeStageId,
  selectedNodeId,
  mobileOpen,
  onSelectStage,
  onSelectNode,
  onCloseMobile,
}: StageNavigatorProps) {
  return (
    <aside className={`stage-nav${mobileOpen ? ' is-mobile-open' : ''}`} aria-label="阶段进度">
      <button type="button" className="mobile-close" onClick={onCloseMobile} aria-label="关闭阶段进度">
        <X size={18} />
      </button>
      <div className="stage-nav__header">
        <p>运行进度</p>
        <strong>{run?.caseName ?? '尚未创建 Run'}</strong>
        <span>{run ? `${run.mode === 'fixture' ? 'Fixture 演示' : '真实研究'} · ${run.id.slice(0, 8)}` : definition.version}</span>
      </div>

      <div className="stage-nav__scroll">
        <ol className="stage-list">
          {definition.stages.map((stage) => {
            const active = stage.id === activeStageId
            const status = stageStatus(stage, run)
            const stageNodes = definition.nodes.filter((node) => node.stageId === stage.id)
            return (
              <li key={stage.id}>
                <button
                  type="button"
                  className={`stage-button${active ? ' is-active' : ''}`}
                  onClick={() => onSelectStage(stage.id)}
                >
                  <span>{String(stage.order).padStart(2, '0')}</span>
                  <strong>{stage.title}</strong>
                  <small className={`stage-status stage-status--${status}`}>{statusLabel[status]}</small>
                </button>
                {active && (
                  <div className="stage-node-list">
                    {stageNodes.map((node) => {
                      const attempt = latestAttempt(run?.steps ?? [], node.id)
                      return (
                        <button
                          key={node.id}
                          type="button"
                          className={selectedNodeId === node.id ? 'is-selected' : ''}
                          onClick={() => {
                            onSelectNode(node.id)
                            onCloseMobile()
                          }}
                        >
                          <i className={`step-dot step-dot--${attempt?.status ?? 'pending'}`} />
                          <span>{node.title}</span>
                          {attempt && <small>#{attempt.attempt}</small>}
                        </button>
                      )
                    })}
                  </div>
                )}
              </li>
            )
          })}
        </ol>
      </div>

      <div className="stage-nav__truth-note">
        <strong>状态来自服务端 Run</strong>
        <span>刷新页面会重新读取持久化状态；人工闸门批准前，下游节点不会启动。</span>
      </div>
    </aside>
  )
}
