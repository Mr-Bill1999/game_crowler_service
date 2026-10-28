import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Icon } from "../components/Icon";

const reducedMotion = () => typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export function Welcome() {
  const [paused, setPaused] = useState(reducedMotion);
  const video = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    const element = video.current;
    if (!element) return;
    if (paused) element.pause();
    // A load still choosing its source rejects with AbortError; only a blocked autoplay means "paused".
    else element.play().catch((error: DOMException) => { if (error.name === "NotAllowedError") setPaused(true); });
  }, [paused]);

  return (
    <div className={`welcome ${paused ? "is-paused" : ""}`}>
      <div className="welcome-scene">
        <video ref={video} className="welcome-video" poster="/hero/hero-poster.webp" autoPlay={!paused} muted loop playsInline preload="auto" aria-hidden="true">
          <source src="/hero/hero-loop.webm" type="video/webm" />
          <source src="/hero/hero-loop.mp4" type="video/mp4" />
        </video>
        <div className="welcome-content">
          <p className="eyebrow"><span className="status-dot" />ТВОЁ СЛЕДУЮЩЕЕ ПРИКЛЮЧЕНИЕ</p>
          <h1>Твоя следующая<br /><span className="gradient-text">любимая игра.</span></h1>
          <p className="welcome-description">Оценки критиков, мнения игроков<br />и главное об игре — в одном месте.</p>
          <nav className="welcome-actions" aria-label="Открыть каталог игр">
            <Link className="btn" to="/games">Открыть галерею<Icon name="arrow-right" /></Link>
            <Link className="btn ghost" to="/games?top=1">Топ Metacritic<Icon name="arrow-up-right" /></Link>
          </nav>
        </div>
      </div>
      <footer className="welcome-footer">
        <span><span className="status-dot" />Обновляется каждый час</span>
        <button className="motion-control" type="button" onClick={() => setPaused((value) => !value)} aria-pressed={paused} aria-label={paused ? "Продолжить анимацию фона" : "Приостановить анимацию фона"}>
          <Icon name={paused ? "play" : "pause"} />{paused ? "Продолжить" : "Пауза"}
        </button>
      </footer>
    </div>
  );
}
