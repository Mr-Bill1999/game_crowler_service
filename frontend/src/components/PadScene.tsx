import { useEffect, useRef } from "react";
import * as THREE from "three";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
import { MeshoptDecoder } from "three/examples/jsm/libs/meshopt_decoder.module.js";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { BUTTON_ACTION, padInput } from "./padInput";

/* The entrance gamepad, a milky modern pad, for looking at and playing with on the preview (the
   run itself is played on keys or on-screen buttons): press the face buttons and the d-pad, pull
   the sticks — the left one runs the wizard. Everything springs back, and every press is passed on
   to padInput for the ASCII hero. A drag anywhere off the parts turns the pad to look at it from
   all sides; it eases back when let go. The model comes from Blender (public/hero/pad.glb) with
   its parts as named nodes: Body, Btn_0..3, DPad (DPad_H, DPad_V), Stick_L/R, StickBase_L/R. The
   soft floor shadow is drawn by the page, under the canvas. */

const MODEL = "/hero/pad.glb";
// Facing the viewer: the face towards the camera, grips down, tipped back a little; on touch
// screens almost straight on, so the buttons face the thumb.
const BASE = new THREE.Euler(1.18, 0, 0, "YXZ");
const BASE_TOUCH = new THREE.Euler(1.42, 0, 0, "YXZ");
/** How far a stick tilts at full pull, in radians. */
const STICK_MAX = 0.38;
/** How far from a finger a part still counts as tapped, in CSS pixels. */
const TOUCH_REACH = 44;

/** A damped spring towards a target; underdamped, so released parts wobble a little. It counts as resting once
   the motion left is below what the eye can see (`eps`, in its own units), and then lands exactly on the target:
   every frame a part springs is a full redraw of the pad. */
class Spring {
  value = 0;
  target = 0;
  private velocity = 0;
  constructor(private stiffness = 180, private damping = 14, private eps = 1e-3) {}
  step(dt: number) {
    this.velocity += ((this.target - this.value) * this.stiffness - this.velocity * this.damping) * dt;
    this.value += this.velocity * dt;
    if (Math.abs(this.target - this.value) < this.eps && Math.abs(this.velocity) < this.eps * 25) {
      this.value = this.target;
      this.velocity = 0;
    }
    return this.value;
  }
  get resting() { return this.value === this.target && this.velocity === 0; }
}

type Press = { node: THREE.Object3D; rest: number; spring: Spring };
type Stick = { pivot: THREE.Object3D; mesh: THREE.Object3D; x: Spring; z: Spring; side: "L" | "R" };

/** How far a drag may turn the pad, in radians: around its vertical axis and towards the viewer. */
const TURN_YAW = 1.1;
const TURN_PITCH = 0.7;

