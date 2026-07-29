import { useEffect, useRef } from 'react'
import * as THREE from 'three'

interface MagicRingsProps {
  color?: string
  colorTwo?: string
  speed?: number
  ringCount?: number
  opacity?: number
  lineThickness?: number
  baseRadius?: number
  radiusStep?: number
  scaleRate?: number
  rotation?: number
}

const vertexShader = `
void main() {
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}
`

const fragmentShader = `
precision highp float;

uniform float uTime, uLineThickness, uBaseRadius, uRadiusStep, uScaleRate;
uniform float uOpacity, uRotation;
uniform vec2 uResolution;
uniform vec3 uColor, uColorTwo;
uniform int uRingCount;

const float HP = 1.5707963;
const float CYCLE = 3.45;

float fade(float t) {
  return t < 0.7 ? smoothstep(0.0, 0.7, t) : 1.0 - smoothstep(2.95, CYCLE - 0.2, t);
}

float ring(vec2 p, float radius, float cut, float offset, float px) {
  float t = mod(uTime + offset, CYCLE);
  float r = radius + t / CYCLE * uScaleRate;
  float d = abs(length(p) - r);
  float a = atan(abs(p.y), abs(p.x)) / HP;
  float thickness = max(1.0 - a, 0.5) * px * uLineThickness;
  float highlight = (1.0 - smoothstep(thickness, thickness * 1.5, d)) + 1.0;
  d += pow(cut * a, 3.0) * r;
  return highlight * exp(-10.0 * d) * fade(t);
}

void main() {
  float px = 1.0 / min(uResolution.x, uResolution.y);
  vec2 p = (gl_FragCoord.xy - 0.5 * uResolution.xy) * px;
  float cr = cos(uRotation), sr = sin(uRotation);
  p = mat2(cr, -sr, sr, cr) * p;
  p.x *= 0.56;

  vec3 c = vec3(0.0);
  float divisor = max(float(uRingCount) - 1.0, 1.0);
  for (int i = 0; i < 10; i++) {
    if (i >= uRingCount) break;
    float fi = float(i);
    float outward = fi / divisor;
    float outwardFade = 1.0 - outward * 0.68;
    vec3 ringColor = mix(uColor, uColorTwo, outward);
    ringColor = mix(ringColor, vec3(1.0), outward * 0.34);
    float value = ring(p, uBaseRadius + fi * uRadiusStep, pow(1.5, fi), i == 0 ? 0.0 : 2.95 * fi, px);
    c = mix(c, ringColor, vec3(value * outwardFade));
  }

  gl_FragColor = vec4(c, max(c.r, max(c.g, c.b)) * uOpacity);
}
`

export function MagicRings({
  color = '#60a5fa',
  colorTwo = '#a78bfa',
  speed = 0.42,
  ringCount = 5,
  opacity = 0.68,
  lineThickness = 2.35,
  baseRadius = 0.34,
  radiusStep = 0.072,
  scaleRate = 0.08,
  rotation = -12,
}: MagicRingsProps) {
  const mountRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const mount = mountRef.current
    if (!mount) return

    let renderer: THREE.WebGLRenderer
    try {
      renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true })
    } catch {
      return
    }
    if (!renderer.capabilities.isWebGL2) {
      renderer.dispose()
      return
    }

    renderer.setClearColor(0x000000, 0)
    mount.appendChild(renderer.domElement)

    const scene = new THREE.Scene()
    const camera = new THREE.OrthographicCamera(-0.5, 0.5, 0.5, -0.5, 0.1, 10)
    camera.position.z = 1
    const uniforms = {
      uTime: { value: 0 },
      uResolution: { value: new THREE.Vector2() },
      uColor: { value: new THREE.Color(color) },
      uColorTwo: { value: new THREE.Color(colorTwo) },
      uLineThickness: { value: lineThickness },
      uBaseRadius: { value: baseRadius },
      uRadiusStep: { value: radiusStep },
      uScaleRate: { value: scaleRate },
      uRingCount: { value: ringCount },
      uOpacity: { value: opacity },
      uRotation: { value: rotation * Math.PI / 180 },
    }
    const material = new THREE.ShaderMaterial({
      vertexShader,
      fragmentShader,
      uniforms,
      transparent: true,
      depthWrite: false,
    })
    const plane = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), material)
    scene.add(plane)

    const resize = () => {
      const width = mount.clientWidth
      const height = mount.clientHeight
      const ratio = Math.min(window.devicePixelRatio, 2)
      renderer.setPixelRatio(ratio)
      renderer.setSize(width, height, false)
      uniforms.uResolution.value.set(width * ratio, height * ratio)
    }
    resize()
    const observer = new ResizeObserver(resize)
    observer.observe(mount)

    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    let frame = 0
    const animate = (time: number) => {
      uniforms.uTime.value = time * 0.001 * speed
      renderer.render(scene, camera)
      if (!reducedMotion) frame = requestAnimationFrame(animate)
    }
    frame = requestAnimationFrame(animate)

    return () => {
      cancelAnimationFrame(frame)
      observer.disconnect()
      plane.geometry.dispose()
      material.dispose()
      renderer.dispose()
      if (renderer.domElement.parentNode === mount) mount.removeChild(renderer.domElement)
    }
  }, [baseRadius, color, colorTwo, lineThickness, opacity, radiusStep, ringCount, rotation, scaleRate, speed])

  return <div className="magic-rings" ref={mountRef} aria-hidden="true" />
}
