import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { MeshoptDecoder } from "three/examples/jsm/libs/meshopt_decoder.module.js";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { asciiPass } from "./asciiKit";

/* The entrance wizard in miniature, dancing on top of the catalog's search field: the same model, light and
   ASCII pass as the welcome stage, on a finer grid so he stays readable this small. Every keystroke in the
   search makes him dance faster for a moment. He draws at 30 fps and only while on screen; with reduced
   motion he holds one pose. */

const MODEL = "/hero/hero.glb";
const CELL = { w: 4, h: 6 }; // finer than the stage's 6×11, with about as many rows on the figure: the same detail, smaller
const HERO_SHARE = 0.86; // of the canvas height, feet on the bottom edge; the rest is room for his hat in the dance

export default function SearchDancer({ input }: { input: React.RefObject<HTMLInputElement | null> }) {
  const host = useRef<HTMLDivElement>(null);
  const [ready, setReady] = useState(false);

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
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    // The stage's own light and lens.
    const scene = new THREE.Scene();
    const key = new THREE.DirectionalLight(0xffffff, 2.6);
    key.position.set(-3, 4, 3);
    const rim = new THREE.DirectionalLight(0xffffff, 0.6);
    rim.position.set(4, 2, -2);
    scene.add(key, rim, new THREE.AmbientLight(0xffffff, 0.55));
    const camera = new THREE.PerspectiveCamera(26, 1, 0.1, 80);
    const target = new THREE.WebGLRenderTarget(8, 8, { minFilter: THREE.LinearFilter, magFilter: THREE.LinearFilter });
    const pass = asciiPass(target);

    const fit = () => {
      const w = el.clientWidth;
      const h = el.clientHeight;
      if (!w || !h) return;
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      renderer.setSize(w, h, false);
      const cols = Math.max(8, Math.floor(w / CELL.w));
      const rows = Math.max(8, Math.floor(h / CELL.h));
      pass.uniforms.uGrid.value.set(cols, rows);
      target.setSize(cols * 2, rows * 2);
      camera.aspect = w / h;
      const viewH = 2.6 / HERO_SHARE;
      const dist = viewH / 2 / Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
      const centreY = viewH / 2 - 0.02;
      camera.position.set(0, centreY, dist);
      camera.lookAt(0, centreY, 0);
      camera.updateProjectionMatrix();
      draw();
    };

    let mixer: THREE.AnimationMixer | null = null;
    let dance: THREE.AnimationAction | null = null;
    let disposed = false;
    const hero = new THREE.Group();
    scene.add(hero);

    const draw = () => {
      renderer.setRenderTarget(target);
      renderer.clear();
      renderer.render(scene, camera);
      renderer.setRenderTarget(null);
      renderer.clear();
      if (pass.ready()) pass.render(renderer);
    };

    // Typing cheers him on: each key adds speed, which settles back to normal.
    let boost = 0;
    const keyed = () => { boost = Math.min(1.2, boost + 0.35); };
    const field = input.current;
    field?.addEventListener("input", keyed);

    let visible = true;
    const seen = new IntersectionObserver(([e]) => { visible = e.isIntersecting; });
    seen.observe(el);

    const clock = new THREE.Clock();
    let frame = 0;
    let acc = 0;
    const tick = () => {
      frame = requestAnimationFrame(tick);
      const dt = Math.min(clock.getDelta(), 1 / 20);
      if (!visible || document.hidden || !mixer) return;
      // Reduced motion: one still pose, drawn once the glyphs are in.
      if (reduce) { if (pass.ready()) { draw(); cancelAnimationFrame(frame); } return; }
      acc += dt;
      if (acc < 1 / 30) return;
      boost *= Math.exp(-acc * 1.6);
      if (dance) dance.timeScale = 1 + boost;
      mixer.update(acc);
      acc = 0;
      draw();
    };

    const loader = new GLTFLoader();
    loader.setMeshoptDecoder(MeshoptDecoder);
    loader.load(MODEL, (gltf) => {
      if (disposed) return;
      hero.add(gltf.scene);
      mixer = new THREE.AnimationMixer(gltf.scene);
      const clip = gltf.animations.find((c) => c.name === "Hero_Dance");
      if (!clip) return;
      dance = mixer.clipAction(clip);
      dance.setLoop(THREE.LoopRepeat, Infinity);
      dance.play();
      if (reduce) mixer.setTime(clip.duration * 0.3);
      frame = requestAnimationFrame(tick);
      setReady(true);
    }, undefined, () => undefined);

    const resize = new ResizeObserver(fit);
    resize.observe(el);
    fit();

    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      resize.disconnect();
      seen.disconnect();
      field?.removeEventListener("input", keyed);
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
  }, [input]);

  return <div ref={host} className={`search-dancer ${ready ? "is-ready" : ""}`} aria-hidden="true" />;
}
