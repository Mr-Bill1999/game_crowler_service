import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, type PipelineEvent } from "../api";

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
  { id: "ai", label: "AI-анализ", prefixes: ["ai."] },
  { id: "deep", label: "Deep analyze", prefixes: ["deep."] },
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
  const start = useMutation({
    mutationFn: api.startRun,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["runs"] }),
  });

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
          {(["hourly", "sweep", "bootstrap"] as const).map((k) => (
            <button key={k} className={`btn ${k === "hourly" ? "" : "ghost"}`} disabled={start.isPending}
                    onClick={() => start.mutate(k)}>
              {{ hourly: "Запустить цикл", sweep: "Sweep каталога", bootstrap: "Первый запуск" }[k]}
            </button>
          ))}
        </div>
      </div>
      {start.data && !start.data.queued && <p className="muted">Такой запуск уже в очереди или идёт.</p>}

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
        <div className="kpi">
          <b>
            {Object.entries(stats.data?.tiers ?? {})
              .map(([t, n]) => `${t} ${n}`)
              .join(" · ") || "—"}
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
      return `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`;
    })
    .join("  ");
}
