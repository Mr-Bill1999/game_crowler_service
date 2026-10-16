"""HTML report for a model comparison run.

  .venv/bin/python -m eval.report eval/results/<run> [--conclusion conclusion.html]   rebuild from saved files
"""

import argparse
import json
import math
from html import escape
from pathlib import Path

CRIT_RU = {"faithfulness": "Точность", "coverage": "Полнота", "specificity": "Конкретика", "clarity": "Язык"}
TASK_RU = {"critic": "Критики", "user": "Игроки", "essence": "Суть игры"}

CSS = """
:root{--paper:#F3F5F9;--surface:#FFF;--ink:#131823;--muted:#586379;--line:#D7DDE8;--accent:#2C47C0;--accent-soft:#E5E9FA;
--good:#1D8A4C;--mixed:#B88A00;--bad:#C3362C;--f-display:"Unbounded","Golos Text",system-ui,sans-serif;
--f-body:"Golos Text",system-ui,-apple-system,"Segoe UI",sans-serif;--f-mono:"JetBrains Mono",ui-monospace,Menlo,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--paper:#0E121A;--surface:#161C27;
--ink:#E5E9F1;--muted:#97A1B4;--line:#283143;--accent:#8A9DFF;--accent-soft:#1D2648;--good:#43C47B;--mixed:#E3BC40;--bad:#F06A5E}}
:root[data-theme="dark"]{color-scheme:dark;--paper:#0E121A;--surface:#161C27;--ink:#E5E9F1;--muted:#97A1B4;--line:#283143;
--accent:#8A9DFF;--accent-soft:#1D2648;--good:#43C47B;--mixed:#E3BC40;--bad:#F06A5E}
*{box-sizing:border-box}body{background:var(--paper);color:var(--ink);font:15px/1.6 var(--f-body);padding-inline:20px;margin:0}
.wrap{max-width:1120px;margin:0 auto;padding-block:44px 90px;display:grid;gap:40px}
h1,h2,h3{font-family:var(--f-display);margin:0;line-height:1.2;text-wrap:balance}h1{font-size:2rem}h2{font-size:1.35rem}h3{font-size:1rem;font-weight:500}
p{margin:0;max-width:72ch}.muted{color:var(--muted)}.eyebrow{font:600 .75rem var(--f-mono);letter-spacing:.08em;text-transform:uppercase;color:var(--accent)}
section{display:grid;gap:16px;min-width:0}.mono{font-family:var(--f-mono);font-variant-numeric:tabular-nums}
.tbl{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:.88rem}th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font:600 .7rem var(--f-mono);letter-spacing:.05em;text-transform:uppercase;color:var(--muted);background:var(--paper);white-space:nowrap}
tr:last-child td{border-bottom:0}td.n{font-family:var(--f-mono);font-variant-numeric:tabular-nums;white-space:nowrap}
tr.rec td{background:var(--accent-soft)}tr.rec td:first-child{box-shadow:inset 3px 0 0 var(--accent)}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font:600 .7rem var(--f-mono);background:var(--accent);color:var(--surface)}
.q{display:inline-grid;place-items:center;min-width:2.6em;padding:2px 6px;border-radius:6px;font:600 .8rem var(--f-mono);color:#fff}
.q.g{background:var(--good)}.q.y{background:var(--mixed);color:#1a1400}.q.r{background:var(--bad)}
.fig{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:16px;overflow-x:auto}
.fig svg{display:block;width:100%;height:auto;min-width:640px;color:var(--muted)}
svg .grid{stroke:var(--line)}svg .tl{fill:var(--muted);font:11px var(--f-mono)}svg .pt{fill:var(--accent)}
svg .pt.rec{fill:var(--good)}svg .lb{fill:var(--ink);font:600 12px var(--f-body)}svg .ax{fill:var(--muted);font:12px var(--f-body)}
.conclusion{background:var(--surface);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:0 10px 10px 0;padding:18px 22px;display:grid;gap:10px}
.conclusion ul{margin:0;padding-left:1.2em;display:grid;gap:6px}
details{background:var(--surface);border:1px solid var(--line);border-radius:10px}
summary{cursor:pointer;padding:12px 16px;font-weight:600;display:flex;gap:12px;flex-wrap:wrap;align-items:center}
summary span{font-weight:400;color:var(--muted);font-family:var(--f-mono);font-size:.8rem}
.task{padding:0 16px 16px;display:grid;gap:10px}.task h3{padding-top:8px}
.outs{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:10px}
.out{border:1px solid var(--line);border-radius:8px;padding:12px;display:grid;gap:6px;align-content:start;font-size:.86rem}
.out header{display:flex;justify-content:space-between;gap:8px;align-items:center;flex-wrap:wrap}
.out b.m{font-family:var(--f-mono);font-size:.78rem}.out ul{margin:0;padding-left:1.1em}.out .lbl{font:600 .7rem var(--f-mono);color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.out .jc{color:var(--muted);font-style:italic}.out .hl{color:var(--bad)}.out.err{border-color:var(--bad)}
"""


