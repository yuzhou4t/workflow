import { Check, Clipboard, FileCode2, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { ContractField, WorkflowDefinition, WorkflowNode } from '../domain/types'

type InspectorTab = 'prompt' | 'input' | 'output'

interface NodeInspectorProps {
  workflow: WorkflowDefinition
  node: WorkflowNode | null
  mobileOpen: boolean
  onCloseMobile: () => void
  onShowIssues: () => void
}

function FieldList({ fields, emptyText }: { fields: ContractField[]; emptyText: string }) {
  if (!fields.length) return <p className="inspector-empty">{emptyText}</p>
  return (
    <div className="contract-list">
      {fields.map((field) => (
        <div className="contract-row" key={`${field.sourceNodeId ?? 'local'}-${field.name}`}>
          <div>
            <code>{field.name}</code>
            <span>{field.label}</span>
          </div>
          <div className="contract-row__meta">
            <span>{field.type}</span>
            {field.required && <strong>必填</strong>}
          </div>
        </div>
      ))}
    </div>
  )
}

export function NodeInspector({
  workflow,
  node,
  mobileOpen,
  onCloseMobile,
  onShowIssues,
}: NodeInspectorProps) {
  const [tab, setTab] = useState<InspectorTab>('prompt')
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const stage = workflow.stages.find((candidate) => candidate.id === node?.stageId)
  const issues = useMemo(
    () => workflow.issues.filter((issue) => node?.issueIds.includes(issue.id)),
    [node?.issueIds, workflow.issues],
  )

  async function copyPrompt(id: string, text: string) {
    await navigator.clipboard.writeText(text)
    setCopiedId(id)
    window.setTimeout(() => setCopiedId(null), 1400)
  }

  return (
    <aside className={`node-inspector${mobileOpen ? ' is-mobile-open' : ''}`} aria-label="节点检查器">
      <button type="button" className="mobile-close" onClick={onCloseMobile} aria-label="关闭节点详情">
        <X size={18} />
      </button>
      {node ? (
        <>
          <header className="node-inspector__header">
            <div>
              <span>{stage?.title ?? node.stageId}</span>
              <h2>{node.title}</h2>
            </div>
            <div className="node-inspector__source">
              <FileCode2 size={14} aria-hidden="true" />
              <span>{workflow.sourceFile}</span>
            </div>
            <dl>
              <div><dt>节点类型</dt><dd>{node.type}</dd></div>
              <div><dt>输入 / 输出</dt><dd>{node.inputs.length} / {node.outputs.length}</dd></div>
              <div><dt>落地检查</dt><dd>{issues.length}</dd></div>
            </dl>
          </header>

          <div className="inspector-tabs" role="tablist" aria-label="节点内容">
            {([
              ['prompt', '提示词'],
              ['input', '输入'],
              ['output', '输出'],
            ] as Array<[InspectorTab, string]>).map(([value, label]) => (
              <button
                key={value}
                type="button"
                role="tab"
                aria-selected={tab === value}
                className={tab === value ? 'is-active' : ''}
                onClick={() => setTab(value)}
              >
                {label}
              </button>
            ))}
          </div>

          <div className="node-inspector__body">
            {tab === 'prompt' && (
              node.prompts.length ? (
                <div className="prompt-stack">
                  {node.prompts.map((prompt) => (
                    <section className="prompt-block" key={prompt.id}>
                      <header>
                        <span>{prompt.role}</span>
                        <button type="button" onClick={() => copyPrompt(prompt.id, prompt.text)}>
                          {copiedId === prompt.id ? <Check size={14} /> : <Clipboard size={14} />}
                          {copiedId === prompt.id ? '已复制' : '复制'}
                        </button>
                      </header>
                      <pre>{prompt.text}</pre>
                    </section>
                  ))}
                </div>
              ) : (
                <p className="inspector-empty">该节点没有提示词；它通过确定性配置完成路由、聚合或输出。</p>
              )
            )}

            {tab === 'input' && (
              <section>
                <div className="inspector-section-heading">
                  <span>Input contract</span>
                  <strong>{node.inputs.length} 个字段或引用</strong>
                </div>
                <FieldList fields={node.inputs} emptyText="该节点没有显式输入字段。" />
              </section>
            )}

            {tab === 'output' && (
              <section>
                <div className="inspector-section-heading">
                  <span>Output contract</span>
                  <strong>{node.outputs.length} 个字段</strong>
                </div>
                <FieldList fields={node.outputs} emptyText="YAML 没有声明该节点的输出字段。" />
                {node.outputSchema !== null && (
                  <div className="schema-block">
                    <span>从 System Prompt 提取的目标 Schema</span>
                    <pre>{JSON.stringify(node.outputSchema, null, 2)}</pre>
                  </div>
                )}
              </section>
            )}
          </div>

          {issues.length > 0 && (
            <button className="node-issue-summary" type="button" onClick={onShowIssues}>
              <span>{issues.length} 项与此节点相关的落地检查</span>
              <strong>查看全部</strong>
            </button>
          )}
        </>
      ) : (
        <div className="inspector-placeholder">
          <FileCode2 size={24} />
          <h2>选择一个节点</h2>
          <p>点击画布或左侧节点列表，查看该阶段的提示词、输入引用和输出 Schema。</p>
        </div>
      )}
    </aside>
  )
}
