import { useEffect, useRef } from "react";
import * as THREE from "three";
import { MeshoptDecoder } from "three/examples/jsm/libs/meshopt_decoder.module.js";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { padInput, type Action } from "./padInput";

/* The entrance stage: a small rigged wizard from Blender (public/hero/hero.glb) rendered live
   and drawn in ASCII like pencil on paper — the darker a cell, the denser its character. He
   starts on top of the gamepad and runs along a dotted floor within the stage:
   - run: the left stick, the d-pad left/right, A/D or ←/→ (he turns side-on while running);
   - jump: pad bottom, d-pad up, Space, W or ↑ — also on the run;
   - wave (pad right, E), dance (pad left, Q);
   - fireball (pad top, F, or a click on the stage — thrown towards the click). The fireball
     flies across the whole page on its own overlay, in the same ASCII grid and colours. */

const MODEL = "/hero/hero.glb";
const RAMP = " .:-=+*#%@";
const GLYPHS = RAMP + "|-/\\";
const CELL_W = 6;
const CELL_H = 11;
const ATLAS_W = 32;
const ATLAS_H = 56;
const CLIPS: Record<Action, string> = { jump: "Hero_Jump", wave: "Hero_Wave", dance: "Hero_Dance", fireball: "Hero_Fireball" };
const KEYS: Record<string, Action> = { Space: "jump", KeyW: "jump", ArrowUp: "jump", KeyF: "fireball", KeyE: "wave", KeyQ: "dance" };
/** How tall the hero is, as a share of the stage height. */
const HERO_H = 0.3;
const RUN_SPEED = 2.8;

const VERTEX = `
varying vec2 vUv;
void main() { vUv = position.xy * 0.5 + 0.5; gl_Position = vec4(position.xy, 0.0, 1.0); }
`;