def qclass(v: float | None) -> str:
    if v is None:
        return "r"
    return "g" if v >= 4 else "y" if v >= 3 else "r"


def q(v: float | None) -> str:
    return f'<span class="q {qclass(v)}">{"—" if v is None else f"{v:.2f}"}</span>'


def money(v: float, digits: int = 4) -> str:
    return f"${v:,.{digits}f}".replace(",", " ")


def recommend(rows: list[dict], tolerance: float = 0.25) -> dict | None:
    """Cheapest model close to the best one, among models judged on at least half as many outputs as the most."""
    most = max((r["judged"] for r in rows), default=0)
    judged = [r for r in rows if r["quality"] is not None and r["judged"] >= most / 2]
    if not judged:
        return None
    best = max(r["quality"] for r in judged)
    near = [r for r in judged if r["quality"] >= best - tolerance and r["failures"] == 0] or judged
    return min(near, key=lambda r: r["cost_per_game"])


def scatter(rows: list[dict], rec: dict | None) -> str:
    most = max((r["judged"] for r in rows), default=0)
    pts = [r for r in rows if r["quality"] is not None and r["cost_per_game"] > 0 and r["judged"] >= most / 2]
    if not pts:
        return ""
    W, H, L, R, T, B = 900, 380, 70, 30, 24, 56
    xs = [math.log10(r["cost_per_game"]) for r in pts]
    x0, x1 = math.floor(min(xs)), math.ceil(max(xs))
    if x1 == x0:
        x1 += 1
    y0 = min(2.5, math.floor(min(r["quality"] for r in pts) * 2) / 2)
    y1 = 5.0

    def X(v: float) -> float:
        return L + (math.log10(v) - x0) / (x1 - x0) * (W - L - R)

    def Y(v: float) -> float:
        return T + (y1 - v) / (y1 - y0) * (H - T - B)

    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Качество разборов по оценке судьи против стоимости анализа одной игры">']
    for e in range(x0, x1 + 1):
        for m in (1, 2, 5):
            v = m * 10 ** e
            if 10 ** x0 <= v <= 10 ** x1:
                x = X(v)
                out.append(f'<line class="grid" x1="{x:.1f}" y1="{T}" x2="{x:.1f}" y2="{H - B}"/>')
                out.append(f'<text class="tl" x="{x:.1f}" y="{H - B + 18}" text-anchor="middle">${v:g}</text>')
    yv = y0
    while yv <= y1 + 1e-9:
        y = Y(yv)
        out.append(f'<line class="grid" x1="{L}" y1="{y:.1f}" x2="{W - R}" y2="{y:.1f}"/>')
        out.append(f'<text class="tl" x="{L - 10}" y="{y + 4:.1f}" text-anchor="end">{yv:.1f}</text>')
        yv += 0.5
    out.append(f'<text class="ax" x="{(L + W - R) / 2}" y="{H - 10}" text-anchor="middle">стоимость анализа одной игры, логарифмическая шкала</text>')
    out.append(f'<text class="ax" x="16" y="{(T + H - B) / 2}" transform="rotate(-90 16 {(T + H - B) / 2})" text-anchor="middle">качество, 1–5</text>')
    for r in pts:
        x, y = X(r["cost_per_game"]), Y(r["quality"])
        cls = "pt rec" if rec and r["model"] == rec["model"] else "pt"
        anchor = "end" if x > W - 220 else "start"
        dx = -10 if anchor == "end" else 10
        out.append(f'<circle class="{cls}" cx="{x:.1f}" cy="{y:.1f}" r="7"/>')
        out.append(f'<text class="lb" x="{x + dx:.1f}" y="{y - 10:.1f}" text-anchor="{anchor}">{escape(r["model"])}</text>')
    out.append("</svg>")
    return "".join(out)


