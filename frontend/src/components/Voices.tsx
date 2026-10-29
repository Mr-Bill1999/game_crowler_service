import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type GameDetail, type ReviewSummary } from "../api";

/* What critics and players say, as charts first and text second:
   - one agreement line: Metascore and Userscore on the same 0–100 track, and how far apart;
   - per audience, a share bar of positive / mixed / negative reviews (Metacritic's own bands,
     the same colours as the score badges), counted from the stored reviews;
   - the summary's first sentence as the headline, top pros and cons, the rest folded. */

type Kind = "critic" | "user";
type Bands = { good: number; mixed: number; bad: number; total: number };
const BAND_LABEL = { good: "хвалят", mixed: "смешанно", bad: "ругают" } as const;

/** Every stored review's score, a hundred at a time, sorted into Metacritic's three bands. */
async function bands(slug: string, kind: Kind, total: number): Promise<Bands> {
  const pages = Math.min(5, Math.ceil(total / 100));
  const out: Bands = { good: 0, mixed: 0, bad: 0, total: 0 };
  const lists = await Promise.all(Array.from({ length: pages }, (_, i) =>
    api.reviews(slug, new URLSearchParams({ kind, sort: "score", limit: "100", offset: String(i * 100) }))));
  for (const list of lists) {
    for (const r of list.items) {
      if (r.score == null) continue;
      const v = kind === "critic" ? r.score : r.score * 10;
      out[v >= 75 ? "good" : v >= 50 ? "mixed" : "bad"] += 1;
      out.total += 1;
    }
  }
  return out;
}

function useBands(g: GameDetail, kind: Kind) {
  const total = g.stored_reviews[kind];
  return useQuery({ queryKey: ["bands", g.slug, kind, total], queryFn: () => bands(g.slug, kind, total), enabled: total > 0, staleTime: 300_000 });
}

export function Voices({ g, critic, user }: { g: GameDetail; critic?: ReviewSummary; user?: ReviewSummary }) {
  if (!critic && !user) return null;
  return (
    <div className="voices">
      <Agreement g={g} />
      <div className="voice-grid">
        {critic && <VoiceCard g={g} kind="critic" s={critic} />}
        {user && <VoiceCard g={g} kind="user" s={user} />}
      </div>
    </div>
  );
}

/** Metascore and Userscore on one 0–100 track: where each sits and how far apart they are. */
function Agreement({ g }: { g: GameDetail }) {
  const meta = g.metascore;
  const usr = g.userscore == null ? null : Math.round(g.userscore * 10);
  if (meta == null && usr == null) return null;
  const gap = meta != null && usr != null ? Math.abs(meta - usr) : null;
  const verdict = gap == null ? null : gap <= 5 ? "критики и игроки сходятся" : gap <= 12 ? `расходятся на ${gap}` : `сильно расходятся: на ${gap}`;
  const lo = meta != null && usr != null ? Math.min(meta, usr) : null;
  const hi = meta != null && usr != null ? Math.max(meta, usr) : null;
  return (
    <figure className="agree" aria-label={`Критики ${meta ?? "—"}, игроки ${usr ?? "—"} из 100`}>
      <figcaption>
        <span>Критики и игроки</span>
        {verdict && <span className="muted">{verdict}</span>}
      </figcaption>
      <div className="agree-track">
        {[25, 50, 75].map((t) => <span key={t} className="agree-tick" style={{ left: `${t}%` }} />)}
        {lo != null && hi != null && <span className="agree-span" style={{ left: `${lo}%`, width: `${hi - lo}%` }} />}
        {meta != null && (
          <span className="agree-dot is-critic" style={{ left: `${meta}%` }} title={`Metascore ${meta}`}>
            <b className={meta >= (usr ?? -1) ? "up" : "down"}>Критики {meta}</b>
          </span>
        )}
        {usr != null && (
          <span className="agree-dot is-user" style={{ left: `${usr}%` }} title={`Userscore ${g.userscore?.toFixed(1)}`}>
            <b className={usr > (meta ?? -1) ? "up" : "down"}>Игроки {(usr / 10).toFixed(1)}</b>
          </span>
        )}
      </div>
      <div className="agree-scale muted"><span>0</span><span>50</span><span>100</span></div>
    </figure>
  );
}

function VoiceCard({ g, kind, s }: { g: GameDetail; kind: Kind; s: ReviewSummary }) {
  const b = useBands(g, kind);
  const [more, setMore] = useState(false);
  const count = kind === "critic" ? g.critic_count : g.user_count;
  // The first sentence leads; the rest of the summary waits behind the toggle.
  const cut = s.summary.search(/[.!?…](\s|$)/);
  const lead = cut > 0 ? s.summary.slice(0, cut + 1) : s.summary;
  const rest = cut > 0 ? s.summary.slice(cut + 1).trim() : "";
  return (
    <article className="voice">
      <header>
        <h3>{kind === "critic" ? "Критики" : "Игроки"}</h3>
        <span className="muted small">{count} {kind === "critic" ? "рецензий" : "оценок"}</span>
      </header>
      <ShareBar b={b.data} pending={b.isPending && g.stored_reviews[kind] > 0} kind={kind} />
      {lead && <p className="voice-lead">{lead}</p>}
      <div className="voice-points">
        <Points sign="+" items={s.liked} open={more} />
        <Points sign="−" items={s.disliked} open={more} />
      </div>
      <button type="button" className="link-btn" onClick={() => setMore(!more)}>
        {more ? "свернуть" : "все плюсы, минусы и подробности"}
      </button>
      {more && rest && <p className="voice-summary">{rest}</p>}
    </article>
  );
}

/** Positive / mixed / negative as one 100% bar, with a legend that carries the numbers. */
function ShareBar({ b, pending, kind }: { b?: Bands; pending: boolean; kind: Kind }) {
  if (!b || !b.total) {
    return <div className={`share ${pending ? "is-pending" : ""}`}><div className="share-bar"><span className="share-empty" /></div>
      <p className="share-legend muted">{pending ? "считаю отзывы…" : "отзывов с оценкой пока нет"}</p></div>;
  }
  const parts = (["good", "mixed", "bad"] as const).filter((k) => b[k] > 0);
  const pct = (n: number) => Math.round((n / b.total) * 100);
  return (
    <div className="share">
      <div className="share-bar" role="img" aria-label={parts.map((k) => `${BAND_LABEL[k]} ${pct(b[k])}%`).join(", ")}>
        {parts.map((k) => (
          <span key={k} className={`share-seg band-${k}`} style={{ flexGrow: b[k] }} title={`${BAND_LABEL[k]}: ${b[k]} из ${b.total} (${pct(b[k])}%)`} />
        ))}
      </div>
      <p className="share-legend">
        {(["good", "mixed", "bad"] as const).map((k) => (
          <span key={k}><i className={`band-${k}`} />{pct(b[k])}% {BAND_LABEL[k]}</span>
        ))}
        <span className="muted">по {b.total} {kind === "critic" ? "рецензиям" : "отзывам с текстом"}</span>
      </p>
    </div>
  );
}

function Points({ sign, items, open }: { sign: "+" | "−"; items: string[]; open: boolean }) {
  if (!items.length) return null;
  const shown = open ? items : items.slice(0, 3);
  return (
    <ul className={`points ${sign === "+" ? "is-pro" : "is-con"}`}>
      {shown.map((x) => <li key={x}><span aria-hidden="true">{sign}</span>{x}</li>)}
      {!open && items.length > 3 && <li className="points-more muted">ещё {items.length - 3}</li>}
    </ul>
  );
}
