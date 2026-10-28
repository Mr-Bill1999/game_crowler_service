import { useEffect, useRef } from "react";
import * as THREE from "three";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
import { MeshoptDecoder } from "three/examples/jsm/libs/meshopt_decoder.module.js";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { BUTTON_ACTION, padInput } from "./padInput";

/* The entrance gamepad, live: drag it to turn it, press the face buttons and the d-pad, pull the
   sticks. Everything springs back, and every press is passed on to padInput for the ASCII hero.
   The model comes from Blender (public/hero/pad.glb) with its parts as named nodes: Body,
   Btn_0..3, DPad (DPad_H, DPad_V), Stick_L/R, StickBase_L/R. The soft floor shadow is drawn
   by the page, under the canvas. */

const MODEL = "/hero/pad.glb";
// Facing the viewer: the face towards the camera, grips down, tipped back a little.
const BASE = new THREE.Euler(1.18, 0, 0, "YXZ");
const STICK_MAX = 0.38;

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
type Stick = { pivot: THREE.Object3D; mesh: THREE.Object3D; x: Spring; z: Spring; side: "left" | "right" };

export default function PadScene({ onReady }: { onReady?: () => void }) {
  const host = useRef<HTMLDivElement>(null);
  const ready = useRef(onReady);
  ready.current = onReady;

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
    } catch {
      return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
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

    const presses = new Map<THREE.Object3D, Press>();
    const sticks = new Map<THREE.Object3D, Stick>();
    let dpad: { node: THREE.Object3D; x: Spring; z: Spring } | null = null;
    let radius = 2;

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
    };
    const resize = new ResizeObserver(fit);
    resize.observe(el);

    const loader = new GLTFLoader();
    loader.setMeshoptDecoder(MeshoptDecoder);
    let disposed = false;
    loader.load(MODEL, (gltf) => {
      if (disposed) return;
      const model = gltf.scene;
      // Centre the pad on its own bounds so it turns around its middle.
      const box = new THREE.Box3().setFromObject(model);
      model.position.sub(box.getCenter(new THREE.Vector3()));
      radius = box.getBoundingSphere(new THREE.Sphere()).radius * 1.15;

      for (const name of ["Btn_0", "Btn_1", "Btn_2", "Btn_3"]) {
        const node = model.getObjectByName(name);
        if (node) presses.set(node, { node, rest: node.position.y, spring: new Spring(420, 22) });
      }
      const dp = model.getObjectByName("DPad");
      if (dp) dpad = { node: dp, x: new Spring(320, 16), z: new Spring(320, 16) };
      for (const side of ["L", "R"]) {
        const mesh = model.getObjectByName(`Stick_${side}`);
        if (!mesh?.parent) continue;
        // Tilt the stick around a point inside the body, below the cap.
        const pivot = new THREE.Group();
        pivot.position.copy(mesh.position).setY(mesh.position.y - 0.32);
        mesh.parent.add(pivot);
        pivot.attach(mesh);
        sticks.set(mesh, { pivot, mesh, x: new Spring(150, 9), z: new Spring(150, 9), side: side === "L" ? "left" : "right" });
      }
      root.add(model);
      fit();
      ready.current?.();
    }, undefined, () => undefined);

    // Turning: a world-space yaw and pitch on top of the resting pose, with inertia and a slow return.
    const yaw = new Spring(26, 9);
    const pitch = new Spring(26, 9);
    let mode: "turn" | "stick" | "press" | null = null;
    let last = { x: 0, y: 0 };
    let held: THREE.Object3D | null = null;
    let idleSince = performance.now();
    const pointer = new THREE.Vector2();
    const ray = new THREE.Raycaster();
    const stickPlane = new THREE.Plane();
    const hitPoint = new THREE.Vector3();

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

    const down = (event: PointerEvent) => {
      const hit = pick(event);
      if (!hit) return;
      try { renderer.domElement.setPointerCapture(event.pointerId); } catch { /* a pointer the browser no longer tracks */ }
      last = { x: event.clientX, y: event.clientY };
      idleSince = performance.now();
      const target = part(hit.object);
      if (target && sticks.has(target)) {
        mode = "stick";
        held = target;
        const stick = sticks.get(target)!;
        const normal = new THREE.Vector3(0, 1, 0).transformDirection(stick.pivot.parent!.matrixWorld);
        stickPlane.setFromNormalAndCoplanarPoint(normal, hit.point);
      } else if (target && presses.has(target)) {
        mode = "press";
        held = target;
        presses.get(target)!.spring.target = 1;
        const action = BUTTON_ACTION[target.name];
        if (action) padInput.act(action);
      } else if (target && dpad && target === dpad.node) {
        mode = "press";
        held = target;
        const local = dpad.node.worldToLocal(hit.point.clone());
        if (Math.abs(local.x) > Math.abs(local.z)) {
          dpad.z.target = -Math.sign(local.x) * 0.16;
          padInput.turn(Math.sign(local.x));
        } else {
          dpad.x.target = Math.sign(local.z) * 0.16;
          if (local.z < 0) padInput.act("jump");
        }
      } else {
        mode = "turn";
      }
      el.dataset.grabbing = mode === "turn" ? "true" : "";
    };

    const move = (event: PointerEvent) => {
      if (!mode) {
        const target = part(pick(event)?.object);
        el.style.cursor = target ? "pointer" : pick(event) ? "grab" : "";
        return;
      }
      idleSince = performance.now();
      if (mode === "turn") {
        yaw.target += (event.clientX - last.x) * 0.009;
        pitch.target = THREE.MathUtils.clamp(pitch.target + (event.clientY - last.y) * 0.009, -1.1, 1.1);
        last = { x: event.clientX, y: event.clientY };
      } else if (mode === "stick" && held) {
        const stick = sticks.get(held)!;
        const rect = renderer.domElement.getBoundingClientRect();
        pointer.set(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
        ray.setFromCamera(pointer, camera);
        if (ray.ray.intersectPlane(stickPlane, hitPoint)) {
          const local = stick.pivot.parent!.worldToLocal(hitPoint.clone()).sub(stick.pivot.position);
          const pull = Math.min(1, Math.hypot(local.x, local.z) / 0.6);
          const angle = Math.atan2(local.z, local.x);
          stick.z.target = -Math.cos(angle) * pull * STICK_MAX;
          stick.x.target = Math.sin(angle) * pull * STICK_MAX;
          // Pad-local x runs right and z towards the grips, which faces down on screen.
          padInput[stick.side] = { x: Math.cos(angle) * pull, y: Math.sin(angle) * pull };
        }
      }
    };

    const up = (event: PointerEvent) => {
      if (renderer.domElement.hasPointerCapture(event.pointerId)) renderer.domElement.releasePointerCapture(event.pointerId);
      if (held && presses.has(held)) presses.get(held)!.spring.target = 0;
      if (held && sticks.has(held)) { const s = sticks.get(held)!; s.x.target = 0; s.z.target = 0; padInput[s.side] = { x: 0, y: 0 }; }
      if (held && dpad && held === dpad.node) { dpad.x.target = 0; dpad.z.target = 0; }
      mode = null;
      held = null;
      idleSince = performance.now();
      el.dataset.grabbing = "";
    };

    const canvas = renderer.domElement;
    canvas.addEventListener("pointerdown", down);
    canvas.addEventListener("pointermove", move);
    canvas.addEventListener("pointerup", up);
    canvas.addEventListener("pointercancel", up);

    const qBase = new THREE.Quaternion().setFromEuler(BASE);
    const qYaw = new THREE.Quaternion();
    const qPitch = new THREE.Quaternion();
    const Y = new THREE.Vector3(0, 1, 0);
    const X = new THREE.Vector3(1, 0, 0);
    let frame = 0;
    let then = performance.now();

    const tick = (now: number) => {
      frame = requestAnimationFrame(tick);
      const dt = Math.min((now - then) / 1000, 1 / 30);
      then = now;
      // After a few quiet seconds the pad drifts back to its resting pose.
      if (mode !== "turn" && now - idleSince > 2600) {
        yaw.target *= 0.96;
        pitch.target *= 0.96;
      }
      qYaw.setFromAxisAngle(Y, yaw.step(dt));
      qPitch.setFromAxisAngle(X, pitch.step(dt));
      root.quaternion.copy(qYaw).multiply(qPitch).multiply(qBase);
      if (!calm) root.position.y = Math.sin(now / 1600) * 0.06;

      for (const p of presses.values()) p.node.position.y = p.rest - p.spring.step(dt) * 0.06;
      if (dpad) { dpad.node.rotation.x = dpad.x.step(dt); dpad.node.rotation.z = dpad.z.step(dt); }
      // The spring may overshoot; the stick itself never leaves its socket.
      const limit = STICK_MAX * 1.05;
      for (const s of sticks.values()) {
        s.pivot.rotation.x = THREE.MathUtils.clamp(s.x.step(dt), -limit, limit);
        s.pivot.rotation.z = THREE.MathUtils.clamp(s.z.step(dt), -limit, limit);
      }
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
