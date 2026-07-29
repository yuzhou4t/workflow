import {
  ArrowLeft,
  ArrowRight,
  BookOpen,
  Check,
  CheckCircle2,
  ChevronRight,
  Circle,
  Clock3,
  Database,
  Pencil,
  FileCheck2,
  FolderKanban,
  GitBranch,
  Lightbulb,
  Network,
  Play,
  Plus,
  RotateCcw,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  Target,
  Upload,
  Users,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { MagicRings } from './MagicRings'
import { FaultyTerminalBackground } from './FaultyTerminalBackground'
import { ElectricBorder } from './ElectricBorder'
import { DotGrid } from './DotGrid'

const stages = [
  { id: 'case', label: '项目中心', short: '项目中心', icon: BookOpen },
  { id: 'task', label: '任务设置', short: '任务设置', icon: Target },
  { id: 'evidence', label: '证据处理', short: '证据处理', icon: Database },
  { id: 'gap', label: '空白', short: '图谱与空白', icon: Network },
  { id: 'hypothesis', label: '假设', short: '候选假设', icon: Lightbulb },
  { id: 'design', label: '设计', short: '研究设计', icon: Settings2 },
  { id: 'execute', label: '执行', short: '执行', icon: GitBranch },
  { id: 'review', label: '成果', short: '审稿与成果', icon: FileCheck2 },
] as const

type StageId = (typeof stages)[number]['id']

const evidenceTabs = ['文献', '政策', '数据', '方法'] as const
let introAnimationPlayed = false

const topicOptions = [
  ['绿色创新', 24],
  ['绿色转型/产业升级', 42],
  ['绿色信贷', 32],
  ['绿色债券', 29],
  ['ESG/信息披露', 18],
  ['碳金融/双碳工具', 12],
  ['政策监管/试验区', 14],
  ['银行与金融机构', 7],
  ['数字金融科技', 2],
  ['普惠金融/乡村振兴', 3],
  ['国际标准/比较', 3],
  ['综合绿色金融体系', 21],
] as const

const schoolOptions = [
  ['政府监管驱动派', 35, '从政策约束、监管执行与政府治理解释绿色金融行为。'],
  ['市场机制优化派', 9, '关注绿色金融工具如何改善资本配置与市场定价。'],
  ['ESG信息价值派', 16, '检验 ESG 信息披露是否降低信息不对称并产生定价价值。'],
  ['绿色金融抑制论', 5, '关注绿色约束可能带来的融资挤压、成本上升与转型负担。'],
  ['转型倒逼机制派', 17, '研究金融与监管压力如何迫使企业调整生产和投资行为。'],
  ['技术创新激励派', 22, '关注绿色金融通过融资、激励与治理机制促进绿色创新。'],
  ['漂绿行为批判派', 3, '比较绿色叙事与真实行动，识别策略性披露和绿洗风险。'],
  ['区域异质效应派', 13, '强调制度、市场化、监管与资源禀赋造成的地区差异。'],
  ['普惠-绿色融合派', 5, '研究普惠金融、乡村振兴与绿色发展目标的协同。'],
  ['银行风险权衡派', 15, '分析银行在环境风险、信贷收益和审慎监管之间的权衡。'],
  ['绿色溢价验证派', 8, '检验绿色资产、绿色债券和低碳企业是否存在价格溢价。'],
  ['气候金融工具派', 27, '研究碳市场、气候投融资及绿色金融工具的有效性。'],
  ['国际标准本地化派', 10, '比较国际绿色标准并讨论其在本土制度中的适配。'],
  ['绿色产业重构派', 16, '关注绿色金融对产业结构、能源结构和企业转型的影响。'],
  ['政企共谋批判派', 1, '识别政策执行中可能出现的策略性响应与政企激励扭曲。'],
  ['数字技术赋能派', 4, '研究金融科技和数字能力如何提升绿色识别与资源配置。'],
  ['社会认知影响派', 1, '关注公众认知、媒体传播与社会压力对绿色行为的影响。'],
] as const

const evidenceData = {
  文献: [
    { title: 'ESG 披露、融资约束与企业价值', meta: '管理世界 · 2023 · 已核验', tag: '核心证据', note: '解释信息不对称机制，使用企业与年份固定效应。' },
    { title: 'Corporate Greenwashing and Financing Costs', meta: 'Journal of Corporate Finance · 2024 · 已核验', tag: '相邻研究', note: '提供绿洗测量思路，但未比较叙事与真实行为偏离。' },
    { title: '绿色创新的政策驱动机制', meta: '经济研究 · 2022 · 已核验', tag: '机制证据', note: '支持绿色政策通过融资渠道影响企业创新。' },
  ],
  政策: [
    { title: '企业可持续披露准则——基本准则（试行）', meta: '财政部 · 2024-12', tag: '披露规则', note: '可形成披露规范变化的制度背景与时间节点。' },
    { title: '绿色金融改革创新试验区总体方案', meta: '人民银行等 · 2017-06', tag: '政策冲击', note: '适合构造地区层面的准自然实验。' },
    { title: '上市公司可持续发展报告指引', meta: '沪深北交易所 · 2024-04', tag: '监管证据', note: '规定气候与环境信息披露范围。' },
  ],
  数据: [
    { title: '上市公司年报与 ESG 报告', meta: '巨潮资讯 · 2007—2025', tag: '公开可得', note: '用于构造绿色叙事、承诺具体度和披露质量。' },
    { title: '绿色专利数据', meta: '国家知识产权局 · 公司—年度', tag: '可替代变量', note: '作为企业真实绿色行为与技术产出的代理变量。' },
    { title: '环境行政处罚', meta: '政府公开平台 · 公司—事件', tag: '获取有成本', note: '用于构造负向真实行为指标，需完成公司名称匹配。' },
  ],
  方法: [
    { title: '双向固定效应回归', meta: '企业 × 年份面板', tag: '基准方法', note: '检验绿色叙事与融资成本之间的条件相关关系。' },
    { title: '叙事—行为偏离指数', meta: '文本强度 − 真实行动标准分', tag: '变量构造', note: '把绿洗从主观标签转化为可复核的连续指标。' },
    { title: '政策冲击 DID', meta: '试验区 × 政策后', tag: '识别增强', note: '作为扩展设计，需要验证平行趋势与政策外溢。' },
  ],
}

const hypotheses = [
  {
    id: 'H-A',
    title: '绿色叙事—真实行为偏离会提高企业债务融资成本',
    mechanism: '叙事承诺升高 → 市场预期提高 → 真实行动不足被识别 → 信任折价 → 融资成本上升',
    data: '年报文本、绿色专利、环境处罚、财务报表',
    method: '双向固定效应 + 偏离指数 + 滞后变量',
    scores: [4.6, 4.2, 4.0, 4.4],
  },
  {
    id: 'H-B',
    title: '强制 ESG 披露降低了绿色叙事与真实绿色行为之间的偏离',
    mechanism: '披露规则强化 → 可比性上升 → 虚假承诺成本提高 → 叙事与行动趋于一致',
    data: '披露规则、年报文本、绿色专利、地区监管',
    method: '多期 DID + 事件研究 + 安慰剂检验',
    scores: [4.3, 3.8, 4.6, 4.1],
  },
  {
    id: 'H-C',
    title: '媒体关注强化了绿洗风险对融资成本的惩罚效应',
    mechanism: '媒体关注提高 → 信息扩散增强 → 绿洗暴露概率上升 → 债权人风险定价',
    data: '新闻文本、年报、专利、债务融资数据',
    method: '调节效应 + 固定效应 + 异质性分析',
    scores: [3.9, 4.4, 3.6, 4.0],
  },
]

const reviewers = [
  { role: '绿色金融专家', verdict: '建议通过', note: '理论机制成立，但应区分一般 ESG 披露与可验证的绿色承诺。', tone: 'good' },
  { role: '计量经济学专家', verdict: '修改后通过', note: '存在反向因果风险，建议使用滞后解释变量，并补充行业×年份固定效应。', tone: 'warn' },
  { role: '数据工程专家', verdict: '修改后通过', note: '环境处罚匹配成本较高，首轮可用绿色专利与环保投资形成行动指数。', tone: 'warn' },
  { role: '反方审稿人', verdict: '提出强反驳', note: '叙事—行为偏离可能来自投资周期差异，应使用两至三年的行动窗口。', tone: 'risk' },
]

const initialCases = [
  {
    id: 'GF-2026-0007',
    name: '绿色叙事、真实绿色行为与融资成本',
    topic: 'ESG/信息披露',
    status: '等待决策',
    stage: '候选假设',
    progress: 56,
    evidence: 27,
    hypotheses: 3,
    updated: '今天 10:18',
    next: '选择并冻结主假设',
    owner: '我的案件',
  },
  {
    id: 'GF-2026-0006',
    name: '绿色信贷政策与重污染企业绿色创新',
    topic: '绿色信贷',
    status: '执行中',
    stage: '稳健性检验',
    progress: 74,
    evidence: 41,
    hypotheses: 2,
    updated: '昨天 18:42',
    next: '等待 3 个并行节点完成',
    owner: '协作案件',
  },
  {
    id: 'GF-2026-0005',
    name: '绿色债券发行、认证质量与绿色溢价',
    topic: '绿色债券',
    status: '等待审稿',
    stage: '多智能体审稿',
    progress: 86,
    evidence: 33,
    hypotheses: 1,
    updated: '07-25 16:07',
    next: '处理计量专家的主要意见',
    owner: '我的案件',
  },
  {
    id: 'GF-2026-0004',
    name: '绿色金融试验区与区域产业低碳转型',
    topic: '政策监管/试验区',
    status: '已完成',
    stage: '成果封存',
    progress: 100,
    evidence: 52,
    hypotheses: 1,
    updated: '07-23 11:30',
    next: '查看或导出研究计划',
    owner: '协作案件',
  },
] as const

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="studio-metric"><span>{label}</span><strong>{value}</strong></div>
}

