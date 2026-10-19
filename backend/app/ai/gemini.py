"""Gemini provider: JSON-schema responses, retries on rate limits, token and cost accounting."""

import asyncio
import json
import random
import re
import time
from dataclasses import asdict

from google import genai
from google.genai import errors, types

from app.ai.base import LLMResult
from app.core import faults, tape

# USD per 1M tokens: standard input, standard output, batch input, batch output.
# ai.google.dev/gemini-api/docs/pricing on 15.10.2026. Output includes thinking tokens.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "gemini-2.5-flash-lite": (0.10, 0.40, 0.05, 0.20),
    "gemini-3.1-flash-lite": (0.25, 1.50, 0.125, 0.75),
    "gemini-3.5-flash-lite": (0.30, 2.50, 0.15, 1.25),
    "gemini-2.5-flash": (0.30, 2.50, 0.15, 1.25),
    "gemini-3-flash-preview": (0.50, 3.00, 0.25, 1.50),
    "gemini-3.8-flash": (0.75, 3.75, 0.375, 1.875),  # doubles on 01.01.2027
    "gemini-2.5-pro": (1.25, 10.00, 0.625, 5.00),
    "gemini-3.1-pro-preview": (2.00, 12.00, 1.00, 6.00),
    "gemini-4-argon": (2.00, 10.00, 1.00, 5.00),  # doubles on 01.12.2026
}
RETRYABLE = {429, 500, 503, 504}
MAX_WAIT_S = 180  # longer retryDelay means a daily limit: fail now, the next cycle tries again
# Gemini's response_json_schema rejects some JSON Schema keywords; ours only need the basics.
_UNSUPPORTED = {"additionalProperties"}


def retry_delay(e: Exception, attempt: int) -> float:
    """Google's 429 carries a RetryInfo delay ("retryDelay": "37s"); honour it, else back off exponentially."""
    m = re.search(r"retryDelay'?\"?:\s*'?\"?(\d+(?:\.\d+)?)s", str(e))
    backoff = min(60, 2 ** attempt) + random.uniform(0, 1)
    return max(backoff, float(m.group(1)) + 1) if m else backoff


def cost_usd(model: str, tokens_in: int, tokens_out: int, batch: bool = False) -> float:
    p = PRICES.get(model)
    if not p:
        return 0.0
    pin, pout = (p[2], p[3]) if batch else (p[0], p[1])
    return (tokens_in * pin + tokens_out * pout) / 1_000_000


def _clean_schema(schema):
    if isinstance(schema, dict):
        return {k: _clean_schema(v) for k, v in schema.items() if k not in _UNSUPPORTED}
    if isinstance(schema, list):
        return [_clean_schema(v) for v in schema]
    return schema


class _Key:
    """One API key: its client and, after a daily-limit answer, when to try it again."""

    def __init__(self, api_key: str, n: int):
        self.client = genai.Client(api_key=api_key)
        self.label = f"key{n}"  # never the key itself: labels reach logs and errors
        self.resting_until = 0.0


