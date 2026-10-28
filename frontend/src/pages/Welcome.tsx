import { useQuery } from "@tanstack/react-query";
import { lazy, Suspense, useState } from "react";
import { Link } from "react-router-dom";
import { api, type GameBrief } from "../api";
import { Icon } from "../components/Icon";

const PadScene = lazy(() => import("../components/PadScene"));
const HeroAscii = lazy(() => import("../components/HeroAscii"));

const topParams = new URLSearchParams({ top: "true", limit: "3" });
const countParams = new URLSearchParams({ limit: "1" });
const pad2 = (n: number) => String(n).padStart(2, "0");
const day = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString("ru-RU", { day: "numeric", month: "long", timeZone: "UTC" }) : "—");

/* The entrance in the light identity: one matte object in the middle, the week's top three
   from the catalogue as a small collection — picked at the bottom, described on the right.
   The gamepad also drives the ASCII hero in the background. */
export function Welcome() {
  const top = useQuery({ queryKey: ["games", topParams.toString()], queryFn: () => api.games(topParams) });
  const count = useQuery({ queryKey: ["games", countParams.toString()], queryFn: () => api.games(countParams) });
  const [picked, setPicked] = useState(0);
  const [live, setLive] = useState(false);
  const games = top.data?.items ?? [];
  const game: GameBrief | undefined = games[picked];

  return (
    <div className="entry">
      <header className="entry-head">
        <Link to="/" className="entry-mark">GameGallery<span>оценки и суть<br />новых игр</span></Link>
        <nav aria-label="Основная навигация">
          <Link to="/games">Каталог{count.data && <sup>{count.data.total}</sup>}</Link>
          <Link to="/games?top=1">Топ Metacritic</Link>
        </nav>
        <Link to="/games" className="entry-go">Галерея<Icon name="arrow-right" /></Link>
      </header>

      <div className="entry-left">
        <section className="entry-title">
          <h1>Твоя следующая.<br /><span>Любимая игра.</span></h1>
          <p>Сначала — оценки.<br />Потом — суть.</p>
        </section>
        <Suspense fallback={null}><HeroAscii /></Suspense>
      </div>

      <figure className={`entry-object ${live ? "is-live" : ""}`}>
        <img src="/hero/pad-milk.webp" alt="" width="959" height="735" />
        <Suspense fallback={null}><PadScene onReady={() => setLive(true)} /></Suspense>
        <figcaption>Кнопки — действия героя · стик и крестовина — поворот · корпус — вращать</figcaption>
      </figure>

      <p className="entry-note"><small>01 — Топ недели</small>Лучшие новинки<br />по оценкам критиков.</p>

      <aside className="entry-panel" aria-live="polite">
        <p className="entry-panel-meta"><span>{pad2(picked + 1)} / {pad2(Math.max(games.length, 3))}</span><span>топ Metacritic</span></p>
        <h2>{game?.title ?? "…"}</h2>
        <p className="entry-panel-sub">{game?.genres?.[0] ?? " "}</p>
        <dl>
          <div><dt>Metacritic</dt><dd>{game?.metascore ?? "—"}</dd></div>
          <div><dt>Критиков</dt><dd>{game?.critic_count ?? "—"}</dd></div>
          <div><dt>Релиз</dt><dd>{day(game?.release_date ?? null)}</dd></div>
        </dl>
        <p className="entry-panel-label">Платформы</p>
        <p className="entry-platforms">{game?.platforms.join(" · ") || "—"}</p>
        {game && <Link className="entry-open" to={`/game/${game.slug}`}>Открыть карточку<Icon name="arrow-up-right" /></Link>}
      </aside>

      <footer className="entry-foot">
        <div className="entry-picks" role="tablist" aria-label="Топ недели">
          {games.map((item, i) => (
            <button key={item.slug} role="tab" aria-selected={i === picked} className={i === picked ? "active" : ""} onClick={() => setPicked(i)}>
              <span className="entry-thumb">{item.cover_url && <img src={item.cover_url} alt="" loading="lazy" onError={(e) => { e.currentTarget.hidden = true; }} />}</span>
              <span><small>{pad2(i + 1)}</small>{item.title}</span>
            </button>
          ))}
        </div>
        <nav className="entry-pill" aria-label="Разделы">
          <Link className="active" to="/games?top=1">Топ 20</Link>
          <Link to="/games">Новинки</Link>
          <Link to="/games?sort=metascore">По оценкам</Link>
        </nav>
        <p className="entry-hint">Обновляется<br />каждый час</p>
      </footer>
    </div>
  );
}
