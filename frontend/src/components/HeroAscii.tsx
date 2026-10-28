import { useEffect, useRef } from "react";
import * as THREE from "three";
import { MeshoptDecoder } from "three/examples/jsm/libs/meshopt_decoder.module.js";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { padInput, type Action } from "./padInput";

/* The entrance hero: a small rigged wizard from Blender (public/hero/hero.glb), rendered live
   and drawn in ASCII like pencil on paper — the darker a cell, the denser its character. The
   gamepad's face buttons play an action (jump, wave, dance, fireball), the left stick turns
   him, the d-pad turns him by a quarter. The fireball leaves his hand as ASCII fire. */

const MODEL = "/hero/hero.glb";
const RAMP = " .:-=+*#%@";
const GLYPHS = RAMP + "|-/\\";
const CELL_W = 6;
const CELL_H = 11;
const ATLAS_W = 32;
const ATLAS_H = 56;
const CLIPS: Record<Action, string> = { jump: "Hero_Jump", wave: "Hero_Wave", dance: "Hero_Dance", fireball: "Hero_Fireball" };

const VERTEX = `
varying vec2 vUv;
void main() { vUv = position.xy * 0.5 + 0.5; gl_Position = vec4(position.xy, 0.0, 1.0); }
`;

const FRAGMENT = `
uniform sampler2D uScene;
uniform sampler2D uAtlas;
uniform vec2 uGrid;
uniform vec2 uFire;
uniform float uFireAge;
uniform float uTime;
varying vec2 vUv;

const float N = ${RAMP.length}.0;
const float G = ${GLYPHS.length}.0;

void main() {
  vec2 cell = floor(vUv * uGrid);
  vec2 inCell = fract(vUv * uGrid);
  vec4 scene = texture2D(uScene, (cell + 0.5) / uGrid);
  float glyphIndex = 0.0;
  vec3 ink = vec3(0.133, 0.149, 0.122);
  float alpha = 0.0;

  if (scene.a > 0.35) {
    vec3 c = scene.rgb / max(scene.a, 0.001);
    float lum = pow(dot(c, vec3(0.299, 0.587, 0.114)), 1.0 / 2.2);
    float dark = clamp(1.0 - lum, 0.0, 1.0);
    glyphIndex = 1.0 + floor(clamp(dark * 1.25, 0.0, 0.999) * (N - 1.0));
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
    ink = mix(ink, vec3(0.486, 0.541, 0.439), smoothstep(0.08, 0.2, sat));
    alpha = 1.0;
  }

  // The fireball: a hot core flying right on a slight arc, a flickering tail behind it.
  if (uFireAge >= 0.0) {
    float aspect = ${(CELL_H / CELL_W).toFixed(3)};
    vec2 head = uFire + vec2(2.0 + uFireAge * 52.0, sin(uFireAge * 9.0) * 0.45);
    vec2 d = (cell - head) * vec2(1.0, aspect);
    float dist = length(d);
    float behind = head.x - cell.x;
    float noise = fract(sin(dot(cell + floor(uTime * 18.0), vec2(12.9898, 78.233))) * 43758.5453);
    vec3 core = vec3(0.71, 0.33, 0.18);
    vec3 tail = vec3(0.85, 0.58, 0.31);
    if (dist < 1.7) {
      glyphIndex = N - 1.0; ink = core; alpha = 1.0;
    } else if (dist < 3.0) {
      glyphIndex = N - 3.0; ink = core; alpha = 0.95;
    } else if (behind > 0.0 && behind < 18.0 && abs(d.y) < 2.8 - behind * 0.13 && noise > behind / 20.0) {
      float t = behind / 18.0;
      glyphIndex = t < 0.35 ? N - 4.0 : (t < 0.7 ? N - 5.0 : 1.0);
      ink = mix(core, tail, t);
      alpha = 1.0 - t * 0.6;
    }
  }

  float glyph = texture2D(uAtlas, vec2((glyphIndex + inCell.x) / G, inCell.y)).r;
  gl_FragColor = vec4(ink, glyph * alpha);
}
`;

