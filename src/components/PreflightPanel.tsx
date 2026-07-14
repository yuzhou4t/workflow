import { ArrowLeft, CheckCircle2, CircleAlert, Play, TriangleAlert } from 'lucide-react'
import type { PreflightItem, ResearchDraft } from '../data/researchDraft'
import type { CaseImportReport } from '../runtime/types'

interface PreflightPanelProps {
  draft: ResearchDraft
  items: PreflightItem[]
  importReport?: CaseImportReport | null
  busy: boolean
  onBack: () => void
  onStart: () => void
}

const iconByLevel = {
  pass: CheckCircle2,
  warning: TriangleAlert,
  blocker: CircleAlert,
}

export function PreflightPanel({ draft, items, importReport, busy, onBack, onStart }: PreflightPanelProps) {
  const blockerCount = items.filter((item) => item.level === 'blocker').length
  const warningCount = items.filter((item) => item.level === 'warning').length

  return (
    <main className="page preflight-page">
      <header className="page-heading">
        <div><h1>开始前检查</h1><p>确认系统将使用什么输入、能做什么，以及哪些内容不会执行。</p></div>
        <button type="button" className="secondary-button" onClick={onBack}><ArrowLeft size={16} />返回修改</button>
      </header>
      <ol className="task-steps" aria-label="创建研究步骤">
        <li className="is-complete"><span>1</span><div><strong>服务配置</strong><small>配置状态已读取</small></div></li>
        <li className="is-complete"><span>2</span><div><strong>研究输入</strong><small>表单已保存</small></div></li>
        <li className="is-active"><span>3</span><div><strong>开始前检查</strong><small>核对阻断项</small></div></li>
        <li><span>4</span><div><strong>开始研究</strong><small>进入执行过程</small></div></li>
      </ol>

      <section className={blockerCount ? 'preflight-summary has-blockers' : 'preflight-summary'}>
        <div><strong>{blockerCount ? `${blockerCount} 个阻断项需要处理` : '输入已经可以开始'}</strong><p>{warningCount ? `另有 ${warningCount} 项提醒，不会阻止本次运行。` : '没有发现额外提醒。'}</p></div>
        <dl><div><dt>运行方式</dt><dd>{draft.mode === 'fixture' ? '流程演示' : '真实研究'}</dd></div><div><dt>案例</dt><dd>{draft.case.title || '未填写'}</dd></div><div><dt>假设数</dt><dd>{draft.case.hypotheses.length}</dd></div><div><dt>变量数</dt><dd>{draft.case.variables.filter((item) => item.name.trim()).length}</dd></div></dl>
      </section>

      {importReport && <section className="import-result-card" aria-label="案例导入结果">
        <div><strong>案例包已完成安全导入</strong><p>主流程只登记分析数据；隐藏参考材料没有进入研究输入。</p></div>
        <dl><div><dt>主数据</dt><dd>{importReport.datasetFilename}</dd></div><div><dt>数据规模</dt><dd>{importReport.rowCount.toLocaleString()} 行 × {importReport.columnCount} 列</dd></div><div><dt>已隔离</dt><dd>{importReport.hiddenFileCount} 个隐藏文件</dd></div><div><dt>已排除</dt><dd>{importReport.excludedFileCount} 个重复/无关文件</dd></div></dl>
        {importReport.reviewItems.length > 0 && <ul>{importReport.reviewItems.map((item) => <li key={item}>{item}</li>)}</ul>}
      </section>}

      <section className="preflight-list">
        {items.map((item) => {
          const Icon = iconByLevel[item.level]
          return <article className={`preflight-item preflight-item--${item.level}`} key={item.id}><Icon size={21} /><div><strong>{item.title}</strong><p>{item.detail}</p></div><span>{{ pass: '通过', warning: '提醒', blocker: '阻断' }[item.level]}</span></article>
        })}
      </section>

      <section className="run-contract-preview">
        <h2>点击开始后会发生什么</h2>
        <ol><li><span>1</span>服务端严格校验 CaseSubmission，并运行 Intake Agent。</li><li><span>2</span>到达 H1 后暂停，要求你确认研究边界。</li><li><span>3</span>批准后才会并行拆解假设、分析数据结构并设计方法。</li><li><span>4</span>H2 冻结分析计划后才允许进入执行器。</li><li><span>5</span>所有论文表述必须经过 H3 逐条授权。</li></ol>
        {draft.mode === 'fixture' && <p className="fixture-warning"><TriangleAlert size={17} />本次只验证工作流，不运行统计模型，也不会生成任何显著性或因果结论。</p>}
      </section>

      <footer className="sticky-form-actions">
        <div><strong>{blockerCount ? '请先返回处理阻断项' : '研究任务将在服务端持久化'}</strong><span>启动后可以刷新页面或从“运行记录”恢复。</span></div>
        <button type="button" className="primary-button" onClick={onStart} disabled={Boolean(blockerCount) || busy}><Play size={17} />{busy ? '正在创建并执行输入校验…' : '确认并开始分析'}</button>
      </footer>
    </main>
  )
}
