import { ArrowRight, FileUp, Settings2, SlidersHorizontal } from 'lucide-react'
import { useRef, useState, type CSSProperties } from 'react'
import type { CaseImportReport, RuntimeConfigStatus } from '../runtime/types'

type LaunchTarget = 'hypoweaver' | 'agent-laboratory'

interface ResearchBenchLauncherProps {
  config: RuntimeConfigStatus | null
  importReport: CaseImportReport | null
  busy: boolean
  busyLabel: string
  compareOpen: boolean
  onToggleCompare: () => void
  onImportCaseFolder: (files: File[], target: LaunchTarget) => Promise<void>
  onOpenAdvanced: () => void
  onOpenSettings: () => void
}

const flowSteps: Array<{ gate?: string; label: string }> = [
  { label: '读取案例与安全校验' },
  { gate: 'H1', label: '确认研究边界' },
  { label: '拆解假设 · 准备数据' },
  { label: '设计并独立审查方法' },
  { gate: 'H2', label: '冻结分析计划' },
  { label: '执行模型 · 独立复现' },
  { gate: 'H3', label: '逐条授权结论' },
  { label: '生成论文与图表' },
  { gate: 'H4', label: '封存成果包' },
]

const directoryInputAttributes = { webkitdirectory: '', directory: '' }

export function ResearchBenchLauncher({
  config,
  importReport,
  busy,
  busyLabel,
  compareOpen,
  onToggleCompare,
  onImportCaseFolder,
  onOpenAdvanced,
  onOpenSettings,
}: ResearchBenchLauncherProps) {
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [target, setTarget] = useState<LaunchTarget>('hypoweaver')
  const qwenReady = Boolean(config?.qwenApiKey.configured)

  function chooseFile(nextTarget: LaunchTarget) {
    setTarget(nextTarget)
    fileInputRef.current?.click()
  }

  return (
    <main className="start">
      <input
        ref={fileInputRef}
        className="file-input-hidden"
        type="file"
        multiple
        {...directoryInputAttributes}
        tabIndex={-1}
        aria-hidden="true"
        onChange={(event) => {
          const files = Array.from(event.currentTarget.files ?? [])
          event.currentTarget.value = ''
          if (files.length) void onImportCaseFolder(files, target)
        }}
      />

      <div className="start__grid">
        <section className="start__intro">
          <div className="start__intro-head">
            <p className="eyebrow">Research Bench · 进行任务</p>
            <h1 className="start__title">选择一个案例，开始验证</h1>
            <p className="start__lead">同一份输入可分别运行 HypoWeaver-Qwen 主流程与 Agent Laboratory 对照基线。隐藏参考材料自动隔离，流程先停在 H1 等待你确认。</p>
          </div>

          <div className="start__intro-action">
            <button type="button" className="start__drop" disabled={busy} onClick={() => chooseFile('hypoweaver')}>
              <span className="start__drop-icon"><FileUp size={24} /></span>
              <span className="start__drop-copy">
                <strong>{busy ? (busyLabel || '正在处理…') : '选择案例文件夹并启动'}</strong>
                <small>选择案例根目录，自动读取 case_profile.json、只上传主 CSV；论文与代码保持隔离。</small>
              </span>
            </button>
            {importReport && (
              <p className="start__import">
                已导入 {importReport.datasetFilename} · {importReport.rowCount.toLocaleString()} 行 × {importReport.columnCount} 列 · 隔离 {importReport.hiddenFileCount} 份隐藏材料 · 跳过 {importReport.excludedFileCount} 个其他文件
              </p>
            )}
          </div>

          <div className="start__intro-foot">
            <ul className="start__list">
              <li><span className="idx">01</span><div><strong>同一案例</strong><small>同一份 CSV 与安全案例说明</small></div></li>
              <li><span className="idx">02</span><div><strong>同一模型</strong><small>{qwenReady ? (config?.qwenModel.value ?? '千问已配置') : '千问尚未配置'}</small></div></li>
              <li><span className="idx">03</span><div><strong>结果隔离</strong><small>隐藏参考不进入运行流程</small></div></li>
            </ul>
            <footer className="start__footer">
              <button type="button" className="text-button" onClick={onOpenAdvanced}><SlidersHorizontal size={14} />手动填写研究输入</button>
              <button type="button" className="text-button" onClick={onOpenSettings}><Settings2 size={14} />模型与执行器配置</button>
            </footer>
          </div>
        </section>

        <aside className="start__flow">
          <div className="start__flow-head">
            <p className="eyebrow">工作流预览 · 启动后实时点亮</p>
            <button type="button" className="text-button" onClick={onToggleCompare}>{compareOpen ? '收起对照' : '加入 Agent Laboratory 对照'}</button>
          </div>
          <div className="flow-rail">
            <ol className="flow-steps">
              {flowSteps.map((step, index) => (
                <li key={step.gate ?? `step-${index}`} className={`flow-step ${step.gate ? 'is-gate' : 'is-minor'}`}>
                  <span className="flow-node" style={step.gate ? ({ '--gate-delay': `${((index / flowSteps.length) * 3.6).toFixed(2)}s` } as CSSProperties) : undefined}>{step.gate ?? ''}</span>
                  <div className="flow-copy"><strong>{step.label}</strong>{step.gate && <small>人工闸门</small>}</div>
                </li>
              ))}
            </ol>
          </div>
          {compareOpen && (
            <button type="button" className="start__baseline" disabled={busy || !qwenReady} onClick={() => chooseFile('agent-laboratory')}>
              <span className="bench-badge">AL</span>
              <span className="start__baseline-copy">
                <strong>{qwenReady ? '启动 Agent Laboratory 基线' : '请先配置千问后再启动基线'}</strong>
                <small>原生调度保持不变；科学有效性不会由基线自行判定。</small>
              </span>
              <ArrowRight size={16} aria-hidden="true" />
            </button>
          )}
        </aside>
      </div>
    </main>
  )
}
