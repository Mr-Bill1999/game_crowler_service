import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { MeshoptDecoder } from "three/examples/jsm/libs/meshopt_decoder.module.js";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { asciiPass, CELL_H, CELL_W, fireOverlay } from "./asciiKit";

/* The wizard travels with you: a small ASCII figure in the bottom right corner of every page
   after the entrance. He stays out of the way and only reacts:
   - runs on the spot while the page scrolls, facing the way it goes;
   - waves when a new page opens;
   - turns towards the games when the pointer is over a tile;
   - now and then waves or dances by himself;
   - a click makes him throw a fireball across the page, dance or wave, in turn.
   He can be sent away for the session; he steps aside while a text field has focus. */

const MODEL = "/hero/hero.glb";
const OFF_KEY = "gg-companion-off";
const CLIPS = { wave: "Hero_Wave", dance: "Hero_Dance", fireball: "Hero_Fireball" } as const;
type Emote = keyof typeof CLIPS;
const CLICKS: Emote[] = ["fireball", "dance", "wave"];

export default function Companion({ path }: { path: string }) {
  const host = useRef<HTMLDivElement>(null);
  const control = useRef<{ emote: (e: Emote) => void } | null>(null);
  const [off, setOff] = useState(() => { try { return sessionStorage.getItem(OFF_KEY) === "1"; } catch { return false; } });
  const [ready, setReady] = useState(false);
  const [typing, setTyping] = useState(false);
  const clicks = useRef(0);

  // A new page: he waves hello.
  useEffect(() => { control.current?.emote("wave"); }, [path]);

  // While a text field has focus (and a phone keyboard may be up), he steps aside.
  useEffect(() => {
    const field = (t: EventTarget | null) => t instanceof HTMLElement && !!t.closest("input, textarea, select, [contenteditable]");
    const on = (e: FocusEvent) => setTyping(field(e.target));
    const offFocus = () => setTyping(false);
    document.addEventListener("focusin", on);
    document.addEventListener("focusout", offFocus);
    return () => { document.removeEventListener("focusin", on); document.removeEventListener("focusout", offFocus); };
  }, []);

  useEffect(() => {
    const el = host.current;
    if (!el || off) return;
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ alpha: true, antialias: false, premultipliedAlpha: false });
    } catch {
      return;
    }
    renderer.setClearColor(0x000000, 0);
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    el.prepend(renderer.domElement);
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const scene = new THREE.Scene();
    const key = new THREE.DirectionalLight(0xffffff, 2.6);
    key.position.set(-3, 4, 3);
    const rim = new THREE.DirectionalLight(0xffffff, 0.6);
    rim.position.set(4, 2, -2);
    scene.add(key, rim, new THREE.AmbientLight(0xffffff, 0.55));
    const camera = new THREE.PerspectiveCamera(26, 1, 0.1, 80);
    const target = new THREE.WebGLRenderTarget(8, 8, { minFilter: THREE.LinearFilter, magFilter: THREE.LinearFilter });
    const pass = asciiPass(target);

    // Framed so he stands on a short dotted ledge along the bottom of his corner.
    const fit = () => {
      const w = el.clientWidth;
      const h = el.clientHeight;
      if (!w || !h) return;
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      renderer.setSize(w, h, false);
      const cols = Math.max(8, Math.floor(w / CELL_W));
      const rows = Math.max(8, Math.floor(h / CELL_H));
      pass.uniforms.uGrid.value.set(cols, rows);
      target.setSize(cols * 2, rows * 2);
      camera.aspect = w / h;
      const viewH = 2.6 / 0.8;
      const dist = viewH / 2 / Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
      // Feet one row above the bottom, where the ledge is.
      const centreY = viewH / 2 - (CELL_H * 1.5 / h) * viewH;
      camera.position.set(0, centreY, dist);
      camera.lookAt(0, centreY, 0);
      camera.updateProjectionMatrix();
      pass.uniforms.uGround.value.set(0, Math.floor(cols * 0.18), Math.ceil(cols * 0.82));
    };
    const resize = new ResizeObserver(fit);
    resize.observe(el);
    fit();

    const hero = new THREE.Group();
    scene.add(hero);
    let mixer: THREE.AnimationMixer | null = null;
    let current: THREE.AnimationAction | null = null;
    let oneShot = false;
    let hand: THREE.Object3D | undefined;
    const actions = new Map<string, THREE.AnimationAction>();
    let moving = false;
    let disposed = false;

    const settle = () => {
      const next = actions.get(moving ? "Hero_Run" : "Hero_Idle");
      if (!next || next === current) return;
      next.reset().play();
      current?.crossFadeTo(next, 0.22, false);
      current = next;
    };
    const fire = fireOverlay();
    let firePending = 0;
    let lastEmote = performance.now();
    const emote = (name: Emote) => {
      const next = actions.get(CLIPS[name]);
      if (!next || !current) return;
      next.reset();
      next.setLoop(THREE.LoopOnce, 1);
      next.clampWhenFinished = true;
      next.play();
      if (current !== next) current.crossFadeTo(next, 0.16, false);
      current = next;
      oneShot = true;
      lastEmote = performance.now();
      if (name === "fireball") firePending = performance.now() + 650;
    };

    const loader = new GLTFLoader();
    loader.setMeshoptDecoder(MeshoptDecoder);
    loader.load(MODEL, (gltf) => {
      if (disposed) return;
      hero.add(gltf.scene);
      hand = gltf.scene.getObjectByName("HandL") ?? gltf.scene.getObjectByName("Hand.L");
      mixer = new THREE.AnimationMixer(gltf.scene);
      for (const clip of gltf.animations) actions.set(clip.name, mixer.clipAction(clip));
      current = actions.get("Hero_Idle") ?? null;
      current?.play();
      mixer.addEventListener("finished", () => { oneShot = false; settle(); });
      control.current = { emote };
      setReady(true);
      emote("wave");
    }, undefined, () => undefined);

    // Scrolling: he runs on the spot, facing the way the page goes.
    let lastY = window.scrollY;
    let scrolledAt = -1e9;
    let facing = 1;
    const scroll = () => {
      const dy = window.scrollY - lastY;
      lastY = window.scrollY;
      if (reduce || Math.abs(dy) < 2) return;
      scrolledAt = performance.now();
      facing = dy > 0 ? 1 : -1;
    };
    window.addEventListener("scroll", scroll, { passive: true });

    // Over a game tile he turns to look at it.
    let looking = false;
    const over = (e: PointerEvent) => { looking = e.target instanceof Element && !!e.target.closest(".tile, .entry-picks button"); };
    document.addEventListener("pointerover", over);

    const clock = new THREE.Clock();
    const handPos = new THREE.Vector3();
    const tmp = new THREE.Vector3();
    let yaw = 0;
    let frame = 0;
    let acc = 0;
    let nextIdle = 25000 + Math.random() * 20000;
    const tick = (now: number) => {
      frame = requestAnimationFrame(tick);
      const dt = Math.min(clock.getDelta(), 1 / 20);
      acc += dt;
      fire.step(dt, now);
      // Thirty frames a second is plenty for a figure this small.
      if (acc < 1 / 30) return;
      const step = acc;
      acc = 0;
      mixer?.update(step);

      const nowMoving = now - scrolledAt < 240;
      if (nowMoving !== moving) { moving = nowMoving; if (!oneShot) settle(); }
      if (!reduce && !moving && !oneShot && now - lastEmote > nextIdle) {
        emote(Math.random() < 0.5 ? "wave" : "dance");
        nextIdle = 25000 + Math.random() * 20000;
      }
      const yawTarget = moving ? facing * (Math.PI / 2) : looking ? -0.55 : 0;
      yaw += (yawTarget - yaw) * Math.min(1, step * 8);
      hero.rotation.y = yaw;

      // The fireball leaves his hand towards the page, to the left.
      if (firePending && now >= firePending && hand) {
        hand.getWorldPosition(handPos);
        const rect = renderer.domElement.getBoundingClientRect();
        tmp.copy(handPos).project(camera);
        fire.launch(rect.left + (tmp.x * 0.5 + 0.5) * rect.width, rect.top + (0.5 - tmp.y * 0.5) * rect.height, -1, -0.12);
        firePending = 0;
      }

      renderer.setRenderTarget(target);
      renderer.clear();
      renderer.render(scene, camera);
      renderer.setRenderTarget(null);
      renderer.clear();
      if (pass.ready()) pass.render(renderer);
    };
    frame = requestAnimationFrame(tick);

    return () => {
      disposed = true;
      control.current = null;
      cancelAnimationFrame(frame);
      resize.disconnect();
      window.removeEventListener("scroll", scroll);
      document.removeEventListener("pointerover", over);
      fire.dispose();
      mixer?.stopAllAction();
      hero.traverse((node) => {
        const mesh = node as THREE.Mesh;
        if (!mesh.isMesh) return;
        mesh.geometry.dispose();
        (Array.isArray(mesh.material) ? mesh.material : [mesh.material]).forEach((m) => m.dispose());
      });
      pass.dispose();
      target.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    };
  }, [off]);

  if (off) return null;
  return (
    <div ref={host} className={`companion ${ready ? "is-ready" : ""} ${typing ? "is-away" : ""}`} aria-hidden={!ready}>
      <div className="companion-ui">
        <button type="button" className="companion-hit" aria-label="Маг: нажми, и он что-нибудь сделает"
          onClick={() => control.current?.emote(CLICKS[clicks.current++ % CLICKS.length])} />
        <button type="button" className="companion-off" aria-label="Спрятать мага до следующего визита" title="Спрятать"
          onClick={() => { try { sessionStorage.setItem(OFF_KEY, "1"); } catch { /* private mode */ } setOff(true); }}>×</button>
      </div>
    </div>
  );
}
