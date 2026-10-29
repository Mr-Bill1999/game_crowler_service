import { useEffect, useId, useRef, useState } from "react";

/* A dropdown in the site's own look, instead of the browser's white native list: a pill that
   opens a paper-toned menu with room around every option. Keyboard: ↑/↓ move, Enter or Space
   picks, Esc closes; a click outside closes too. */

export type Option = { value: string; label: string };

export function Select({ value, options, onChange, label, disabled = false, className = "" }: {
  value: string;
  options: Option[];
  onChange: (value: string) => void;
  label: string;
  disabled?: boolean;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const id = useId();
  const current = options.find((o) => o.value === value) ?? options[0];

  useEffect(() => {
    if (!open) return;
    setActive(Math.max(0, options.findIndex((o) => o.value === value)));
    const away = (e: PointerEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", away);
    return () => document.removeEventListener("pointerdown", away);
  }, [open, options, value]);

  const pick = (o: Option) => { onChange(o.value); setOpen(false); };
  const key = (e: React.KeyboardEvent) => {
    if (disabled) return;
    if (e.key === "Escape") { setOpen(false); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) { setOpen(true); return; }
      setActive((a) => (a + (e.key === "ArrowDown" ? 1 : -1) + options.length) % options.length);
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (open) pick(options[active]); else setOpen(true);
    } else if (e.key === "Tab") setOpen(false);
  };

  return (
    <div ref={root} className={`select ${open ? "is-open" : ""} ${className}`}>
      <button type="button" className="select-btn" aria-haspopup="listbox" aria-expanded={open} aria-controls={id}
        aria-label={`${label}: ${current?.label ?? ""}`} disabled={disabled} onClick={() => setOpen(!open)} onKeyDown={key}>
        <span>{current?.label}</span>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
      </button>
      {open && (
        <ul id={id} role="listbox" aria-label={label} className="select-menu">
          {options.map((o, i) => (
            <li key={o.value} role="option" aria-selected={o.value === value} className={`${i === active ? "is-active" : ""} ${o.value === value ? "is-on" : ""}`}
              onPointerEnter={() => setActive(i)} onClick={() => pick(o)}>
              {o.label}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
