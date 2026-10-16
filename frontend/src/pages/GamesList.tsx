import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, fmtDate, type GameBrief } from "../api";
import { Score, TierPill } from "../components/Score";
import { Cover } from "../components/Cover";
import { Icon } from "../components/Icon";

const SORTS = [
  { value: "release", label: "Дата релиза" },
  { value: "metascore", label: "Metascore" },
  { value: "userscore", label: "Userscore" },
  { value: "reviews", label: "Число отзывов" },
];
const PAGE = 48;

export function GamesList() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const sort = params.get("sort") ?? "release";
  const platforms = params.getAll("platform");
  const archived = params.get("archived") === "1";
  const top = params.get("top") === "1";
  const [draft, setDraft] = useState(q);
  const [limit, setLimit] = useState(PAGE);

  const update = (mutate: (p: URLSearchParams) => void) => {
    const next = new URLSearchParams(params);
    mutate(next);
    setParams(next, { replace: true });
    setLimit(PAGE);
  };

  useEffect(() => {
    const t = setTimeout(() => draft !== q && update((p) => (draft ? p.set("q", draft) : p.delete("q"))), 300);
    return () => clearTimeout(t);
  }, [draft]);

  const query = useMemo(() => {
    const p = new URLSearchParams({ sort, limit: String(limit) });
    if (q) p.set("q", q);
    if (archived) p.set("archived", "true");
    if (top) p.set("top", "true");
    platforms.forEach((x) => p.append("platform", x));
    return p;
  }, [q, sort, archived, top, platforms.join("|"), limit]);

  const games = useQuery({ queryKey: ["games", query.toString()], queryFn: () => api.games(query), placeholderData: (prev) => prev });
  const allPlatforms = useQuery({ queryKey: ["platforms"], queryFn: api.platforms });

  const togglePlatform = (name: string) =>
    update((p) => {
      const cur = p.getAll("platform");
      p.delete("platform");
      (cur.includes(name) ? cur.filter((x) => x !== name) : [...cur, name]).forEach((x) => p.append("platform", x));
    });

  return (
    <div className="page catalog-page">
      <div className="page-head catalog-head">
        <p className="eyebrow"><span className="status-dot" />НАЙДИ ТО, ВО ЧТО ЗАХОЧЕТСЯ ИГРАТЬ</p>
        <h1>{top ? "Топ новых релизов" : "Твоя игровая галерея"}<span className="gradient-text">.</span></h1>
        <p className="muted">
          {games.data ? `${games.data.total.toLocaleString("ru-RU")} игр` : "Загружаю…"} ·{" "}
          {top ? "Топ-20 из New Releases на Metacritic" : "Новинки, оценки и суть отзывов — в одном месте"}
        </p>
      </div>

      <div className="toolbar">
        <label className="search-field" htmlFor="search">
          <Icon name="search" />
          <input
          id="search"
          className="search"
          type="search"
          placeholder="Поиск по названию"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
        </label>
        <label className="field">
          <Icon name="sliders" />
          <span className="sr-only">Сортировка</span>
          <select id="sort" value={sort} disabled={top} onChange={(e) => update((p) => p.set("sort", e.target.value))}>
            {SORTS.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="catalog-filters">
        <div className="chips" role="group" aria-label="Платформы">
          <button className={`chip ${platforms.length === 0 ? "on" : ""}`} aria-pressed={platforms.length === 0} onClick={() => update((p) => p.delete("platform"))}>Все платформы</button>
          {allPlatforms.data?.map((pl) => (
            <button
              key={pl.name}
              className={`chip ${platforms.includes(pl.name) ? "on" : ""}`}
              onClick={() => togglePlatform(pl.name)}
              aria-pressed={platforms.includes(pl.name)}
            >
              {pl.name} <em>{pl.games}</em>
            </button>
          ))}
        </div>
        <label className="check archive-check">
          <input
            id="archived"
            type="checkbox"
            checked={archived}
            onChange={(e) => update((p) => (e.target.checked ? p.set("archived", "1") : p.delete("archived")))}
          />
          Архив
        </label>
      </div>

      <div className="catalog-section-head">
        <div className="feed-tabs" role="group" aria-label="Подборка игр">
          <button className={!top ? "active" : ""} aria-pressed={!top} onClick={() => update((p) => p.delete("top"))}>Новые релизы</button>
          <button className={top ? "active" : ""} aria-pressed={top} onClick={() => update((p) => (top ? p.delete("top") : p.set("top", "1")))}>Топ Metacritic <span>20</span></button>
        </div>
        <span className="catalog-update"><span className="status-dot" />Обновляется каждый час</span>
      </div>

      {games.isError && <p className="error" role="alert">Не удалось загрузить список: {String(games.error.message)}</p>}
      {games.data && games.data.items.length === 0 && (
        <div className="empty">
          <h3>Ничего не найдено</h3>
          <p>Сбросьте фильтры или запустите первый сбор в DevPanel.</p>
        </div>
      )}

      <div className="grid" aria-busy={games.isPending}>
        {games.isPending && Array.from({ length: 8 }, (_, i) => <div className="tile tile-skeleton" key={i} aria-hidden="true"><div className="cover" /><div className="tile-body"><span /><span /><span /></div></div>)}
        {games.data?.items.map((g) => <GameTile key={g.id} g={g} />)}
      </div>

      {games.data && games.data.items.length < games.data.total && (
        <button className="btn ghost more" onClick={() => setLimit((l) => l + PAGE)}>
          Показать ещё
        </button>
      )}
      <footer className="catalog-footer"><span>Оценки критиков и игроков · Metacritic</span><span>GameGallery</span></footer>
    </div>
  );
}

function GameTile({ g }: { g: GameBrief }) {
  return (
    <Link to={`/game/${g.slug}`} className="tile">
      <Cover className="cover" src={g.cover_url}>
        <div className="cover-badges">
          {g.top_rank != null && <span className="pill rank" title="Место в блоке New Releases на Metacritic">В топе · #{g.top_rank}</span>}
          <TierPill tier={g.tier} />
        </div>
      </Cover>
      <div className="tile-body">
        <div className="tile-title"><h3>{g.title}</h3><Icon name="arrow-up-right" /></div>
        {g.genres.length > 0 && <p className="tile-genres">{g.genres.join(" · ")}</p>}
        <p className="tile-date">{fmtDate(g.release_date)}</p>
        <p className="tile-platforms" title={g.platforms.join(", ")}>{g.platforms.join(" · ")}</p>
        <div className="tile-bottom">
          <div className="tile-rating"><Score value={g.metascore} kind="meta" /><span>Критики<small>{g.critic_count.toLocaleString("ru-RU")} рецензий</small></span></div>
          <div className="tile-rating"><Score value={g.userscore} kind="user" /><span>Игроки<small>{g.user_count.toLocaleString("ru-RU")} оценок</small></span></div>
        </div>
      </div>
    </Link>
  );
}
