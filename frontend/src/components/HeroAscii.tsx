import { useEffect, useRef } from "react";
import * as THREE from "three";
import { MeshoptDecoder } from "three/examples/jsm/libs/meshopt_decoder.module.js";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { padInput, type Action } from "./padInput";

/* The entrance stage: a small rigged wizard from Blender (public/hero/hero.glb) rendered live
   and drawn in ASCII like pencil on paper — the darker a cell, the denser its character. Winged
   mobs fly in from the right and he fights them:
   - fireball (pad top, F, a click on the stage — aimed at the click) knocks one down;
   - dance (pad left, Q) is a spin attack close around him;
   - wave (pad right, E) pushes every mob in front back;
   - jump (pad bottom, Space/W/↑) lets a mob pass under him; otherwise a mob knocks him back.
   The stick, the d-pad, A/D and ←/→ turn him. */

const MODEL = "/hero/hero.glb";
const RAMP = " .:-=+*#%@";
const GLYPHS = RAMP + "|-/\\";
const CELL_W = 6;
const CELL_H = 11;
const ATLAS_W = 32;
const ATLAS_H = 56;
const BURSTS = 4;
const CLIPS: Record<Action, string> = { jump: "Hero_Jump", wave: "Hero_Wave", dance: "Hero_Dance", fireball: "Hero_Fireball" };
const KEYS: Record<string, Action> = { Space: "jump", KeyW: "jump", ArrowUp: "jump", KeyF: "fireball", KeyE: "wave", KeyQ: "dance" };
/** How tall the hero is, as a share of the stage height. */
const HERO_H = 0.3;

const VERTEX = `
varying vec2 vUv;
void main() { vUv = position.xy * 0.5 + 0.5; gl_Position = vec4(position.xy, 0.0, 1.0); }
`;

