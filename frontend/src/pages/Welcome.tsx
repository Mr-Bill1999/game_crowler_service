import { useQuery } from "@tanstack/react-query";
import { lazy, Suspense, useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Link } from "react-router-dom";
import { api, type GameBrief } from "../api";
import { Icon } from "../components/Icon";
import type { Mode } from "../components/HeroAscii";
import { gameHud } from "../components/padInput";

const PadScene = lazy(() => import("../components/PadScene"));
const HeroAscii = lazy(() => import("../components/HeroAscii"));

const topParams = new URLSearchParams({ top: "true", limit: "20" });
const countParams = new URLSearchParams({ limit: "1" });
const pad2 = (n: number) => String(n).padStart(2, "0");
const BEST_KEY = "gg-run-best";
const readBest = () => { try { return Number(localStorage.getItem(BEST_KEY)) || 0; } catch { return 0; } };
/** A finger, not a mouse: decides how the run is played (pad or keycaps), whatever the width. */
const touchScreen = () => window.matchMedia("(pointer: coarse)").matches || window.matchMedia("(hover: none)").matches;

const five = (n: number) => String(n).padStart(5, "0");

/** Score and lives over the stage while the run is on, typed in the same ASCII as the game. */
function RunHud({ best, onExit }: { best: number; onExit: () => void }) {
  const hud = useSyncExternalStore(gameHud.subscribe, gameHud.get);
  return (
    <div className="run-hud">
      <pre className="run-score">{"SCORE "}<b>{five(hud.score)}</b>{"\nHI    "}<span>{five(Math.max(best, hud.score))}</span></pre>
      <div className="run-side">
        <pre className="run-lives" aria-label={`Жизней: ${hud.lives}`}>{[0, 1, 2].map((i) => <b key={i} className={i < hud.lives ? "on" : ""}>{i < hud.lives ? "<3" : "</3"}</b>)}</pre>
        <button type="button" className="run-key" onClick={onExit}>[esc] выйти</button>
      </div>
      {touchScreen() && <pre className="run-hint">{"крестовина ^ прыжок  v пригнуться\nверхняя кнопка * огонь"}</pre>}
    </div>
  );
}

/* On a computer the run is played on the keyboard, so the pad gives way to keycaps: W jumps,
   A and D step, S ducks, R fires. They light up as they are pressed, and a click on one presses
   it too. */
const KEYCAPS = [
  { key: "w", label: "W", hint: "прыжок", codes: ["KeyW", "ArrowUp", "Space"] },
  { key: "a", label: "A", hint: "назад", codes: ["KeyA", "ArrowLeft"] },
  { key: "s", label: "S", hint: "пригнуться", codes: ["KeyS", "ArrowDown"] },
  { key: "d", label: "D", hint: "вперёд", codes: ["KeyD", "ArrowRight"] },
  { key: "r", label: "R", hint: "огонь", codes: ["KeyR", "KeyF"] },
] as const;

function RunKeys() {
  const [down, setDown] = useState<ReadonlySet<string>>(new Set());
  useEffect(() => {
    const on = (e: KeyboardEvent) => setDown((d) => (d.has(e.code) ? d : new Set(d).add(e.code)));
    const off = (e: KeyboardEvent) => setDown((d) => { if (!d.has(e.code)) return d; const n = new Set(d); n.delete(e.code); return n; });
    const clear = () => setDown(new Set());
    window.addEventListener("keydown", on);
    window.addEventListener("keyup", off);
    window.addEventListener("blur", clear);
    return () => { window.removeEventListener("keydown", on); window.removeEventListener("keyup", off); window.removeEventListener("blur", clear); };
  }, []);
  const press = (code: string, type: "keydown" | "keyup") => window.dispatchEvent(new KeyboardEvent(type, { code }));
  return (
    <div className="run-keys" aria-label="Управление: W — прыжок, A и D — шаг, S — пригнуться, R — огонь">
      {KEYCAPS.map((k) => (
        <button key={k.key} type="button" tabIndex={-1} className={`keycap key-${k.key} ${k.codes.some((c) => down.has(c)) ? "is-down" : ""}`}
          onPointerDown={(e) => { e.preventDefault(); press(k.codes[0], "keydown"); }}
          onPointerUp={() => press(k.codes[0], "keyup")} onPointerLeave={() => press(k.codes[0], "keyup")}>
          <b>{k.label}</b>
          <small>{k.hint}</small>
        </button>
      ))}
    </div>
  );
}

const day = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString("ru-RU", { day: "numeric", month: "long", timeZone: "UTC" }) : "—");

/* The entrance in the light identity: a stage with an ASCII hero and a 3D gamepad that controls
   him, the Metacritic top 20 as a strip at the bottom — the picked game is described on the right. */
