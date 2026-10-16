export type GameBrief = {
  id: number;
  slug: string;
  title: string;
  cover_url: string | null;
  release_date: string | null;
  genres: string[];
  developer: string | null;
  platforms: string[];
  metascore: number | null;
  critic_count: number;
  userscore: number | null;
  user_count: number;
  tier: string;
  analyzed: boolean;
  must_play: boolean;
  top_rank: number | null;
};

export type ReviewSummary = {
  summary: string;
  consensus: string;
  liked: string[];
  disliked: string[];
  notable_quotes: string[];
  tech_issues?: string[];
};

export type Essence = {
  genre: string;
  core_loop: string;
  mechanics: string[];
  setting: string;
  unique_features: string[];
  comparable_to: string[];
};

export type SimilarItem = GameBrief & {
  score?: number;
  reasons?: { tags: string[]; signals: Record<"gameplay" | "tags" | "world", number> };
};

export type LightEssence = {
  genre: string;
  gameplay: string;
  world: string;
  mechanics: string[];
  perspective: string[];
  modes: string[];
  setting: string[];
  tone: string[];
  series: string;
  comparable_to: string[];
  confidence: "low" | "medium" | "high";
};

export type GameDetail = GameBrief & {
  description: string | null;
  publisher: string | null;
  rating: string | null;
  video: { title: string | null; embed_url: string } | null;
  platform_scores: { name: string; slug: string; metascore: number | null; critic_count: number | null }[];
  stored_reviews: { critic: number; user: number };
  analysis: Partial<Record<"critic" | "user" | "essence", { payload: unknown; model: string; created_at: string }>>;
  similar: { method: "essence" | "genre" | "none"; items: SimilarItem[]; series: SimilarItem[] };
  light_essence: LightEssence | null;
  deep: {
    id: number;
    status: string;
    error: string | null;
    facts: DeepFacts | null;
    video_title: string | null;
    updated_at: string;
  } | null;
  checked_at: string | null;
  next_refresh_at: string | null;
  archived_at: string | null;
};

export type Review = {
  id: string;
  platform: string | null;
  author: string | null;
  publication: string | null;
  score: number | null;
  date: string | null;
  quote: string | null;
  url: string | null;
  spoiler: boolean;
};

export type PipelineEvent = {
  ts: string;
  run_id: string | null;
  stage: string;
  status: "ok" | "retry" | "error" | "skip";
  slug?: string;
  ms?: number;
  bytes?: number;
  http?: number;
  [key: string]: unknown;
};

export type Run = {
  id: string;
  kind: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  stats: Record<string, unknown>;
};

// Anonymous id for per-user limits (deep analyze); there are no accounts yet.
function clientId(): string {
  try {
    let id = localStorage.getItem("ga:client-id");
    if (!id) {
      id = crypto.randomUUID();
      localStorage.setItem("ga:client-id", id);
    }
    return id;
  } catch {
    return "";
  }
}

export type DeepFacts = {
  matches_game: boolean;
  summary: string;
  facts: { text: string; t: number; at: string; url: string }[];
  mechanics_shown: string[];
  author_liked: string[];
  author_disliked: string[];
  tech_issues: string[];
  has_commentary: boolean;
  video: { id: string; url: string; title: string; channel: string; views: number; duration_s: number };
  clips: { start_s: number; end_s: number; label: string | null }[];
};

export type DeepQuota = {
  pool: number;
  used: number;
  user_used: number;
  per_user: number;
  left: number;
  open: boolean;
  reason: string | null;
  resets_at: string;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  const id = clientId();
  if (id) headers.set("X-Client-Id", id);
  const r = await fetch(path, { ...init, headers });
  if (!r.ok) {
    const body = await r.json().catch(() => null);
    throw new Error(body?.detail ?? `Сервер ответил ${r.status}`);
  }
  return r.json();
}

export const api = {
  games: (params: URLSearchParams) => request<{ total: number; items: GameBrief[] }>(`/api/games?${params}`),
  platforms: () => request<{ name: string; games: number }[]>("/api/platforms"),
  game: (slug: string) => request<GameDetail>(`/api/games/${slug}`),
  reviews: (slug: string, params: URLSearchParams) =>
    request<{ total: number; items: Review[] }>(`/api/games/${slug}/reviews?${params}`),
  deepAnalyze: (slug: string) =>
    request<{ id: number; cached: boolean }>(`/api/games/${slug}/deep-analyze`, { method: "POST" }),
  deepQuota: () => request<DeepQuota>("/api/deep/quota"),
  runs: () => request<Run[]>("/api/pipeline/runs"),
  stats: () =>
    request<{
      tiers: Record<string, number>;
      due_next_hour: number;
      reviews: Record<string, number>;
      queued_jobs: number;
      rate_limit_rps: number;
    }>("/api/pipeline/stats"),
  startRun: (kind: string) =>
    request<{ queued: boolean }>("/api/pipeline/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind }),
    }),
};

export const fmtDate = (d: string | null) =>
  d ? new Date(d).toLocaleDateString("ru-RU", { day: "numeric", month: "short", year: "numeric" }) : "—";
