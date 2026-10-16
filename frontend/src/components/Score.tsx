type Props = { value: number | null; kind: "meta" | "user"; size?: "s" | "l" };

/** Metacritic's own colour bands: 75+ / 50–74 / below 50 (userscore on a 0–10 scale). */
export function Score({ value, kind, size = "s" }: Props) {
  if (value == null) {
    return (
      <span className={`score tbd ${size} ${kind}`} title="Оценки пока нет" aria-label={kind === "meta" ? "Оценки критиков пока нет" : "Оценки игроков пока нет"}>
        —
      </span>
    );
  }
  const norm = kind === "meta" ? value : value * 10;
  const band = norm >= 75 ? "good" : norm >= 50 ? "mixed" : "bad";
  const label = kind === "meta" ? String(value) : value.toFixed(1);
  return (
    <span className={`score ${band} ${size} ${kind}`} title={kind === "meta" ? "Metascore" : "Userscore"} aria-label={`${kind === "meta" ? "Критики" : "Игроки"}: ${label} из ${kind === "meta" ? "100" : "10"}`}>
      {label}
    </span>
  );
}

const TIER_LABEL: Record<string, string> = {
  hot: "Раз в час",
  warm: "Раз в 6 часов",
  cool: "Раз в сутки",
  dormant: "Нет отзывов",
  archived: "Архив",
  new: "Новая",
};

export function TierPill({ tier }: { tier: string }) {
  return <span className={`pill tier-${tier}`}>{TIER_LABEL[tier] ?? tier}</span>;
}
