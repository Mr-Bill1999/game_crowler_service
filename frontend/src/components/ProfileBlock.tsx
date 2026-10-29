import { useState } from "react";
import type { GameDetail, GameProfile, ProfileFactor } from "../api";

const FACTORS: Record<string, { name: string; question: string }> = {
  "1": { name: "Ключевая идентичность", question: "Какую роль и опыт предлагает игра, что обязано сохраниться" },
  "2": { name: "Художественный крючок", question: "Что делает её узнаваемой: визуал, звук, повествование" },
  "3": { name: "Геймдизайнерское ядро", question: "Что игрок делает постоянно, как устроены риск и награда" },
  "4": { name: "Эмоциональное предложение", question: "Какие переживания и за счёт каких решений" },
  "5": { name: "Противоречия и ограничения", question: "Где сила отталкивает, для кого опыт не работает" },
  "6": { name: "Рыночная дифференциация", question: "Чем отличается от альтернатив и кому это важно" },
};

const SOURCES: Record<string, string> = {
  mc: "Metacritic",
  steam_store: "Магазин Steam",
  steam_reviews: "Отзывы Steam",
  steam_news: "Новости разработчика",
  critic_full: "Полные рецензии",
};

const CONFIDENCE: Record<ProfileFactor["confidence"], string> = {
  high: "уверенно",
  medium: "умеренно",
  low: "мало данных",
};

const BANDS = [
  { from: 0, to: 30, label: "скудно" },
  { from: 30, to: 60, label: "частично" },
  { from: 60, to: 80, label: "достаточно" },
  { from: 80, to: 100, label: "богато" },
];

export function ProfileBlock({ g }: { g: GameDetail }) {
  const p = g.profile;
  if (!p) {
    return (
      <section className="block placeholder profile-block">
        <h2>Профиль игры</h2>
        <p>Данные об игре с других платформ ещё собираются. Профиль появится, когда их будет достаточно.</p>
      </section>
    );
  }
  return (
    <section className="block profile-block" aria-labelledby="profile-title">
      <div className="profile-head">
        <h2 id="profile-title">Профиль игры</h2>
        <span className={`band band-${p.band}`}>{p.band_label}</span>
      </div>
      {p.profile ? (
        <FactorMap p={p} />
      ) : (
        <p className="profile-thin">
          Данных пока мало для выводов: профиль строится от наполненности {p.threshold} из 100. Ниже видно, что уже
          собрано и чего не хватает.
        </p>
      )}
      <Fullness p={p} />
    </section>
  );
}

/* The six factors as a radar that is also the way in: one ray per factor, its length how much
   evidence stands behind it (0–100), the dot's fill how sure the model is (full, half, hollow).
   Picking a ray or its label opens that factor's verdict and evidence beside the chart, so six
   long cards become one view. */
const SHORT: Record<string, string> = {
  "1": "Идентичность", "2": "Арт-крючок", "3": "Геймдизайн", "4": "Эмоции", "5": "Ограничения", "6": "Рынок",
};

function FactorMap({ p }: { p: GameProfile }) {
  const [picked, setPicked] = useState("1");
  const ids = Object.keys(FACTORS);
  const W = 380, H = 330, cx = W / 2, cy = H / 2 + 4, R = 112;
  const angle = (i: number) => -Math.PI / 2 + (i * 2 * Math.PI) / ids.length;
  const at = (i: number, r: number) => [cx + Math.cos(angle(i)) * r, cy + Math.sin(angle(i)) * r] as const;
  const ring = (k: number) => ids.map((_, i) => at(i, R * k).join(",")).join(" ");
  const values = ids.map((id) => Math.max(4, p.factors[id]?.score ?? 0));
  const shape = values.map((v, i) => at(i, (R * v) / 100).join(",")).join(" ");
  return (
    <div className="factor-map">
      <figure className="radar">
        <svg viewBox={`0 0 ${W} ${H}`} role="group" aria-label="Покрытие данными по шести аспектам профиля">
          {[0.25, 0.5, 0.75, 1].map((k) => <polygon key={k} className="radar-grid" points={ring(k)} />)}
          {ids.map((id, i) => {
            const [x, y] = at(i, R);
            return <line key={id} className={`radar-axis${picked === id ? " on" : ""}`} x1={cx} y1={cy} x2={x} y2={y} />;
          })}
          <polygon className="radar-shape" points={shape} />
          {ids.map((id, i) => {
            const v = p.factors[id]?.score ?? 0;
            const conf = p.profile?.[id]?.confidence ?? "low";
            const [x, y] = at(i, (R * Math.max(4, v)) / 100);
            const [lx, ly] = at(i, R + 26);
            const anchor = Math.abs(lx - cx) < 8 ? "middle" : lx > cx ? "start" : "end";
            const pick = () => setPicked(id);
            return (
              <g key={id} className={`radar-point${picked === id ? " on" : ""}`} role="button" tabIndex={0} aria-pressed={picked === id}
                aria-label={`${FACTORS[id].name}: покрытие ${v} из 100, ${CONFIDENCE[conf]}`}
                onClick={pick} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } }}>
                <title>{`${FACTORS[id].name} · ${v} из 100 · ${CONFIDENCE[conf]}`}</title>
                <line className="radar-hit" x1={cx} y1={cy} x2={lx} y2={ly} />
                <circle className={`radar-dot conf-${conf}`} cx={x} cy={y} r={picked === id ? 6 : 4.5} />
                <text x={lx} y={ly} textAnchor={anchor} dominantBaseline="middle">
                  <tspan className="radar-num">{id} </tspan>{SHORT[id]}
                  <tspan className="radar-val" x={lx} dy="14">{v}</tspan>
                </text>
              </g>
            );
          })}
        </svg>
        <figcaption className="muted small">
          Длина луча — сколько данных за выводом, из 100.
          <span className="radar-legend">
            <span><i className="radar-key conf-high" />уверенно</span>
            <span><i className="radar-key conf-medium" />умеренно</span>
            <span><i className="radar-key conf-low" />мало данных</span>
          </span>
        </figcaption>
      </figure>
      <div className="factor-detail">
        <FactorCard id={picked} meta={FACTORS[picked]} factor={p.profile?.[picked]} p={p} />
      </div>
    </div>
  );
}