export default function PadScene({ onReady, inspect = false }: { onReady?: () => void; inspect?: boolean }) {
  const host = useRef<HTMLDivElement>(null);
  const ready = useRef(onReady);
  ready.current = onReady;
  const inspectRef = useRef(inspect);
  inspectRef.current = inspect;

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
    const sticks = new Map<THREE.Object3D, Stick>();
    let radius = 2;
    let dirty = true;
    let model: THREE.Object3D | null = null;

    // Tell the hero where the pad's top edge is on screen, so he stands right on it.
    const publishTop = () => {
      if (!model) return;
      // Measured in the resting pose, even if the pad is being turned right now.
      const now = root.quaternion.clone();
      root.quaternion.copy(baseQ);
      root.updateMatrixWorld(true);
      const box = new THREE.Box3().setFromObject(model);
      root.quaternion.copy(now);
      root.updateMatrixWorld(true);
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
        if (node) presses.set(node, { node, rest: node.position.y, spring: new Spring(420, 22, 0.02) });
      }
      const dp = model.getObjectByName("DPad");
      if (dp) dpad = { node: dp, x: new Spring(320, 16, 0.005), z: new Spring(320, 16, 0.005) };
      for (const side of ["L", "R"] as const) {
        const mesh = model.getObjectByName(`Stick_${side}`);
        if (!mesh?.parent) continue;
        // Tilt the stick around a point inside the body, below the cap.
        const pivot = new THREE.Group();
        pivot.position.copy(mesh.position).setY(mesh.position.y - 0.32);
        mesh.parent.add(pivot);
        pivot.attach(mesh);
        sticks.set(mesh, { pivot, mesh, x: new Spring(150, 9, 0.004), z: new Spring(150, 9, 0.004), side });
      }
      root.add(model);
      fit();
      dirty = true;
      ready.current?.();
    }, undefined, () => undefined);

    let pressing = false;
    let held: THREE.Object3D | null = null;
    let pulling: Stick | null = null;
    const stickPlane = new THREE.Plane();
    const hitPoint = new THREE.Vector3();
    // Turning the pad to look at it: soft springs, so it eases back with a little sway.
    const turn = { yaw: new Spring(36, 8), pitch: new Spring(36, 8) };
    let turning: { x: number; y: number; yaw: number; pitch: number } | null = null;
    const baseQ = new THREE.Quaternion();
    const turnQ = new THREE.Quaternion();
    const turnE = new THREE.Euler(0, 0, 0, "YXZ");
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
        if (presses.has(node) || sticks.has(node) || node === dpad?.node) return node;
      }
      return null;
    };

    // A finger rarely lands on a small button exactly: without a direct hit, take the part whose
    // centre is nearest on screen within reach, and the tap's point on the plane of the pad's face.
    const nearest = (event: PointerEvent) => {
      const rect = renderer.domElement.getBoundingClientRect();
      const nodes = [...presses.keys(), ...sticks.keys(), ...(dpad ? [dpad.node] : [])];
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
      if ((!target || !point) && inspectRef.current) {
        // Off the buttons on the preview: the drag turns the pad.
        turning = { x: event.clientX, y: event.clientY, yaw: turn.yaw.value, pitch: turn.pitch.value };
        try { renderer.domElement.setPointerCapture(event.pointerId); } catch { /* untracked pointer */ }
        el.style.cursor = "grabbing";
        return;
      }
      if (!target || !point) return;
      try { renderer.domElement.setPointerCapture(event.pointerId); } catch { /* a pointer the browser no longer tracks */ }
      if (sticks.has(target)) {
        pulling = sticks.get(target)!;
        const normal = new THREE.Vector3(0, 1, 0).transformDirection(pulling.pivot.parent!.matrixWorld);
        stickPlane.setFromNormalAndCoplanarPoint(normal, point);
        return;
      }
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
      if (turning) {
        const k = 1 / Math.max(160, el.clientWidth * 0.5);
        turn.yaw.target = THREE.MathUtils.clamp(turning.yaw + (event.clientX - turning.x) * k * TURN_YAW * 1.6, -TURN_YAW, TURN_YAW);
        turn.pitch.target = THREE.MathUtils.clamp(turning.pitch + (event.clientY - turning.y) * k * TURN_PITCH * 1.6, -TURN_PITCH, TURN_PITCH);
        return;
      }
      if (pulling) {
        const rect = renderer.domElement.getBoundingClientRect();
        pointer.set(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
        ray.setFromCamera(pointer, camera);
        if (ray.ray.intersectPlane(stickPlane, hitPoint)) {
          const local = pulling.pivot.parent!.worldToLocal(hitPoint.clone()).sub(pulling.pivot.position);
          const pull = Math.min(1, Math.hypot(local.x, local.z) / 0.6);
          const angle = Math.atan2(local.z, local.x);
          pulling.z.target = -Math.cos(angle) * pull * STICK_MAX;
          pulling.x.target = Math.sin(angle) * pull * STICK_MAX;
          // The left stick runs the wizard, like the d-pad.
          if (pulling.side === "L") padInput.dpad = { x: Math.abs(Math.cos(angle) * pull) > 0.3 ? Math.cos(angle) * pull : 0, y: 0 };
        }
        return;
      }
      if (!pressing) el.style.cursor = part(pick(event)?.object) ? "pointer" : inspectRef.current && pick(event) ? "grab" : "";
    };

    const up = (event: PointerEvent) => {
      if (renderer.domElement.hasPointerCapture(event.pointerId)) renderer.domElement.releasePointerCapture(event.pointerId);
      if (held && presses.has(held)) presses.get(held)!.spring.target = 0;
      if (held && dpad && held === dpad.node) { dpad.x.target = 0; dpad.z.target = 0; padInput.dpad = { x: 0, y: 0 }; }
      if (pulling) { pulling.x.target = 0; pulling.z.target = 0; if (pulling.side === "L") padInput.dpad = { x: 0, y: 0 }; pulling = null; }
      pressing = false;
      held = null;
      if (turning) { turning = null; turn.yaw.target = 0; turn.pitch.target = 0; el.style.cursor = ""; }
    };
    // A long press on a phone must not open a menu or select anything.
    const noMenu = (e: Event) => e.preventDefault();

    const canvas = renderer.domElement;
    canvas.addEventListener("pointerdown", down);
    canvas.addEventListener("pointermove", move);
    canvas.addEventListener("pointerup", up);
    canvas.addEventListener("pointercancel", up);
    canvas.addEventListener("contextmenu", noMenu);

    baseQ.setFromEuler(touch ? BASE_TOUCH : BASE);
    root.quaternion.copy(baseQ);
    let frame = 0;
    let then = performance.now();
    let drawn = 0;

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
      // The spring may overshoot; the stick itself never leaves its socket.
      const limit = STICK_MAX * 1.05;
      for (const st of sticks.values()) {
        if (st.x.resting && st.z.resting) continue;
        st.pivot.rotation.x = THREE.MathUtils.clamp(st.x.step(dt), -limit, limit);
        st.pivot.rotation.z = THREE.MathUtils.clamp(st.z.step(dt), -limit, limit);
        moving = true;
      }
      // Back to still the moment the game starts.
      if (!inspectRef.current && !turning) { turn.yaw.target = 0; turn.pitch.target = 0; }
      if (!(turn.yaw.resting && turn.pitch.resting) || turning) {
        turnE.set(turn.pitch.step(dt), turn.yaw.step(dt), 0);
        root.quaternion.copy(turnQ.setFromEuler(turnE)).multiply(baseQ);
        moving = true;
      }
      if (!moving && !dirty) return;
      // In the game the pad only answers taps: 30 fps is enough for a button going down, and leaves the GPU to
      // the run. The frame skipped here is drawn on the next one.
      if (!inspectRef.current && now - drawn < 1000 / 30 - 4) { dirty = true; return; }
      dirty = false;
      drawn = now;
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
      canvas.removeEventListener("contextmenu", noMenu);
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
