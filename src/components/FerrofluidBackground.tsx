import { useEffect, useRef } from 'react'
import { Mesh, Program, Renderer, Triangle } from 'ogl'

const vertex = `
attribute vec2 position;
attribute vec2 uv;
varying vec2 vUv;
void main() {
  vUv = uv;
  gl_Position = vec4(position, 0.0, 1.0);
}`

const fragment = `
precision highp float;
uniform vec3 iResolution;
uniform float iTime;
varying vec2 vUv;

#define PI 3.14159265

float hash(vec3 p) {
  p = fract(p * .1031);
  p += dot(p, p.zyx + 33.33);
  return fract((p.x + p.y) * p.z);
}

float sinlerp(float a, float b, float w) {
  return mix(a, b, (sin(w * PI - PI / 2.0) + 1.0) / 2.0);
}

float vn(vec2 p, float s, float seed) {
  vec2 cell = floor(p / s);
  vec2 rel = mod(p, s);
  float g1 = hash(vec3(cell, seed));
  float g2 = hash(vec3(cell.x + 1.0, cell.y, seed));
  float g3 = hash(vec3(cell.x + 1.0, cell.y + 1.0, seed));
  float g4 = hash(vec3(cell.x, cell.y + 1.0, seed));
  return sinlerp(sinlerp(g1, g2, rel.x / s), sinlerp(g4, g3, rel.x / s), rel.y / s);
}

float dbn(vec2 p, float s, float seed) {
  float o = s / 2.0;
  return (
    2.0 * vn(p, s, seed) +
    1.5 * vn(p + vec2(o, o), s, seed + .1) +
    1.25 * vn(p + vec2(-o, o), s, seed + .2) +
    1.125 * vn(p + vec2(o, -o), s, seed + .3) +
    vn(p + vec2(-o, -o), s, seed + .4)
  ) / 7.0;
}

float smin(float a, float b, float k) {
  float r = exp2(-a / k) + exp2(-b / k);
  return -k * log2(r);
}

void main() {
  vec2 p = vUv * iResolution.xy / iResolution.y * 430.0;
  float t = iTime * 22.0;
  vec2 flow = vec2(.16, -1.0);
  vec2 side = vec2(1.0, .16);
  float d1 = vn(p + side * t, 60.0, 10.0) * 45.0;
  float d2 = vn(p - side * t, 120.0, 15.0) * 82.0;
  float a = dbn(p + d1 + flow * t * .55, 40.0, 1.0);
  float b = dbn(p + d2 - flow * t * .42, 40.0, 0.0);
  float merged = smin(a, b, .11);
  float band = (.24 - abs((merged - .4) * 2.0)) * 5.0;
  float light = clamp(band - vn(p + flow * t, 60.0, 12.0) * 1.35, 0.0, 1.0);
  light = pow(light, 2.3) * 1.8;
  float blend = clamp(.5 + (a - b) * .8, 0.0, 1.0);
  vec3 blue = vec3(.18, .34, .72);
  vec3 violet = vec3(.42, .24, .72);
  vec3 color = mix(blue, violet, blend) * light;
  float alpha = max(color.r, max(color.g, color.b)) * .22;
  gl_FragColor = vec4(color, alpha);
}`

export function FerrofluidBackground() {
  const hostRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const host = hostRef.current
    if (!host) return

    let renderer: Renderer
    try {
      renderer = new Renderer({ alpha: true, antialias: true, dpr: Math.min(window.devicePixelRatio || 1, 1.5) })
    } catch {
      return
    }

    const gl = renderer.gl
    gl.clearColor(0, 0, 0, 0)
    const canvas = gl.canvas as HTMLCanvasElement
    canvas.setAttribute('aria-hidden', 'true')
    host.appendChild(canvas)

    const uniforms = {
      iResolution: { value: [1, 1, 1] },
      iTime: { value: 0 },
    }
    const program = new Program(gl, { vertex, fragment, uniforms })
    const geometry = new Triangle(gl)
    const mesh = new Mesh(gl, { geometry, program })
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    let frame = 0

    const resize = () => {
      const bounds = host.getBoundingClientRect()
      renderer.setSize(Math.max(bounds.width, 1), Math.max(bounds.height, 1))
      uniforms.iResolution.value = [gl.drawingBufferWidth, gl.drawingBufferHeight, 1]
    }
    resize()
    const observer = new ResizeObserver(resize)
    observer.observe(host)

    const render = (time: number) => {
      uniforms.iTime.value = reduceMotion ? 0 : time * .001
      renderer.render({ scene: mesh })
      if (!reduceMotion) frame = requestAnimationFrame(render)
    }
    frame = requestAnimationFrame(render)

    return () => {
      cancelAnimationFrame(frame)
      observer.disconnect()
      canvas.remove()
      program.remove()
      geometry.remove()
    }
  }, [])

  return <div ref={hostRef} className="studio-ferrofluid" aria-hidden="true" />
}
