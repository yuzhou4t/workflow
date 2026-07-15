import { ChevronDown, ChevronRight, FileUp, Settings2 } from 'lucide-react'
import { useRef, useState } from 'react'
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

const hypoweaverStages = [
  '读取案例',
  'H1 确认研究边界',
  '拆解假设与数据',
  '设计并审查方法',
  'H2 冻结分析计划',
  '执行模型与诊断',
  'H3 授权结论',
  '生成研究成果',
]

const agentLaboratoryStages = [
  '形成研究计划',
  '准备分析数据',
  '运行实验',
  '解释结果',
  '生成研究报告',
]

const directoryInputAttributes = { webkitdirectory: '', directory: '' }

function PreviewLane({ name, badge, stages, note }: {
  name: string
  badge: string
  stages: string[]
  note: string
}) {
  return (
    <section className="bench-lane bench-lane--preview">
      <header className="bench-lane__header">
        <div><span className="bench-badge">{badge}</span><h2>{name}</h2></div>
        <small>等待案例</small>
      </header>
      <ol className="compact-flow">
        {stages.map((stage, index) => (
          <li key={stage}>
            <span>{index + 1}</span>
            <div><strong>{stage}</strong><small>尚未开始</small></div>
          </li>
        ))}
      </ol>
      <p className="lane-note">{note}</p>
    </section>
  )
}

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
  const executorReady = Boolean(config?.researchEngineUrl.value)

  function chooseFile(nextTarget: LaunchTarget) {
    setTarget(nextTarget)
    fileInputRef.current?.click()
  }

  return (
    <main className="bench-page bench-page--launcher">
      <header className="bench-hero">
        <div>
          <p className="eyebrow">Research Bench</p>
          <h1>选择一个案例，开始验证</h1>
          <p>同一份输入可以分别运行 HypoWeaver-Qwen 与 Agent Laboratory。</p>
        </div>
        <div className="bench-hero__actions">
          <button type="button" className="quiet-button" onClick={onToggleCompare}>
            {compareOpen ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
            {compareOpen ? '收起基线' : '展开基线'}
          </button>
          <button type="button" className="primary-button" disabled={busy} onClick={() => chooseFile('hypoweaver')}>
            <FileUp size={16} />{busy ? '正在处理…' : '选择案例文件夹并启动'}
          </button>
        </div>
      </header>

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

      <section className="shared-input-strip" aria-label="公平对比约束">
        <div><span>01</span><strong>同一案例</strong><small>同一份 CSV 与安全案例说明</small></div>
        <div><span>02</span><strong>同一模型</strong><small>{qwenReady ? config?.qwenModel.value ?? '千问已配置' : '千问尚未配置'}</small></div>
        <div><span>03</span><strong>结果隔离</strong><small>隐藏参考不进入运行流程</small></div>
        <button type="button" onClick={onOpenSettings}><Settings2 size={14} />配置</button>
      </section>

      {busy && <div className="bench-operation"><span className="loading-mark" /><strong>{busyLabel || '正在准备案例…'}</strong></div>}
      {importReport && (
        <p className="import-inline">
          已导入 {importReport.datasetFilename} · {importReport.rowCount.toLocaleString()} 行 × {importReport.columnCount} 列
          {` · 隔离 ${importReport.hiddenFileCount} 份隐藏材料 · 跳过 ${importReport.excludedFileCount} 个其他文件`}
        </p>
      )}

      <div className={`bench-grid ${compareOpen ? 'is-comparing' : ''}`}>
        <PreviewLane
          name="HypoWeaver-Qwen"
          badge="HW"
          stages={hypoweaverStages}
          note={executorReady ? '千问与研究执行器已就绪。' : '真实执行前需要完成千问与研究执行器配置。'}
        />
        {compareOpen && (
          <div className="baseline-preview-wrap">
            <PreviewLane
              name="Agent Laboratory"
              badge="AL"
              stages={agentLaboratoryStages}
              note="原生调度保持不变；科学有效性不会由基线自行判定。"
            />
            <button type="button" className="secondary-button baseline-launch" disabled={busy || !qwenReady} onClick={() => chooseFile('agent-laboratory')}>
              <FileUp size={15} />选择案例文件夹并启动基线
            </button>
          </div>
        )}
      </div>

      <footer className="launcher-footer">
        <button type="button" className="text-button" onClick={onOpenAdvanced}>手动填写研究输入</button>
        <p>选择案例根目录后会自动读取 case_profile.json、只上传主 CSV；论文与代码保持隔离，流程先停在 H1。</p>
      </footer>
    </main>
  )
}
