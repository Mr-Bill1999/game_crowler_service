import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, fmtDate, type Essence, type GameDetail, type LightEssence, type ReviewSummary, type SimilarItem } from "../api";
import { tagLabel } from "../tags";
import { Score, TierPill } from "../components/Score";
import { Cover } from "../components/Cover";
import { Icon } from "../components/Icon";

export function GameCard() {
  const { slug = "" } = useParams();
  const game = useQuery({ queryKey: ["game", slug], queryFn: () => api.game(slug) });

  if (game.isPending) return <div className="page muted">Загружаю карточку…</div>;
  if (game.isError) return <div className="page error">Не удалось открыть игру: {game.error.message}</div>;
  const g = game.data;

  return (
    <div className="page card-page">
      <Link to="/games" className="back">
        <Icon name="arrow-left" /> Все игры
      </Link>
      <section className="hero">
        <Cover className="hero-cover" src={g.cover_url} alt={`Обложка ${g.title}`} />
        <div className="hero-info">
          <h1>{g.title}</h1>
          <p className="muted">
            {g.developer ?? "Разработчик неизвестен"}
            {g.publisher && g.publisher !== g.developer ? ` · издатель ${g.publisher}` : ""} · {fmtDate(g.release_date)}
          </p>
          <div className="hero-scores">
            <div className="big-score">
              <Score value={g.metascore} kind="meta" size="l" />
              <span>
                Metascore
                <br />
                <em>{g.critic_count} рецензий</em>
              </span>
            </div>
            <div className="big-score">
              <Score value={g.userscore} kind="user" size="l" />
              <span>
                Userscore
                <br />
                <em>{g.user_count} оценок</em>
              </span>
            </div>
          </div>
          <table className="platforms">
            <tbody>
              {g.platform_scores.map((p) => (
                <tr key={p.slug}>
                  <td>{p.name}</td>
                  <td>
                    <Score value={p.metascore} kind="meta" />
                  </td>
                  <td className="mono muted">{p.critic_count ?? 0} рец.</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="tags">
            {g.genres.map((x) => (
              <span key={x} className="pill">
                {x}
              </span>
            ))}
            {g.rating && <span className="pill">ESRB {g.rating}</span>}
            <TierPill tier={g.tier} />
          </div>
        </div>
      </section>

      {g.description && <p className="description">{g.description}</p>}

      {g.video && (
        <section className="block">
          <h2>Видео</h2>
          <div className="video">
            <iframe src={g.video.embed_url} title={g.video.title ?? "Видео"} allowFullScreen />
          </div>
          {g.video.title && <p className="muted small">{g.video.title}</p>}
        </section>
      )}

      <AnalysisBlock g={g} />
      <DeepBlock g={g} />
      <ReviewsBlock g={g} />

      <SimilarBlock g={g} />

      <p className="muted small mono">
        Проверено {g.checked_at ? new Date(g.checked_at).toLocaleString("ru-RU") : "—"} · следующая проверка{" "}
        {g.next_refresh_at ? new Date(g.next_refresh_at).toLocaleString("ru-RU") : "не запланирована (архив)"}
      </p>
    </div>
  );
}

function List({ title, items }: { title: string; items?: string[] }) {
  if (!items?.length) return null;
  return (
    <div>
      <h3>{title}</h3>
      <ul>
        {items.map((x) => (
          <li key={x}>{x}</li>
        ))}
      </ul>
    </div>
  );
}

function AnalysisBlock({ g }: { g: GameDetail }) {
  const essence = g.analysis.essence?.payload as Essence | undefined;
  const critic = g.analysis.critic?.payload as ReviewSummary | undefined;
  const user = g.analysis.user?.payload as ReviewSummary | undefined;
  const light = g.light_essence;
  if (!essence && !critic && !user && light) {
    return (
      <section className="block analysis-block">
        <LightEssenceView e={light} />
      </section>
    );
  }
  if (!essence && !critic && !user) {
    return (
      <section className="block placeholder analysis-block">
        <h2>Суть игры и саммари отзывов</h2>
        <p>
          Здесь появятся суть игры, саммари критиков и игроков и списки «понравилось / не понравилось». Отзывы уже
          собраны ({g.stored_reviews.critic} критиков, {g.stored_reviews.user} игроков), анализ запустится после
          подключения LLM.
        </p>
      </section>
    );
  }
  return (
    <section className="block analysis-block">
      {!essence && light && <LightEssenceView e={light} />}
      {essence && (
        <>
          <h2>Суть игры</h2>
          <p>
            <b>{essence.genre}.</b> {essence.core_loop}
          </p>
          <div className="cols">
            <List title="Механики" items={essence.mechanics} />
            <List title="Особенности" items={essence.unique_features} />
          </div>
        </>
      )}
      <div className="cols">
        {[["Критики", critic] as const, ["Игроки", user] as const].map(([label, s]) =>
          s ? (
            <div key={label} className="summary">
              <h2>{label}</h2>
              <p>{s.summary}</p>
              <List title="Понравилось" items={s.liked} />
              <List title="Не понравилось" items={s.disliked} />
            </div>
          ) : null,
        )}
      </div>
    </section>
  );
}

const DEEP_STATUS: Record<string, string> = {
  queued: "в очереди",
  running: "ищем летсплей и смотрим отрезки…",
  done: "готово",
  no_video: "подходящий летсплей не найден",
  search_error: "поиск на YouTube не удался",
  mismatch: "найденное видео не похоже на эту игру",
  error: "ошибка анализа",
  failed: "анализ не удался, квота не списана — можно повторить",
  lost: "задача потерялась, квота не списана — можно повторить",
  not_configured: "не настроено",
};

function DeepBlock({ g }: { g: GameDetail }) {
  const qc = useQueryClient();
  const quota = useQuery({ queryKey: ["deep-quota"], queryFn: api.deepQuota, refetchInterval: 60_000 });
  const run = useMutation({
    mutationFn: () => api.deepAnalyze(g.slug),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["deep-quota"] });
      setTimeout(() => qc.invalidateQueries({ queryKey: ["game", g.slug] }), 1500);
    },
  });
  const status = g.deep?.status;
  const pending = status === "queued" || status === "running";
  useEffect(() => {
    if (!pending) return;
    const t = setInterval(() => qc.invalidateQueries({ queryKey: ["game", g.slug] }), 5000);
    return () => clearInterval(t);
  }, [pending, qc, g.slug]);
  // a finished analysis is shared, so re-opening it never needs quota
  const blocked = quota.data && !quota.data.open && status !== "done";
  const f = status === "done" ? g.deep?.facts : null;
  return (
    <section className="block">
      <div className="deep">
        <div>
          <h2>Deep analyze</h2>
          <p className="muted">
            Находим самый популярный летсплей на YouTube, Gemini смотрит три отрезка по ссылке и выделяет факты об игре
            с таймкодами.
          </p>
          {g.deep && (
            <p className={`deep-status s-${status}`}>
              Статус: {DEEP_STATUS[status ?? ""] ?? status}
              {g.deep.error && status !== "done" ? ` · ${g.deep.error}` : ""}
            </p>
          )}
          {quota.data && status !== "done" && (
            <p className="muted deep-quota">
              {quota.data.open
                ? `Сегодня осталось: ${quota.data.left} (общий пул ${quota.data.used} из ${quota.data.pool}). Уже готовые анализы бесплатны.`
                : `${quota.data.reason}. Обновится в ${new Date(quota.data.resets_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}.`}
            </p>
          )}
          {run.isError && <p className="error">Не удалось запустить: {run.error.message}</p>}
        </div>
        {status !== "done" && (
          <button className="btn" onClick={() => run.mutate()} disabled={run.isPending || pending || !!blocked}>
            {run.isPending ? "Запускаю…" : "Запустить Deep analyze"}
          </button>
        )}
      </div>
      {f && (
        <div className="deep-result">
          <p className="muted small">
            По видео{" "}
            <a href={f.video.url} target="_blank" rel="noreferrer">
              «{f.video.title}»
            </a>{" "}
            · {f.video.channel} · {f.video.views.toLocaleString("ru-RU")} просмотров · отрезки{" "}
            {f.clips.map((c) => `${clock(c.start_s)}–${clock(c.end_s)}`).join(", ")}
          </p>
          <p>{f.summary}</p>
          {f.facts.length > 0 && (
            <>
              <h3>Факты, которых нет в описании</h3>
              <ul className="deep-facts">
                {f.facts.map((x) => (
                  <li key={`${x.t}-${x.text}`}>
                    <a className="mono" href={x.url} target="_blank" rel="noreferrer">
                      {x.at}
                    </a>{" "}
                    {x.text}
                  </li>
                ))}
              </ul>
            </>
          )}
          <div className="cols">
            <List title="Показанные механики" items={f.mechanics_shown} />
            <List title="Технические проблемы" items={f.tech_issues} />
          </div>
          {f.has_commentary && (
            <div className="cols">
              <List title="Автору понравилось" items={f.author_liked} />
              <List title="Автору не понравилось" items={f.author_disliked} />
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function clock(s: number): string {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

function ReviewsBlock({ g }: { g: GameDetail }) {
  const [kind, setKind] = useState<"critic" | "user">("critic");
  const [platform, setPlatform] = useState("");
  const [sort, setSort] = useState("date");
  const [limit, setLimit] = useState(20);
  const params = new URLSearchParams({ kind, sort, limit: String(limit) });
  if (platform) params.set("platform", platform);
  const reviews = useQuery({
    queryKey: ["reviews", g.slug, params.toString()],
    queryFn: () => api.reviews(g.slug, params),
    placeholderData: (prev) => prev,
  });

  return (
    <section className="block">
      <div className="reviews-head">
        <h2>Отзывы</h2>
        <div className="tabs" role="tablist">
          {(["critic", "user"] as const).map((k) => (
            <button
              key={k}
              role="tab"
              aria-selected={kind === k}
              className={kind === k ? "on" : ""}
              onClick={() => {
                setKind(k);
                setLimit(20);
              }}
            >
              {k === "critic" ? `Критики · ${g.stored_reviews.critic}` : `Игроки · ${g.stored_reviews.user}`}
            </button>
          ))}
        </div>
        <select id="review-platform" aria-label="Платформа отзывов" value={platform} onChange={(e) => setPlatform(e.target.value)}>
          <option value="">Все платформы</option>
          {g.platform_scores.map((p) => (
            <option key={p.slug} value={p.name}>
              {p.name}
            </option>
          ))}
        </select>
        <select id="review-sort" aria-label="Сортировка отзывов" value={sort} onChange={(e) => setSort(e.target.value)}>
          <option value="date">Сначала новые</option>
          <option value="score">Сначала высокие оценки</option>
        </select>
      </div>
      {kind === "user" && (
        <p className="muted small">
          На Metacritic {g.user_count} оценок игроков, из них с текстом {g.stored_reviews.user}. Собираем все отзывы с
          текстом.
        </p>
      )}
      <div className="reviews">
        {reviews.data?.items.map((r) => (
          <article key={r.id} className="review">
            <Score value={r.score} kind={kind === "critic" ? "meta" : "user"} />
            <div>
              <p className="review-meta">
                <b>{r.publication ?? r.author ?? "Аноним"}</b>
                {r.publication && r.author ? ` · ${r.author}` : ""} · {r.platform} · {fmtDate(r.date)}
                {r.spoiler && <span className="pill">спойлер</span>}
              </p>
              <p>{r.quote}</p>
              {r.url && (
                <a href={r.url} target="_blank" rel="noreferrer">
                  Полная рецензия
                </a>
              )}
            </div>
          </article>
        ))}
        {reviews.data?.items.length === 0 && <p className="muted">Отзывов пока нет.</p>}
      </div>
      {reviews.data && reviews.data.items.length < reviews.data.total && (
        <button className="btn ghost more" onClick={() => setLimit((l) => l + 20)}>
          Ещё отзывы ({reviews.data.total - reviews.data.items.length})
        </button>
      )}
    </section>
  );
}

const SIGNAL_LABEL = { gameplay: "игровой процесс", tags: "теги", world: "мир" } as const;

function SimilarBlock({ g }: { g: GameDetail }) {
  const { method, items, series } = g.similar;
  return (
    <section className="block">
      <h2>Похожие игры</h2>
      <p className="muted small">
        {method === "essence"
          ? "По сути игры: игровой процесс, теги механик и мир. Под каждой игрой — что у них общего."
          : "Пока по совпадению жанра: суть этой игры ещё не разобрана."}
      </p>
      {method === "essence" && items.length > 0 && items.length < 3 && (
        <p className="muted small">Похожих игр в каталоге пока мало: показаны только действительно близкие.</p>
      )}
      <div className="similar">
        {items.map((s) => (
          <SimilarCard key={s.id} s={s} />
        ))}
        {items.length === 0 && <p className="muted">По сути игры близких в каталоге пока нет.</p>}
      </div>
      {series.length > 0 && (
        <>
          <h3>Из той же серии</h3>
          <div className="similar">
            {series.map((s) => (
              <SimilarCard key={s.id} s={s} />
            ))}
          </div>
        </>
      )}
    </section>
  );
}

function SimilarCard({ s }: { s: SimilarItem }) {
  const top = s.reasons
    ? (Object.entries(s.reasons.signals) as [keyof typeof SIGNAL_LABEL, number][]).sort((a, b) => b[1] - a[1])[0]
    : null;
  return (
    <Link to={`/game/${s.slug}`} className="similar-item">
      <Cover src={s.cover_url} />
      <span>{s.title}</span>
      <Score value={s.metascore} kind="meta" />
      {s.reasons && (
        <span className="similar-why">
          {s.reasons.tags.length > 0 ? s.reasons.tags.map(tagLabel).join(" · ") : null}
          {s.reasons.tags.length === 0 && top ? `ближе всего: ${SIGNAL_LABEL[top[0]]}` : null}
        </span>
      )}
    </Link>
  );
}

function LightEssenceView({ e }: { e: LightEssence }) {
  const tags = [...e.mechanics, ...e.perspective, ...e.modes, ...e.setting, ...e.tone];
  return (
    <>
      <h2>Суть игры</h2>
      <p className="muted small">По описанию в магазине: отзывов для полного разбора пока мало.</p>
      <p>
        <b>{e.genre}.</b> {e.gameplay}
      </p>
      <p>{e.world}</p>
      <div className="pills">
        {tags.map((t) => (
          <span key={t} className="pill">
            {tagLabel(t)}
          </span>
        ))}
      </div>
    </>
  );
}