class QuotaExhausted(Exception):
    """Every key has answered with a daily limit; the next cycle tries again."""


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str | list[str], default_model: str = "gemini-3.1-flash-lite", max_attempts: int = 6,
                 rpm: float | None = None, concurrency: int | None = None):
        """rpm/concurrency pace calls per process; the free tier answers 429 to bursts well below its daily quota.
        Several keys are used in turn: rpm is per key, so the pace grows with the keys that still have quota,
        and a key that hits its daily limit rests for an hour while the call moves on."""
        keys = [api_key] if isinstance(api_key, str) else list(dict.fromkeys(api_key))
        if not keys:
            raise ValueError("no Gemini API key")
        self._keys = [_Key(k, i + 1) for i, k in enumerate(keys)]
        self._turn = 0
        self.client = self._keys[0].client  # model listing in eval scripts
        self.default_model = default_model
        self.max_attempts = max_attempts
        self._gate = asyncio.Semaphore(concurrency * len(keys)) if concurrency else None
        self._rpm = rpm
        self._next = 0.0
        self._lock = asyncio.Lock()

    def _active(self) -> list[_Key]:
        now = time.monotonic()
        return [k for k in self._keys if k.resting_until <= now]

    def _pick(self) -> _Key:
        active = self._active()
        if not active:
            raise QuotaExhausted(f"all {len(self._keys)} Gemini keys are over their daily limit")
        self._turn = (self._turn + 1) % len(active)
        return active[self._turn]

    def _rest(self, key: _Key) -> bool:
        """A daily limit on this key: set it aside for an hour. True when another key can take the call."""
        key.resting_until = time.monotonic() + 3600
        return bool(self._active())

    async def _pace(self) -> None:
        if not self._rpm:
            return
        async with self._lock:
            interval = 60.0 / (self._rpm * max(1, len(self._active())))
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(time.monotonic(), self._next) + interval

    async def _call(self, key: _Key, model: str, prompt: str, config):
        await self._pace()
        if self._gate is None:
            return await key.client.aio.models.generate_content(model=model, contents=prompt, config=config)
        async with self._gate:
            return await key.client.aio.models.generate_content(model=model, contents=prompt, config=config)

    async def generate_json(self, *, system: str, prompt: str | list[types.Part], schema: dict,
                            model: str | None = None, thinking_level: str | None = None,
                            max_output_tokens: int = 16384, media_resolution: str | None = None) -> LLMResult:
        """`prompt` is text, or parts mixing text and media (YouTube clips for deep analyze)."""
        model = model or self.default_model
        request = {"model": model, "system": system, "schema": schema, "thinking": thinking_level,
                   "max_output_tokens": max_output_tokens, "media_resolution": media_resolution,
                   "prompt": prompt if isinstance(prompt, str)
                   else [p.model_dump(mode="json", exclude_none=True) for p in prompt]}
        attempt = 0
        while attempt < self.max_attempts:
            attempt += 1
            key = self._pick()
            try:
                if kind := faults.pick("gemini"):
                    raise faults.gemini_failure(kind)
                return await tape.call(
                    "gemini", request,
                    lambda key=key: self._generate_once(key, system=system, prompt=prompt, schema=schema, model=model,
                                                        thinking_level=thinking_level,
                                                        max_output_tokens=max_output_tokens,
                                                        media_resolution=media_resolution),
                    encode=asdict, decode=lambda d: LLMResult(**d), summary=f"{model} {system[:60]}")
            except errors.APIError as e:
                delay = retry_delay(e, attempt)
                # a daily limit answers "retry in hours": another key takes the call, or the cycle fails now
                # rather than holding a worker slot until tomorrow
                if e.code == 429 and delay > MAX_WAIT_S and self._rest(key):
                    attempt -= 1
                    continue
                if e.code not in RETRYABLE or attempt == self.max_attempts or delay > MAX_WAIT_S:
                    raise
                await asyncio.sleep(delay)
        raise RuntimeError("unreachable")

    async def _generate_once(self, key: _Key, *, system: str, prompt: str | list[types.Part], schema: dict, model: str,
                             thinking_level: str | None, max_output_tokens: int,
                             media_resolution: str | None) -> LLMResult:
        if isinstance(prompt, list):
            prompt = types.Content(role="user", parts=prompt)
        config = types.GenerateContentConfig(
            media_resolution=f"MEDIA_RESOLUTION_{media_resolution.upper()}" if media_resolution else None,
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=_clean_schema(schema),
            max_output_tokens=max_output_tokens,
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel[thinking_level.upper()])
            if thinking_level else None,
        )
        t0 = time.perf_counter()
        r = await self._call(key, model, prompt, config)
        latency = time.perf_counter() - t0
        if not r.text:
            reason = r.candidates[0].finish_reason if r.candidates else "no candidates"
            raise ValueError(f"{model}: empty response ({reason})")
        u = r.usage_metadata
        thinking = u.thoughts_token_count or 0
        tokens_in, tokens_out = u.prompt_token_count or 0, (u.candidates_token_count or 0) + thinking
        return LLMResult(
            data=json.loads(r.text), model=model, tokens_in=tokens_in, tokens_out=tokens_out,
            cost_usd=cost_usd(model, tokens_in, tokens_out), latency_s=round(latency, 2), thinking_tokens=thinking,
        )

    async def embed(self, texts: list[str], *, model: str, dim: int) -> list[list[float]]:
        """One vector per text. Each text is its own Content: gemini-embedding-2 merges a plain list of strings into
        a single vector. Per-minute 429s are waited out; a daily one (retry in hours) is raised to the caller."""
        contents = [types.Content(parts=[types.Part(text=t)]) for t in texts]
        config = types.EmbedContentConfig(task_type="SEMANTIC_SIMILARITY", output_dimensionality=dim)

        async def once(key: _Key) -> list[list[float]]:
            r = await key.client.aio.models.embed_content(model=model, contents=contents, config=config)
            return [e.values for e in r.embeddings]

        attempt = 0
        while attempt < self.max_attempts:
            attempt += 1
            key = self._pick()
            try:
                if kind := faults.pick("gemini-embed"):
                    raise faults.gemini_failure(kind)
                return await tape.call("gemini-embed", {"model": model, "dim": dim, "texts": texts},
                                       lambda key=key: once(key), summary=f"{model} x{len(texts)}")
            except errors.APIError as e:
                delay = retry_delay(e, attempt)
                if e.code == 429 and delay > MAX_WAIT_S and self._rest(key):
                    attempt -= 1
                    continue
                if e.code not in RETRYABLE or attempt == self.max_attempts or delay > MAX_WAIT_S:
                    raise
                await asyncio.sleep(delay)
        raise RuntimeError("unreachable")
