import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Icon } from "../components/Icon";
import { GameReel } from "../components/GameReel";

const previewParams = new URLSearchParams({ sort: "release", limit: "200" });

export function Welcome() {
  const [paused, setPaused] = useState(false);
  const games = useQuery({ queryKey: ["games", previewParams.toString()], queryFn: () => api.games(previewParams) });
  const covers = useMemo(() => games.data?.items.flatMap((game) => game.cover_url ? [game.cover_url] : []) ?? [], [games.data]);

  return (
    <div className={`welcome ${paused ? "is-paused" : ""}`}>
      <div className="welcome-scene">
        <GameReel sources={covers} />
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
        <button className="motion-control" type="button" onClick={() => setPaused((value) => !value)} aria-pressed={paused} aria-label={paused ? "Продолжить движение обложек" : "Приостановить движение обложек"}>
          <Icon name={paused ? "play" : "pause"} />{paused ? "Продолжить" : "Пауза"}
        </button>
      </footer>
    </div>
  );
}
