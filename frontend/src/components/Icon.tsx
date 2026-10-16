import type { ReactNode } from "react";

type IconName = "arrow-right" | "arrow-up-right" | "arrow-left" | "search" | "pause" | "play" | "sparkles" | "terminal" | "sliders";

const paths: Record<IconName, ReactNode> = {
  "arrow-right": <><path d="M4 12h15M13 5l7 7-7 7" /></>,
  "arrow-up-right": <><path d="M6 18 18 6M6 6h12v12" /></>,
  "arrow-left": <><path d="M20 12H5M11 5l-7 7 7 7" /></>,
  search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 4 4" /></>,
  pause: <><path d="M8 5v14M16 5v14" /></>,
  play: <path d="m8 5 11 7-11 7Z" />,
  sparkles: <><path d="m12 3 2.7 6.3L21 12l-6.3 2.7L12 21l-2.7-6.3L3 12l6.3-2.7Z" /><path d="m20 2 .8 1.2L22 4l-1.2.8L20 6l-.8-1.2L18 4l1.2-.8Z" /></>,
  terminal: <><path d="m5 6 6 6-6 6M14 18h6" /></>,
  sliders: <><path d="M4 7h7m4 0h5M4 17h3m4 0h9" /><circle cx="13" cy="7" r="2" /><circle cx="9" cy="17" r="2" /></>,
};

export function Icon({ name, className = "" }: { name: IconName; className?: string }) {
  return <svg className={`icon ${className}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}
