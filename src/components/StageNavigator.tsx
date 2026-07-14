import { Search } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { WorkflowDefinition } from '../domain/types'

interface StageNavigatorProps {
  workflow: WorkflowDefinition
  activeStageId: string
  selectedNodeId: string | null
  mobileOpen: boolean
  onSelectStage: (stageId: string) => void
  onSelectNode: (nodeId: string) => void
  onCloseMobile: () => void
}

export function StageNavigator({
  workflow,
  activeStageId,
  selectedNodeId,
  mobileOpen,
  onSelectStage,
  onSelectNode,
  onCloseMobile,
}: StageNavigatorProps) {
  const [query, setQuery] = useState('')
  const normalizedQuery = query.trim().toLowerCase()

  const filteredNodes = useMemo(
    () =>
      normalizedQuery
        ? workflow.nodes.filter((node) =>
            [node.title, node.type, ...node.prompts.map((prompt) => prompt.text)]
              .join('\n')
              .toLowerCase()
              .includes(normalizedQuery),
          )
        : [],
    [normalizedQuery, workflow.nodes],
  )

  function selectNode(nodeId: string) {
    const node = workflow.nodes.find((candidate) => candidate.id === nodeId)
    if (node) onSelectStage(node.stageId)
    onSelectNode(nodeId)
    onCloseMobile()
  }

  return (
    <aside className={`stage-nav${mobileOpen ? ' is-mobile-open' : ''}`} aria-label="阶段导航">
      <div className="stage-nav__header">
        <p>流程结构</p>
        <strong>{workflow.id === 'app-a' ? '研究闭环' : '独立评测'}</strong>
        <span>{workflow.stats.nodes} 节点 · {workflow.stats.edges} 连线</span>
      </div>

      <label className="node-search">
        <Search size={15} aria-hidden="true" />
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="搜索节点或提示词"
          aria-label="搜索节点或提示词"
        />
      </label>

      <div className="stage-nav__scroll">
        {normalizedQuery ? (
          <div className="search-results">
            <span>{filteredNodes.length} 个匹配节点</span>
            {filteredNodes.map((node) => (
              <button
                key={node.id}
                type="button"
                className={selectedNodeId === node.id ? 'is-selected' : ''}
                onClick={() => selectNode(node.id)}
              >
                <strong>{node.title}</strong>
                <small>{workflow.stages.find((stage) => stage.id === node.stageId)?.title}</small>
              </button>
            ))}
          </div>
        ) : (
          <ol className="stage-list">
            {workflow.stages.map((stage) => {
              const active = stage.id === activeStageId
              const stageNodes = workflow.nodes.filter((node) => node.stageId === stage.id)
              return (
                <li key={stage.id}>
                  <button
                    type="button"
                    className={`stage-button${active ? ' is-active' : ''}`}
                    onClick={() => onSelectStage(stage.id)}
                  >
                    <span>{String(stage.order).padStart(2, '0')}</span>
                    <strong>{stage.title}</strong>
                    <small>{stage.nodeIds.length}</small>
                  </button>
                  {active && (
                    <div className="stage-node-list">
                      {stageNodes.map((node) => (
                        <button
                          key={node.id}
                          type="button"
                          className={selectedNodeId === node.id ? 'is-selected' : ''}
                          onClick={() => selectNode(node.id)}
                        >
                          <span>{node.title}</span>
                          {node.issueIds.length > 0 && <i>{node.issueIds.length}</i>}
                        </button>
                      ))}
                    </div>
                  )}
                </li>
              )
            })}
          </ol>
        )}
      </div>

      <div className="stage-nav__truth-note">
        <strong>按 YAML 真实结构显示</strong>
        <span>{workflow.id === 'app-a' ? '方法与执行均为互斥路由，不是并行运行。' : '隐藏材料只在 App B 中读取。'}</span>
      </div>
    </aside>
  )
}