export function Welcome() {
  const top = useQuery({ queryKey: ["games", topParams.toString()], queryFn: () => api.games(topParams) });
  const count = useQuery({ queryKey: ["games", countParams.toString()], queryFn: () => api.games(countParams) });
  const [picked, setPicked] = useState(0);
  const [live, setLive] = useState(false);
  const games = top.data?.items ?? [];
  const game: GameBrief | undefined = games[picked];
  const strip = useRef<HTMLDivElement>(null);
  const [mode, setMode] = useState<Mode>("intro");
  // The device decides how the run is played: keycaps on a computer, the pad on a phone.
  const [touch] = useState(touchScreen);
  const [round, setRound] = useState(0);
  const [best, setBest] = useState(readBest);
  const [result, setResult] = useState<{ score: number; record: boolean } | null>(null);

  const play = useCallback(() => {
    (document.activeElement as HTMLElement | null)?.blur();
    setResult(null);
    setMode("play");
    setRound((r) => r + 1);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, []);
  const exit = useCallback(() => { setMode("intro"); setResult(null); }, []);
  const over = useCallback((score: number) => {
    const record = score > readBest();
    if (record) {
      try { localStorage.setItem(BEST_KEY, String(score)); } catch { /* private mode */ }
      setBest(score);
    }
    setResult({ score, record });
    setMode("over");
    return record;
  }, []);

  // On the game-over card: Enter or Space plays again.
  useEffect(() => {
    if (mode !== "over") return;
    const key = (e: KeyboardEvent) => { if (e.code === "Enter" || e.code === "Space") { e.preventDefault(); play(); } };
    const timer = setTimeout(() => window.addEventListener("keydown", key), 500);
    return () => { clearTimeout(timer); window.removeEventListener("keydown", key); };
  }, [mode, play]);

  // The strip turns with a vertical wheel too, and can be dragged.
  useEffect(() => {
    const el = strip.current;
    if (!el) return;
    const wheel = (e: WheelEvent) => {
      if (Math.abs(e.deltaY) <= Math.abs(e.deltaX) || el.scrollWidth <= el.clientWidth) return;
      el.scrollLeft += e.deltaY;
      e.preventDefault();
    };
    let drag: { x: number; left: number; moved: boolean } | null = null;
    const down = (e: PointerEvent) => { if (e.pointerType === "mouse") drag = { x: e.clientX, left: el.scrollLeft, moved: false }; };
    const move = (e: PointerEvent) => {
      if (!drag) return;
      const dx = e.clientX - drag.x;
      if (Math.abs(dx) > 4) drag.moved = true;
      el.scrollLeft = drag.left - dx;
    };
    const up = () => { if (drag?.moved) el.dataset.dragged = "1"; drag = null; setTimeout(() => delete el.dataset.dragged, 0); };
    el.addEventListener("wheel", wheel, { passive: false });
    el.addEventListener("pointerdown", down);
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return () => {
      el.removeEventListener("wheel", wheel);
      el.removeEventListener("pointerdown", down);
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
  }, []);

  return (
    <div className={`entry ${mode !== "intro" ? "is-playing" : ""} ${touch ? "is-touch" : "is-desk"}`}>
      <header className="entry-head">
        <Link to="/" className="entry-mark">GameGallery</Link>
        <nav aria-label="Основная навигация">
          <Link to="/games">Каталог{count.data && <sup>{count.data.total}</sup>}</Link>
          <Link to="/games?top=1">Топ Metacritic</Link>
        </nav>
        <Link to="/games" className="entry-go">Галерея<Icon name="arrow-right" /></Link>
      </header>

      {/* The stage: the ASCII hero across it, the headline over its top, the gamepad at its foot. */}
      <section className="entry-stage">
        <Suspense fallback={null}><HeroAscii mode={mode} round={round} onOver={over} onExit={exit} /></Suspense>
        {/* The sun of the scene is the Play button: the hero's ASCII scene draws it in this slot; in
            the run it stays up there as the sun. */}
        <button type="button" className={`entry-sun ${mode !== "intro" ? "is-up" : ""}`} onClick={play} aria-label="Играть"
          tabIndex={mode === "intro" ? 0 : -1}>
          <span className="entry-sun-slot" aria-hidden="true" />
          <span className="entry-sun-text">play{best ? ` · рекорд ${best}` : ""}</span>
        </button>
        {mode === "play" && <RunHud best={best} onExit={exit} />}
        {mode === "over" && result && (
          <div className="run-over" role="dialog" aria-label="Игра окончена">
            <pre className="run-over-title">{result.record ? "* * *  NEW RECORD  * * *" : "-=  G A M E   O V E R  =-"}</pre>
            <pre className="run-over-score">{five(result.score)}</pre>
            {!result.record && <pre className="run-over-best">HI {five(best)}</pre>}
            <div className="run-over-actions">
              <button type="button" className="run-key run-again" onClick={play}>[enter] ещё раз</button>
              <button type="button" className="run-key" onClick={exit}>[esc] выйти</button>
            </div>
          </div>
        )}
        <div className="entry-title">
          <h1>Твоя следующая.<br /><span>Любимая игра.</span></h1>
        </div>
        <figure className={`entry-object ${live ? "is-live" : ""}`}>
          <img src="/hero/pad-front.webp" alt="" width="900" height="408" />
          <Suspense fallback={null}><PadScene onReady={() => setLive(true)} inspect={mode === "intro"} /></Suspense>
          {!touch && mode === "play" && <RunKeys />}
        </figure>
      </section>

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
        <div ref={strip} className="entry-picks" role="tablist" aria-label="Топ-20 Metacritic">
          {games.map((item, i) => (
            <button key={item.slug} role="tab" aria-selected={i === picked} className={i === picked ? "active" : ""}
              onClick={() => { if (!strip.current?.dataset.dragged) setPicked(i); }}>
              <span className="entry-thumb">{item.cover_url && <img src={item.cover_url} alt="" loading="lazy" onError={(e) => { e.currentTarget.hidden = true; }} />}</span>
              <span className="entry-pick-text"><small>{pad2(i + 1)}</small><span className="entry-pick-title">{item.title}</span></span>
            </button>
          ))}
        </div>
      </footer>
    </div>
  );
}
