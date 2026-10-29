import { useEffect, useRef } from "react";
import * as THREE from "three";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
import { MeshoptDecoder } from "three/examples/jsm/libs/meshopt_decoder.module.js";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { BUTTON_ACTION, padInput } from "./padInput";

/* The entrance gamepad, an old-console flat pad in one milky white, live but standing still:
   press the face buttons and the d-pad. Everything springs back, and every press is passed on to
   padInput for the ASCII hero. The model comes from Blender (public/hero/pad.glb) with its parts
   as named nodes: Body, Btn_0..3, DPad (DPad_H, DPad_V). The soft floor shadow is drawn by the
   page, under the canvas. */

const MODEL = "/hero/pad.glb";
// Almost straight on, so the buttons face the viewer, tipped back just enough to read as an object;
// on touch screens flatter still, under the thumb.
const BASE = new THREE.Euler(1.34, 0, 0, "YXZ");
const BASE_TOUCH = new THREE.Euler(1.44, 0, 0, "YXZ");
/** How far from a finger a part still counts as tapped, in CSS pixels. */
const TOUCH_REACH = 44;

/** A damped spring towards a target; underdamped, so released parts wobble a little. */
class Spring {
  value = 0;
  target = 0;
  private velocity = 0;
  constructor(private stiffness = 180, private damping = 14) {}
  step(dt: number) {
    this.velocity += ((this.target - this.value) * this.stiffness - this.velocity * this.damping) * dt;
    this.value += this.velocity * dt;
    return this.value;
  }
  get resting() { return Math.abs(this.target - this.value) < 1e-4 && Math.abs(this.velocity) < 1e-3; }
}

type Press = { node: THREE.Object3D; rest: number; spring: Spring };

