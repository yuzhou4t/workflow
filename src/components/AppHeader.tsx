import { History, Plus, Settings2 } from 'lucide-react'
import type { RuntimeConfigStatus } from '../runtime/types'

export type AppView = 'new' | 'runs' | 'settings'

interface AppHeaderProps {
  view: AppView
  config: RuntimeConfigStatus | null
  onChangeView: (view: AppView) => void
}

const navigation: Array<{ id: AppView; label: string; icon: typeof Plus }> = [
  { id: 'new', label: '新建', icon: Plus },
  { id: 'runs', label: '运行记录', icon: History },
  { id: 'settings', label: '配置', icon: Settings2 },
]

export function AppHeader({ view, config, onChangeView }: AppHeaderProps) {
  const qwenReady = Boolean(config?.qwenApiKey.configured)
  const executorReady = Boolean(config?.researchEngineUrl.value)

  return (
    <header className="app-header">
      <button className="brand" type="button" onClick={() => onChangeView('new')}>
        <span className="brand__mark" aria-hidden="true">R</span>
        <span><strong>Research Bench</strong><small>实证工作流对照台</small></span>
      </button>
      <nav aria-label="主要导航">
        {navigation.map(({ id, label, icon: Icon }) => (
          <button key={id} type="button" className={view === id ? 'is-active' : ''} onClick={() => onChangeView(id)}>
            <Icon size={17} aria-hidden="true" />{label}
          </button>
        ))}
      </nav>
      <button className="config-summary" type="button" onClick={() => onChangeView('settings')}>
        <span className={qwenReady && executorReady ? 'status-dot is-ready' : 'status-dot'} />
        {qwenReady && executorReady ? '服务已就绪' : '需要配置'}
      </button>
    </header>
  )
}
