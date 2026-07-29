import { useEffect, useRef } from 'react'

export function ElectricBorder() {
  const ref = useRef<HTMLCanvasElement | null>(null)

  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const context = canvas.getContext('2d')
    if (!context) return
    let frame = 0

    const draw = (time: number) => {
      const bounds = canvas.getBoundingClientRect()
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      if (canvas.width !== Math.round(bounds.width * dpr) || canvas.height !== Math.round(bounds.height * dpr)) {
        canvas.width = Math.round(bounds.width * dpr)
        canvas.height = Math.round(bounds.height * dpr)
      }
      context.setTransform(dpr, 0, 0, dpr, 0, 0)
      context.clearRect(0, 0, bounds.width, bounds.height)

      const width = bounds.width
      const height = bounds.height
      const inset = 2
      const jitter = (index: number) => Math.sin(time * .007 + index * 17.31) * 2.2 + Math.sin(time * .015 + index * 4.7) * 1.15
      const edge = (x1: number, y1: number, x2: number, y2: number, start: number) => {
        const points: Array<[number, number]> = []
        const segments = Math.max(14, Math.round(Math.hypot(x2 - x1, y2 - y1) / 11))
        for (let index = 0; index <= segments; index += 1) {
          const progress = index / segments
          const x = x1 + (x2 - x1) * progress
          const y = y1 + (y2 - y1) * progress
          const sideways = jitter(start + index)
          const normalX = -(y2 - y1) / Math.max(Math.hypot(x2 - x1, y2 - y1), 1)
          const normalY = (x2 - x1) / Math.max(Math.hypot(x2 - x1, y2 - y1), 1)
          points.push([x + normalX * sideways, y + normalY * sideways])
        }
        return points
      }

      const path = [
        ...edge(inset + 10, inset, width - inset - 10, inset, 1),
        ...edge(width - inset, inset + 10, width - inset, height - inset - 10, 80),
        ...edge(width - inset - 10, height - inset, inset + 10, height - inset, 160),
        ...edge(inset, height - inset - 10, inset, inset + 10, 240),
      ]
      const render = (stroke: string, lineWidth: number, blur: number) => {
        context.save()
        context.beginPath()
        path.forEach(([x, y], index) => index ? context.lineTo(x, y) : context.moveTo(x, y))
        context.closePath()
        context.strokeStyle = stroke
        context.lineWidth = lineWidth
        context.shadowColor = '#b89ad5'
        context.shadowBlur = blur
        context.stroke()
        context.restore()
      }
      render('rgba(174, 132, 215, .36)', 4.5, 13)
      render('rgba(231, 216, 244, .92)', 1.1, 5)
      frame = requestAnimationFrame(draw)
    }
    frame = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(frame)
  }, [])

  return <canvas ref={ref} className="electric-border" aria-hidden="true" />
}