def render_output(task: str, data: dict | None, err: str | None) -> str:
    if err:
        return f'<p class="hl">Ошибка: {escape(err)}</p>'
    if not data:
        return '<p class="muted">Не запускалось: мало отзывов.</p>'
    if task == "essence":
        parts = [f"<p><b>{escape(data.get('genre', ''))}.</b> {escape(data.get('core_loop', ''))}</p>"]
        for key, title in (("mechanics", "Механики"), ("unique_features", "Особенности"), ("comparable_to", "Похожа на")):
            items = data.get(key) or []
            if items:
                parts.append(f'<span class="lbl">{title}</span><ul>' + "".join(f"<li>{escape(x)}</li>" for x in items) + "</ul>")
        return "".join(parts)
    parts = [f"<p>{escape(data.get('summary', ''))}</p>"]
    for key, title in (("liked", "Понравилось"), ("disliked", "Не понравилось"), ("tech_issues", "Технические проблемы")):
        items = data.get(key) or []
        if items:
            parts.append(f'<span class="lbl">{title}</span><ul>' + "".join(f"<li>{escape(x)}</li>" for x in items) + "</ul>")
    return "".join(parts)


def build_report(meta: dict, rows: list[dict], records: list[dict], judgements: list[dict],
                 conclusion: str | None = None) -> str:
    chosen = meta.get("recommended")
    rec = next((r for r in rows if r["model"] == chosen), None) if chosen else recommend(rows)
    rows_sorted = sorted(rows, key=lambda r: r["cost_per_game"])
    head = ("<tr><th>Модель</th><th>Размышления</th><th>Качество</th>" + "".join(f"<th>{v}</th>" for v in CRIT_RU.values())
            + "<th>Выдумки</th><th>Сбои</th><th>$ / игра</th><th>Медиана $</th><th>$ / мес</th><th>$ / мес batch</th><th>Время</th></tr>")
    body = []
    for r in rows_sorted:
        cls = ' class="rec"' if rec and r["model"] == rec["model"] else ""
        tag = ' <span class="pill">выбор</span>' if cls else ""
        body.append(
            f"<tr{cls}><td class='n'>{escape(r['model'])}{tag}</td><td class='n'>{escape(str(r['thinking']))}</td>"
            f"<td>{q(r['quality'])}</td>" + "".join(f"<td class='n'>{'—' if r[c] is None else f'{r[c]:.2f}'}</td>" for c in CRIT_RU)
            + f"<td class='n'>{r['hallucinations']}</td><td class='n'>{r['failures']}{' · квота ' + str(r['quota_skipped']) if r.get('quota_skipped') else ''}</td>"
            f"<td class='n'>{money(r['cost_per_game'])}</td><td class='n'>{money(r['cost_per_game_median'])}</td>"
            f"<td class='n'>{money(r['monthly'], 2)}</td><td class='n'>{money(r['monthly_batch'], 2)}</td>"
            f"<td class='n'>{r['latency_p50']} с</td></tr>")

    games_html = []
    by_slug = {}
    for r in records:
        by_slug.setdefault(r["slug"], []).append(r)
    jmap = {(j["slug"], j["task"]): j for j in judgements}
    for g in meta["games"]:
        tasks_html = []
        for task in ("critic", "user", "essence"):
            j = jmap.get((g["slug"], task))
            outs = []
            for r in sorted(by_slug.get(g["slug"], []), key=lambda x: x["model"]):
                t = r["tasks"][task]
                rating = (j or {}).get("ratings", {}).get(r["model"]) if j else None
                score = (sum(rating[c] for c in CRIT_RU) / 4) if rating else None
                judge_bits = ""
                if rating:
                    judge_bits = (f"<p class='jc'>{escape(rating.get('comment', ''))}</p>"
                                  + "".join(f"<p class='hl'>Выдумка: {escape(h)}</p>" for h in rating.get("hallucinations") or []))
                outs.append(f"<div class='out{' err' if t['error'] else ''}'><header><b class='m'>{escape(r['model'])}</b>"
                            f"{q(score) if rating else ''}<span class='mono muted'>{money(t['cost_usd'])} · {t['latency_s']:.1f} с</span></header>"
                            f"{render_output(task, t['data'], t['error'])}{judge_bits}</div>")
            tasks_html.append(f"<div class='task'><h3>{TASK_RU[task]}</h3><div class='outs'>{''.join(outs)}</div></div>")
        games_html.append(f"<details><summary>{escape(g['title'])}<span>{g['critic']} рецензий · {g['user']} отзывов игроков</span></summary>{''.join(tasks_html)}</details>")

    total_cost = sum(r["cost_total"] for r in rows) + meta.get("judge_cost", 0)
    concl = f"<section class='conclusion'>{conclusion}</section>" if conclusion else ""
    unavailable = (f"<p class='muted'>Недоступны по ключу: {escape(', '.join(meta['unavailable']))}.</p>"
                   if meta.get("unavailable") else "")
    return f"""<title>Модель для разборов отзывов</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Unbounded:wght@700&family=Golos+Text:wght@400;600&family=JetBrains+Mono:wght@400;600&display=swap">
<style>{CSS}</style>
<div class="wrap">
<header style="display:grid;gap:12px"><div class="eyebrow">GameAnalytics · сравнение моделей · прогон {escape(meta['run'])}</div>
<h1>Модель для разборов отзывов</h1>
<p class="muted">{len(meta['games'])} игр из базы, у каждой три задачи: разбор критиков, разбор игроков и суть игры. Ответы моделей вслепую и в случайном порядке оценивала {escape(meta['judge'])}, сверяясь с исходными отзывами. Весь прогон стоил {money(total_cost, 2)}.</p>
{unavailable}</header>
{concl}
<section><div class="eyebrow">Итог</div><h2>Качество и стоимость</h2>
<div class="tbl"><table><thead>{head}</thead><tbody>{''.join(body)}</tbody></table></div>
<p class="muted">Качество — средняя оценка судьи по четырём критериям, 1–5. «Выдумки» — утверждения, которых судья не нашёл в отзывах. $ / мес — при {meta['monthly_games']} анализах игр в месяц; batch вдвое дешевле, результат приходит в течение суток. Время — медиана полного анализа одной игры. {"«Выбор» — рекомендация, обоснование в выводе выше." if chosen else "«Выбор» — самая дешёвая модель, отстающая от лучшей не больше чем на 0,25 балла и без сбоев."} Модели, оценённые на малой части задач, в выборе не участвуют: их цена за игру посчитана по выполненным задачам и занижена.</p></section>
<section><h2>Качество против цены</h2><figure style="margin:0;display:grid;gap:8px"><div class="fig">{scatter(rows, rec)}</div>
<figcaption class="muted">Каждая точка — модель, проверенная на большей части задач. Выше и левее — лучше. Зелёная точка — выбор.</figcaption></figure></section>
<section><h2>Ответы по играм</h2><p class="muted">Названия моделей здесь открыты. Судья их не видел.</p>{''.join(games_html)}</section>
<section><h2>Как читать результат</h2><p class="muted">Судья видел только исходные отзывы и ответы под буквами; спорные утверждения проверял поиском по полному тексту отзывов. Отдельные оценки можно перепроверить глазами по разделу выше. Выборка — {len(meta['games'])} игр, разница меньше 0,2 балла лежит в пределах шума. Цены — официальный прайс Google на 15.10.2026; у gemini-3.8-flash они вдвое вырастут 01.01.2027.</p></section>
</div>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run", type=Path)
    ap.add_argument("--conclusion", type=Path)
    a = ap.parse_args()
    summary = json.loads((a.run / "summary.json").read_text())
    records = [json.loads(p.read_text()) for p in sorted((a.run / "outputs").rglob("*.json"))]
    judgements = [json.loads(p.read_text()) for p in sorted((a.run / "judgements").glob("*.json"))]
    conclusion = a.conclusion.read_text() if a.conclusion else None
    (a.run / "report.html").write_text(build_report(summary["meta"], summary["models"], records, judgements, conclusion))
    print(a.run / "report.html")


if __name__ == "__main__":
    main()
