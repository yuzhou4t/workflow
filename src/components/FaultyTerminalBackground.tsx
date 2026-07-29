import { useEffect, useRef } from 'react'

export function FaultyTerminalBackground() {
  const ref = useRef<HTMLCanvasElement | null>(null)
  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    let frame = 0
    const draw = (time: number) => {
      const rect = canvas.getBoundingClientRect(), dpr = Math.min(devicePixelRatio || 1, 2)
      if (canvas.width !== Math.floor(rect.width * dpr) || canvas.height !== Math.floor(rect.height * dpr)) { canvas.width = Math.floor(rect.width * dpr); canvas.height = Math.floor(rect.height * dpr) }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, rect.width, rect.height)
      const step = 9, cx = rect.width / 2, cy = rect.height / 2, chars = '01<>[]{}+=*#@/\\'
      const nightMode = document.documentElement.dataset.theme === 'night'
      const glyphColor = nightMode ? '218, 199, 241' : '124, 58, 237'
      ctx.font = '7px ui-monospace, monospace'; ctx.textAlign = 'center'
      for (let y = 0; y < rect.height; y += step) for (let x = 0; x < rect.width; x += step) {
        const dx = (x - cx) / Math.max(cx, 1), dy = (y - cy) / Math.max(cy, 1), r = Math.hypot(dx, dy), a = Math.atan2(dy, dx)
        const band = Math.sin(r * 22 - a * 6 + time * .0012) + Math.sin(a * 9 + time * .0008)
        if (band > .68 || (r > .68 && band > .2)) { const alpha = Math.min(nightMode ? .82 : .72, .16 + (band + 1) * .18); ctx.fillStyle = `rgba(${glyphColor}, ${alpha})`; ctx.fillText(chars[(Math.floor(x / step) * 7 + Math.floor(y / step) * 11) % chars.length], x, y) }
      }
      frame = requestAnimationFrame(draw)
    }
    frame = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(frame)
  }, [])
  return <canvas ref={ref} className="faulty-terminal" aria-hidden="true" />
}
