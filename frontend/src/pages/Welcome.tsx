import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Icon } from "../components/Icon";

const countParams = new URLSearchParams({ limit: "1" });
const plural = (n: number) => (n % 10 === 1 && n % 100 !== 11 ? "игра" : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20) ? "игры" : "игр");

export function Welcome() {
  const video = useRef<HTMLVideoElement>(null);
  const games = useQuery({ queryKey: ["games", countParams.toString()], queryFn: () => api.games(countParams) });
  const total = games.data?.total;

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
        <h1>Твоя следующая<br /><span className="gradient-text">любимая игра.</span></h1>
        <p className="welcome-description">Оценки критиков, мнения игроков и главное об игре — в одном месте.</p>
        <div className="welcome-actions">
          <Link className="btn" to="/games">Открыть галерею<Icon name="arrow-right" /></Link>
          <Link className="welcome-link" to="/games?top=1">Топ Metacritic<Icon name="arrow-up-right" /></Link>
        </div>
      </section>
      <p className="welcome-meta">
        {total ? <>{total.toLocaleString("ru-RU")} {plural(total)} · </> : null}Metacritic и Steam · обновляется каждый час
      </p>
    </div>
  );
}
