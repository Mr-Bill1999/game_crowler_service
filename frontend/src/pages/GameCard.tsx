import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, fmtDate, type Essence, type GameDetail, type LightEssence, type ReviewSummary, type SimilarItem } from "../api";
import { tagLabel } from "../tags";
import { Score, TierPill } from "../components/Score";
import { Cover } from "../components/Cover";
import { Icon } from "../components/Icon";
import { Clamp, Fold } from "../components/Fold";
import { ProfileBlock } from "../components/ProfileBlock";

/* The game card, compact: the verdict fits the first screen (scores, platforms, the essence in a
   line), the rest is one page of folded sections with a sticky nav to jump between them. Nothing
   is dropped — long text is clamped and long lists folded, each a click away. */

const SECTIONS = [
  ["overview", "Обзор"],
  ["profile", "Профиль"],
  ["deep", "Летсплей"],
  ["reviews", "Отзывы"],
  ["similar", "Похожие"],
] as const;

export function GameCard() {
  const { slug = "" } = useParams();
  const game = useQuery({ queryKey: ["game", slug], queryFn: () => api.game(slug) });

  // The section nav sticks under the site's own sticky header, whatever its height.
  useEffect(() => {
    const bar = document.querySelector<HTMLElement>(".topbar");
    if (!bar) return;
    const set = () => document.documentElement.style.setProperty("--topbar-h", `${bar.offsetHeight}px`);
    set();
    const ro = new ResizeObserver(set);
    ro.observe(bar);
    return () => ro.disconnect();
  }, []);

  if (game.isPending) return <div className="page muted">Загружаю карточку…</div>;
  if (game.isError) return <div className="page error">Не удалось открыть игру: {game.error.message}</div>;
  const g = game.data;
  const essence = g.analysis.essence?.payload as Essence | undefined;
  const light = g.light_essence;
  const lead = essence ? { genre: essence.genre, text: essence.core_loop } : light ? { genre: light.genre, text: light.gameplay } : null;

  return (
    <div className="page card-page">
      <Link to="/games" className="back">
        <Icon name="arrow-left" /> Все игры
      </Link>
      <section className="hero">
        <Cover className="hero-cover" src={g.cover_url} alt={`Обложка ${g.title}`} />
        <div className="hero-info">
          <h1>{g.title}</h1>
          <p className="hero-meta">
            {g.developer ?? "Разработчик неизвестен"}
            {g.publisher && g.publisher !== g.developer ? ` · издатель ${g.publisher}` : ""} · {fmtDate(g.release_date)}
          </p>
          {lead && (
            <p className="hero-lead">
              <b>{lead.genre}.</b> {lead.text}
            </p>
          )}
          <div className="hero-scores">
            <div className="big-score">
              <Score value={g.metascore} kind="meta" size="l" />
              <span>Metascore<em>{g.critic_count} рецензий</em></span>
            </div>
            <div className="big-score">
              <Score value={g.userscore} kind="user" size="l" />
              <span>Userscore<em>{g.user_count} оценок</em></span>
            </div>
          </div>
          {g.platform_scores.length > 0 && (
            <ul className="platform-chips" aria-label="Оценки по платформам">
              {g.platform_scores.map((p) => (
                <li key={p.slug} title={`${p.critic_count ?? 0} рецензий`}>
                  <span>{p.name}</span>
                  <b className="mono">{p.metascore ?? "—"}</b>
                </li>
              ))}
            </ul>
          )}
          <div className="tags">
            {g.genres.map((x) => (
              <span key={x} className="pill">{x}</span>
            ))}
            {g.rating && <span className="pill">ESRB {g.rating}</span>}
          </div>
        </div>
      </section>

      <nav className="card-nav" aria-label="Разделы карточки">
        {SECTIONS.map(([id, label]) => (
          <a key={id} href={`#${id}`} onClick={(e) => { e.preventDefault(); document.getElementById(id)?.scrollIntoView({ behavior: "smooth" }); }}>
            {label}
            {id === "reviews" && <sup>{g.stored_reviews.critic + g.stored_reviews.user}</sup>}
          </a>
        ))}
      </nav>

      <Overview g={g} />
      <ProfileBlock g={g} />
      <DeepBlock g={g} />
      <ReviewsBlock g={g} />
      <SimilarBlock g={g} />

      <p className="card-foot muted small mono">
        <TierPill tier={g.tier} /> Проверено {g.checked_at ? new Date(g.checked_at).toLocaleString("ru-RU") : "—"} · следующая проверка{" "}
        {g.next_refresh_at ? new Date(g.next_refresh_at).toLocaleString("ru-RU") : "не запланирована (архив)"}
      </p>
    </div>
  );
}

type Said = { text: string; who: "К" | "И" };
/** Critics' and players' points, taken in turn so both voices show in the first few. */
const interleave = (a: string[] = [], b: string[] = []): Said[] => {
  const out: Said[] = [];
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    if (a[i]) out.push({ text: a[i], who: "К" });
    if (b[i]) out.push({ text: b[i], who: "И" });
  }
  return out;
};
const saidItem = (s: Said, i: number) => (
  <li key={i}><span className={`who who-${s.who === "К" ? "c" : "u"}`} title={s.who === "К" ? "критики" : "игроки"}>{s.who}</span>{s.text}</li>
);

