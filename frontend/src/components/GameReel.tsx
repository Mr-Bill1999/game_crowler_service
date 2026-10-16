import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { ReelAssets, type ReelCover } from "./reelAssets";

type Observe = (element: HTMLElement, callback: (near: boolean, priority: number) => void) => () => void;
const DURATIONS = [140, 165, 130, 155];

function outsideRow(element: HTMLElement) {
  const x = new DOMMatrixReadOnly(getComputedStyle(element).transform).m41;
  return x + element.offsetWidth <= -32 || x >= element.parentElement!.clientWidth + 32;
}

function ReelTile({ assets, observe, delay, x }: { assets: ReelAssets; observe: Observe; delay: number; x: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const refresh = useRef<() => void>(() => {});
  const [src, setSrc] = useState<string | null>(null);

  useEffect(() => {
    const element = ref.current!;
    let live = true;
    let near = false;
    let priority = 1;
    let loading = false;
    let current: ReelCover | null = null;
    let prepared: ReelCover | null = null;
    const commit = () => {
      if (!prepared || (current && !outsideRow(element))) return;
      current?.release();
      current = prepared;
      prepared = null;
      setSrc(current.src);
    };
    const prepare = async (retries = 1) => {
      if (!live || loading || prepared || (!near && !current) || document.hidden) { commit(); return; }
      loading = true;
      const next = await assets.acquire(current?.src ?? null, priority);
      loading = false;
      if (!live) { next?.release(); return; }
      if (next) { prepared = next; commit(); }
      else if (!current && retries > 0) void prepare(retries - 1);
    };
    const unobserve = observe(element, (isNear, nextPriority) => {
      near = isNear;
      priority = nextPriority;
      if (!current && near) void prepare();
      else commit();
    });
    const unsubscribe = assets.subscribe(() => { if (!current && near) void prepare(); });
    refresh.current = () => { commit(); void prepare(); };
    return () => {
      live = false;
      unobserve();
      unsubscribe();
      current?.release();
      prepared?.release();
      refresh.current = () => {};
    };
  }, [assets, observe]);

  return (
    <div ref={ref} className="game-cover reel-tile" style={{ animationDelay: `${-delay}s`, transform: `translateX(${x}px)` }} onAnimationIteration={() => refresh.current()}>
      {src && <img src={src} alt="" decoding="async" draggable={false} />}
    </div>
  );
}

export function GameReel({ sources }: { sources: string[] }) {
  const wallRef = useRef<HTMLDivElement>(null);
  const [assets] = useState(() => new ReelAssets());
  const lifecycle = useRef(0);
  const callbacks = useRef(new Map<HTMLElement, (near: boolean, priority: number) => void>());
  const intersection = useRef<IntersectionObserver | null>(null);
  const [layout, setLayout] = useState({ width: 1600, rows: 7, columns: 10, step: 320, tileHeight: 167 });

  useEffect(() => {
    const run = ++lifecycle.current;
    // StrictMode replays effects in development; keep the same queue for that replay.
    return () => { queueMicrotask(() => { if (lifecycle.current === run) assets.dispose(); }); };
  }, [assets]);
  useEffect(() => assets.setSources(sources), [assets, sources]);

  const observe = useCallback<Observe>((element, callback) => {
    callbacks.current.set(element, callback);
    intersection.current?.observe(element);
    return () => { callbacks.current.delete(element); intersection.current?.unobserve(element); };
  }, []);

  useLayoutEffect(() => {
    const wall = wallRef.current!;
    const scene = wall.parentElement!;
    const measure = () => {
      const tile = wall.querySelector<HTMLElement>(".reel-tile")!;
      const style = getComputedStyle(tile);
      const tileWidth = parseFloat(style.width);
      const tileHeight = parseFloat(style.height);
      const angle = Math.PI / 12;
      const width = Math.ceil(scene.clientWidth * Math.cos(angle) + scene.clientHeight * Math.sin(angle) + tileWidth);
      const height = scene.clientHeight * Math.cos(angle) + scene.clientWidth * Math.sin(angle) + tileHeight;
      const step = tileWidth + 20;
      const next = { width, step, tileHeight, columns: Math.ceil(width / step) + 4, rows: Math.ceil(height / (tileHeight + 20)) + 1 };
      setLayout((previous) => Object.keys(next).every((key) => previous[key as keyof typeof next] === next[key as keyof typeof next]) ? previous : next);
    };
    const observer = new ResizeObserver(measure);
    observer.observe(scene);
    measure();
    const visibility = new IntersectionObserver((entries) => {
      const bounds = scene.getBoundingClientRect();
      for (const entry of entries) {
        const rect = entry.boundingClientRect;
        const visible = rect.right > bounds.left && rect.left < bounds.right && rect.bottom > bounds.top && rect.top < bounds.bottom;
        callbacks.current.get(entry.target as HTMLElement)?.(entry.isIntersecting, visible ? 0 : 1);
      }
    }, { root: scene, rootMargin: "240px" });
    intersection.current = visibility;
    callbacks.current.forEach((_, element) => visibility.observe(element));
    return () => { observer.disconnect(); visibility.disconnect(); intersection.current = null; };
  }, []);

  return (
    <div ref={wallRef} className="reel-wall" aria-hidden="true" style={{ width: layout.width }}>
      {Array.from({ length: layout.rows }, (_, row) => {
        const duration = DURATIONS[row % DURATIONS.length] * layout.columns / (layout.columns - 3);
        return (
          <div className="reel-track" key={row} style={{ height: layout.tileHeight, "--reel-duration": `${duration}s`, "--reel-start": `${-2 * layout.step}px`, "--reel-end": `${(layout.columns - 2) * layout.step}px` } as CSSProperties}>
            {Array.from({ length: layout.columns }, (_, slot) => <ReelTile key={slot} assets={assets} observe={observe} delay={slot * duration / layout.columns + row * 19} x={(slot - 2) * layout.step} />)}
          </div>
        );
      })}
    </div>
  );
}
