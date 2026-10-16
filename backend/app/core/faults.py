"""Fault injection for the test stand: make external services fail on purpose, in the way they really fail.

  GA_FAULTS="metacritic:http503:0.3; metacritic@discovery.top:http503; gemini:daily:1:1; youtube:quota"
            source[@stage] : kind [: rate 0..1, default 1] [: how many times at most, default unlimited]

A fault replaces one attempt of a call before the tape is consulted, so the client's own retries, backoff and
error handling run exactly as they would against the real service, in replay mode too.

  metacritic, youtube   http<code> (http503, http429, http403...), timeout, reset, html (200 with a Cloudflare page),
                        schema (200 with JSON missing the expected fields), quota (YouTube's 403 quotaExceeded)
  gemini, gemini-embed  http429 (per-minute, retry in 20 s), daily (429, retry in 11 h), http503,
                        empty (no text in the answer), badjson (text that is not JSON)

Production never sets GA_FAULTS; with it empty this module does nothing.
"""

import json
import random
from dataclasses import dataclass
from functools import lru_cache

import httpx

from app.core.config import get_settings

CLOUDFLARE_PAGE = b"<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"


class FaultSpecError(ValueError):
    pass


@dataclass
class Fault:
    source: str
    kind: str
    rate: float = 1.0
    left: int | None = None  # None = no limit


def parse(spec: str) -> list[Fault]:
    out = []
    for item in filter(None, (x.strip() for x in (spec or "").split(";"))):
        parts = [p.strip() for p in item.split(":")]
        if len(parts) < 2:
            raise FaultSpecError(f"fault needs source:kind, got {item!r}")
        rate = float(parts[2]) if len(parts) > 2 and parts[2] else 1.0
        left = int(parts[3]) if len(parts) > 3 and parts[3] else None
        out.append(Fault(parts[0], parts[1], rate, left))
    return out


@lru_cache
def _state(spec: str, seed: int) -> tuple[list[Fault], random.Random]:
    return parse(spec), random.Random(seed)


def reset() -> None:
    """Forget counters and the random sequence; tests call it between cases."""
    _state.cache_clear()


def pick(source: str, stage: str | None = None) -> str | None:
    """The fault to inject into this attempt, if any. "metacritic@discovery.top" narrows a fault to one stage."""
    s = get_settings()
    if not s.faults:
        return None
    faults, rng = _state(s.faults, s.fault_seed)
    for f in faults:
        src, _, only = f.source.partition("@")
        if src != source or f.left == 0 or only and not (stage or "").startswith(only):
            continue
        if rng.random() < f.rate:
            if f.left is not None:
                f.left -= 1
            return f.kind
    return None


def http_response(kind: str, url: str) -> httpx.Response:
    """What an HTTP service answers when it fails this way; transport failures raise instead."""
    request = httpx.Request("GET", url)
    if kind == "timeout":
        raise httpx.ReadTimeout("injected timeout", request=request)
    if kind == "reset":
        raise httpx.RemoteProtocolError("injected: peer closed connection", request=request)
    if kind == "html":
        return httpx.Response(200, headers={"content-type": "text/html"}, content=CLOUDFLARE_PAGE, request=request)
    if kind == "schema":
        return httpx.Response(200, json={"data": {}, "meta": {"changed": True}}, request=request)
    if kind == "quota":
        body = {"error": {"code": 403, "message": "quota exceeded", "errors": [{"reason": "quotaExceeded"}]}}
        return httpx.Response(403, json=body, request=request)
    if kind.startswith("http") and kind[4:].isdigit():
        code = int(kind[4:])
        headers = {"retry-after": "2"} if code == 429 else {}
        return httpx.Response(code, headers=headers, json={"error": {"code": code}}, request=request)
    raise FaultSpecError(f"unknown HTTP fault {kind!r}")


def gemini_failure(kind: str) -> Exception:
    """The exception google-genai raises for this failure, or what our parsing raises for a broken answer."""
    from google.genai import errors

    def exhausted(delay: str) -> errors.ClientError:
        return errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota", "details": [
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]}})

    if kind == "http429":
        return exhausted("20s")
    if kind == "daily":
        return exhausted("40000s")
    if kind == "http503":
        return errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "overloaded"}})
    if kind == "empty":
        return ValueError("injected: empty response (SAFETY)")
    if kind == "badjson":
        try:
            json.loads('{"summary": "обрыв')
        except json.JSONDecodeError as e:
            return e
    raise FaultSpecError(f"unknown Gemini fault {kind!r}")