function Overview({ g }: { g: GameDetail }) {
  const essence = g.analysis.essence?.payload as Essence | undefined;
  const critic = g.analysis.critic?.payload as ReviewSummary | undefined;
  const user = g.analysis.user?.payload as ReviewSummary | undefined;
  const light = g.light_essence;
  const [video, setVideo] = useState(false);
  const liked = interleave(critic?.liked, user?.liked);
  const disliked = interleave(critic?.disliked, user?.disliked);

  return (
    <section id="overview" className="block">
      <h2>Обзор</h2>

      {(liked.length > 0 || disliked.length > 0) && (
        <div className="cols proscons">
          <div>
            <h3>Хвалят</h3>
            <Fold items={liked} n={4} render={saidItem} className="said said-pro" />
          </div>
          <div>
            <h3>Ругают</h3>
            <Fold items={disliked} n={4} render={saidItem} className="said said-con" />
          </div>
        </div>
      )}

      {(critic || user) && (
        <div className="cols voices">
          {([["Критики", critic], ["Игроки", user]] as const).map(([label, s]) =>
            s ? (
              <div key={label}>
                <h3>{label}</h3>
                <Clamp lines={3}><p>{s.summary}</p></Clamp>
              </div>
            ) : null,
          )}
        </div>
      )}

      {essence && (
        <div className="essence">
          <h3>Механики и особенности</h3>
          <div className="pills">{essence.mechanics.map((m) => <span key={m} className="pill">{m}</span>)}</div>
          <Fold items={essence.unique_features} n={2} render={(x) => <li key={x}>{x}</li>} />
        </div>
      )}
      {!essence && light && <LightEssenceView e={light} />}
      {!essence && !critic && !user && !light && (
        <p className="muted small">
          Суть игры и саммари отзывов появятся после разбора: собрано {g.stored_reviews.critic} рецензий критиков и {g.stored_reviews.user} отзывов игроков.
        </p>
      )}

      {g.description && (
        <div>
          <h3>Описание из магазина</h3>
          <Clamp lines={3} className="description"><p>{g.description}</p></Clamp>
        </div>
      )}

      {g.video && (
        video ? (
          <div className="video">
            <iframe src={`${g.video.embed_url}${g.video.embed_url.includes("?") ? "&" : "?"}autoplay=1`} title={g.video.title ? decode(g.video.title) : "Видео"} allow="autoplay; fullscreen" allowFullScreen />
          </div>
        ) : (
          <button type="button" className="video-open" onClick={() => setVideo(true)}>
            <Icon name="play" />
            <span>Трейлер{g.video.title ? <small>{decode(g.video.title)}</small> : null}</span>
          </button>
        )
      )}
    </section>
  );
}

/** Video titles arrive with HTML entities (&#8211;, &amp;); the browser turns them back into text. */
function decode(text: string) {
  const el = document.createElement("textarea");
  el.innerHTML = text;
  return el.value;
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
    <section id="deep" className="block">
      <div className="deep">
        <div>
          <h2>Летсплей</h2>
          <p className="muted small">
            {f ? (
              <>
                По видео <a href={f.video.url} target="_blank" rel="noreferrer">«{f.video.title}»</a> · {f.video.channel} ·{" "}
                {f.video.views.toLocaleString("ru-RU")} просмотров · отрезки {f.clips.map((c) => `${clock(c.start_s)}–${clock(c.end_s)}`).join(", ")}
              </>
            ) : (
              "Gemini смотрит популярный летсплей на YouTube и выписывает факты об игре с таймкодами."
            )}
          </p>
          {g.deep && status !== "done" && (
            <p className={`deep-status s-${status}`}>
              {DEEP_STATUS[status ?? ""] ?? status}
              {g.deep.error ? ` · ${g.deep.error}` : ""}
            </p>
          )}
          {quota.data && status !== "done" && (
            <p className="muted small">
              {quota.data.open
                ? `Сегодня осталось ${quota.data.left} из ${quota.data.pool}; готовые разборы бесплатны.`
                : `${quota.data.reason}. Обновится в ${new Date(quota.data.resets_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}.`}
            </p>
          )}
          {run.isError && <p className="error">Не удалось запустить: {run.error.message}</p>}
        </div>
        {status !== "done" && (
          <button className="btn" onClick={() => run.mutate()} disabled={run.isPending || pending || !!blocked}>
            {run.isPending ? "Запускаю…" : "Разобрать летсплей"}
          </button>
        )}
      </div>
      {f && (
        <div className="deep-result">
          <Clamp lines={3}><p>{f.summary}</p></Clamp>
          {f.facts.length > 0 && (
            <div>
              <h3>Факты, которых нет в описании</h3>
              <Fold
                items={f.facts}
                n={4}
                className="deep-facts"
                render={(x) => (
                  <li key={`${x.t}-${x.text}`}>
                    <a className="mono" href={x.url} target="_blank" rel="noreferrer">{x.at}</a> {x.text}
                  </li>
                )}
              />
            </div>
          )}
          {(f.mechanics_shown.length > 0 || f.tech_issues.length > 0 || f.has_commentary) && (
            <details className="more">
              <summary>Механики, техника и мнение автора</summary>
              <div className="cols">
                <List title="Показанные механики" items={f.mechanics_shown} />
                <List title="Технические проблемы" items={f.tech_issues} />
                {f.has_commentary && <List title="Автору понравилось" items={f.author_liked} />}
                {f.has_commentary && <List title="Автору не понравилось" items={f.author_disliked} />}
              </div>
            </details>
          )}
        </div>
      )}
    </section>
  );
}

