import { History, Moon, Sun } from 'lucide-react'
import { useState } from 'react'
import type { RuntimeConfigStatus } from '../runtime/types'
import { getTheme, toggleTheme, type ThemeMode } from '../runtime/theme'

export type AppView = 'new' | 'runs' | 'settings'

interface AppHeaderProps {
  view: AppView
  config: RuntimeConfigStatus | null
  onChangeView: (view: AppView) => void
  onShowHistory: () => void
}

export function AppHeader({ view, config, onChangeView, onShowHistory }: AppHeaderProps) {
  const qwenReady = Boolean(config?.qwenApiKey.configured)
  const executorReady = Boolean(config?.researchEngineUrl.value)
  const [theme, setTheme] = useState<ThemeMode>(() => getTheme())

  return (
    <header className="app-header">
      <button className="brand" type="button" onClick={() => onChangeView('new')} title="返回首页 · 进行任务">
        <span className="brand__mark" aria-hidden="true">R</span>
        <span><strong>Research Bench</strong><small>实证工作流对照台</small></span>
      </button>
      <div className="header-actions">
        <button type="button" className={`header-link ${view === 'runs' ? 'is-active' : ''}`} onClick={onShowHistory}>
          <History size={15} aria-hidden="true" />查看历史记录
        </button>
        <button
          className="theme-toggle"
          type="button"
          aria-label={theme === 'night' ? '切换到日间模式' : '切换到夜间模式'}
          title={theme === 'night' ? '日间模式' : '夜间模式'}
          onClick={() => setTheme(toggleTheme())}
        >
          {theme === 'night' ? <Sun size={16} aria-hidden="true" /> : <Moon size={16} aria-hidden="true" />}
        </button>
        <button className={`config-summary ${view === 'settings' ? 'is-active' : ''}`} type="button" onClick={() => onChangeView('settings')}>
          <span className={qwenReady && executorReady ? 'status-dot is-ready' : 'status-dot'} />
          {qwenReady && executorReady ? '服务已就绪' : '需要配置'}
        </button>
      </div>
    </header>
  )
}
