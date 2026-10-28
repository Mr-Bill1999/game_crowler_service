import { useEffect, useRef } from "react";
import { Link } from "react-router-dom";
import { Icon } from "../components/Icon";

export function Welcome() {
  const video = useRef<HTMLVideoElement>(null);

  // The scene is decoration: it plays by itself, and people who asked for less motion get the still poster.
  useEffect(() => {
    const element = video.current;
    if (!element || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    element.play().catch(() => undefined);
  }, []);

  return (
    <div className="welcome">
      <video ref={video} className="welcome-video" poster="/hero/hero-poster.webp" muted loop playsInline preload="auto" aria-hidden="true">
        <source src="/hero/hero-loop.webm" type="video/webm" />
        <source src="/hero/hero-loop.mp4" type="video/mp4" />
      </video>
      <section className="welcome-content">
        <h1>Твоя следующая<br />любимая игра</h1>
        <Link className="welcome-cta" to="/games">Открыть галерею<span><Icon name="arrow-right" /></span></Link>
      </section>
    </div>
  );
}
