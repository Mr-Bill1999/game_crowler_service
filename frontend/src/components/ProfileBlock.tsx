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
        <div className="factor-grid">
          {Object.entries(FACTORS).map(([id, f]) => (
            <FactorCard key={id} id={id} meta={f} factor={p.profile?.[id]} p={p} />
          ))}
        </div>
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
        <span className={`confidence conf-${factor?.confidence ?? "low"}`} title={`Покрытие фактора: ${score} из 100`}>
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
    <div className="fullness" aria-label={`Наполненность ${p.fullness} из 100`}>
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
          <h4 className="small muted">По факторам</h4>
          <ul className="factor-bars">
            {Object.entries(FACTORS).map(([id, f]) => {
              const v = p.factors[id]?.score ?? 0;
              return (
                <li key={id}>
                  <span className="small">{f.name}</span>
                  <span className="bar" aria-hidden="true">
                    <span style={{ width: `${v}%` }} className={v >= 60 ? "ok" : v >= 30 ? "mid" : "low"} />
                  </span>
                  <span className="mono small">{v}</span>
                </li>
              );
            })}
          </ul>
        </div>
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
    </div>
  );
}
