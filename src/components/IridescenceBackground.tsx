import { useEffect, useRef } from 'react'
import { Mesh, Program, Renderer, Triangle } from 'ogl'

const vertex = `attribute vec2 position; attribute vec2 uv; varying vec2 vUv; void main(){ vUv=uv; gl_Position=vec4(position,0.,1.); }`
const fragment = `precision highp float; uniform vec3 iResolution; uniform float iTime; varying vec2 vUv;
void main(){ vec2 uv=(vUv-.5)*vec2(iResolution.x/iResolution.y,1.); float t=iTime*.6; float w=sin(uv.x*4.2+t)+sin((uv.y+uv.x*.45)*5.1-t*.72)+sin(length(uv)*7.-t*.45); vec3 s=.5+.5*sin(w*.88+vec3(0.,2.1,4.2)); vec3 base=vec3(.315,.225,.455); vec3 color=mix(base,mix(vec3(.25,.46,.94),vec3(.70,.38,.98),s.r),.48+s.g*.28)*(.44+s.b*.35); float fade=1.-smoothstep(.15,1.15,length(uv))*.55; gl_FragColor=vec4(color,(.15+s.g*.16)*fade); }`

export function IridescenceBackground() {
  const hostRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    let renderer: Renderer
    try { renderer = new Renderer({ alpha: true, antialias: true, dpr: Math.min(window.devicePixelRatio || 1, 1.5) }) } catch { return }
    const gl = renderer.gl
    const canvas = gl.canvas as HTMLCanvasElement
    canvas.setAttribute('aria-hidden', 'true')
    host.appendChild(canvas)
    const uniforms = { iResolution: { value: [1, 1, 1] }, iTime: { value: 0 } }
    const program = new Program(gl, { vertex, fragment, uniforms })
    const geometry = new Triangle(gl)
    const mesh = new Mesh(gl, { geometry, program })
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const resize = () => { const bounds = host.getBoundingClientRect(); renderer.setSize(Math.max(bounds.width, 1), Math.max(bounds.height, 1)); uniforms.iResolution.value = [gl.drawingBufferWidth, gl.drawingBufferHeight, 1] }
    resize(); const observer = new ResizeObserver(resize); observer.observe(host)
    let frame = 0
    const render = (time: number) => { uniforms.iTime.value = reduceMotion ? 0 : time * .001; renderer.render({ scene: mesh }); if (!reduceMotion) frame = requestAnimationFrame(render) }
    frame = requestAnimationFrame(render)
    return () => { cancelAnimationFrame(frame); observer.disconnect(); canvas.remove(); program.remove(); geometry.remove() }
  }, [])
  return <div ref={hostRef} className="studio-iridescence" aria-hidden="true" />
}