function List({ title, items }: { title: string; items?: string[] }) {
  if (!items?.length) return null;
  return (
    <div>
      <h3>{title}</h3>
      <ul>{items.map((x) => <li key={x}>{x}</li>)}</ul>
    </div>
  );
}

function clock(s: number): string {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

const FIRST_REVIEWS = 5;

function ReviewsBlock({ g }: { g: GameDetail }) {
  const [kind, setKind] = useState<"critic" | "user">("critic");
  const [platform, setPlatform] = useState("");
  const [sort, setSort] = useState("date");
  const [limit, setLimit] = useState(FIRST_REVIEWS);
  const params = new URLSearchParams({ kind, sort, limit: String(limit) });
  if (platform) params.set("platform", platform);
  const reviews = useQuery({
    queryKey: ["reviews", g.slug, params.toString()],
    queryFn: () => api.reviews(g.slug, params),
    placeholderData: (prev) => prev,
  });

  return (
    <section id="reviews" className="block">
      <div className="reviews-head">
        <h2>Отзывы</h2>
        <div className="tabs" role="tablist">
          {(["critic", "user"] as const).map((k) => (
            <button
              key={k}
              role="tab"
              aria-selected={kind === k}
              className={kind === k ? "on" : ""}
              title={k === "user" ? `На Metacritic ${g.user_count} оценок игроков, с текстом — ${g.stored_reviews.user}` : undefined}
              onClick={() => { setKind(k); setLimit(FIRST_REVIEWS); }}
            >
              {k === "critic" ? `Критики · ${g.stored_reviews.critic}` : `Игроки · ${g.stored_reviews.user}`}
            </button>
          ))}
        </div>
        <select aria-label="Платформа отзывов" value={platform} onChange={(e) => setPlatform(e.target.value)}>
          <option value="">Все платформы</option>
          {g.platform_scores.map((p) => <option key={p.slug} value={p.name}>{p.name}</option>)}
        </select>
        <select aria-label="Сортировка отзывов" value={sort} onChange={(e) => setSort(e.target.value)}>
          <option value="date">Сначала новые</option>
          <option value="score">Сначала высокие оценки</option>
        </select>
      </div>
      <div className="reviews">
        {reviews.data?.items.map((r) => (
          <article key={r.id} className="review">
            <Score value={r.score} kind={kind === "critic" ? "meta" : "user"} />
            <div>
              <p className="review-meta">
                <b>{r.publication ?? r.author ?? "Аноним"}</b>
                {r.publication && r.author ? ` · ${r.author}` : ""} · {r.platform} · {fmtDate(r.date)}
                {r.spoiler && <span className="pill">спойлер</span>}
                {r.url && <a href={r.url} target="_blank" rel="noreferrer">полностью <Icon name="arrow-up-right" /></a>}
              </p>
              <Clamp lines={2}><p>{r.quote}</p></Clamp>
            </div>
          </article>
        ))}
        {reviews.data?.items.length === 0 && <p className="muted">Отзывов пока нет.</p>}
      </div>
      {reviews.data && reviews.data.items.length < reviews.data.total && (
        <button className="btn ghost more" onClick={() => setLimit((l) => l + 15)}>
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
    <section id="similar" className="block">
      <h2>Похожие игры</h2>
      <p className="muted small">
        {method === "essence"
          ? "По сути игры: игровой процесс, теги механик и мир. Под каждой — что общего."
          : "Пока по совпадению жанра: суть этой игры ещё не разобрана."}
      </p>
      {items.length > 0 ? (
        <div className="similar">{items.map((s) => <SimilarCard key={s.id} s={s} />)}</div>
      ) : (
        <p className="muted">По сути игры близких в каталоге пока нет.</p>
      )}
      {series.length > 0 && (
        <>
          <h3>Из той же серии</h3>
          <div className="similar">{series.map((s) => <SimilarCard key={s.id} s={s} />)}</div>
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
      <span className="similar-title">{s.title}<Score value={s.metascore} kind="meta" /></span>
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
    <div className="essence">
      <h3>Мир и теги <span className="muted">· по описанию в магазине, отзывов для полного разбора пока мало</span></h3>
      <Clamp lines={2}><p>{e.world}</p></Clamp>
      <div className="pills">{tags.map((t) => <span key={t} className="pill">{tagLabel(t)}</span>)}</div>
    </div>
  );
}
