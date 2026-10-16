import { useEffect, useState, type ReactNode } from "react";

export function Cover({ src, alt = "", className = "", children, loading = "lazy" }: { src: string | null; alt?: string; className?: string; children?: ReactNode; loading?: "lazy" | "eager" }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);
  return (
    <div className={`game-cover ${className}`}>
      {src && !failed ? <img src={src} alt={alt} loading={loading} decoding="async" onError={() => setFailed(true)} /> : (
        <span className="cover-fallback"><span className="brand-mark" aria-hidden="true"><span /><span /></span>{alt && <span>Обложка недоступна</span>}</span>
      )}
      {children}
    </div>
  );
}