const FRAGMENT = `
uniform sampler2D uScene;
uniform sampler2D uAtlas;
uniform vec2 uGrid;
uniform vec2 uFire;
uniform vec2 uFireDir;
uniform float uFireOn;
uniform vec3 uBursts[${BURSTS}];
uniform float uTime;
varying vec2 vUv;

const float N = ${RAMP.length}.0;
const float G = ${GLYPHS.length}.0;
const float ASPECT = ${(CELL_H / CELL_W).toFixed(3)};
const vec3 INK = vec3(0.133, 0.149, 0.122);
const vec3 SAGE = vec3(0.486, 0.541, 0.439);
const vec3 CLAY = vec3(0.62, 0.30, 0.20);
const vec3 FIRE = vec3(0.71, 0.33, 0.18);
const vec3 EMBER = vec3(0.85, 0.58, 0.31);

float hash(vec2 p) { return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }

void main() {
  vec2 cell = floor(vUv * uGrid);
  vec2 inCell = fract(vUv * uGrid);
  vec4 scene = texture2D(uScene, (cell + 0.5) / uGrid);
  float glyphIndex = 0.0;
  vec3 ink = INK;
  float alpha = 0.0;

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
    // Colour by what is drawn: the sage hat, the terracotta mobs, ink for the rest.
    float hi = max(max(c.r, c.g), c.b);
    float sat = hi > 0.0 ? (hi - min(min(c.r, c.g), c.b)) / hi : 0.0;
    ink = c.r > c.g * 1.2 && sat > 0.25 ? CLAY : mix(INK, SAGE, smoothstep(0.08, 0.2, sat));
    alpha = 1.0;
  }

  // The fireball: a hot core and a flickering tail streaming behind it.
  if (uFireOn > 0.5) {
    vec2 d = (cell - uFire) * vec2(1.0, ASPECT);
    float dist = length(d);
    float behind = -dot(d, uFireDir);
    float side = abs(d.x * uFireDir.y - d.y * uFireDir.x);
    float noise = hash(cell + floor(uTime * 18.0));
    if (dist < 1.7) {
      glyphIndex = N - 1.0; ink = FIRE; alpha = 1.0;
    } else if (dist < 3.0) {
      glyphIndex = N - 3.0; ink = FIRE; alpha = 0.95;
    } else if (behind > 0.0 && behind < 18.0 && side < 2.8 - behind * 0.13 && noise > behind / 20.0) {
      float t = behind / 18.0;
      glyphIndex = t < 0.35 ? N - 4.0 : (t < 0.7 ? N - 5.0 : 1.0);
      ink = mix(FIRE, EMBER, t);
      alpha = 1.0 - t * 0.6;
    }
  }

  // Bursts where a mob was hit: a ring of stars with a hot centre, fading out.
  for (int i = 0; i < ${BURSTS}; i++) {
    vec3 b = uBursts[i];
    if (b.z < 0.0 || b.z > 0.7) continue;
    float dist = length((cell - b.xy) * vec2(1.0, ASPECT));
    float ring = 1.0 - smoothstep(0.0, 1.4, abs(dist - b.z * 26.0));
    if (dist < 2.2 * (1.0 - b.z / 0.7)) { glyphIndex = N - 2.0; ink = FIRE; alpha = 1.0; }
    else if (ring * step(0.5, hash(cell + float(i))) > 0.5) { glyphIndex = N - 4.0; ink = EMBER; alpha = 1.0 - b.z / 0.7; }
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

type Mob = { group: THREE.Group; wings: THREE.Object3D[]; speed: number; push: number; phase: number; baseY: number; passed: boolean };

/** A winged slime: a terracotta body, two eyes, flapping wings. */
function mobKit() {
  const body = new THREE.SphereGeometry(0.3, 20, 14);
  const eye = new THREE.SphereGeometry(0.075, 10, 8);
  const wing = new THREE.BoxGeometry(0.46, 0.035, 0.24);
  const clay = new THREE.MeshStandardMaterial({ color: 0xb4603d, roughness: 0.55 });
  const wingClay = new THREE.MeshStandardMaterial({ color: 0x9a4c30, roughness: 0.6 });
  const white = new THREE.MeshStandardMaterial({ color: 0xf2eee6, roughness: 0.4 });
  const make = (): Mob => {
    const group = new THREE.Group();
    const b = new THREE.Mesh(body, clay);
    b.scale.set(1.1, 0.95, 1);
    group.add(b);
    for (const x of [-0.11, 0.11]) {
      const e = new THREE.Mesh(eye, white);
      e.position.set(x, 0.06, 0.27);
      group.add(e);
    }
    const wings = [-1, 1].map((side) => {
      const pivot = new THREE.Group();
      pivot.position.set(0.26 * side, 0.08, 0);
      const w = new THREE.Mesh(wing, wingClay);
      w.position.x = 0.22 * side;
      pivot.add(w);
      group.add(pivot);
      return pivot;
    });
    return { group, wings, speed: 0, push: 0, phase: Math.random() * 6, baseY: 0, passed: false };
  };
  return { make, dispose: () => [body, eye, wing, clay, wingClay, white].forEach((x) => x.dispose()) };
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
        uFire: { value: new THREE.Vector2() },
        uFireDir: { value: new THREE.Vector2(1, 0) },
        uFireOn: { value: 0 },
        uBursts: { value: Array.from({ length: BURSTS }, () => new THREE.Vector3(0, 0, -1)) },
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

    // The hero stands on top of the gamepad (.entry-object, a sibling on the stage); a view offset
    // keeps him there while the camera looks straight at him. The world's edges are where mobs
    // come and go.
    const edge = { left: -4, right: 6 };
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
      const feet = pad ? Math.min(0.95, (pad.top + pad.height * 0.2 - box.top) / h) : 0.95;
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
      edge.left = at(-1);
      edge.right = at(1);
    };
    const resize = new ResizeObserver(fit);
    resize.observe(el);
    fit();

    let mixer: THREE.AnimationMixer | null = null;
    let idle: THREE.AnimationAction | null = null;
    let current: THREE.AnimationAction | null = null;
    let currentAction: Action | null = null;
    let actionAt = 0;
    const actions = new Map<string, THREE.AnimationAction>();
    const hero = new THREE.Group();
    scene.add(hero);
    let hand: THREE.Object3D | undefined;

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
        currentAction = null;
      });
    }, undefined, () => undefined);

    // Combat state.
    const kit = mobKit();
    const mobs: Mob[] = [];
    const bursts: { pos: THREE.Vector3; at: number }[] = [];
    let fire: { pos: THREE.Vector3; vel: THREE.Vector3 } | null = null;
    let firePending: { at: number; aim: THREE.Vector3 | null } | null = null;
    let nextSpawn = performance.now() + 1500;
    let knock = 0;

    const burst = (pos: THREE.Vector3) => {
      bursts.push({ pos: pos.clone(), at: performance.now() });
      if (bursts.length > BURSTS) bursts.shift();
    };
    const drop = (mob: Mob) => {
      scene.remove(mob.group);
      mobs.splice(mobs.indexOf(mob), 1);
    };
    const kill = (mob: Mob) => {
      burst(mob.group.getWorldPosition(new THREE.Vector3()));
      drop(mob);
    };

    const play = (action: Action, aim: THREE.Vector3 | null = null) => {
      const next = actions.get(CLIPS[action]);
      if (!next || !current) return;
      next.reset();
      next.setLoop(THREE.LoopOnce, 1);
      next.clampWhenFinished = true;
      next.play();
      if (current !== next) current.crossFadeTo(next, 0.18, false);
      current = next;
      currentAction = action;
      actionAt = performance.now();
      if (action === "fireball") firePending = { at: actionAt + 650, aim };
      if (action === "wave") for (const m of mobs) if (m.group.position.x < hero.position.x + 5) m.push = 4.5;
    };
    const offAct = padInput.onAct((action) => play(action));

    let yaw = 0;
    let yawTarget = 0;
    const turn = (step: number) => { yawTarget += step * (Math.PI / 2); };
    const offTurn = padInput.onTurn(turn);

    // Desktop: keys for every action; a click on the stage throws a fireball where it lands.
    const keydown = (e: KeyboardEvent) => {
      if (e.repeat || (e.target instanceof HTMLElement && e.target.closest("input, textarea, select, [contenteditable]"))) return;
      const action = KEYS[e.code];
      if (action) { play(action); e.preventDefault(); return; }
      if (e.code === "KeyA" || e.code === "ArrowLeft") { turn(-1); e.preventDefault(); }
      if (e.code === "KeyD" || e.code === "ArrowRight") { turn(1); e.preventDefault(); }
    };
    window.addEventListener("keydown", keydown);
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
    const tmp = new THREE.Vector3();
    let frame = 0;
    const tick = (now: number) => {
      frame = requestAnimationFrame(tick);
      const dt = Math.min(clock.getDelta(), 1 / 20);
      mixer?.update(dt);
      yaw += (yawTarget + padInput.left.x * 1.1 - yaw) * Math.min(1, dt * 6);
      hero.rotation.y = yaw;
      knock += (0 - knock) * Math.min(1, dt * 5);
      hero.position.x = -knock;

      // Mobs fly in from beyond the right edge at different heights.
      if (now > nextSpawn && mobs.length < 4 && edge.right > 1) {
        const mob = kit.make();
        mob.baseY = 0.7 + Math.random() * 1.9;
        mob.speed = 0.9 + Math.random() * 0.6;
        mob.group.position.set(edge.right + 0.8, mob.baseY, (Math.random() - 0.5) * 0.6);
        scene.add(mob.group);
        mobs.push(mob);
        nextSpawn = now + 1800 + Math.random() * 2200;
      }
      const airborne = currentAction === "jump" && now - actionAt > 180 && now - actionAt < 950;
      const spinning = currentAction === "dance" && now - actionAt > 250 && now - actionAt < 2300;
      for (const mob of [...mobs]) {
        mob.phase += dt * 9;
        mob.push = Math.max(0, mob.push - dt * 6);
        const g = mob.group;
        g.position.x += (-mob.speed + mob.push) * dt;
        g.position.y = mob.baseY + Math.sin(mob.phase * 0.35) * 0.15;
        g.rotation.z = Math.sin(mob.phase * 0.35) * 0.12;
        mob.wings[0].rotation.z = Math.sin(mob.phase) * 0.7;
        mob.wings[1].rotation.z = -Math.sin(mob.phase) * 0.7;
        const dx = g.position.x - hero.position.x;
        if (spinning && Math.abs(dx) < 1.7) { kill(mob); continue; }
        if (!mob.passed && Math.abs(dx) < 0.45) {
          if (airborne) mob.passed = true;
          else { kill(mob); knock = 0.5; continue; }
        }
        if (g.position.x < edge.left - 1) drop(mob);
      }

      // The fireball leaves the throwing hand at the moment of release, towards the aim or right.
      if (firePending && now >= firePending.at && hand) {
        hand.getWorldPosition(handPos);
        const dir = firePending.aim ? firePending.aim.clone().sub(handPos).setZ(0) : new THREE.Vector3(1, 0, 0);
        if (dir.lengthSq() < 0.01) dir.set(1, 0, 0);
        fire = { pos: handPos.clone(), vel: dir.normalize().multiplyScalar(7) };
        firePending = null;
      }
      const grid = ascii.uniforms.uGrid.value as THREE.Vector2;
      const toCell = (p: THREE.Vector3) => {
        tmp.copy(p).project(camera);
        return new THREE.Vector2((tmp.x * 0.5 + 0.5) * grid.x, (tmp.y * 0.5 + 0.5) * grid.y);
      };
      if (fire) {
        fire.pos.addScaledVector(fire.vel, dt);
        const shot = fire;
        const hit = mobs.find((m) => m.group.position.distanceTo(shot.pos) < 0.55);
        if (hit) { kill(hit); fire = null; }
        else if (shot.pos.x > edge.right + 1 || shot.pos.x < edge.left - 1 || shot.pos.y < -0.5 || shot.pos.y > 6) fire = null;
      }
      ascii.uniforms.uFireOn.value = fire ? 1 : 0;
      if (fire) {
        const head = toCell(fire.pos);
        ascii.uniforms.uFire.value.copy(head);
        const ahead = toCell(fire.pos.clone().addScaledVector(fire.vel, 0.05)).sub(head);
        ahead.y *= CELL_H / CELL_W;
        ascii.uniforms.uFireDir.value.copy(ahead.lengthSq() > 0 ? ahead.normalize() : new THREE.Vector2(1, 0));
      }
      const slots = ascii.uniforms.uBursts.value as THREE.Vector3[];
      slots.forEach((slot, i) => {
        const b = bursts[i];
        if (!b) { slot.z = -1; return; }
        const c = toCell(b.pos);
        slot.set(c.x, c.y, (now - b.at) / 1000);
      });
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
      window.removeEventListener("keydown", keydown);
      stage?.removeEventListener("pointerdown", click);
      mixer?.stopAllAction();
      hero.traverse((node) => {
        const mesh = node as THREE.Mesh;
        if (!mesh.isMesh) return;
        mesh.geometry.dispose();
        (Array.isArray(mesh.material) ? mesh.material : [mesh.material]).forEach((m) => m.dispose());
      });
      kit.dispose();
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
