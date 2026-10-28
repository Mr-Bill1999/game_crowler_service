import * as THREE from "three";

/* What the entrance hero and the companion share: the ASCII pass that turns a small render into
   characters like pencil on paper, its glyph atlas, and the fireball's page-wide overlay. */

export const RAMP = " .:-=+*#%@";
export const GLYPHS = RAMP + "|-/\\";
export const CELL_W = 6;
export const CELL_H = 11;
const ATLAS_W = 32;
const ATLAS_H = 56;

export const VERTEX = `
varying vec2 vUv;
void main() { vUv = position.xy * 0.5 + 0.5; gl_Position = vec4(position.xy, 0.0, 1.0); }
`;

export const FRAGMENT = `
uniform sampler2D uScene;
uniform sampler2D uAtlas;
uniform vec2 uGrid;
uniform vec3 uGround;
uniform float uTime;
varying vec2 vUv;

const float N = ${RAMP.length}.0;
const float G = ${GLYPHS.length}.0;
const vec3 INK = vec3(0.133, 0.149, 0.122);
const vec3 SAGE = vec3(0.486, 0.541, 0.439);

void main() {
  vec2 cell = floor(vUv * uGrid);
  vec2 inCell = fract(vUv * uGrid);
  vec4 scene = texture2D(uScene, (cell + 0.5) / uGrid);
  float glyphIndex = 0.0;
  vec3 ink = INK;
  float alpha = 0.0;

  // The floor he runs on: a faint dotted line across the stage.
  if (cell.y == uGround.x && cell.x >= uGround.y && cell.x <= uGround.z && mod(cell.x, 2.0) < 1.0) {
    glyphIndex = 1.0; alpha = 0.45;
  }

  if (scene.a > 0.35) {
    vec3 c = scene.rgb / max(scene.a, 0.001);
    float lum = pow(dot(c, vec3(0.299, 0.587, 0.114)), 1.0 / 2.2);
    glyphIndex = 1.0 + floor(clamp((1.0 - lum) * 1.25, 0.0, 0.999) * (N - 1.0));
    // The silhouette is traced in lines along its edge.
    vec2 px = 1.0 / uGrid;
    float dx = step(0.35, texture2D(uScene, (cell + vec2(1.5, 0.5)) * px).a) - step(0.35, texture2D(uScene, (cell + vec2(-0.5, 0.5)) * px).a);
    float dy = step(0.35, texture2D(uScene, (cell + vec2(0.5, 1.5)) * px).a) - step(0.35, texture2D(uScene, (cell + vec2(0.5, -0.5)) * px).a);
    if (dx != 0.0 || dy != 0.0) {
      if (dy == 0.0) glyphIndex = N;
      else if (dx == 0.0) glyphIndex = N + 1.0;
      else glyphIndex = dx * dy > 0.0 ? N + 3.0 : N + 2.0;
    }
    float hi = max(max(c.r, c.g), c.b);
    float sat = hi > 0.0 ? (hi - min(min(c.r, c.g), c.b)) / hi : 0.0;
    ink = mix(INK, SAGE, smoothstep(0.08, 0.2, sat));
    alpha = 1.0;
  }

  float glyph = texture2D(uAtlas, vec2((glyphIndex + inCell.x) / G, inCell.y)).r;
  gl_FragColor = vec4(ink, glyph * alpha);
}
`;

export async function atlas() {
  const font = `500 ${ATLAS_H * 0.74}px "JetBrains Mono", ui-monospace, monospace`;
  await document.fonts.load(font).catch(() => undefined);
  const canvas = document.createElement("canvas");
  canvas.width = ATLAS_W * GLYPHS.length;
  canvas.height = ATLAS_H;
  const ctx = canvas.getContext("2d")!;
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#fff";
  ctx.font = font;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  [...GLYPHS].forEach((ch, i) => ctx.fillText(ch, i * ATLAS_W + ATLAS_W / 2, ATLAS_H / 2 + 2));
  return canvas;
}

/** The fireball's own layer over the whole page: the same ASCII grid and colours as the stage,
    so it can leave the stage and cross the panel, the strip, everything, until the window edge. */
