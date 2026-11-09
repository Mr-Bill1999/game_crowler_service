import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, type Health, type HealthSource, type HealthState, type PipelineEvent, type Presence } from "../api";

const MAX_EVENTS = 3000;

/** Pipeline graph: each node aggregates events whose stage starts with one of its prefixes. */
const NODES = [
  { id: "run", label: "Планировщик", prefixes: ["run.", "worker."] },
  { id: "discovery", label: "Discovery и топ-20", prefixes: ["discovery.", "top."] },
  { id: "card", label: "Карточки", prefixes: ["fetch.card"] },
  { id: "critic", label: "Отзывы критиков", prefixes: ["fetch.critic"] },
  { id: "user", label: "Отзывы игроков", prefixes: ["fetch.user"] },
  { id: "store", label: "Сохранение", prefixes: ["card."] },
  { id: "tier", label: "Уровни", prefixes: ["tier."] },
  { id: "ai", label: "AI-анализ", prefixes: ["ai.", "facets."] },
  { id: "profile", label: "Профили: Steam и критики", prefixes: ["profile."] },
  { id: "deep", label: "Deep analyze", prefixes: ["deep."] },
  { id: "health", label: "Здоровье источников", prefixes: ["health."] },
];

const nodeOf = (stage: string) => NODES.find((n) => n.prefixes.some((p) => stage.startsWith(p)))?.id ?? "run";

function useEventStream() {
  const [events, setEvents] = useState<PipelineEvent[]>([]);
  const [live, setLive] = useState(false);
  const buffer = useRef<PipelineEvent[]>([]);

  useEffect(() => {
    const es = new EventSource("/api/events/stream?history=1500");
    es.onopen = () => setLive(true);
    es.onerror = () => setLive(false);
    es.onmessage = (m) => buffer.current.push(JSON.parse(m.data));
    // Batch renders: events can arrive hundreds per second during a run.
    const flush = setInterval(() => {
      if (!buffer.current.length) return;
      const batch = buffer.current;
      buffer.current = [];
      setEvents((prev) => [...prev, ...batch].slice(-MAX_EVENTS));
    }, 250);
    return () => {
      es.close();
      clearInterval(flush);
    };
  }, []);
  return { events, live };
}

const RUN_LABEL = { hourly: "Запустить цикл", sweep: "Sweep каталога" } as const;
type RunKind = keyof typeof RUN_LABEL;