function FactorCard({ id, meta, factor, p }: {
  id: string;
  meta: { name: string; question: string };
  factor?: ProfileFactor;
  p: GameProfile;
}) {
  const score = p.factors[id]?.score ?? 0;
  return (
    <article className={`factor${factor?.confidence === "low" ? " factor-low" : ""}`}>
      <header>
        <span className="factor-num mono">{id}</span>
        <div>
          <h3>{meta.name}</h3>
          <p className="muted small">{meta.question}</p>
        </div>
        <span className={`conf-label conf-${factor?.confidence ?? "low"}`} title={`Покрытие фактора: ${score} из 100`}>
          {CONFIDENCE[factor?.confidence ?? "low"]}
        </span>
      </header>
      {factor?.summary && <p>{factor.summary}</p>}
      {!!factor?.points.length && (
        <ul>
          {factor.points.map((pt, i) => (
            <li key={i}>
              {pt.text}
              <Sources ids={pt.evidence} p={p} />
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}

/** Where a point comes from: one chip per source, its evidence in the tooltip, a link when there is one. */
function Sources({ ids, p }: { ids: number[]; p: GameProfile }) {
  const bySource = new Map<string, { claims: string[]; url?: string; dev: boolean }>();
  for (const id of ids) {
    const e = p.evidence[String(id)];
    if (!e) continue;
    const s = bySource.get(e.source) ?? { claims: [], dev: e.is_claim };
    s.claims.push(e.claim);
    s.url ??= e.urls[0];
    bySource.set(e.source, s);
  }
  if (!bySource.size) return null;
  return (
    <span className="point-sources">
      {[...bySource].map(([src, s]) => {
        const label = `${SOURCES[src] ?? src}${s.claims.length > 1 ? ` ×${s.claims.length}` : ""}`;
        const title = (s.dev ? "Заявление разработчика. " : "") + s.claims.join("\n");
        return s.url ? (
          <a key={src} className="src-chip" href={s.url} target="_blank" rel="noreferrer" title={title}>
            {label}
          </a>
        ) : (
          <span key={src} className={`src-chip${s.dev ? " src-dev" : ""}`} title={title}>
            {label}
          </span>
        );
      })}
    </span>
  );
}

function Fullness({ p }: { p: GameProfile }) {
  const sources = Object.entries(p.sources).sort((a, b) => b[1].evidence - a[1].evidence);
  return (
    <details className="fullness" aria-label={`Наполненность ${p.fullness} из 100`}>
      <summary>Наполненность {p.fullness} из 100 · источники и чего не хватает</summary>
      <div className="fullness-top">
        <div>
          <h3>Наполненность</h3>
          <p className="muted small">
            Насколько суждения выше опираются на данные: конкретные и подтверждённые разными источниками
            доказательства по каждому фактору.
          </p>
        </div>
        <div className="fullness-num">
          <span className="mono">{p.fullness}</span>
          <span className="muted small">из 100</span>
        </div>
      </div>
      <div className="fullness-scale">
        <div className="fullness-track">
          {BANDS.map((b) => (
            <span key={b.from} className="fullness-band" style={{ left: `${b.from}%`, width: `${b.to - b.from}%` }} />
          ))}
          <span className={`fullness-fill band-${p.band}`} style={{ width: `${Math.max(2, p.fullness)}%` }} />
          <span className="fullness-threshold" style={{ left: `${p.threshold}%` }} title={`Порог профиля: ${p.threshold}`} />
        </div>
        <div className="fullness-labels small muted">
          {BANDS.map((b) => (
            <span key={b.from} style={{ left: `${b.from}%`, width: `${b.to - b.from}%` }}>
              {b.label}
            </span>
          ))}
        </div>
      </div>
      <div className="fullness-cols">
        <div>
          <h4 className="small muted">Источники</h4>
          <ul className="source-list small">
            {sources.map(([src, s]) => (
              <li key={src}>
                <span>{SOURCES[src] ?? src}</span>
                <span className="mono muted">
                  {s.documents} текстов · {s.evidence} доказательств
                </span>
              </li>
            ))}
            {p.steam?.url && (
              <li>
                <a href={p.steam.url} target="_blank" rel="noreferrer">
                  Страница в Steam
                </a>
                {p.steam.reviews?.total_reviews != null && (
                  <span className="mono muted">{p.steam.reviews.total_reviews} отзывов всего</span>
                )}
              </li>
            )}
          </ul>
          {!!p.missing.length && (
            <>
              <h4 className="small muted">Чего не хватает</h4>
              <ul className="missing small">
                {p.missing.map((m) => (
                  <li key={m}>{m}</li>
                ))}
              </ul>
            </>
          )}
        </div>
      </div>
      <p className="muted small mono">
        Собрано {p.enriched_at ? new Date(p.enriched_at).toLocaleString("ru-RU") : "—"}
        {p.profiled_at && ` · профиль ${new Date(p.profiled_at).toLocaleString("ru-RU")}`}
        {p.model && ` · ${p.model}`}
      </p>
    </details>
  );
}