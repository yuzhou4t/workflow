import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { gsap } from 'gsap'

interface DotGridProps {
  dotSize?: number
  gap?: number
  baseColor?: string
  activeColor?: string
  proximity?: number
  speedTrigger?: number
  shockRadius?: number
  shockStrength?: number
  maxSpeed?: number
  resistance?: number
  returnDuration?: number
}

interface Dot {
  cx: number
  cy: number
  xOffset: number
  yOffset: number
  moving: boolean
}

const rgb = (hex: string) => {
  const match = hex.match(/^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i)
  return match
    ? { r: parseInt(match[1], 16), g: parseInt(match[2], 16), b: parseInt(match[3], 16) }
    : { r: 0, g: 0, b: 0 }
}

export function DotGrid({
  dotSize = 4,
  gap = 18,
  baseColor = '#d9cee0',
  activeColor = '#4f176b',
  proximity = 120,
  speedTrigger = 100,
  shockRadius = 250,
  shockStrength = 5,
  maxSpeed = 5000,
  resistance = 750,
  returnDuration = 1.5,
}: DotGridProps) {
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const dotsRef = useRef<Dot[]>([])
  const pointer = useRef({ x: -1000, y: -1000, lastX: 0, lastY: 0, lastTime: 0 })
  const [isNight, setIsNight] = useState(() => document.documentElement.dataset.theme === 'night')
  const resolvedBaseColor = isNight ? '#5c5962' : baseColor
  const resolvedActiveColor = isNight ? '#b477f0' : activeColor
  const base = useMemo(() => rgb(resolvedBaseColor), [resolvedBaseColor])
  const active = useMemo(() => rgb(resolvedActiveColor), [resolvedActiveColor])

  useEffect(() => {
    const root = document.documentElement
    const syncTheme = () => setIsNight(root.dataset.theme === 'night')
    const observer = new MutationObserver(syncTheme)
    observer.observe(root, { attributes: true, attributeFilter: ['data-theme'] })
    return () => observer.disconnect()
  }, [])

  const build = useCallback(() => {
    const wrap = wrapRef.current
    const canvas = canvasRef.current
    if (!wrap || !canvas) return
    const { width, height } = wrap.getBoundingClientRect()
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    canvas.width = Math.max(1, Math.floor(width * dpr))
    canvas.height = Math.max(1, Math.floor(height * dpr))
    canvas.style.width = `${width}px`
    canvas.style.height = `${height}px`
    const cell = dotSize + gap
    const columns = Math.floor((width + gap) / cell)
    const rows = Math.floor((height + gap) / cell)
    const startX = (width - (cell * columns - gap)) / 2 + dotSize / 2
    const startY = (height - (cell * rows - gap)) / 2 + dotSize / 2
    dotsRef.current = Array.from({ length: columns * rows }, (_, index) => ({
      cx: startX + (index % columns) * cell,
      cy: startY + Math.floor(index / columns) * cell,
      xOffset: 0,
      yOffset: 0,
      moving: false,
    }))
  }, [dotSize, gap])

  useEffect(() => {
    build()
    const observer = new ResizeObserver(build)
    if (wrapRef.current) observer.observe(wrapRef.current)
    return () => observer.disconnect()
  }, [build])

  useEffect(() => {
    let frame = 0
    const draw = () => {
      const canvas = canvasRef.current
      const context = canvas?.getContext('2d')
      if (!canvas || !context) return
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      context.setTransform(dpr, 0, 0, dpr, 0, 0)
      context.clearRect(0, 0, canvas.width / dpr, canvas.height / dpr)
      dotsRef.current.forEach((dot) => {
        const distance = Math.hypot(dot.cx - pointer.current.x, dot.cy - pointer.current.y)
        const amount = Math.max(0, 1 - distance / proximity)
        const r = Math.round(base.r + (active.r - base.r) * amount)
        const g = Math.round(base.g + (active.g - base.g) * amount)
        const b = Math.round(base.b + (active.b - base.b) * amount)
        context.beginPath()
        context.arc(dot.cx + dot.xOffset, dot.cy + dot.yOffset, dotSize / 2, 0, Math.PI * 2)
        context.fillStyle = `rgb(${r},${g},${b})`
        context.fill()
      })
      frame = requestAnimationFrame(draw)
    }
    frame = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(frame)
  }, [active, base, dotSize, proximity])

  useEffect(() => {
    const inside = (event: MouseEvent) => {
      const rect = canvasRef.current?.getBoundingClientRect()
      return rect && event.clientX >= rect.left && event.clientX <= rect.right && event.clientY >= rect.top && event.clientY <= rect.bottom ? rect : null
    }
    const release = (dot: Dot, x: number, y: number) => {
      dot.moving = true
      gsap.killTweensOf(dot)
      gsap.to(dot, {
        xOffset: x,
        yOffset: y,
        duration: Math.max(.18, resistance / 1800),
        ease: 'power2.out',
        onComplete: () => gsap.to(dot, {
          xOffset: 0,
          yOffset: 0,
          duration: returnDuration,
          ease: 'elastic.out(1,.75)',
          onComplete: () => { dot.moving = false },
        }),
      })
    }
    const onMove = (event: MouseEvent) => {
      const rect = inside(event)
      if (!rect) return
      const now = performance.now()
      const dt = pointer.current.lastTime ? now - pointer.current.lastTime : 16
      const vx = Math.max(-maxSpeed, Math.min(maxSpeed, ((event.clientX - pointer.current.lastX) / dt) * 1000))
      const vy = Math.max(-maxSpeed, Math.min(maxSpeed, ((event.clientY - pointer.current.lastY) / dt) * 1000))
      pointer.current = { x: event.clientX - rect.left, y: event.clientY - rect.top, lastX: event.clientX, lastY: event.clientY, lastTime: now }
      if (Math.hypot(vx, vy) <= speedTrigger) return
      dotsRef.current.forEach((dot) => {
        const distance = Math.hypot(dot.cx - pointer.current.x, dot.cy - pointer.current.y)
        if (distance < proximity && !dot.moving) release(dot, (dot.cx - pointer.current.x + vx * .005) * .18, (dot.cy - pointer.current.y + vy * .005) * .18)
      })
    }
    const onClick = (event: MouseEvent) => {
      const rect = inside(event)
      if (!rect) return
      const x = event.clientX - rect.left
      const y = event.clientY - rect.top
      dotsRef.current.forEach((dot) => {
        const distance = Math.hypot(dot.cx - x, dot.cy - y)
        if (distance < shockRadius && !dot.moving) {
          const falloff = Math.max(0, 1 - distance / shockRadius)
          release(dot, (dot.cx - x) * shockStrength * falloff, (dot.cy - y) * shockStrength * falloff)
        }
      })
    }
    window.addEventListener('mousemove', onMove, { passive: true })
    window.addEventListener('click', onClick)
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('click', onClick)
    }
  }, [maxSpeed, proximity, resistance, returnDuration, shockRadius, shockStrength, speedTrigger])

  return <div className="dot-grid" ref={wrapRef} aria-hidden="true"><canvas ref={canvasRef} /></div>
}
