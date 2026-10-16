# Similar games: repeat experiment (15.10.2026)

Script: [backend/eval/similarity_test.py](../backend/eval/similarity_test.py). Production uses the mix chosen by the
ablation below — gameplay 0.5, tags 0.3, world 0.2, no Metacritic genre — plus a floor on gameplay similarity of 0.2
(see [quality-mvp.md](quality-mvp.md)).

273 games with a store description; light essence from description + genres (gemini-3.5-flash-lite, $0.16 total);
facet embeddings gemini-embedding-001, 768 dims (gemini-embedding-2 hit its free 1000 texts/day). 20 queries, one per
main genre; pooled top-5 of four variants = 228 pairs, graded blind 0-2 (2 — same form of play, 1 — partly: genre family or main mechanic, 0 — different play; setting
and tone alone never earn more than 0). Metrics: nDCG@5 on the pool.

| variant | nDCG@5 | strong@5 | relevant@5 | first hit | max k-occ | never shown |
|---|---|---|---|---|---|---|
| V0 Metacritic genre + Metascore | 0.42 | 0.11 | 0.49 | 0.45 | 26 | 37% |
| V1 whole essence, one vector | 0.81 | 0.26 | 0.84 | 0.90 | 27 | 6.6% |
| V2 gameplay, centred | 0.79 | 0.26 | 0.80 | 0.95 | 15 | 4.8% |
| V3 fusion .40/.25/.20/.15 | 0.80 | 0.31 | 0.84 | 0.85 | 13 | 2.2% |

V3 vs V0: +0.385 nDCG, bootstrap p<0.001. V3 vs V1 / V2: -0.007 / +0.012, p 0.56 / 0.38 — no difference.

Condensed-list ablation on the judged pairs: gameplay+tags+world .5/.3/.2 → nDCG 0.82, strong 0.30, max k-occ 14.
Metacritic genre as a signal lowers every mix it is added to (alone 0.52).

Takeaways: the gain comes from the essence text (short, about play, no names), not from splitting it into facets;
facets + tags keep quality and cut hubness by half (max 14 vs 27), and give "why similar" for the UI.
Limits: one grader, 20 queries, store descriptions only; embedding model not compared (001 only).