export function fireOverlay() {
  const canvas = document.createElement("canvas");
  canvas.setAttribute("aria-hidden", "true");
  Object.assign(canvas.style, { position: "fixed", inset: "0", width: "100vw", height: "100vh", pointerEvents: "none", zIndex: "60" });
  document.body.appendChild(canvas);
  const ctx = canvas.getContext("2d")!;
  const font = `500 ${CELL_H}px "JetBrains Mono", ui-monospace, monospace`;
  let shot: { x: number; y: number; dx: number; dy: number; speed: number } | null = null;
  let puff: { x: number; y: number; age: number } | null = null;
  let drawn = false;
  const size = () => {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(window.innerWidth * dpr);
    canvas.height = Math.round(window.innerHeight * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  };
  size();
  window.addEventListener("resize", size);
  const hash = (a: number, b: number) => { const v = Math.sin(a * 12.9898 + b * 78.233) * 43758.5453; return v - Math.floor(v); };
  const cell = (cx: number, cy: number, ch: string, color: string, alpha: number) => {
    ctx.globalAlpha = alpha;
    ctx.fillStyle = color;
    ctx.fillText(ch, cx * CELL_W, cy * CELL_H + CELL_H / 2);
  };
  return {
    launch(x: number, y: number, dx: number, dy: number, speed = 480) {
      const len = Math.hypot(dx, dy) || 1;
      shot = { x, y, dx: dx / len, dy: dy / len, speed };
    },
    /** The flying head in page pixels. */
    head: () => (shot ? { x: shot.x, y: shot.y } : null),
    /** Put the fireball out where it is, with a puff. */
    stop() {
      if (shot) puff = { x: shot.x, y: shot.y, age: 0 };
      shot = null;
    },
    step(dt: number, now: number) {
      if (!shot && !drawn && !puff) return;
      ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
      drawn = false;
      ctx.font = font;
      ctx.textBaseline = "middle";
      if (puff) {
        puff.age += dt;
        const r = 1 + puff.age * 14;
        for (let i = 0; i < 10; i++) {
          const a = (i / 10) * Math.PI * 2;
          cell(Math.round((puff.x + Math.cos(a) * r * CELL_W) / CELL_W), Math.round((puff.y + Math.sin(a) * r * CELL_W) / CELL_H), i % 2 ? "*" : ".", "#c0663a", Math.max(0, 1 - puff.age / 0.3));
        }
        ctx.globalAlpha = 1;
        drawn = true;
        if (puff.age > 0.3) puff = null;
      }
      if (!shot) return;
      shot.x += shot.dx * shot.speed * dt;
      shot.y += shot.dy * shot.speed * dt;
      const margin = 160;
      if (shot.x < -margin || shot.y < -margin || shot.x > window.innerWidth + margin || shot.y > window.innerHeight + margin) { shot = null; return; }
      const hx = shot.x / CELL_W;
      const hy = shot.y / CELL_H;
      const tick = Math.floor(now / 110);
      // Every cell around the head and along the tail, snapped to the grid, picks its glyph.
      for (let cy = Math.floor(hy) - 6; cy <= Math.ceil(hy) + 6; cy++) {
        for (let cx = Math.floor(hx) - 24; cx <= Math.ceil(hx) + 24; cx++) {
          const ox = cx + 0.5 - hx;
          const oy = (cy + 0.5 - hy) * (CELL_H / CELL_W);
          const dist = Math.hypot(ox, oy);
          const behind = -(ox * shot.dx + oy * shot.dy);
          const side = Math.abs(ox * shot.dy - oy * shot.dx);
          if (dist < 1.7) cell(cx, cy, "@", "#b5542e", 1);
          else if (dist < 3) cell(cx, cy, "#", "#b5542e", 0.95);
          else if (behind > 0 && behind < 20 && side < 2.8 - behind * 0.12 && hash(cx + tick, cy) > behind / 22) {
            const t = behind / 20;
            cell(cx, cy, t < 0.35 ? "*" : t < 0.7 ? "+" : ".", t < 0.5 ? "#c0663a" : "#d9944f", 1 - t * 0.6);
          }
        }
      }
      ctx.globalAlpha = 1;
      drawn = true;
    },
    dispose() {
      window.removeEventListener("resize", size);
      canvas.remove();
    },
  };
}


/** The ASCII pass as a full-canvas triangle over a render target, ready to draw with a camera of its own. */
export function asciiPass(target: THREE.WebGLRenderTarget) {
  const material = new THREE.ShaderMaterial({
    vertexShader: VERTEX,
    fragmentShader: FRAGMENT,
    transparent: true,
    uniforms: {
      uScene: { value: target.texture },
      uAtlas: { value: null as THREE.Texture | null },
      uGrid: { value: new THREE.Vector2(1, 1) },
      uGround: { value: new THREE.Vector3(-1, 0, 0) },
      uTime: { value: 0 },
    },
  });
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array([-1, -1, 0, 3, -1, 0, -1, 3, 0]), 3));
  const quad = new THREE.Mesh(geometry, material);
  quad.frustumCulled = false;
  const scene = new THREE.Scene();
  scene.add(quad);
  const camera = new THREE.OrthographicCamera();
  let disposed = false;
  atlas().then((canvas) => {
    if (disposed) return;
    const tex = new THREE.CanvasTexture(canvas);
    tex.minFilter = THREE.LinearFilter;
    tex.generateMipmaps = false;
    material.uniforms.uAtlas.value = tex;
  });
  return {
    material,
    uniforms: material.uniforms,
    ready: () => material.uniforms.uAtlas.value !== null,
    render: (renderer: THREE.WebGLRenderer) => renderer.render(scene, camera),
    dispose() {
      disposed = true;
      geometry.dispose();
      material.uniforms.uAtlas.value?.dispose();
      material.dispose();
    },
  };
}