function BlurIntroText({ text, animate }: { text: string; animate: boolean }) {
  return (
    <span className={`blur-intro-text ${animate ? 'is-animating' : ''}`} aria-label={text}>
      {Array.from(text).map((character, index) => (
        <span aria-hidden="true" key={`${character}-${index}`} style={{ animationDelay: `${index * 55}ms` }}>
          {character}
        </span>
      ))}
    </span>
  )
}

function CircularIntroText({ text }: { text: string }) {
  const characters = Array.from(text)
  return (
    <span className="circular-intro-text" aria-hidden="true">
      <span className="circular-intro-text__rotor">
        {characters.map((character, index) => (
          <span
            key={`${character}-${index}`}
            style={{ transform: `rotate(${index * (360 / characters.length)}deg) translateY(calc(-1 * var(--circular-radius)))` }}
          >
            {character}
          </span>
        ))}
      </span>
    </span>
  )
}

export function ResearchCaseStudio() {
  const [stage, setStage] = useState<StageId>(() => {
    const requested = new URLSearchParams(window.location.search).get('stage')
    return stages.some((item) => item.id === requested) ? requested as StageId : 'case'
  })
  const [activeEvidence, setActiveEvidence] = useState<(typeof evidenceTabs)[number]>('文献')
  const [researchTopic, setResearchTopic] = useState<(typeof topicOptions)[number][0]>('ESG/信息披露')
  const [researchSchool, setResearchSchool] = useState<(typeof schoolOptions)[number][0]>('漂绿行为批判派')
  const [researchQuestion, setResearchQuestion] = useState('企业绿色叙事与真实绿色行为之间的偏离，是否会提高债权人感知风险并推高债务融资成本？')
  const [literatureFile, setLiteratureFile] = useState('')
  const [dataFile, setDataFile] = useState('')
  const [argumentIdea, setArgumentIdea] = useState('')
  const [methodPreference, setMethodPreference] = useState('暂无明确倾向')
  const [expectedOutcome, setExpectedOutcome] = useState('完整实证研究方案')
  const [selectedGap, setSelectedGap] = useState('叙事与行动之间缺少可验证的偏离度量')
  const [selectedHypothesis, setSelectedHypothesis] = useState('H-A')
  const [hypothesisTitles, setHypothesisTitles] = useState<Record<string, string>>(() => Object.fromEntries(hypotheses.map((item) => [item.id, item.title])))
  const [editingHypothesis, setEditingHypothesis] = useState<string | null>(null)
  const [editingTitle, setEditingTitle] = useState('')
  const [selectedDesign, setSelectedDesign] = useState<'A' | 'B' | 'C'>('A')
  const [frozen, setFrozen] = useState(false)
  const [runStarted, setRunStarted] = useState(false)
  const [previewGenerating, setPreviewGenerating] = useState(false)
  const [reviewAccepted, setReviewAccepted] = useState<string[]>([])
  const [notice, setNotice] = useState<string | null>(null)
  const caseListRef = useRef<HTMLDivElement | null>(null)
  const [caseListPaused, setCaseListPaused] = useState(false)
  const [cases, setCases] = useState<Array<(typeof initialCases)[number] | {
    id: string
    name: string
    topic: string
    status: string
    stage: string
    progress: number
    evidence: number
    hypotheses: number
    updated: string
    next: string
    owner: string
  }>>([...initialCases])
  const [caseSearch, setCaseSearch] = useState('')
  const [caseStatus, setCaseStatus] = useState('全部状态')
  const [caseOwner, setCaseOwner] = useState('全部案件')
  const [createCaseOpen, setCreateCaseOpen] = useState(false)
  const [newCaseName, setNewCaseName] = useState('')
  const [newCaseTopic, setNewCaseTopic] = useState<(typeof topicOptions)[number][0]>('ESG/信息披露')
  const [projectGraphMode, setProjectGraphMode] = useState<'topic' | 'stage' | 'evidence'>('topic')
  const [selectedProjectNode, setSelectedProjectNode] = useState<string>('GF-2026-0007')
  const [playIntro, setPlayIntro] = useState(false)

  useEffect(() => {
    if (introAnimationPlayed) return
    introAnimationPlayed = true
    setPlayIntro(true)
  }, [])

  const stageIndex = stages.findIndex((item) => item.id === stage)
  const currentHypothesis = useMemo(() => {
    const item = hypotheses.find((candidate) => candidate.id === selectedHypothesis) ?? hypotheses[0]
    return { ...item, title: hypothesisTitles[item.id] ?? item.title }
  }, [hypothesisTitles, selectedHypothesis])

  function beginHypothesisEdit(id: string, title: string) {
    setSelectedHypothesis(id)
    setFrozen(false)
    setEditingHypothesis(id)
    setEditingTitle(title)
  }

  function saveHypothesisTitle(id: string) {
    const nextTitle = editingTitle.trim()
    if (nextTitle) setHypothesisTitles((current) => ({ ...current, [id]: nextTitle }))
    setEditingHypothesis(null)
  }
  const selectedTopic = topicOptions.find(([name]) => name === researchTopic) ?? topicOptions[0]
  const selectedSchool = schoolOptions.find(([name]) => name === researchSchool) ?? schoolOptions[0]
  const visibleCases = cases.filter((item) => {
    const matchesSearch = `${item.name}${item.id}${item.topic}`.toLowerCase().includes(caseSearch.trim().toLowerCase())
    const matchesStatus = caseStatus === '全部状态' || item.status === caseStatus
    const matchesOwner = caseOwner === '全部案件' || item.owner === caseOwner
    return matchesSearch && matchesStatus && matchesOwner
  })
  useEffect(() => {
    if (stage !== 'case' || caseListPaused || visibleCases.length < 2) return
    const timer = window.setInterval(() => {
      const list = caseListRef.current
      if (!list) return
      const nextTop = list.scrollTop + 104
      const reachedEnd = nextTop + list.clientHeight >= list.scrollHeight - 8
      list.scrollTo({ top: reachedEnd ? 0 : nextTop, behavior: 'smooth' })
    }, 2800)
    return () => window.clearInterval(timer)
  }, [caseListPaused, stage, visibleCases.length])
  const decisionCount = cases.filter((item) => item.status.includes('等待')).length
  const runningCount = cases.filter((item) => item.status === '执行中').length
  const selectedGraphProject = cases.find((item) => item.id === selectedProjectNode) ?? cases[0]
  const graphHubs = projectGraphMode === 'topic'
    ? Array.from(new Set(cases.map((item) => item.topic))).slice(0, 4)
    : projectGraphMode === 'stage'
      ? ['任务与证据', '假设与设计', '执行验证', '审稿成果']
      : ['文献资产', '政策证据', '数据变量', '方法模板']
  const graphHubPositions = [
    { x: 210, y: 105 },
    { x: 650, y: 105 },
    { x: 210, y: 355 },
    { x: 650, y: 355 },
  ]
  const graphProjectPositions = [
    { x: 390, y: 115 },
    { x: 480, y: 205 },
    { x: 350, y: 285 },
    { x: 535, y: 340 },
    { x: 450, y: 420 },
    { x: 300, y: 205 },
    { x: 590, y: 245 },
  ]

  function graphHubIndexForCase(item: (typeof cases)[number]): number {
    if (projectGraphMode === 'topic') {
      const index = graphHubs.indexOf(item.topic)
      return index >= 0 ? index : 0
    }
    if (projectGraphMode === 'stage') {
      if (item.stage.includes('任务') || item.stage.includes('证据')) return 0
      if (item.stage.includes('假设') || item.stage.includes('设计')) return 1
      if (item.stage.includes('检验') || item.stage.includes('执行')) return 2
      return 3
    }
    return cases.indexOf(item) % 4
  }

  function move(offset: number) {
    const next = stages[Math.min(stages.length - 1, Math.max(0, stageIndex + offset))]
    setStage(next.id)
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  function showNotice(message: string) {
    setNotice(message)
    window.setTimeout(() => setNotice(null), 2400)
  }

  return (
    <main className="studio-page">
      <FaultyTerminalBackground />
      <header className="studio-casebar">
        <div className="studio-casebar__magic"><MagicRings /></div>
        <div className="studio-casebar__intro">
          <div className="studio-casebar__title">
            <CircularIntroText text="证据驱动型 AI SCIENTIST · 文献 · 政策 · 数据 · 方法 · " />
            <h1><BlurIntroText text="HypoWeaver-Qwen" animate={playIntro} /></h1>
          </div>
        </div>
      </header>

      <nav className="studio-stepper" aria-label="研究案件阶段">
        {stages.map((item, index) => {
          const active = item.id === stage
          const complete = index < stageIndex
          return (
            <button
              type="button"
              className={`${active ? 'is-active' : ''} ${complete ? 'is-complete' : ''}`}
              key={item.id}
              onClick={() => setStage(item.id)}
            >
              <small>{item.label}</small>
            </button>
          )
        })}
      </nav>

      {notice && <div className="studio-toast" role="status"><CheckCircle2 size={16} />{notice}</div>}

      {stage === 'case' && (
      <section className="studio-view">
          <div className="studio-heading">
            <div><h2>项目进度</h2></div>
            <button className="primary-button" type="button" onClick={() => setCreateCaseOpen(true)}><Plus size={15} />新建研究案件</button>
          </div>
          <div className="studio-metrics">
            <Metric label="全部案件" value={String(cases.length)} />
            <Metric label="进行中" value={String(runningCount + decisionCount)} />
            <Metric label="待人工决策" value={String(decisionCount)} />
            <Metric label="已完成" value={String(cases.filter((item) => item.status === '已完成').length)} />
          </div>
          <div className="case-toolbar">
            <label><Search size={14} /><input value={caseSearch} onChange={(event) => setCaseSearch(event.target.value)} placeholder="搜索案件名称、编号或主题" /></label>
            <select value={caseStatus} onChange={(event) => setCaseStatus(event.target.value)}>
              <option>全部状态</option><option>等待决策</option><option>执行中</option><option>等待审稿</option><option>已完成</option>
            </select>
            <select value={caseOwner} onChange={(event) => setCaseOwner(event.target.value)}>
              <option>全部案件</option><option>我的案件</option><option>协作案件</option>
            </select>
          </div>
          <div className="case-center">
            <div className="case-list">
              <header>
                <span>研究案件</span><span>阶段</span><span>状态</span><span>进度</span><span>最近更新</span><span />
              </header>
              <div
                ref={caseListRef}
                className="case-list__scroll"
                onMouseEnter={() => setCaseListPaused(true)}
                onMouseLeave={() => setCaseListPaused(false)}
              >
              {visibleCases.length ? visibleCases.map((item) => (
                <article
                  key={item.id}
                  className={`magic-bento-row ${selectedProjectNode === item.id ? 'is-selected' : ''}`}
                  onMouseEnter={(event) => {
                    setSelectedProjectNode(item.id)
                    event.currentTarget.style.setProperty('--bento-glow-intensity', '1')
                  }}
                  onMouseMove={(event) => {
                    const bounds = event.currentTarget.getBoundingClientRect()
                    event.currentTarget.style.setProperty('--bento-glow-x', `${event.clientX - bounds.left}px`)
                    event.currentTarget.style.setProperty('--bento-glow-y', `${event.clientY - bounds.top}px`)
                  }}
                  onMouseLeave={(event) => event.currentTarget.style.setProperty('--bento-glow-intensity', '0')}
                >
                  <div className="case-list__title">
                    <span className="case-folder"><FolderKanban size={15} /></span>
                    <div><strong>{item.name}</strong><small>{item.id} · {item.topic} · {item.owner}</small></div>
                  </div>
                  <span className="case-stage">{item.stage}</span>
                  <span className={`case-status is-${item.status}`}>{item.status}</span>
                  <span className="case-progress"><i><b style={{ width: `${item.progress}%` }} /></i><small>{item.progress}%</small></span>
                  <span className="case-updated">{item.updated}</span>
                  <button type="button" aria-label={`进入案件：${item.name}`} onClick={() => {
                    if (item.status === '已完成') setStage('review')
                    else if (item.status === '等待审稿') setStage('review')
                    else if (item.status === '执行中') setStage('execute')
                    else setStage('task')
                  }}><ArrowRight size={15} /></button>
                  <div className="case-list__next"><Clock3 size={12} /><span>下一步：{item.next}</span><em>{item.evidence} 条证据 · {item.hypotheses} 个假设</em></div>
                </article>
              )) : <div className="case-empty"><Search size={20} /><strong>没有匹配的案件</strong><p>调整搜索词或筛选条件后重试。</p></div>}
              </div>
            </div>
            <aside className="case-attention">
              <header><span className="studio-index">需要你处理</span><strong>{decisionCount}</strong></header>
              {cases.filter((item) => item.status.includes('等待')).map((item) => (
                <button type="button" key={item.id} onClick={() => setStage(item.status === '等待审稿' ? 'review' : 'hypothesis')}>
                  <span>{item.status}</span><strong>{item.name}</strong><small>{item.next}</small>
                </button>
              ))}
              <div className="case-attention__guide">
                <Sparkles size={16} />
                <div><strong>还没有明确题目？</strong><p>使用研究罗盘，从 12 个主题与 17 个理论流派中寻找方向。</p></div>
                <button type="button" onClick={() => setStage('task')}>打开研究罗盘</button>
              </div>
            </aside>
          </div>
          <section className="project-network">
            <header>
              <div><p className="exec-eyebrow">Project Intelligence Map</p><h3>历史项目关系网络</h3><p>汇总全部项目，识别可复用的主题、证据与研究路径。</p></div>
              <div className="project-network__modes" role="group" aria-label="项目关系视图">
                <button type="button" className={projectGraphMode === 'topic' ? 'is-active' : ''} onClick={() => setProjectGraphMode('topic')}>主题关系</button>
                <button type="button" className={projectGraphMode === 'stage' ? 'is-active' : ''} onClick={() => setProjectGraphMode('stage')}>阶段关系</button>
                <button type="button" className={projectGraphMode === 'evidence' ? 'is-active' : ''} onClick={() => setProjectGraphMode('evidence')}>证据复用</button>
              </div>
            </header>
            <div className="project-network__layout">
              <div className="project-network__canvas">
                <DotGrid
                  dotSize={4}
                  gap={18}
                  baseColor="#d9cee0"
                  activeColor="#4f176b"
                  proximity={120}
                  shockRadius={250}
                  shockStrength={5}
                  resistance={750}
                  returnDuration={1.5}
                />
                <svg viewBox="0 0 860 480" role="img" aria-label="历史研究项目关系连线">
                  <g className="project-network__edges">
                    {cases.map((item, index) => {
                      const project = graphProjectPositions[index % graphProjectPositions.length]
                      const hub = graphHubPositions[graphHubIndexForCase(item)]
                      return <line key={`${projectGraphMode}-${item.id}`} x1={hub.x} y1={hub.y} x2={project.x} y2={project.y} />
                    })}
                    {graphHubPositions.slice(0, graphHubs.length).map((hub, index) => <line className="is-core" key={`core-${index}`} x1="430" y1="240" x2={hub.x} y2={hub.y} />)}
                  </g>
                </svg>
                <div className="project-network__core"><Network size={17} /><strong>项目知识核</strong><small>{cases.length} 个项目</small></div>
                {graphHubs.map((hub, index) => {
                  const position = graphHubPositions[index]
                  const relatedCount = cases.filter((item) => graphHubIndexForCase(item) === index).length
                  return <div className="project-network__hub" key={`${projectGraphMode}-${hub}`} style={{ left: `${position.x / 8.6}%`, top: `${position.y / 4.8}%` }}><strong>{hub}</strong><small>{relatedCount} 个项目</small></div>
                })}
                {cases.map((item, index) => {
                  const position = graphProjectPositions[index % graphProjectPositions.length]
                  const selected = item.id === selectedProjectNode
                  return (
                    <button
                      type="button"
                      key={item.id}
                      className={`project-network__node ${selected ? 'is-selected' : ''} is-progress-${item.progress}`}
                      style={{ left: `${position.x / 8.6}%`, top: `${position.y / 4.8}%` }}
                      onClick={() => setSelectedProjectNode(item.id)}
                    >
                      <span>{String(index + 1).padStart(2, '0')}</span>
                      <strong>{item.name}</strong>
                      <small>{item.progress}% · {item.status}</small>
                    </button>
                  )
                })}
                <div className="project-network__legend"><span><i className="is-hub" />关系中心</span><span><i className="is-project" />历史项目</span><span><i className="is-core" />知识汇总</span></div>
              </div>
              <aside className="project-network__detail">
                <span className="studio-badge">{selectedGraphProject.status}</span>
                <h4>{selectedGraphProject.name}</h4>
                <p>{selectedGraphProject.id} · {selectedGraphProject.topic}</p>
                <dl>
                  <div><dt>当前阶段</dt><dd>{selectedGraphProject.stage}</dd></div>
                  <div><dt>研究进度</dt><dd>{selectedGraphProject.progress}%</dd></div>
                  <div><dt>结构化证据</dt><dd>{selectedGraphProject.evidence} 条</dd></div>
                  <div><dt>候选假设</dt><dd>{selectedGraphProject.hypotheses} 个</dd></div>
                </dl>
                <div className="project-network__reuse">
                  <strong>可复用资产</strong>
                  <span>主题词典与检索式</span>
                  <span>变量定义和数据字段</span>
                  <span>研究设计与审稿记录</span>
                </div>
                <button type="button" className="primary-button" onClick={() => {
                  if (selectedGraphProject.status === '已完成' || selectedGraphProject.status === '等待审稿') setStage('review')
                  else if (selectedGraphProject.status === '执行中') setStage('execute')
                  else setStage('task')
                }}>进入此项目<ArrowRight size={14} /></button>
              </aside>
            </div>
          </section>
          {createCaseOpen && (
            <div className="case-modal-backdrop" role="presentation" onMouseDown={() => setCreateCaseOpen(false)}>
              <section className="case-modal" role="dialog" aria-modal="true" aria-labelledby="create-case-title" onMouseDown={(event) => event.stopPropagation()}>
                <header><div><p className="exec-eyebrow">Create Research Case</p><h3 id="create-case-title">新建研究案件</h3></div><button type="button" onClick={() => setCreateCaseOpen(false)} aria-label="关闭">×</button></header>
                <label>案件名称<input value={newCaseName} onChange={(event) => setNewCaseName(event.target.value)} placeholder="例如：绿色债券与企业融资约束" autoFocus /></label>
                <label>主题方向<select value={newCaseTopic} onChange={(event) => setNewCaseTopic(event.target.value as (typeof topicOptions)[number][0])}>{topicOptions.map(([name]) => <option value={name} key={name}>{name}</option>)}</select></label>
                <div className="case-modal__choice"><button type="button" className="is-selected"><strong>空白案件</strong><span>从任务定义开始</span></button><button type="button"><strong>使用研究模板</strong><span>预填数据与方法路径</span></button></div>
                <footer><button type="button" className="quiet-button" onClick={() => setCreateCaseOpen(false)}>取消</button><button type="button" className="primary-button" disabled={!newCaseName.trim()} onClick={() => {
                  const nextId = `GF-2026-${String(cases.length + 8).padStart(4, '0')}`
                  setCases((current) => [{
                    id: nextId,
                    name: newCaseName.trim(),
                    topic: newCaseTopic,
                    status: '等待决策',
                    stage: '任务定义',
                    progress: 8,
                    evidence: 0,
                    hypotheses: 0,
                    updated: '刚刚',
                    next: '确认研究问题与边界',
                    owner: '我的案件',
                  }, ...current])
                  setResearchTopic(newCaseTopic)
                  setNewCaseName('')
                  setCreateCaseOpen(false)
                  showNotice(`案件 ${nextId} 已创建`)
                }}>创建案件<ArrowRight size={14} /></button></footer>
              </section>
            </div>
          )}
        </section>
      )}

      {stage === 'task' && (
        <section className="studio-view">
          <div className="studio-heading"><div><h2>确定项目主题</h2><p>以下内容将控制后续检索、变量构造和方法推荐。</p></div><span className="studio-badge studio-badge--wait">H1 · 等待确认</span></div>
          <div className="studio-form-grid">
            <label>
              主题方向
              <select value={researchTopic} onChange={(event) => setResearchTopic(event.target.value as (typeof topicOptions)[number][0])}>
                {topicOptions.map(([name, count]) => <option value={name} key={name}>{name} · {count} 篇</option>)}
              </select>
            </label>
            <label>
              理论流派 / 问题视角
              <select value={researchSchool} onChange={(event) => setResearchSchool(event.target.value as (typeof schoolOptions)[number][0])}>
                {schoolOptions.map(([name, count]) => <option value={name} key={name}>{name} · {count} 篇</option>)}
              </select>
            </label>
            <div className="studio-taxonomy is-wide">
              <div><span>主题方向</span><strong>{selectedTopic[0]}</strong><small>知识图谱中收录 {selectedTopic[1]} 篇</small></div>
              <ChevronRight size={16} aria-hidden="true" />
              <div><span>理论视角</span><strong>{selectedSchool[0]}</strong><small>{selectedSchool[2]}</small></div>
              <button
                type="button"
                onClick={() => {
                  setResearchQuestion(`在“${researchTopic}”领域中，${researchSchool}所强调的核心机制如何影响企业绿色行为及其经济后果？`)
                  showNotice('已按知识图谱分类生成研究问题草案')
                }}
              >
                <Sparkles size={14} />按分类生成问题
              </button>
            </div>
            <label className="is-wide">核心研究问题<textarea value={researchQuestion} onChange={(event) => setResearchQuestion(event.target.value)} /></label>
            <label>研究对象<input defaultValue="A 股非金融上市公司" /></label>
            <label>样本期间<input defaultValue="2015—2024" /></label>
            <label>数据约束<select defaultValue="公开数据优先"><option>公开数据优先</option><option>允许半公开数据</option></select></label>
            <label>验证深度<select defaultValue="小样本执行 + 完整研究设计"><option>小样本执行 + 完整研究设计</option><option>仅研究计划</option></select></label>
          </div>
          <div className="studio-callout"><ShieldCheck size={18} /><div><strong>边界检查通过</strong><p>研究对象、时间范围、核心变量和数据约束均已明确，可以并行启动证据扫描。</p></div></div>
          <section className="studio-brief-collector">
            <header>
              <div><h3>补充研究材料与成果偏好</h3><p>上传已有材料并说明研究倾向，系统将据此调整后续证据检索与方案生成。</p></div>
              <span>支持 PDF、DOCX、XLSX、CSV</span>
            </header>
            <div className="studio-brief-collector__grid">
              <label className="studio-upload-field">
                <span><strong>相关文献上传</strong><small>选填 · 可上传论文、政策或研究报告</small></span>
                <input type="file" accept=".pdf,.doc,.docx,.txt" onChange={(event) => setLiteratureFile(event.target.files?.[0]?.name ?? '')} />
                <em><Upload size={16} />{literatureFile || '选择文献文件'}</em>
              </label>
              <label className="studio-upload-field">
                <span><strong>数据上传</strong><small>选填 · 可上传原始数据或变量清单</small></span>
                <input type="file" accept=".csv,.xlsx,.xls,.json,.dta" onChange={(event) => setDataFile(event.target.files?.[0]?.name ?? '')} />
                <em><Database size={16} />{dataFile || '选择数据文件'}</em>
              </label>
              <label className="studio-collector-field is-wide">
                <span><strong>论证思路</strong><small>选填 · 描述理论机制、关键关系或预期论证路径</small></span>
                <textarea value={argumentIdea} onChange={(event) => setArgumentIdea(event.target.value)} placeholder="例如：从信息不对称机制出发，检验绿色叙事与真实行为偏离如何影响债权人风险判断……" />
              </label>
              <label className="studio-collector-field">
                <span><strong>方法倾向</strong><small>用于调整研究设计推荐</small></span>
                <select value={methodPreference} onChange={(event) => setMethodPreference(event.target.value)}>
                  <option>暂无明确倾向</option><option>面板固定效应</option><option>双重差分 DID</option><option>工具变量 IV</option><option>文本分析与机器学习</option><option>案例与混合研究</option>
                </select>
              </label>
              <label className="studio-collector-field">
                <span><strong>期望成果</strong><small>决定后续输出的深度与格式</small></span>
                <select value={expectedOutcome} onChange={(event) => setExpectedOutcome(event.target.value)}>
                  <option>完整实证研究方案</option><option>可执行研究计划</option><option>论文初稿与图表</option><option>政策研究报告</option><option>数据与方法说明书</option>
                </select>
              </label>
            </div>
            <footer>
              <small>{literatureFile || dataFile || argumentIdea ? '已记录补充材料' : '当前未添加选填材料'}</small>
              <button type="button" className="primary-button" onClick={() => showNotice('材料与偏好已应用到研究任务')}>应用到研究任务<ArrowRight size={14} /></button>
            </footer>
          </section>
        </section>
      )}

      {stage === 'evidence' && (
        <section className="studio-view">
          <div className="studio-heading"><div><h2>证据梳理</h2><p>所有条目均保留来源、抽取结果、核验状态和被引用位置。</p></div><button className="secondary-button" type="button" onClick={() => showNotice('增量扫描已启动，复用 21 条缓存证据')}>增量扫描<Search size={15} /></button></div>
          <div className="studio-evidence-summary">
            {evidenceTabs.map((tab, index) => <button type="button" key={tab} className={activeEvidence === tab ? 'is-active' : ''} onClick={() => setActiveEvidence(tab)}><span>{['12', '5', '6', '4'][index]}</span><strong>{tab}</strong><small>{['论文与引用', '监管与制度', '字段与可得性', '模型与识别'][index]}</small></button>)}
          </div>
          <div className="studio-evidence-list">
            {evidenceData[activeEvidence].map((item, index) => (
              <article key={item.title}>
                <span className="studio-source-icon">[{index + 1}]</span>
                <div><h3>{item.title}</h3><small>{item.meta}</small><p>{item.note}</p></div>
                <div className="studio-evidence-actions"><span className="studio-badge">{item.tag}</span><button type="button" onClick={() => showNotice(`已打开：${item.title}`)}>查看详情<ChevronRight size={14} /></button></div>
              </article>
            ))}
          </div>
          <div className="studio-evidence-add">
            <button type="button" aria-label="添加更多证据" onClick={() => showNotice('已打开新增证据入口：可上传材料或启动增量扫描')}>添加</button>
            <span>更多证据</span>
          </div>
        </section>
      )}

      {stage === 'gap' && (
        <section className="studio-view">
          <div className="studio-heading"><div><h2>缺口研究</h2><p>选择一张 GapCard，后续候选假设将以它为共同起点。</p></div><span className="studio-badge studio-badge--wait">人工决策门</span></div>
          <div className="studio-gap-layout">
            <div className="studio-graph" aria-label="研究图谱示意">
              <div className="graph-node graph-node--main">绿色叙事</div>
              <div className="graph-node graph-node--top">信息不对称</div>
              <div className="graph-node graph-node--right">融资成本</div>
              <div className="graph-node graph-node--bottom">真实绿色行为</div>
              <div className="graph-node graph-node--left">披露监管</div>
              <span className="graph-line graph-line--a" /><span className="graph-line graph-line--b" /><span className="graph-line graph-line--c" /><span className="graph-line graph-line--d" />
              <p>关系图仅展示已核验对象；虚线区域代表证据缺口。</p>
            </div>
            <div className="studio-gap-list">
              {[
                ['叙事与行动之间缺少可验证的偏离度量', '证据空白', '高', '文献集中讨论披露水平，较少把文本承诺与专利、环保投资等真实行为进行公司—年度匹配。'],
                ['强制披露是否真正降低绿洗仍缺乏因果证据', '政策空白', '中', '现有研究更多讨论披露数量，较少检验披露规则对叙事—行为一致性的影响。'],
                ['债权人何时能够识别绿洗的边界机制不足', '机制空白', '中', '媒体关注、监管强度和分析师覆盖可能改变绿洗信息进入定价的速度。'],
              ].map(([title, type, novelty, description]) => (
                <button type="button" key={title} className={selectedGap === title ? 'is-selected' : ''} onClick={() => setSelectedGap(title)}>{selectedGap === title && <ElectricBorder />}
                  <span>{type}</span><strong>{title}</strong><p>{description}</p><small>新颖性：{novelty} · 公开数据可验证</small>
                </button>
              ))}
            </div>
          </div>
        </section>
      )}

      {stage === 'hypothesis' && (
        <section className="studio-view">
          <div className="studio-heading"><div><h2>假设选择</h2><p>研究空白：{selectedGap}</p></div><span className={`studio-badge ${frozen ? 'studio-badge--done' : 'studio-badge--wait'}`}>{frozen ? '主假设已冻结' : 'H2 · 等待选择'}</span></div>
          <div className="studio-hypotheses">
            {hypotheses.map((item) => {
              const selected = selectedHypothesis === item.id
              const average = (item.scores.reduce((sum, value) => sum + value, 0) / item.scores.length).toFixed(1)
              const title = hypothesisTitles[item.id] ?? item.title
              return (
                <article key={item.id} className={selected ? 'is-selected' : ''}>{selected && <ElectricBorder />}
                  <header><span>{item.id}</span><strong>综合 {average}/5</strong></header>
                  <div className="hypothesis-title-row">
                    {editingHypothesis === item.id ? (
                      <input
                        className="hypothesis-title-input"
                        value={editingTitle}
                        autoFocus
                        aria-label={`编辑 ${item.id} 假设`}
                        onChange={(event) => setEditingTitle(event.target.value)}
                        onBlur={() => saveHypothesisTitle(item.id)}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter') event.currentTarget.blur()
                          if (event.key === 'Escape') { setEditingHypothesis(null); setEditingTitle(title) }
                        }}
                      />
                    ) : <h3>{title}</h3>}
                    <button type="button" className="hypothesis-edit" title="编辑假设" aria-label={`编辑 ${item.id} 假设`} onClick={() => beginHypothesisEdit(item.id, title)}><Pencil size={14} /></button>
                  </div>
                  <dl><div><dt>机制链</dt><dd>{item.mechanism}</dd></div><div><dt>数据</dt><dd>{item.data}</dd></div><div><dt>方法</dt><dd>{item.method}</dd></div></dl>
                  <div className="studio-scorebar">{['新颖性', '数据', '方法', '政策价值'].map((label, i) => <span key={label}><small>{label}</small><i><b style={{ width: `${item.scores[i] * 20}%` }} /></i></span>)}</div>
                  <button type="button" className={selected ? 'primary-button' : 'secondary-button'} onClick={() => { setSelectedHypothesis(item.id); setFrozen(false) }}>{selected ? <><Check size={14} />已选择</> : '选择此假设'}</button>
                </article>
              )
            })}
          </div>
          <div className="studio-decision"><div><strong>当前选择：{currentHypothesis.id}</strong><p>{currentHypothesis.title}</p></div><button type="button" className="primary-button" onClick={() => { setFrozen(true); showNotice('主假设 v1.0 已冻结，后续修改将创建新版本') }}><ShieldCheck size={15} />冻结主假设</button></div>
        </section>
      )}

      {stage === 'design' && (
        <section className="studio-view">
          <div className="studio-heading"><div><h2>方案确定</h2><p>{currentHypothesis.title}</p></div><span className={`studio-badge ${frozen ? 'studio-badge--done' : 'studio-badge--risk'}`}>{frozen ? '假设 v1.0 已冻结' : '请先冻结假设'}</span></div>
          <div className="studio-design-grid">
            <article className={selectedDesign === 'A' ? 'is-selected' : ''} onClick={() => setSelectedDesign('A')}>{selectedDesign === 'A' && <ElectricBorder />}<span>方案 A · 推荐</span><h3>公开数据可执行方案</h3><p>以年报文本和绿色专利构造偏离指数，先完成双向固定效应基准验证。</p><ul><li>样本：A 股非金融企业</li><li>行动窗口：当年及未来两年</li><li>固定效应：企业、年份、行业×年份</li><li>稳健性：替代文本词典、滞后变量</li></ul><button type="button" className="primary-button" disabled={!frozen} onClick={() => { setSelectedDesign('A'); showNotice('方案 A 已设为 FormalResearchContract v1') }}>选择并冻结</button></article>
            <article className={selectedDesign === 'B' ? 'is-selected' : ''} onClick={() => setSelectedDesign('B')}>{selectedDesign === 'B' && <ElectricBorder />}<span>方案 B · 识别优先</span><h3>披露政策多期 DID</h3><p>利用披露规则分批实施构造政策冲击，评估叙事—行动偏离的治理效果。</p><ul><li>需要准确的处理组规则</li><li>必须验证平行趋势</li><li>需要处理政策外溢</li><li>数据准备成本较高</li></ul><button type="button" className="secondary-button" disabled={!frozen} onClick={() => setSelectedDesign('B')}>选择方案</button></article>
            <article className={selectedDesign === 'C' ? 'is-selected' : ''} onClick={() => setSelectedDesign('C')}>{selectedDesign === 'C' && <ElectricBorder />}<span>方案 C · 测量优先</span><h3>多源绿洗测量模型</h3><p>融合专利、环保投资与处罚事件，优先提高行动指标的内容效度。</p><ul><li>多源公司名称匹配</li><li>缺失机制需单独处理</li><li>适合形成测量贡献</li><li>执行周期最长</li></ul><button type="button" className="secondary-button" disabled={!frozen} onClick={() => setSelectedDesign('C')}>选择方案</button></article>
          </div>
          {!frozen && <div className="studio-callout studio-callout--risk"><Circle size={18} /><div><strong>研究合同尚不可冻结</strong><p>返回候选假设页完成 H2 选择，防止看到结果后改变研究问题。</p></div></div>}
        </section>
      )}

      {stage === 'execute' && (
        <section className="studio-view">
          <div className="studio-heading"><div><h2>观察并行 DAG 如何生成可复现证据</h2><p>本页使用演示状态，不会执行真实统计代码或消耗模型额度。</p></div><button type="button" className="primary-button" onClick={() => setRunStarted(true)} disabled={runStarted}><Play size={15} />{runStarted ? '演示运行中' : '开始演示运行'}</button></div>
          <div className="studio-run-layout">
            <div className="studio-dag" aria-label="执行分支流程图">
              <article className={`studio-dag__node studio-dag__node--entry ${runStarted ? 'is-done' : ''}`}><span>{runStarted ? <Check size={13} /> : '01'}</span><div><strong>数据哈希与样本校验</strong><small>串行入口</small></div><em>{runStarted ? '已完成' : '等待依赖'}</em></article>
              <section className="studio-dag__branch">
                <header><span>并行分支 A</span><small>三项数据构建任务可同时开始</small></header>
                <div className="studio-dag__branch-nodes">
                  {[['02A', '年报绿色叙事抽取'], ['02B', '绿色专利公司匹配'], ['02C', '融资成本变量构造']].map(([id, name], index) => <article key={id} className={`studio-dag__node ${runStarted && index < 2 ? 'is-done' : ''}`}><span>{runStarted && index < 2 ? <Check size={13} /> : id}</span><div><strong>{name}</strong><small>并行执行</small></div><em>{runStarted && index < 2 ? '已完成' : '等待依赖'}</em></article>)}
                </div>
              </section>
              <article className="studio-dag__node studio-dag__node--merge"><span>03</span><div><strong>偏离指数与面板合并</strong><small>分支汇合</small></div><em>等待汇合</em></article>
              <section className="studio-dag__branch">
                <header><span>并行分支 B</span><small>多种识别策略并行验证</small></header>
                <div className="studio-dag__branch-nodes">
                  {[['04A', '基准固定效应模型'], ['04B', '替代变量稳健性'], ['04C', '两年行动窗口检验']].map(([id, name], index) => <article key={id} className={`studio-dag__node ${runStarted && index === 0 ? 'is-running' : ''}`}><span>{id}</span><div><strong>{name}</strong><small>并行执行</small></div><em>{runStarted && index === 0 ? '运行中' : '等待依赖'}</em></article>)}
                </div>
              </section>
              <article className="studio-dag__node studio-dag__node--exit"><span>05</span><div><strong>独立复算与数值审计</strong><small>串行出口</small></div><em>等待依赖</em></article>
            </div>
            <aside className="studio-card studio-run-panel">
              <header><span className="studio-index">运行摘要</span><GitBranch size={16} /></header>
              <Metric label="节点" value={runStarted ? '6 / 9' : '0 / 9'} />
              <Metric label="缓存复用" value={runStarted ? '71%' : '—'} />
              <Metric label="执行状态" value={runStarted ? 'running' : 'ready'} />
              <Metric label="科学状态" value="not_assessed" />
              <p>执行成功不会自动升级科学结论。结果还需经过独立复算、Claim Gate 和多智能体审稿。</p>
              {runStarted && <button className="secondary-button" type="button" onClick={() => setRunStarted(false)}><RotateCcw size={14} />重置演示</button>}
            </aside>
          </div>
        </section>
      )}

      {stage === 'review' && (
        <section className="studio-view">
          <div className="studio-heading"><div><h2>智能审稿</h2><p>保留原始意见、回应、修订内容和最终授权范围。</p></div><span className="studio-badge studio-badge--wait">4 条意见待处理</span></div>
          <div className="studio-review-layout">
            <div className="studio-reviewers">
              {reviewers.map((reviewer) => {
                const accepted = reviewAccepted.includes(reviewer.role)
                return <article key={reviewer.role}><header><span className={`review-tone is-${reviewer.tone}`} /><strong>{reviewer.role}</strong><em>{reviewer.verdict}</em></header><p>{reviewer.note}</p><footer><button type="button" className={accepted ? 'is-accepted' : ''} onClick={() => setReviewAccepted((current) => current.includes(reviewer.role) ? current.filter((item) => item !== reviewer.role) : [...current, reviewer.role])}>{accepted ? <><Check size={13} />已纳入修订</> : '纳入修订'}</button><button type="button" onClick={() => showNotice(`已展开${reviewer.role}的证据与回应链`)}>查看依据</button></footer></article>
              })}
            </div>
            <aside className="studio-plan">
              <header><div><span className="studio-index">Research Plan v0.8</span><h3>十项标准化成果</h3></div><span>{reviewAccepted.length}/4 意见已处理</span></header>
              <ol>
                {['待研究问题', '解决思路', '必要技术手段', '数据集：Source / Target', '论文标题', '论文摘要', '方法论', '实验设计', '初步结果', '真实参考文献'].map((item, index) => <li key={item}><span>{String(index + 1).padStart(2, '0')}</span><strong>{item}</strong><em>{index < 4 ? '已有草稿' : index < 8 ? '等待修订' : '等待执行'}</em></li>)}
              </ol>
              <button type="button" className={`primary-button studio-plan__preview-button ${previewGenerating ? 'is-generating' : ''}`} disabled={reviewAccepted.length < 2} onClick={() => {
                setPreviewGenerating(true)
                showNotice('已生成研究计划预览，所有结论保留证据回链')
                window.setTimeout(() => setPreviewGenerating(false), 1800)
              }}><span>{previewGenerating ? '正在生成预览' : '生成成果预览'}</span><ArrowRight size={15} /></button>
            </aside>
          </div>
        </section>
      )}

      <footer className="studio-footer">
        <button type="button" className="quiet-button" disabled={stageIndex === 0} onClick={() => move(-1)}><ArrowLeft size={15} />上一步</button>
        <ol className="studio-footer__stepper" aria-label="研究流程进度">
          {stages.map((item, index) => (
            <li key={item.id} className={index < stageIndex ? 'is-complete' : index === stageIndex ? 'is-active' : ''}>
              <button type="button" onClick={() => setStage(item.id)} aria-current={index === stageIndex ? 'step' : undefined}>
                <span>{index < stageIndex ? '✓' : index + 1}</span>
                <small>{item.short}</small>
              </button>
            </li>
          ))}
        </ol>
        <button type="button" className="primary-button" disabled={stageIndex === stages.length - 1} onClick={() => move(1)}>下一步<ArrowRight size={15} /></button>
      </footer>
    </main>
  )
}