const FRAGMENT = `
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

/** The fireball's own layer over the whole page: the same ASCII grid and colours as the stage,
    so it can leave the stage and cross the panel, the strip, everything, until the window edge. */
function fireOverlay() {
  const canvas = document.createElement("canvas");
  canvas.setAttribute("aria-hidden", "true");
  Object.assign(canvas.style, { position: "fixed", inset: "0", width: "100vw", height: "100vh", pointerEvents: "none", zIndex: "60" });
  document.body.appendChild(canvas);
  const ctx = canvas.getContext("2d")!;
  const font = `500 ${CELL_H}px "JetBrains Mono", ui-monospace, monospace`;
  let shot: { x: number; y: number; dx: number; dy: number } | null = null;
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
    launch(x: number, y: number, dx: number, dy: number) {
      const len = Math.hypot(dx, dy) || 1;
      shot = { x, y, dx: dx / len, dy: dy / len };
    },
    step(dt: number, now: number) {
      if (!shot && !drawn) return;
      ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
      drawn = false;
      if (!shot) return;
      shot.x += shot.dx * 480 * dt;
      shot.y += shot.dy * 480 * dt;
      const margin = 160;
      if (shot.x < -margin || shot.y < -margin || shot.x > window.innerWidth + margin || shot.y > window.innerHeight + margin) { shot = null; return; }
      ctx.font = font;
      ctx.textBaseline = "middle";
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
    const camera = new THREE.PerspectiveCamera(26, 1, 0.1, 80);
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
        uGround: { value: new THREE.Vector3(-1, 0, 0) },
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

    // The hero starts on top of the gamepad (.entry-object, a sibling on the stage); a view offset
    // puts the world's origin there. He may run between the stage edges.
    const edge = { left: -4, right: 6 };
    const tmp = new THREE.Vector3();
    const toCell = (p: THREE.Vector3) => {
      const grid = ascii.uniforms.uGrid.value as THREE.Vector2;
      tmp.copy(p).project(camera);
      return new THREE.Vector2((tmp.x * 0.5 + 0.5) * grid.x, (tmp.y * 0.5 + 0.5) * grid.y);
    };
    const fit = () => {
      const w = el.clientWidth;
      const h = el.clientHeight;
      if (!w || !h) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      renderer.setPixelRatio(dpr);
      renderer.setSize(w, h, false);
      const cols = Math.max(8, Math.floor(w / CELL_W));
      const rows = Math.max(8, Math.floor(h / CELL_H));
      ascii.uniforms.uGrid.value.set(cols, rows);
      target.setSize(cols * 2, rows * 2);
      const box = el.getBoundingClientRect();
      const pad = el.parentElement?.querySelector(".entry-object")?.getBoundingClientRect();
      const heroX = pad ? Math.min(0.85, Math.max(0.15, (pad.left + pad.width / 2 - box.left) / w)) : 0.5;
      // The pad stands almost straight on on touch screens, so its top edge sits higher in its box.
      const touch = window.matchMedia("(pointer: coarse)").matches || window.innerWidth <= 720;
      const feet = pad ? Math.min(0.95, (pad.top + pad.height * (touch ? 0.12 : 0.2) - box.top) / h) : 0.95;
      const full = w * 2 * Math.max(heroX, 1 - heroX);
      camera.aspect = full / h;
      camera.setViewOffset(full, h, full / 2 - heroX * w, 0, w, h);
      const viewH = 2.6 / HERO_H;
      const dist = viewH / 2 / Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
      const centreY = (feet - 0.5) * viewH;
      camera.position.set(0, centreY, dist);
      camera.lookAt(0, centreY, 0);
      camera.updateProjectionMatrix();
      camera.updateMatrixWorld();
      const at = (ndcX: number) => {
        const p = new THREE.Vector3(ndcX, 0, 0.5).unproject(camera).sub(camera.position).normalize();
        return camera.position.x + p.x * (-camera.position.z / p.z);
      };
      edge.left = at(-1) + 0.5;
      edge.right = at(1) - 0.5;
      const from = toCell(new THREE.Vector3(edge.left - 0.3, 0, 0));
      const to = toCell(new THREE.Vector3(edge.right + 0.3, 0, 0));
      ascii.uniforms.uGround.value.set(Math.floor(from.y) - 1, Math.floor(from.x), Math.floor(to.x));
    };
    const resize = new ResizeObserver(fit);
    resize.observe(el);
    fit();

    let mixer: THREE.AnimationMixer | null = null;
    let current: THREE.AnimationAction | null = null;
    let oneShot = false;
    let currentAction: Action | null = null;
    const actions = new Map<string, THREE.AnimationAction>();
    const hero = new THREE.Group();
    scene.add(hero);
    let hand: THREE.Object3D | undefined;
    let moving = false;

    // Idle or run, whichever fits the movement; one-shot actions play over it and hand back.
    const base = () => actions.get(moving ? "Hero_Run" : "Hero_Idle") ?? null;
    const settle = () => {
      const next = base();
      if (!next || next === current) return;
      next.reset().play();
      current?.crossFadeTo(next, 0.22, false);
      current = next;
    };

    const loader = new GLTFLoader();
    loader.setMeshoptDecoder(MeshoptDecoder);
    loader.load(MODEL, (gltf) => {
      if (disposed) return;
      hero.add(gltf.scene);
      // GLTFLoader drops the dot from Blender names: "Hand.L" arrives as "HandL".
      hand = gltf.scene.getObjectByName("HandL") ?? gltf.scene.getObjectByName("Hand.L");
      mixer = new THREE.AnimationMixer(gltf.scene);
      for (const clip of gltf.animations) actions.set(clip.name, mixer.clipAction(clip));
      current = actions.get("Hero_Idle") ?? null;
      current?.play();
      mixer.addEventListener("finished", () => {
        oneShot = false;
        currentAction = null;
        settle();
      });
    }, undefined, () => undefined);

    const fire = fireOverlay();
    let firePending: { at: number; aim: THREE.Vector3 | null } | null = null;
    let facing = 1;

    const play = (action: Action, aim: THREE.Vector3 | null = null) => {
      const next = actions.get(CLIPS[action]);
      if (!next || !current) return;
      next.reset();
      next.setLoop(THREE.LoopOnce, 1);
      next.clampWhenFinished = true;
      next.play();
      if (current !== next) current.crossFadeTo(next, 0.16, false);
      current = next;
      oneShot = true;
      currentAction = action;
      if (action === "fireball") firePending = { at: performance.now() + 650, aim };
    };
    const offAct = padInput.onAct((action) => play(action));

    // Desktop: held keys run, single keys act; a click on the stage throws a fireball at it.
    const held = new Set<string>();
    const keydown = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLElement && e.target.closest("input, textarea, select, [contenteditable]")) return;
      if (["KeyA", "KeyD", "ArrowLeft", "ArrowRight"].includes(e.code)) { held.add(e.code); e.preventDefault(); return; }
      const action = KEYS[e.code];
      if (action) { if (!e.repeat) play(action); e.preventDefault(); }
    };
    const keyup = (e: KeyboardEvent) => held.delete(e.code);
    const blur = () => held.clear();
    window.addEventListener("keydown", keydown);
    window.addEventListener("keyup", keyup);
    window.addEventListener("blur", blur);
    const stage = el.parentElement;
    const ray = new THREE.Raycaster();
    const ground = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);
    const click = (e: PointerEvent) => {
      if (e.button !== 0 || !(e.target instanceof Element) || e.target.closest(".entry-object, a, button")) return;
      const rect = renderer.domElement.getBoundingClientRect();
      const ndc = new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
      ray.setFromCamera(ndc, camera);
      play("fireball", ray.ray.intersectPlane(ground, new THREE.Vector3()));
    };
    stage?.addEventListener("pointerdown", click);

    const clock = new THREE.Clock();
    const handPos = new THREE.Vector3();
    let yaw = 0;
    let frame = 0;
    const tick = (now: number) => {
      frame = requestAnimationFrame(tick);
      const dt = Math.min(clock.getDelta(), 1 / 20);
      mixer?.update(dt);

      // Running: the strongest of stick, d-pad and keys, kept inside the stage.
      let input = padInput.left.x + padInput.dpad.x;
      if (held.has("KeyA") || held.has("ArrowLeft")) input -= 1;
      if (held.has("KeyD") || held.has("ArrowRight")) input += 1;
      input = THREE.MathUtils.clamp(input, -1, 1);
      const busy = oneShot && currentAction !== "jump";
      const speed = busy ? 0 : input * RUN_SPEED;
      hero.position.x = THREE.MathUtils.clamp(hero.position.x + speed * dt, edge.left, edge.right);
      const nowMoving = Math.abs(speed) > 0.2;
      if (Math.abs(input) > 0.1) facing = Math.sign(input);
      if (nowMoving !== moving) { moving = nowMoving; if (!oneShot) settle(); }
      const run = actions.get("Hero_Run");
      if (run) run.timeScale = 0.6 + Math.abs(input) * 0.6;
      // Side-on while running, back to the viewer when he stops.
      const yawTarget = moving ? facing * (Math.PI / 2) : 0;
      yaw += (yawTarget - yaw) * Math.min(1, dt * 10);
      hero.rotation.y = yaw;

      // The fireball leaves the throwing hand at the moment of release, towards the aim or ahead,
      // and from there flies on the page overlay until it leaves the window.
      if (firePending && now >= firePending.at && hand) {
        hand.getWorldPosition(handPos);
        const aim = firePending.aim ?? handPos.clone().add(new THREE.Vector3(facing, 0, 0));
        const rect = renderer.domElement.getBoundingClientRect();
        const toPage = (p: THREE.Vector3) => {
          tmp.copy(p).project(camera);
          return { x: rect.left + (tmp.x * 0.5 + 0.5) * rect.width, y: rect.top + (0.5 - tmp.y * 0.5) * rect.height };
        };
        const from = toPage(handPos);
        const to = toPage(aim);
        fire.launch(from.x, from.y, to.x - from.x, to.y - from.y || 0);
        firePending = null;
      }
      fire.step(dt, now);
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
      window.removeEventListener("keydown", keydown);
      window.removeEventListener("keyup", keyup);
      window.removeEventListener("blur", blur);
      stage?.removeEventListener("pointerdown", click);
      fire.dispose();
      mixer?.stopAllAction();
      hero.traverse((node) => {
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
