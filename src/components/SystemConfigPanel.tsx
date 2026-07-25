import { CheckCircle2, Copy, KeyRound, ServerCog, TestTube2, TriangleAlert } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { ConnectionTestResult, RuntimeConfigStatus, RuntimeConfigUpdate } from '../runtime/types'

interface SystemConfigPanelProps {
  status: RuntimeConfigStatus | null
  accessTokenPresent: boolean
  accessTokenVerified: boolean
  busy: boolean
  onRefresh: () => Promise<void>
  onSetAccessToken: (token: string) => void
  onSave: (input: RuntimeConfigUpdate) => Promise<boolean>
  onTest: (target: ConnectionTestResult['target']) => Promise<ConnectionTestResult>
}

function StateLine({ ready, children }: { ready: boolean; children: React.ReactNode }) {
  return <span className={ready ? 'service-state is-ready' : 'service-state'}>{ready ? <CheckCircle2 size={15} /> : <TriangleAlert size={15} />}{children}</span>
}

export function SystemConfigPanel({ status, accessTokenPresent, accessTokenVerified, busy, onRefresh, onSetAccessToken, onSave, onTest }: SystemConfigPanelProps) {
  const [qwenApiKey, setQwenApiKey] = useState('')
  const [qwenModel, setQwenModel] = useState('qwen-plus')
  const [qwenBaseUrl, setQwenBaseUrl] = useState('https://dashscope.aliyuncs.com/compatible-mode/v1')
  const [executorUrl, setExecutorUrl] = useState('')
  const [executorToken, setExecutorToken] = useState('')
  const [workflowToken, setWorkflowToken] = useState('')
  const [testResult, setTestResult] = useState<ConnectionTestResult | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (!status) return
    setQwenModel(status.qwenModel.value ?? 'qwen-plus')
    setQwenBaseUrl(status.qwenBaseUrl.value ?? 'https://dashscope.aliyuncs.com/compatible-mode/v1')
    setExecutorUrl(status.researchEngineUrl.value ?? '')
  }, [status])

  async function save() {
    setSaved(false)
    if (workflowToken.trim()) onSetAccessToken(workflowToken)
    const normalizedQwenUrl = qwenBaseUrl.trim()
    const normalizedExecutorUrl = executorUrl.trim()
    const success = await onSave({
      qwenApiKey: qwenApiKey.trim() || undefined,
      qwenModel: qwenModel.trim() !== status?.qwenModel.value ? qwenModel.trim() : undefined,
      qwenBaseUrl: normalizedQwenUrl !== status?.qwenBaseUrl.value ? normalizedQwenUrl : undefined,
      researchEngineUrl: normalizedExecutorUrl && normalizedExecutorUrl !== status?.researchEngineUrl.value ? normalizedExecutorUrl : undefined,
      researchEngineToken: executorToken.trim() || undefined,
      clearResearchEngineUrl: !normalizedExecutorUrl && Boolean(status?.researchEngineUrl.value),
    })
    if (!success) return
    setQwenApiKey('')
    setExecutorToken('')
    setWorkflowToken('')
    setSaved(true)
  }

  async function test(target: ConnectionTestResult['target']) {
    setTestResult(null)
    setTestResult(await onTest(target))
  }

  const configPath = status?.configPath ?? 'backend/var/runtime-config.json'
  const tokenReady = !status?.workflowApiTokenRequired || accessTokenVerified

  return (
    <main className="settings">
      <header className="settings__hero">
        <div>
          <p className="eyebrow">配置</p>
          <h1 className="settings__title">模型与执行器</h1>
          <p className="settings__lead">先配置服务，再开始真实研究；流程演示模式无需任何密钥。密钥只写入后端私有文件（权限 0600），不会回显到浏览器，环境变量始终优先。</p>
        </div>
        <button type="button" className="secondary-button" onClick={onRefresh} disabled={busy}>重新检测</button>
      </header>

      <div className="settings__body">
        <div className="settings__inner">
        <p className="settings__path">
          <KeyRound size={14} aria-hidden="true" />
          密钥写入 <code>{configPath}</code>
          <button type="button" onClick={() => navigator.clipboard.writeText(configPath)}><Copy size={13} />复制路径</button>
        </p>

        <div className="settings__cols">
          <section className="settings__block">
            <div className="settings__block-head">
              <div className="settings__block-title"><KeyRound size={16} aria-hidden="true" /><h2>千问模型</h2></div>
              <StateLine ready={Boolean(status?.qwenApiKey.configured)}>{status?.qwenApiKey.configured ? `已配置 · ${status.qwenApiKey.source}` : '尚未配置 API 密钥'}</StateLine>
            </div>
            <label>API 密钥<input type="password" autoComplete="off" value={qwenApiKey} onChange={(event) => setQwenApiKey(event.target.value)} placeholder={status?.qwenApiKey.configured ? '已配置；留空表示不修改' : '输入 DASHSCOPE_API_KEY'} /></label>
            <div className="settings__grid">
              <label>模型 ID<input value={qwenModel} onChange={(event) => setQwenModel(event.target.value)} placeholder="qwen-plus" spellCheck={false} /><small className="field-help">区分大小写；百炼公共模型用小写 ID，例如 qwen-plus。</small></label>
              <label>API 地址<input value={qwenBaseUrl} onChange={(event) => setQwenBaseUrl(event.target.value)} /></label>
            </div>
            {status?.qwenApiKey.configured && qwenBaseUrl.trim() !== status.qwenBaseUrl.value && <p className="config-field-warning">{status.qwenApiKey.source === 'environment' ? '千问密钥来自环境变量；为避免密钥被转发到其他地址，请同时在后端环境变量 QWEN_BASE_URL 中修改地址。页面不能修改这一组合。' : '切换 API 地址时需要重新输入千问 API 密钥，避免把已有密钥误发给新的服务地址。'}</p>}
            <button type="button" className="test-button" onClick={() => test('qwen')} disabled={busy || !status?.qwenApiKey.configured}><TestTube2 size={15} />测试千问连接</button>
          </section>

          <section className="settings__block">
            <div className="settings__block-head">
              <div className="settings__block-title"><ServerCog size={16} aria-hidden="true" /><h2>Python 研究执行器</h2></div>
              <StateLine ready={Boolean(status?.researchEngineUrl.value)}>{status?.researchEngineUrl.value ? `已配置 · ${status.researchEngineUrl.source}` : '尚未配置执行器地址'}</StateLine>
            </div>
            <label>执行器地址<input value={executorUrl} onChange={(event) => setExecutorUrl(event.target.value)} placeholder="http://127.0.0.1:9000" /></label>
            <label>执行器令牌<input type="password" autoComplete="off" value={executorToken} onChange={(event) => setExecutorToken(event.target.value)} placeholder={status?.researchEngineToken.configured ? '已配置；留空表示不修改' : '可选：RESEARCH_ENGINE_TOKEN'} /></label>
            {status?.researchEngineToken.configured && executorUrl.trim() !== status.researchEngineUrl.value && <p className="config-field-warning">{status.researchEngineToken.source === 'environment' ? '执行器令牌来自环境变量；请同时在后端环境变量 RESEARCH_ENGINE_URL 中修改地址。页面不能修改这一组合。' : '切换执行器地址时需要重新输入执行器令牌。'}</p>}
            <button type="button" className="test-button" onClick={() => test('research_engine')} disabled={busy || !status?.researchEngineUrl.value}><TestTube2 size={15} />测试执行器连接</button>
          </section>
        </div>

        <section className="settings__block settings__block--wide">
          <div className="settings__block-head">
            <div className="settings__block-title"><h2>工作流访问令牌</h2></div>
            <StateLine ready={tokenReady}>{status?.workflowApiTokenRequired ? (accessTokenVerified ? '已由后端验证' : accessTokenPresent ? '已填写但尚未验证；点“保存配置”验证' : '后端要求访问令牌') : '本机后端当前未要求访问令牌'}</StateLine>
          </div>
          <label>访问令牌<input type="password" autoComplete="off" value={workflowToken} onChange={(event) => setWorkflowToken(event.target.value)} placeholder={accessTokenPresent ? '当前标签页已有令牌；留空表示不修改' : '输入后端 HYPOWEAVER_API_TOKEN'} /></label>
          <p className="settings__hint"><code>HYPOWEAVER_API_TOKEN</code> 只用于访问工作流后端，与千问密钥不同。仅保存在当前浏览器标签页的 sessionStorage，关闭即清除，不写入前端构建产物。</p>
        </section>

        {testResult && <p className={testResult.success ? 'settings__result is-success' : 'settings__result is-failure'} role="status"><strong>{testResult.success ? '连接成功' : '连接失败'}</strong>{testResult.message}</p>}
        {saved && <p className="settings__result is-success" role="status"><strong>配置已保存</strong>后端已重新读取脱敏配置状态。</p>}
        </div>
      </div>

      <footer className="settings__foot">
        <span>{status?.qwenApiKey.configured ? '千问已就绪，可进行真实研究。' : '流程演示无需密钥；真实研究需先保存千问 API。'}</span>
        <button type="button" className="primary-button" onClick={save} disabled={busy}>{busy ? '正在保存…' : '保存配置'}</button>
      </footer>
    </main>
  )
}
