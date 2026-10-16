# MVP quality check on answers we already have

16.10.2026. No model was called: every number comes from summaries, essences, similar-games lists and deep analyses
already produced, checked against the inputs they were made from. Automatic checks: `backend/eval/quality_mvp.py`
(output in `backend/eval/results/quality-mvp/`, kept local). Hand grades by Claude against the rebuilt input.
Too few cases for firm conclusions: this is a health check of the MVP, not a benchmark.

## Review summaries

57 summaries: 26 critic and 5 player summaries made by the worker (lean first read, inputs rebuilt exactly: same
reviews as of the analysis watermark, same seeded sample, same score statistics), 24 from the model comparison
(production model, full read of 12 big games, earlier English prompt) and 6 lean/incremental from the strategy test.

| Size (texts read) | Summaries | Quotes verbatim | Broken quotes | Percents not from the data | Tone vs score: match / opposite | Hand grade |
|---|---|---|---|---|---|---|
| under 10 | 14 db + 2 | 60/61 | 0 | 1 of 11 | 13/14 · 0 | 4.25 (4 graded) |
| 10–49 | 13 db + 2 | 54/58 | 1 (old run) | 0 of 13 | 13/15 · 0 | 3.75 (4 graded) |
| 50–199 | 4 db + 12 | 51/56 | 1 (old run) | 0 of 2 | 14/16 · 0 | 4.5 (2 graded) |
| 200 and more | 10 | 35/43 | 2 (old runs) | 0 of 6 | 8/10 · 0 | lean and incremental: faithfulness 5.0, quality 4.67, 0 hallucinations (blind, strategy test) |

Unverified quotes that are not broken are trimmed or lightly edited (a changed first word, a typo fixed).

What holds:
- Facts are grounded. In the 10 summaries read by hand every point traced back to a review; details are specific
  (Norgan, Lost Rites and Relic Appraisal in Dragon's Dogma 2; the Polish audio balance in Toy Story 3).
- Statistics are used as given: 31 of 32 percentages are the ones we passed in.
- No summary turns a positive game negative or the other way round; small samples are not overstated
  ("all critics agree" never appears under 5 reviews); list limits hold in every summary the worker made.
- The production lean and incremental modes beat a full read on the biggest games (5.0 vs 4.0 faithfulness).

What breaks, most to least important:
1. **Our critic bands, not the model.** The statistics call 50–79 "mixed" and 80+ "positive"; Metacritic's green
   starts at 75. With Metascore 75–77 the model faithfully writes "mixed" (Toy Story 3, Bluey, Ratatan): 5 of the
   7 tone mismatches. Fix: bands 75/50 as on Metacritic.
2. **Surface errors of a fast model.** "Иггроки", the title "Order of the Sinking *State*", "со средним баллами";
   one in ten summaries. Fix: the title is passed and required verbatim; a cheap check flags summaries where the
   exact title or obvious typos appear wrong.
3. **Internals in the reader's text.** "в нашей выборке 96%", "в выборке игроков все 100%". Fix: a prompt rule to
   speak of player reviews, never of a sample.
4. **Quotes not quite quotes: 4 of 218 broken** (all from the earlier runs, none in the worker's summaries): a Spanish
   quote with a Russian word inside, an English–Italian mix, a splice across reviews. 14 more are trimmed or
   lightly edited. Fix without the model: snap each quote to the closest span of the review it came from, drop it
   below 85 % overlap.
5. **One invented percent**: "83 % критиков" in a players' summary (Valor Mortis) where no critic share was given.
6. **Design filed as technical issues**: 9 of 120 tech items (unclear checkpoints, dark interiors, systems hard for
   newcomers). Fix: one line in the prompt on what counts as technical.

## Similar games

344 of 425 games have lists (the other 81 have store texts under 150 characters and fall back to genre).
Every recommendation shares tags with its game; the most recommended game sits in 16 lists; 15 games (4 %) are never
recommended. Hand check of the top 5 for the 20 New Releases games: about 85 % at least partly similar, about 15 %
strong matches, in line with the blind experiment (84 % / 31 % on 20 queries).

Problems and fixes, none needs the model:
1. **Weak matches fill the list when the catalogue has no peers** (Bluey gets Gym Bros; Planet Zoo 2 gets a stock
   market game). The final score is a per-game z-score, so the best of nothing still scores ~2. The absolute centred
   gameplay cosine separates well on the blind grades: under 0.2 → 44–93 % misses and no strong match; over 0.4 →
   4–11 % misses. Fix: hide candidates under 0.2 and say "few similar games yet"; that hides 14 % of slots and
   leaves 8 games with fewer than 3.
2. **Series missed**: "Disney/Pixar Toy Story: Retro Roundup!" has no series while two other Toy Story games do, so
   they appear as similar instead of in the series block. Fix: a game whose title contains a known series name
   belongs to it.
3. **Tags missed**: Castlevania is a "метроидвания" in its own genre text but not in its tags, so Annalog, Horrorvania
   and Echo Weaver are not offered. Fix: map Metacritic genres and the genre text onto the tag vocabulary
   (Metroidvania, roguelike, visual novel, tower defense…).

## Deep analyze

Two cases, the video itself not watchable here: checked that timecodes fall inside their clips, that facts are not
in the store text, and whether reviews corroborate them.
- **Castlevania: Belmont's Curse — 4/5.** Five facts, all inside the clips; mana from attacks, tarot cards, the map
  and the grappling whip are confirmed by critics (Slant Magazine describes the whip pull). Silent video: no
  author opinion (chosen before the preference for commentary).
- **Warhammer 40,000: Boltgun 2 — 2/5.** The search took a "FULL DEMO" with 6 000 views (the game came out two days
  earlier); three shallow facts (HUD, toxic zones, floor arrows). The author's view is there and useful. Fix: skip
  demos and set a minimum of views, falling back to "not enough let's plays yet" rather than a weak video.

## Verdict for the MVP

Summaries are trustworthy on facts and proportions at every size, from 3 reviews to thousands; what users would
notice is polish (typos, "the sample", the 75 band). Similar games are useful where the catalogue has peers and
need a floor where it does not. Deep analyze works when there is a real let's play, and should refuse when there
is none. All fixes above are code and prompt changes; none needs a new model run to verify, except a small re-run of
the summary prompt on the stand's tapes.