/** Asked on every run button: the password, and a moment to change one's mind about a misclick. */
function RunDialog({ kind, onClose, onStarted }: { kind: RunKind; onClose: () => void; onStarted: (queued: boolean) => void }) {
  const [password, setPassword] = useState("");
  const field = useRef<HTMLInputElement>(null);
  const run = useMutation({
    mutationFn: api.startRun,
    onSuccess: (r) => onStarted(r.queued),
    onError: () => { setPassword(""); field.current?.focus(); },
  });
  useEffect(() => {
    field.current?.focus();
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [onClose]);
  const error = run.error instanceof Error ? run.error.message : null;
  return (
    <div className="run-dialog-back" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <form className="run-dialog" role="dialog" aria-modal="true" aria-labelledby="run-dialog-title"
            onSubmit={(e) => { e.preventDefault(); if (password) run.mutate({ kind, password }); }}>
        <h2 id="run-dialog-title">{RUN_LABEL[kind]}</h2>
        <p className="muted">Запуск пайплайна на проде. Введите пароль администратора.</p>
        {/* A hidden user name lets password managers file and fill this password. */}
        <input type="text" name="username" autoComplete="username" value="devpanel" readOnly hidden />
        <input ref={field} className="run-dialog-field" type="password" name="password" autoComplete="current-password"
               placeholder="Пароль" value={password} onChange={(e) => setPassword(e.target.value)}
               aria-invalid={!!error} aria-describedby={error ? "run-dialog-error" : undefined} />
        {error && <p id="run-dialog-error" className="run-dialog-error" role="alert">{error}</p>}
        <div className="run-dialog-actions">
          <button type="button" className="btn ghost" onClick={onClose}>Отмена</button>
          <button type="submit" className="btn" disabled={!password || run.isPending}>{run.isPending ? "Запускаю…" : "Запустить"}</button>
        </div>
      </form>
    </div>
  );
}

const PAGE_LABEL: Record<string, string> = {
  game: "играют", welcome: "на входе", catalog: "в каталоге", card: "в карточках", devpanel: "в DevPanel", other: "где-то ещё",
};

const dayLabel = (iso: string) => new Date(`${iso}T12:00:00`).toLocaleDateString("ru-RU", { day: "numeric", month: "short" });

/** Who is on the site: online now by page, uniques today and for the week, and a bar per day. */
function Visitors({ data }: { data?: Presence }) {
  const days = data?.days ?? [];
  const peak = Math.max(1, ...days.map((d) => d.visitors));
  const pages = Object.entries(data?.pages ?? {}).sort((a, b) => (b[1] ?? 0) - (a[1] ?? 0));
  return (
    <section className="visitors" aria-label="Посетители">
      <div className="visitors-now">
        <b className={data?.online ? "is-on" : ""}>{data ? data.online : "—"}</b>
        <span>
          {data ? `онлайн сейчас` : "нет данных"}
          {pages.length > 0 && <> · {pages.map(([k, n]) => `${n} ${PAGE_LABEL[k] ?? k}`).join(" · ")}</>}
        </span>
      </div>
      <dl className="visitors-sums">
        <div><dt>сегодня</dt><dd>{data?.today ?? "—"}</dd></div>
        <div><dt>7 дней</dt><dd>{data?.week ?? "—"}</dd></div>
        <div><dt>{days.length} дней</dt><dd>{data?.period ?? "—"}</dd></div>
      </dl>
      <div className="visitors-days" role="img"
           aria-label={days.map((d) => `${dayLabel(d.date)}: ${d.visitors}`).join(", ")}>
        {days.map((d, i) => (
          <div key={d.date} className={`vday ${i === days.length - 1 ? "is-today" : ""}`}
               title={`${dayLabel(d.date)} · ${d.visitors} посетителей`}>
            <i style={{ height: `${d.visitors ? Math.max(4, (d.visitors / peak) * 100) : 0}%` }} />
          </div>
        ))}
        {days.length > 0 && <span className="vdays-axis"><span>{dayLabel(days[0].date)}</span><span>сегодня</span></span>}
      </div>
    </section>
  );
}

const STATE_LABEL: Record<HealthState, string> = {
  ok: "работает", degraded: "с перебоями", stale: "нет новых данных", quota: "квота на сегодня",
  down: "не работает", unknown: "нет данных",
};
const KIND_LABEL: Record<string, string> = {
  redirect: "переадресация", not_json: "ответ не JSON", http_4xx: "ошибка 4xx", http_429: "лимит 429",
  http_5xx: "ошибка 5xx", network: "сеть", parse: "форма ответа", quota: "квота", paused: "пауза", other: "ошибка",
};

function ago(iso?: string | null): string {
  if (!iso) return "—";
  const m = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (m < 1) return "только что";
  if (m < 60) return `${m} мин назад`;
  const h = Math.round(m / 60);
  return h < 48 ? `${h} ч назад` : `${Math.round(h / 24)} дн назад`;
}

const TIER_ORDER = ["new", "hot", "warm", "cool", "dormant", "archived"];
const SERVICE = new Set(["worker", "backup", "server"]);

/** 24 hourly slots, always all of them: an empty hour is a faint baseline, not a missing bar. */
function HourBars({ hours }: { hours: { ok: number; fail: number }[] }) {
  const slots = [...Array(Math.max(0, 24 - hours.length)).fill(null), ...hours];
  const peak = Math.max(1, ...hours.map((h) => h.ok + h.fail));
  const ok = hours.reduce((a, h) => a + h.ok, 0);
  const fail = hours.reduce((a, h) => a + h.fail, 0);
  return (
    <div className="hbars" role="img" aria-label={`за 24 часа: ${ok} успешных, ${fail} с ошибкой`}>
      {slots.map((h, i) => (
        <span key={i} className={!h || h.ok + h.fail === 0 ? "hb-none" : ""}
              title={h ? `${24 - i} ч назад: ${h.ok} успешных, ${h.fail} с ошибкой` : `${24 - i} ч назад: нет данных`}>
          {h && h.fail > 0 && <i className="fail" style={{ height: `${Math.max(8, (h.fail / peak) * 100)}%` }} />}
          {h && h.ok > 0 && <i className="ok" style={{ height: `${Math.max(8, (h.ok / peak) * 100)}%` }} />}
        </span>
      ))}
    </div>
  );
}

/** One row per outside source; a click opens the details (last error, canary, since when). */
function HealthRow({ s }: { s: HealthSource }) {
  const bad = s.state !== "ok" && s.state !== "unknown";
  return (
    <details className={`hrow is-${s.state}`} open={bad && s.state === "down"}>
      <summary>
        <span className="hdot" aria-hidden="true" />
        <b className="hname">{s.label}</b>
        <span className="hstate">{STATE_LABEL[s.state]}</span>
        <span className="hreason">{s.reason}</span>
        <HourBars hours={s.hours} />
        <span className="hlast">{s.last_ok ? `успех ${ago(s.last_ok)}` : "успехов нет"}</span>
      </summary>
      <dl className="hmore">
        {s.since && <div><dt>в этом состоянии с</dt><dd>{new Date(s.since).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</dd></div>}
        {s.canary && <div><dt>контрольный запрос</dt><dd>{s.canary.ok ? "прошёл" : "не прошёл"} {ago(s.canary.at)} · {s.canary.detail}</dd></div>}
        {s.last_error?.err_text && <div><dt>последняя ошибка</dt><dd className="herr">{ago(s.last_error.err_at)} · {KIND_LABEL[s.last_error.err_kind ?? ""] ?? "ошибка"} · {s.last_error.err_text}</dd></div>}
        {!s.since && !s.canary && !s.last_error?.err_text && <div><dt>подробностей нет</dt><dd>—</dd></div>}
      </dl>
    </details>
  );
}

function HealthBlock({ data, error }: { data?: Health; error: boolean }) {
  const problems = data?.sources.filter((s) => s.state !== "ok" && s.state !== "unknown") ?? [];
  const sources = data?.sources.filter((s) => !SERVICE.has(s.id)) ?? [];
  const service = data?.sources.filter((s) => SERVICE.has(s.id)) ?? [];
  return (
    <section className="health" aria-label="Здоровье источников">
      <div className="health-head">
        <h2>Здоровье источников</h2>
        <span className={`hsum is-${error ? "down" : data?.status ?? "unknown"}`}>
          {error ? "нет связи с API" : !data ? "загрузка…" : problems.length
            ? `проблем: ${problems.length} — ${problems.map((p) => p.label).join(", ")}`
            : "всё работает"}
        </span>
        <span className="hchecked">{data?.checked_at ? `проверено ${ago(data.checked_at)} · каждый час в :45` : ""}</span>
      </div>
      <div className="hservice">
        {service.map((s) => (
          <article key={s.id} className={`hcard is-${s.state}`}>
            <header><span className="hdot" aria-hidden="true" /><b>{s.label}</b><span className="hstate">{STATE_LABEL[s.state]}</span></header>
            <p>{s.reason}</p>
          </article>
        ))}
      </div>
      <div className="hlist">
        <div className="hlist-head" aria-hidden="true">
          <span /><span>источник</span><span>состояние</span><span>почему</span><span>вызовы за 24 ч</span><span>последний успех</span>
        </div>
        {sources.map((s) => <HealthRow key={s.id} s={s} />)}
      </div>
    </section>
  );
}

export function DevPanel() {
  const { events, live } = useEventStream();
  const [now, setNow] = useState(Date.now());
  const [node, setNode] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [slug, setSlug] = useState("");
  const qc = useQueryClient();

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const stats = useQuery({ queryKey: ["stats"], queryFn: api.stats, refetchInterval: 5000 });
  const runs = useQuery({ queryKey: ["runs"], queryFn: api.runs, refetchInterval: 5000 });
  const visitors = useQuery({ queryKey: ["presence"], queryFn: api.presence, refetchInterval: 10_000 });
  const health = useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: 60_000 });
  const start = useMutation({
    mutationFn: api.startRun,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["runs"] }),
  });
  const [asking, setAsking] = useState<RunKind | null>(null);
  const [notQueued, setNotQueued] = useState(false);
  const ask = (k: RunKind) => {
    setNotQueued(false);
    if (stats.data?.admin_required === false) start.mutate({ kind: k });  // the local stand has no password
    else setAsking(k);
  };

  const byNode = useMemo(() => {
    const acc: Record<string, { ok: number; retry: number; error: number; skip: number; ms: number[]; last: number }> = {};
    for (const n of NODES) acc[n.id] = { ok: 0, retry: 0, error: 0, skip: 0, ms: [], last: 0 };
    for (const e of events) {
      const a = acc[nodeOf(e.stage)];
      a[e.status] = (a[e.status] ?? 0) + 1;
      if (typeof e.ms === "number" && e.stage.startsWith("fetch.")) a.ms.push(e.ms);
      a.last = Math.max(a.last, Date.parse(e.ts));
    }
    return acc;
  }, [events]);

  const httpEvents = events.filter((e) => e.http != null);
  const rps = httpEvents.filter((e) => now - Date.parse(e.ts) < 10_000).length / 10;
  const lastRun = runs.data?.[0];
  const running = lastRun?.status === "running";

  const shown = events
    .filter((e) => !node || nodeOf(e.stage) === node)
    .filter((e) => !status || e.status === status)
    .filter((e) => !slug || (e.slug ?? "").includes(slug))
    .slice(-400)
    .reverse();

  return (
    <div className="page devpanel-page">
      <div className="dp-top">
        <div>
          <h1>DevPanel</h1>
          <p className="muted">Каждое действие пайплайна в реальном времени.</p>
        </div>
        <div className="dp-actions">
          {(["hourly", "sweep"] as const).map((k) => (
            <button key={k} className={`btn ${k === "hourly" ? "" : "ghost"}`} disabled={start.isPending}
                    onClick={() => ask(k)}>
              {RUN_LABEL[k]}
            </button>
          ))}
        </div>
      </div>
      {(notQueued || (start.data && !start.data.queued)) && <p className="muted">Такой запуск уже в очереди или идёт.</p>}
      {asking && <RunDialog kind={asking} onClose={() => setAsking(null)}
                            onStarted={(queued) => { setAsking(null); setNotQueued(!queued); qc.invalidateQueries({ queryKey: ["runs"] }); }} />}

      <HealthBlock data={health.data} error={health.isError} />

      <Visitors data={visitors.data} />

      <div className="kpis">
        <div className={`kpi ${live ? "live" : "down"}`}>
          <b>{live ? "● LIVE" : "○ нет связи"}</b>
          <span>поток событий</span>
        </div>
        <div className="kpi">
          <b>{rps.toFixed(1)}</b>
          <span>запросов/с · лимит {stats.data?.rate_limit_rps ?? "—"}</span>
        </div>
        <div className="kpi">
          <b>{running ? "идёт" : lastRun ? lastRun.status : "—"}</b>
          <span>{lastRun ? `${lastRun.kind} · ${new Date(lastRun.started_at).toLocaleTimeString("ru-RU")}` : "запусков не было"}</span>
        </div>
        <div className="kpi">
          <b>{stats.data?.due_next_hour ?? "—"}</b>
          <span>игр к проверке в ближайший час</span>
        </div>
        <div className="kpi tiers">
          <b>
            {Object.entries(stats.data?.tiers ?? {})
              .sort((x, y) => TIER_ORDER.indexOf(x[0]) - TIER_ORDER.indexOf(y[0]))
              .map(([t, n]) => <span key={t}>{t} <em>{n.toLocaleString("ru-RU")}</em></span>)}
            {!stats.data && "—"}
          </b>
          <span>игры по уровням</span>
        </div>
      </div>

      <div className="dp-body">
        <aside className="dp-graph" aria-label="Стадии пайплайна">
          {NODES.map((n, i) => {
            const a = byNode[n.id];
            const active = now - a.last < 3000;
            const ms = a.ms.length ? [...a.ms].sort((x, y) => x - y)[Math.floor(a.ms.length / 2)] : null;
            return (
              <div key={n.id} className="dp-node-wrap">
                {i > 0 && <span className="dp-edge" aria-hidden="true" />}
                <button
                  className={`dp-node ${active ? "active" : ""} ${a.error ? "has-error" : ""} ${node === n.id ? "selected" : ""}`}
                  onClick={() => setNode(node === n.id ? null : n.id)}
                  aria-pressed={node === n.id}
                >
                  <span className="dp-node-label">{n.label}</span>
                  <span className="dp-node-counts">
                    <span className="ok">{a.ok}</span>
                    {a.retry > 0 && <span className="retry">↻{a.retry}</span>}
                    {a.error > 0 && <span className="err">✕{a.error}</span>}
                    {a.skip > 0 && <span className="skip">–{a.skip}</span>}
                    {ms != null && <span className="ms">p50 {ms} мс</span>}
                  </span>
                </button>
              </div>
            );
          })}
          <div className="runs">
            <h3>Последние запуски</h3>
            {runs.data?.slice(0, 6).map((r) => (
              <div key={r.id} className={`run s-${r.status}`}>
                <span className="mono">{r.id}</span>
                <span>
                  {r.status}
                  {typeof r.stats.seconds === "number" ? ` · ${r.stats.seconds} с` : ""}
                  {typeof r.stats.games === "number" ? ` · ${r.stats.games} игр` : ""}
                </span>
              </div>
            ))}
          </div>
        </aside>

        <section className="dp-log-wrap">
          <div className="dp-filters">
            <select id="dp-status" value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">Все статусы</option>
              <option value="ok">ok</option>
              <option value="retry">retry</option>
              <option value="error">error</option>
              <option value="skip">skip</option>
            </select>
            <input id="dp-slug" type="search" placeholder="Фильтр по игре" value={slug}
                   onChange={(e) => setSlug(e.target.value)} />
            {node && (
              <button className="btn ghost" onClick={() => setNode(null)}>
                Сбросить стадию
              </button>
            )}
            <span className="muted small">{shown.length} из {events.length} событий</span>
          </div>
          <div className="dp-log" role="log" aria-live="off">
            {shown.length === 0 && (
              <p className="dp-empty">Событий пока нет. Запустите цикл кнопкой выше, и действия появятся здесь.</p>
            )}
            {shown.map((e, i) => (
              <div key={`${e.ts}-${i}`} className={`line st-${e.status} ${e.ms && e.ms > 3000 ? "slow" : ""}`}>
                <span className="t">{e.ts.slice(11, 23)}</span>
                <span className="stage">{e.stage}</span>
                <span className="status">{e.status}</span>
                <span className="slug">{e.slug ?? ""}</span>
                <span className="detail">{detail(e)}</span>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}

const SKIP = new Set(["ts", "stage", "status", "slug", "run_id"]);

function detail(e: PipelineEvent): string {
  return Object.entries(e)
    .filter(([k, v]) => !SKIP.has(k) && v !== null && v !== undefined)
    .map(([k, v]) => {
      if (k === "ms") return `${((v as number) / 1000).toFixed(2)}s`;
      if (k === "bytes") return `${Math.round((v as number) / 1024)}KB`;
      return `${k}=${typeof v === "object" ? compact(v) : v}`;
    })
    .join("  ");
}

/** {"mc":0,"steam_reviews":100} -> "mc 0, steam_reviews 100": nested values read as text, not as JSON. */
function compact(v: unknown): string {
  if (Array.isArray(v)) return v.map((x) => (typeof x === "object" ? compact(x) : String(x))).join(", ");
  if (v && typeof v === "object")
    return Object.entries(v as Record<string, unknown>)
      .map(([k, x]) => `${k} ${typeof x === "object" && x !== null ? `(${compact(x)})` : String(x).slice(0, 90)}`)
      .join(", ");
  return String(v);
}