async function atlas() {
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

export default function HeroAscii() {
  const host = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ alpha: true, antialias: false, premultipliedAlpha: false });
    } catch {
      return;
    }
    renderer.setClearColor(0x000000, 0);
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    el.appendChild(renderer.domElement);

    // The 3D scene, drawn into a small target: one texel per character cell.
    const scene = new THREE.Scene();
    const key = new THREE.DirectionalLight(0xffffff, 2.6);
    key.position.set(-3, 4, 3);
    const rim = new THREE.DirectionalLight(0xffffff, 0.6);
    rim.position.set(4, 2, -2);
    scene.add(key, rim, new THREE.AmbientLight(0xffffff, 0.55));
    const camera = new THREE.PerspectiveCamera(26, 1, 0.1, 50);
    const target = new THREE.WebGLRenderTarget(8, 8, { minFilter: THREE.LinearFilter, magFilter: THREE.LinearFilter });

    // The ASCII pass over the whole canvas.
    const ascii = new THREE.ShaderMaterial({
      vertexShader: VERTEX,
      fragmentShader: FRAGMENT,
      transparent: true,
      uniforms: {
        uScene: { value: target.texture },
        uAtlas: { value: null },
        uGrid: { value: new THREE.Vector2(1, 1) },
        uFire: { value: new THREE.Vector2() },
        uFireAge: { value: -1 },
        uTime: { value: 0 },
      },
    });
    const quadGeometry = new THREE.BufferGeometry();
    quadGeometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array([-1, -1, 0, 3, -1, 0, -1, 3, 0]), 3));
    const quad = new THREE.Mesh(quadGeometry, ascii);
    quad.frustumCulled = false;
    const post = new THREE.Scene();
    post.add(quad);
    const postCamera = new THREE.OrthographicCamera();

    let disposed = false;
    atlas().then((canvas) => {
      if (disposed) return;
      const tex = new THREE.CanvasTexture(canvas);
      tex.minFilter = THREE.LinearFilter;
      tex.generateMipmaps = false;
      ascii.uniforms.uAtlas.value = tex;
    });

    // The canvas may be wider than its column (so the fireball can fly on); the hero stays
    // centred on the column through a view offset.
    const fit = () => {
      const column = el.clientWidth;
      const w = renderer.domElement.clientWidth || column;
      const h = el.clientHeight;
      if (!w || !h) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      renderer.setPixelRatio(dpr);
      renderer.setSize(w, h, false);
      const cols = Math.max(8, Math.floor(w / CELL_W));
      const rows = Math.max(8, Math.floor(h / CELL_H));
      ascii.uniforms.uGrid.value.set(cols, rows);
      target.setSize(cols * 2, rows * 2);
      const full = Math.max(w, 2 * w - column);
      camera.aspect = full / h;
      camera.setViewOffset(full, h, full - w, 0, w, h);
      camera.position.set(0, 1.5, 6.2);
      camera.lookAt(0, 1.3, 0);
      camera.updateProjectionMatrix();
    };
    const resize = new ResizeObserver(fit);
    resize.observe(el);
    fit();

    let mixer: THREE.AnimationMixer | null = null;
    let idle: THREE.AnimationAction | null = null;
    let current: THREE.AnimationAction | null = null;
    const actions = new Map<string, THREE.AnimationAction>();
    const hero = new THREE.Group();
    scene.add(hero);
    let hand: THREE.Object3D | undefined;
    let fireAt = -1;
    let firePending = -1;

    const loader = new GLTFLoader();
    loader.setMeshoptDecoder(MeshoptDecoder);
    loader.load(MODEL, (gltf) => {
      if (disposed) return;
      hero.add(gltf.scene);
      // GLTFLoader drops the dot from Blender names: "Hand.L" arrives as "HandL".
      hand = gltf.scene.getObjectByName("HandL") ?? gltf.scene.getObjectByName("Hand.L");
      mixer = new THREE.AnimationMixer(gltf.scene);
      for (const clip of gltf.animations) actions.set(clip.name, mixer.clipAction(clip));
      idle = actions.get("Hero_Idle") ?? null;
      idle?.play();
      current = idle;
      mixer.addEventListener("finished", () => {
        if (!idle || !current || current === idle) return;
        idle.reset().play();
        current.crossFadeTo(idle, 0.35, false);
        current = idle;
      });
    }, undefined, () => undefined);

    const play = (action: Action) => {
      const next = actions.get(CLIPS[action]);
      if (!next || !current) return;
      next.reset();
      next.setLoop(THREE.LoopOnce, 1);
      next.clampWhenFinished = true;
      next.play();
      if (current !== next) current.crossFadeTo(next, 0.18, false);
      current = next;
      if (action === "fireball") firePending = performance.now() + 650;
    };
    const offAct = padInput.onAct(play);

    let yaw = 0;
    let yawTarget = 0;
    const offTurn = padInput.onTurn((step) => { yawTarget += step * (Math.PI / 2); });

    const clock = new THREE.Clock();
    const handPos = new THREE.Vector3();
    let frame = 0;
    const tick = (now: number) => {
      frame = requestAnimationFrame(tick);
      const dt = Math.min(clock.getDelta(), 1 / 20);
      mixer?.update(dt);
      // The left stick turns him while held; the d-pad sets where he settles.
      const stickYaw = padInput.left.x * 1.1;
      yaw += (yawTarget + stickYaw - yaw) * Math.min(1, dt * 6);
      hero.rotation.y = yaw;

      // The fireball leaves the throwing (left) hand at the moment of release.
      if (firePending > 0 && now >= firePending && hand) {
        firePending = -1;
        hand.getWorldPosition(handPos).project(camera);
        const grid = ascii.uniforms.uGrid.value as THREE.Vector2;
        ascii.uniforms.uFire.value.set((handPos.x * 0.5 + 0.5) * grid.x, (handPos.y * 0.5 + 0.5) * grid.y);
        fireAt = now;
      }
      const grid = ascii.uniforms.uGrid.value as THREE.Vector2;
      const age = fireAt > 0 ? (now - fireAt) / 1000 : -1;
      if (age > (grid.x + 20) / 52) fireAt = -1;
      ascii.uniforms.uFireAge.value = fireAt > 0 ? age : -1;
      ascii.uniforms.uTime.value = now / 1000;

      renderer.setRenderTarget(target);
      renderer.clear();
      renderer.render(scene, camera);
      renderer.setRenderTarget(null);
      renderer.clear();
      if (ascii.uniforms.uAtlas.value) renderer.render(post, postCamera);
    };
    frame = requestAnimationFrame(tick);

    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      resize.disconnect();
      offAct();
      offTurn();
      mixer?.stopAllAction();
      scene.traverse((node) => {
        const mesh = node as THREE.Mesh;
        if (!mesh.isMesh) return;
        mesh.geometry.dispose();
        (Array.isArray(mesh.material) ? mesh.material : [mesh.material]).forEach((m) => m.dispose());
      });
      quadGeometry.dispose();
      (ascii.uniforms.uAtlas.value as THREE.Texture | null)?.dispose();
      ascii.dispose();
      target.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    };
  }, []);

  return <div ref={host} className="entry-hero" aria-hidden="true" />;
}
