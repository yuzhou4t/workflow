import { useRef } from 'react'
import { ArrowRight, Database, FileUp, FlaskConical, Play, Plus, Settings2, ShieldCheck, Trash2 } from 'lucide-react'
import type { ResearchDraft } from '../data/researchDraft'
import type { CaseImportReport, DataStructure, RuntimeConfigStatus, VariableRole } from '../runtime/types'

interface ResearchInputFormProps {
  draft: ResearchDraft
  config: RuntimeConfigStatus | null
  importReport: CaseImportReport | null
  busy: boolean
  onChange: (draft: ResearchDraft) => void
  onLoadDemo: () => void
  onImportCaseFile: (file: File) => Promise<void>
  onOpenSettings: () => void
  onCheck: () => void
}

const structureOptions: Array<[DataStructure, string]> = [
  ['panel', '面板数据'], ['cross_section', '截面数据'], ['time_series', '时间序列'],
  ['spatial_panel', '空间面板'], ['event', '事件数据'], ['unknown', '暂不确定'],
]

const roleOptions: Array<[VariableRole, string]> = [
  ['outcome', '结果变量'], ['treatment', '处理变量'], ['exposure', '核心解释变量'],
  ['mediator', '机制变量'], ['moderator', '调节变量'], ['control', '控制变量'],
  ['id', '个体主键'], ['time', '时间变量'], ['spatial_id', '空间主键'], ['event_date', '事件日期'], ['unknown', '待确定'],
]

