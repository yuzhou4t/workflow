/**
 * 主题控制器：日间(day) / 夜间(night)。
 * - 读取顺序：localStorage 覆盖 > 系统 prefers-color-scheme > 默认 day
 * - 切换时给 <body> 加 .theming 触发一次性丝滑整屏过渡
 * - 主题写在 <html data-theme>，供 theme.css 的 :root[data-theme] 消费
 */
export type ThemeMode = 'day' | 'night'

const STORAGE_KEY = 'hw-theme'
const TRANSITION_MS = 600

export function getTheme(): ThemeMode {
  const saved = localStorage.getItem(STORAGE_KEY)
  if (saved === 'day' || saved === 'night') return saved
  const prefersDark = typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-color-scheme: dark)').matches
  return prefersDark ? 'night' : 'day'
}

export function applyTheme(mode: ThemeMode, animate = false): void {
  if (animate) {
    document.body.classList.add('theming')
    window.setTimeout(() => document.body.classList.remove('theming'), TRANSITION_MS)
  }
  document.documentElement.setAttribute('data-theme', mode)
  localStorage.setItem(STORAGE_KEY, mode)
}

export function initTheme(): void {
  applyTheme(getTheme())
}

export function toggleTheme(): ThemeMode {
  const next: ThemeMode = getTheme() === 'night' ? 'day' : 'night'
  applyTheme(next, true)
  return next
}
