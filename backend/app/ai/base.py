"""Provider-agnostic LLM interface. Gemini and Claude implementations land in the next stage,
after a blind comparison on 20 real games picks the default model."""

from dataclasses import dataclass
from typing import Protocol


@dataclass
class LLMResult:
    data: dict
    model: str
    tokens_in: int
    tokens_out: int  # visible output plus thinking tokens: both are billed as output
    cost_usd: float
    latency_s: float = 0.0
    thinking_tokens: int = 0


class LLMProvider(Protocol):
    name: str

    async def generate_json(self, *, system: str, prompt: str, schema: dict, model: str | None = None,
                            thinking_level: str | None = None) -> LLMResult:
        """Return a response validated against `schema`."""
        ...


_STR_LIST = {"type": "array", "items": {"type": "string"}}


def _top(n: int) -> dict:
    """A list capped at n items: a rolling summary must replace weaker points, not keep appending."""
    return {"type": "array", "maxItems": n, "items": {"type": "string"}}


# Limits sized to the card in the UI and checked against Gemini (it enforces maxItems).
SUMMARY_LIMITS = {"liked": 6, "disliked": 6, "notable_quotes": 4, "tech_issues": 4}

REVIEW_SUMMARY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "consensus", "liked", "disliked", "notable_quotes"],
    "properties": {
        "summary": {"type": "string"},
        "consensus": {"type": "string", "enum": ["positive", "mixed", "negative"]},
        **{field: _top(n) for field, n in SUMMARY_LIMITS.items()},
    },
}

ESSENCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["genre", "core_loop", "mechanics", "setting", "unique_features", "comparable_to"],
    "properties": {
        "genre": {"type": "string"},
        "core_loop": {"type": "string"},
        "mechanics": _STR_LIST,
        "setting": {"type": "string"},
        "unique_features": _STR_LIST,
        "comparable_to": _STR_LIST,
    },
}
