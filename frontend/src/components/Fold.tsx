import { useLayoutEffect, useRef, useState, type ReactNode } from "react";

/* Ways to keep long content short without losing it: text clamped to a few lines, lists folded
   to their first items, each with a toggle that only appears when there is more. */

/** Text clamped to a few lines, with a toggle only when there is more. */
export function Clamp({ lines = 3, className = "", children }: { lines?: number; className?: string; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [more, setMore] = useState(false);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || open) return;
    const check = () => setMore(el.scrollHeight > el.clientHeight + 2);
    check();
    const ro = new ResizeObserver(check);
    ro.observe(el);
    return () => ro.disconnect();
  }, [open, children]);
  return (
    <div className={`clamp ${className}`}>
      <div ref={ref} className="clamp-text" style={open ? undefined : { WebkitLineClamp: lines }} data-open={open || undefined}>
        {children}
      </div>
      {(more || open) && (
        <button type="button" className="link-btn" onClick={() => setOpen(!open)}>{open ? "свернуть" : "дальше"}</button>
      )}
    </div>
  );
}

/** The first few items of a list, the rest behind "ещё N". */
export function Fold<T>({ items, n = 3, render, className = "" }: { items: T[]; n?: number; render: (item: T, i: number) => ReactNode; className?: string }) {
  const [open, setOpen] = useState(false);
  if (!items.length) return null;
  const shown = open ? items : items.slice(0, n);
  return (
    <>
      <ul className={className}>{shown.map(render)}</ul>
      {items.length > n && (
        <button type="button" className="link-btn" onClick={() => setOpen(!open)}>
          {open ? "свернуть" : `ещё ${items.length - n}`}
        </button>
      )}
    </>
  );
}