export default function PadScene({ onReady }: { onReady?: () => void }) {
  const host = useRef<HTMLDivElement>(null);
  const ready = useRef(onReady);
  ready.current = onReady;

  useEffect(() => {
    const el = host.current;
    if (!el) return;

    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
    } catch {
      return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, window.matchMedia("(pointer: coarse)").matches ? 1.5 : 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.AgXToneMapping;
    renderer.toneMappingExposure = 1.05;
    el.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    const pmrem = new THREE.PMREMGenerator(renderer);
    const room = new RoomEnvironment();
    scene.environment = pmrem.fromScene(room, 0.04).texture;
    scene.environmentIntensity = 0.55;

    const key = new THREE.DirectionalLight(0xfff3e6, 2.2);
    key.position.set(-3, 6, 4);
    scene.add(key, new THREE.HemisphereLight(0xffffff, 0xd9d1c4, 0.6));

    const camera = new THREE.PerspectiveCamera(28, 1, 0.1, 100);
    const root = new THREE.Group();
    scene.add(root);

    const touch = window.matchMedia("(pointer: coarse)").matches || window.innerWidth <= 720;
    const presses = new Map<THREE.Object3D, Press>();
    let dpad: { node: THREE.Object3D; x: Spring; z: Spring } | null = null;
    let radius = 2;
    let dirty = true;
    let model: THREE.Object3D | null = null;

    // Tell the hero where the pad's top edge is on screen, so he stands right on it.
    const publishTop = () => {
      if (!model) return;
      root.updateMatrixWorld(true);
      const box = new THREE.Box3().setFromObject(model);
      let top = 1;
      const p = new THREE.Vector3();
      for (const x of [box.min.x, box.max.x]) for (const y of [box.min.y, box.max.y]) for (const z of [box.min.z, box.max.z]) {
        p.set(x, y, z).project(camera);
        top = Math.min(top, 0.5 - p.y * 0.5);
      }
      padInput.top = top;
      window.dispatchEvent(new Event("padfit"));
    };

    const fit = () => {
      const { clientWidth: w, clientHeight: h } = el;
      if (!w || !h) return;
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      const half = THREE.MathUtils.degToRad(camera.fov / 2);
      const dist = Math.max(radius / Math.tan(half), radius / (Math.tan(half) * camera.aspect)) * 1.02;
      camera.position.set(0, radius * 0.15, dist);
      camera.lookAt(0, 0, 0);
      camera.updateProjectionMatrix();
      publishTop();
      dirty = true;
    };
    const resize = new ResizeObserver(fit);
    resize.observe(el);

    const loader = new GLTFLoader();
    loader.setMeshoptDecoder(MeshoptDecoder);
    let disposed = false;
    loader.load(MODEL, (gltf) => {
      if (disposed) return;
      model = gltf.scene;
      // Centre the pad on its own bounds.
      const box = new THREE.Box3().setFromObject(model);
      model.position.sub(box.getCenter(new THREE.Vector3()));
      radius = box.getBoundingSphere(new THREE.Sphere()).radius * (touch ? 0.66 : 0.8);

      for (const name of ["Btn_0", "Btn_1", "Btn_2", "Btn_3"]) {
        const node = model.getObjectByName(name);
        if (node) presses.set(node, { node, rest: node.position.y, spring: new Spring(420, 22) });
      }
      const dp = model.getObjectByName("DPad");
      if (dp) dpad = { node: dp, x: new Spring(320, 16), z: new Spring(320, 16) };
      root.add(model);
      fit();
      dirty = true;
      ready.current?.();
    }, undefined, () => undefined);

    let pressing = false;
    let held: THREE.Object3D | null = null;
    const pointer = new THREE.Vector2();
    const ray = new THREE.Raycaster();

    const pick = (event: PointerEvent) => {
      const rect = renderer.domElement.getBoundingClientRect();
      pointer.set(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
      ray.setFromCamera(pointer, camera);
      return ray.intersectObject(root, true)[0];
    };
    const part = (object: THREE.Object3D | undefined) => {
      for (let node = object; node; node = node.parent ?? undefined) {
        if (presses.has(node) || node === dpad?.node) return node;
      }
      return null;
    };

    // A finger rarely lands on a small button exactly: without a direct hit, take the part whose
    // centre is nearest on screen within reach, and the tap's point on the plane of the pad's face.
    const nearest = (event: PointerEvent) => {
      const rect = renderer.domElement.getBoundingClientRect();
      const nodes = [...presses.keys(), ...(dpad ? [dpad.node] : [])];
      let best: THREE.Object3D | null = null;
      let bestDist = TOUCH_REACH;
      const p = new THREE.Vector3();
      for (const node of nodes) {
        node.getWorldPosition(p).project(camera);
        const d = Math.hypot(rect.left + (p.x * 0.5 + 0.5) * rect.width - event.clientX, rect.top + (0.5 - p.y * 0.5) * rect.height - event.clientY);
        if (d < bestDist) { bestDist = d; best = node; }
      }
      if (!best) return null;
      const face = new THREE.Vector3(0, 1, 0).transformDirection(root.matrixWorld);
      const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(face, best.getWorldPosition(new THREE.Vector3()));
      const point = ray.ray.intersectPlane(plane, new THREE.Vector3()) ?? best.getWorldPosition(new THREE.Vector3());
      return { target: best, point };
    };

    const down = (event: PointerEvent) => {
      const hit = pick(event);
      let target = part(hit?.object);
      let point = hit?.point;
      if (!target && event.pointerType !== "mouse") {
        const near = nearest(event);
        if (near) { target = near.target; point = near.point; }
      }
      if (!target || !point) return;
      try { renderer.domElement.setPointerCapture(event.pointerId); } catch { /* a pointer the browser no longer tracks */ }
      if (presses.has(target)) {
        pressing = true;
        held = target;
        presses.get(target)!.spring.target = 1;
        const action = BUTTON_ACTION[target.name];
        if (action) padInput.act(action);
      } else if (dpad && target === dpad.node) {
        pressing = true;
        held = target;
        const local = dpad.node.worldToLocal(point.clone());
        // Left and right run while held, down ducks while held; up jumps.
        if (Math.abs(local.x) > Math.abs(local.z)) {
          dpad.z.target = -Math.sign(local.x) * 0.16;
          padInput.dpad = { x: Math.sign(local.x), y: 0 };
        } else {
          dpad.x.target = Math.sign(local.z) * 0.16;
          padInput.dpad = { x: 0, y: Math.sign(local.z) };
          if (local.z < 0) padInput.act("jump");
        }
      }
    };

    const move = (event: PointerEvent) => {
      if (!pressing) el.style.cursor = part(pick(event)?.object) ? "pointer" : "";
    };

    const up = (event: PointerEvent) => {
      if (renderer.domElement.hasPointerCapture(event.pointerId)) renderer.domElement.releasePointerCapture(event.pointerId);
      if (held && presses.has(held)) presses.get(held)!.spring.target = 0;
      if (held && dpad && held === dpad.node) { dpad.x.target = 0; dpad.z.target = 0; padInput.dpad = { x: 0, y: 0 }; }
      pressing = false;
      held = null;
    };

    const canvas = renderer.domElement;
    canvas.addEventListener("pointerdown", down);
    canvas.addEventListener("pointermove", move);
    canvas.addEventListener("pointerup", up);
    canvas.addEventListener("pointercancel", up);

    root.quaternion.setFromEuler(touch ? BASE_TOUCH : BASE);
    let frame = 0;
    let then = performance.now();

    const tick = (now: number) => {
      frame = requestAnimationFrame(tick);
      const dt = Math.min((now - then) / 1000, 1 / 30);
      then = now;

      // The pad stands still: draw only while a part is still springing, or after a resize or load.
      let moving = false;
      for (const p of presses.values()) {
        if (p.spring.resting) continue;
        p.node.position.y = p.rest - p.spring.step(dt) * 0.06;
        moving = true;
      }
      if (dpad && !(dpad.x.resting && dpad.z.resting)) {
        dpad.node.rotation.x = dpad.x.step(dt);
        dpad.node.rotation.z = dpad.z.step(dt);
        moving = true;
      }
      if (!moving && !dirty) return;
      dirty = false;
      renderer.render(scene, camera);
    };
    frame = requestAnimationFrame(tick);

    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      resize.disconnect();
      canvas.removeEventListener("pointerdown", down);
      canvas.removeEventListener("pointermove", move);
      canvas.removeEventListener("pointerup", up);
      canvas.removeEventListener("pointercancel", up);
      scene.traverse((node) => {
        const mesh = node as THREE.Mesh;
        if (!mesh.isMesh) return;
        mesh.geometry.dispose();
        (Array.isArray(mesh.material) ? mesh.material : [mesh.material]).forEach((m) => m.dispose());
      });
      scene.environment?.dispose();
      room.dispose();
      pmrem.dispose();
      renderer.dispose();
      canvas.remove();
    };
  }, []);

  return <div ref={host} className="pad-scene" />;
}