export function ResearchInputForm({ draft, config, importReport, busy, onChange, onLoadDemo, onImportCaseFile, onOpenSettings, onCheck }: ResearchInputFormProps) {
  const fileInputRef = useRef<HTMLInputElement>(null)
  const updateCase = (patch: Partial<ResearchDraft['case']>) => onChange({ ...draft, case: { ...draft.case, ...patch } })
  const qwenReady = Boolean(config?.qwenApiKey.configured)
  const executorReady = Boolean(config?.researchEngineUrl.value)
  const canAutoStart = draft.mode === 'fixture' || (qwenReady && executorReady)

  return (
    <main className="page research-input-page">
      <header className="page-heading">
        <div><h1>创建一次假设验证</h1><p>先把研究问题、假设、变量和数据边界说明清楚，系统才会选择方法。</p></div>
        <button type="button" className="secondary-button" onClick={onLoadDemo}><FlaskConical size={16} />载入演示案例</button>
      </header>

      <ol className="task-steps" aria-label="创建研究步骤">
        <li className="is-complete"><span>1</span><div><strong>服务配置</strong><small>检查模型与执行器</small></div></li>
        <li className="is-active"><span>2</span><div><strong>研究输入</strong><small>填写假设与变量</small></div></li>
        <li><span>3</span><div><strong>开始前检查</strong><small>发现阻断项</small></div></li>
        <li><span>4</span><div><strong>开始研究</strong><small>进入执行过程</small></div></li>
      </ol>

      <section className="quick-import-card">
        <header><span><FileUp size={21} /></span><div><h2>推荐：直接选择分析数据</h2><p>点击按钮选择一个 CSV，系统会自动上传、登记数据、生成保守研究草稿，并在 H1 停下让你确认。</p></div></header>
        <div className="quick-import-flow" aria-label="一键导入过程"><span>选择运行方式</span><i>→</i><span>选择一个 CSV</span><i>→</i><span>自动登记并生成输入</span><i>→</i><span>启动至 H1</span></div>
        <div className="mode-selector" role="radiogroup" aria-label="运行方式">
          <button type="button" role="radio" aria-checked={draft.mode === 'fixture'} className={draft.mode === 'fixture' ? 'is-selected' : ''} onClick={() => onChange({ ...draft, mode: 'fixture' })}><strong>流程演示</strong><span>不需要 API，验证整条状态链路，不产生统计结论。</span></button>
          <button type="button" role="radio" aria-checked={draft.mode === 'research'} className={draft.mode === 'research' ? 'is-selected' : ''} onClick={() => onChange({ ...draft, mode: 'research' })}><strong>真实研究</strong><span>使用已配置的千问与 Python 执行器运行真实数据。</span></button>
        </div>
        <input ref={fileInputRef} className="file-input-hidden" type="file" accept=".csv,text/csv" tabIndex={-1} aria-hidden="true" onChange={(event) => { const file = event.currentTarget.files?.[0]; event.currentTarget.value = ''; if (file) void onImportCaseFile(file) }} />
        <div className="quick-import-actions"><div><ShieldCheck size={17} /><span><strong>盲测隔离默认开启</strong>只有你选择的 CSV 会上传；论文、附录和分析代码无需选择，也不会进入 App A。</span></div><button type="button" className="primary-button" disabled={busy} onClick={() => fileInputRef.current?.click()}><Play size={17} />{busy ? '正在上传并分析…' : canAutoStart ? '选择 CSV 并启动' : '选择 CSV 并检查配置'}</button></div>
        {importReport && <div className="quick-import-result"><strong>最近一次导入：</strong><span>{importReport.datasetFilename} · {importReport.rowCount.toLocaleString()} 行 × {importReport.columnCount} 列 · 未上传隐藏参考材料</span></div>}
      </section>

      <section className="service-strip">
        <div><span className={qwenReady ? 'status-dot is-ready' : 'status-dot'} /><div><strong>千问模型</strong><small>{qwenReady ? `${config?.qwenModel.value} · 已配置` : '真实研究前需要配置'}</small></div></div>
        <div><span className={executorReady ? 'status-dot is-ready' : 'status-dot'} /><div><strong>Python 执行器</strong><small>{executorReady ? config?.researchEngineUrl.value : '真实研究前需要配置'}</small></div></div>
        <button type="button" onClick={onOpenSettings}><Settings2 size={15} />前往配置</button>
      </section>

      <section className="form-section">
        <div className="section-heading"><span>01</span><div><h2>你要验证什么？</h2><p>这些内容会直接进入 Intake Agent，不会从论文结论中反推。</p></div></div>
        <div className="field-grid field-grid--two">
          <label>案例名称 <em>必填</em><input value={draft.case.title} onChange={(event) => updateCase({ title: event.target.value })} placeholder="例如：绿色金融试验区政策评估" /></label>
          <label>案例编号<input value={draft.case.caseId} onChange={(event) => updateCase({ caseId: event.target.value })} /></label>
        </div>
        <label>研究问题 <em>必填</em><textarea rows={3} value={draft.case.researchQuestion} onChange={(event) => updateCase({ researchQuestion: event.target.value })} placeholder="用一句可以被数据回答的问题描述研究目标。" /></label>
      </section>

      <section className="form-section">
        <div className="section-heading"><span>02</span><div><h2>待验证假设</h2><p>每条假设都需要可观察的方向；理论机制可以为空，但不会由系统事后编造。</p></div></div>
        <div className="repeat-list">
          {draft.case.hypotheses.map((hypothesis, index) => (
            <article className="hypothesis-row" key={`${hypothesis.hypothesisId}-${index}`}>
              <div className="repeat-list__title"><strong>{hypothesis.hypothesisId || `H${index + 1}`}</strong>{draft.case.hypotheses.length > 1 && <button type="button" aria-label="删除假设" onClick={() => updateCase({ hypotheses: draft.case.hypotheses.filter((_, itemIndex) => itemIndex !== index) })}><Trash2 size={15} /></button>}</div>
              <label>假设陈述 <em>必填</em><input value={hypothesis.statement} onChange={(event) => updateCase({ hypotheses: draft.case.hypotheses.map((item, itemIndex) => itemIndex === index ? { ...item, statement: event.target.value } : item) })} placeholder="例如：绿色金融政策促进企业绿色创新。" /></label>
              <div className="field-grid field-grid--two">
                <label>预期方向<select value={hypothesis.expectedDirection} onChange={(event) => updateCase({ hypotheses: draft.case.hypotheses.map((item, itemIndex) => itemIndex === index ? { ...item, expectedDirection: event.target.value as typeof hypothesis.expectedDirection } : item) })}><option value="positive">正向</option><option value="negative">负向</option><option value="nonlinear">非线性</option><option value="heterogeneous">异质性</option><option value="unspecified">不预设</option></select></label>
                <label>理论机制<input value={hypothesis.mechanism} onChange={(event) => updateCase({ hypotheses: draft.case.hypotheses.map((item, itemIndex) => itemIndex === index ? { ...item, mechanism: event.target.value } : item) })} placeholder="例如：缓解融资约束" /></label>
              </div>
            </article>
          ))}
        </div>
        <button type="button" className="inline-add" onClick={() => updateCase({ hypotheses: [...draft.case.hypotheses, { hypothesisId: `H${draft.case.hypotheses.length + 1}`, statement: '', expectedDirection: 'unspecified', mechanism: '' }] })}><Plus size={15} />新增假设</button>
      </section>

      <section className="form-section">
        <div className="section-heading"><span>03</span><div><h2>研究对象与样本</h2><p>数据层级决定可用方法；“暂不确定”会在方法路由前要求人工补充。</p></div></div>
        <div className="field-grid field-grid--three">
          <label>分析单位<input value={draft.case.unitOfAnalysis} onChange={(event) => updateCase({ unitOfAnalysis: event.target.value })} placeholder="企业—年度" /></label>
          <label>样本时间范围<input value={draft.case.samplePeriod} onChange={(event) => updateCase({ samplePeriod: event.target.value })} placeholder="2010—2024" /></label>
          <label>数据结构<select value={draft.case.dataStructureHint} onChange={(event) => updateCase({ dataStructureHint: event.target.value as DataStructure })}>{structureOptions.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
        </div>
      </section>

      <section className="form-section">
        <div className="section-heading"><span>04</span><div><h2>变量设计</h2><p>至少需要一个结果变量。面板研究还应明确个体主键与时间变量。</p></div></div>
        <div className="variable-table-wrap"><table className="variable-table"><thead><tr><th>变量名</th><th>中文名称</th><th>角色</th><th>定义/构造</th><th>来源</th><th /></tr></thead><tbody>
          {draft.case.variables.map((variable, index) => (
            <tr key={`${index}-${variable.role}`}>
              <td><input aria-label={`变量 ${index + 1} 名称`} value={variable.name} onChange={(event) => updateCase({ variables: draft.case.variables.map((item, itemIndex) => itemIndex === index ? { ...item, name: event.target.value } : item) })} placeholder="green_patent" /></td>
              <td><input aria-label={`变量 ${index + 1} 中文名称`} value={variable.label} onChange={(event) => updateCase({ variables: draft.case.variables.map((item, itemIndex) => itemIndex === index ? { ...item, label: event.target.value } : item) })} placeholder="绿色专利" /></td>
              <td><select aria-label={`变量 ${index + 1} 角色`} value={variable.role} onChange={(event) => updateCase({ variables: draft.case.variables.map((item, itemIndex) => itemIndex === index ? { ...item, role: event.target.value as VariableRole } : item) })}>{roleOptions.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></td>
              <td><input aria-label={`变量 ${index + 1} 定义`} value={variable.definition} onChange={(event) => updateCase({ variables: draft.case.variables.map((item, itemIndex) => itemIndex === index ? { ...item, definition: event.target.value } : item) })} /></td>
              <td><input aria-label={`变量 ${index + 1} 来源`} value={variable.source} onChange={(event) => updateCase({ variables: draft.case.variables.map((item, itemIndex) => itemIndex === index ? { ...item, source: event.target.value } : item) })} /></td>
              <td><button type="button" aria-label={`删除变量 ${index + 1}`} onClick={() => updateCase({ variables: draft.case.variables.filter((_, itemIndex) => itemIndex !== index) })}><Trash2 size={15} /></button></td>
            </tr>
          ))}
        </tbody></table></div>
        <button type="button" className="inline-add" onClick={() => updateCase({ variables: [...draft.case.variables, { name: '', label: '', role: 'control', definition: '', source: '' }] })}><Plus size={15} />新增变量</button>
      </section>

      <section className="form-section">
        <div className="section-heading"><span>05</span><div><h2>数据资产</h2><p>一键导入会自动计算哈希并登记本地数据；也可以手动填写已有执行器资产。</p></div></div>
        <p className="dataset-registration-note"><strong>推荐：</strong>使用页面上方“选择 CSV 并启动”。手动模式仅适用于已经在 Python 执行器侧登记、并已取得 Dataset ID、SHA256 和字节数的数据。</p>
        {!draft.case.datasetRefs.length ? <div className="dataset-empty"><Database size={22} /><div><strong>尚未登记数据资产</strong><span>{draft.mode === 'fixture' ? '流程演示可以继续；输出将明确标记为 plan-only。' : '真实研究会在开始前检查中被阻断。'}</span></div></div> : (
          <div className="repeat-list">{draft.case.datasetRefs.map((dataset, index) => <article className="dataset-row" key={`${dataset.datasetId}-${index}`}>
            <div className="field-grid field-grid--three"><label>Dataset ID<input value={dataset.datasetId} onChange={(event) => updateCase({ datasetRefs: draft.case.datasetRefs.map((item, itemIndex) => itemIndex === index ? { ...item, datasetId: event.target.value } : item) })} /></label><label>文件名<input value={dataset.filename} onChange={(event) => updateCase({ datasetRefs: draft.case.datasetRefs.map((item, itemIndex) => itemIndex === index ? { ...item, filename: event.target.value } : item) })} /></label><label>SHA256<input value={dataset.sha256} onChange={(event) => updateCase({ datasetRefs: draft.case.datasetRefs.map((item, itemIndex) => itemIndex === index ? { ...item, sha256: event.target.value } : item) })} placeholder="64 位十六进制哈希" /></label></div>
            <div className="field-grid field-grid--three"><label>数据角色<select value={dataset.role} onChange={(event) => updateCase({ datasetRefs: draft.case.datasetRefs.map((item, itemIndex) => itemIndex === index ? { ...item, role: event.target.value as typeof dataset.role } : item) })}><option value="main">主分析数据</option><option value="supplementary">补充数据</option></select></label><label>文件类型<input value={dataset.mimeType} onChange={(event) => updateCase({ datasetRefs: draft.case.datasetRefs.map((item, itemIndex) => itemIndex === index ? { ...item, mimeType: event.target.value } : item) })} /></label><label>文件字节数<input type="number" min="1" value={dataset.sizeBytes || ''} onChange={(event) => updateCase({ datasetRefs: draft.case.datasetRefs.map((item, itemIndex) => itemIndex === index ? { ...item, sizeBytes: Number(event.target.value) || 0 } : item) })} /></label></div>
            <button type="button" onClick={() => updateCase({ datasetRefs: draft.case.datasetRefs.filter((_, itemIndex) => itemIndex !== index) })}><Trash2 size={15} />移除</button>
          </article>)}</div>
        )}
        <button type="button" className="inline-add" onClick={() => updateCase({ datasetRefs: [...draft.case.datasetRefs, { datasetId: '', role: 'main', filename: '', mimeType: 'text/csv', sha256: '', sizeBytes: 0 }] })}><Plus size={15} />登记已有数据资产</button>
      </section>

      <section className="form-section">
        <div className="section-heading"><span>06</span><div><h2>客观事实与研究边界</h2><p>每行一项。不要在这里填写原论文方向、显著性或回归结论。</p></div></div>
        <div className="field-grid field-grid--two">
          <label>已知政策事实<textarea rows={5} value={draft.case.knownPolicyFacts.join('\n')} onChange={(event) => updateCase({ knownPolicyFacts: event.target.value.split('\n') })} placeholder="政策名称、实施时间、试点范围等客观事实" /></label>
          <label>研究约束<textarea rows={5} value={draft.case.constraints.join('\n')} onChange={(event) => updateCase({ constraints: event.target.value.split('\n') })} /></label>
        </div>
      </section>

      <footer className="sticky-form-actions">
        <div><strong>下一步不会立即调用模型</strong><span>系统先检查必填项、配置和可执行边界。</span></div>
        <button type="button" className="primary-button" onClick={onCheck}>检查输入并准备开始 <ArrowRight size={17} /></button>
      </footer>
    </main>
  )
}
