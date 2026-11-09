"""Several Gemini keys: a daily limit on one moves the call to the next; all exhausted fails at once."""

import asyncio
from types import SimpleNamespace

import pytest
from google.genai import errors

from app.ai.gemini import GeminiProvider, QuotaExhausted
from app.core import faults
from app.core.config import Settings


def answer() -> SimpleNamespace:
    usage = SimpleNamespace(thoughts_token_count=0, prompt_token_count=10, candidates_token_count=5)
    return SimpleNamespace(text='{"ok": true}', candidates=[], usage_metadata=usage)


def fake_client(log: list, label: str, fail: str | None):
    async def generate_content(**_):
        log.append(label)
        if fail:
            raise faults.gemini_failure(fail)
        return answer()

    return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))


def provider(fails: list[str | None], log: list) -> GeminiProvider:
    p = GeminiProvider([f"k{i}" for i in range(len(fails))], "m", max_attempts=3)
    for i, (k, fail) in enumerate(zip(p._keys, fails, strict=True)):
        k.client = fake_client(log, f"key{i + 1}", fail)
    return p


def call(p: GeminiProvider):
    return asyncio.run(p.generate_json(system="s", prompt="p", schema={"type": "object"}))


def test_daily_limit_moves_to_next_key_and_rests_the_spent_one():
    log: list = []
    p = provider(["daily", None], log)
    p._turn = len(p._keys) - 1  # the next pick is key1
    assert call(p).data == {"ok": True}
    assert log == ["key1", "key2"]
    log.clear()
    call(p)
    assert log == ["key2"]  # key1 rests for an hour


def test_all_keys_exhausted_fails_without_waiting():
    log: list = []
    p = provider(["daily", "daily"], log)
    with pytest.raises(errors.ClientError):  # the last key's own answer, so the caller sees why
        call(p)
    assert sorted(log) == ["key1", "key2"]
    with pytest.raises(QuotaExhausted):  # every key rests: no call is made at all
        call(p)
    assert len(log) == 2


def test_settings_collect_keys_once_and_scale_budgets():
    s = Settings(gemini_api_key="a", gemini_api_keys="b, a,,c")
    assert s.gemini_keys == ["a", "b", "c"]
    assert s.llm_daily_budget == 3 * s.llm_daily_tokens
    assert Settings(gemini_api_key=None, gemini_api_keys="").llm_daily_budget == s.llm_daily_tokens



def test_a_key_refused_with_403_is_set_aside_and_the_next_key_answers():
    """Google closed the free tier for one key's project (November 2026): it answered 403 to every call it got."""
    log: list = []
    p = provider([None, None], log)

    async def refused(**_):
        log.append("key1")
        raise errors.ClientError(403, {"error": {"code": 403, "status": "PERMISSION_DENIED",
                                                 "message": "The Gemini API free tier is not available for this project."}})

    p._keys[0].client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=refused)))
    for _ in range(3):
        assert call(p).data == {"ok": True}
    assert log.count("key1") == 1 and log.count("key2") == 3  # asked once, then set aside; nothing failed
    assert "key1" in p.refused and "free tier" in p.refused["key1"][1]


def test_every_key_refused_fails():
    log: list = []
    p = provider([None], log)

    async def refused(**_):
        raise errors.ClientError(403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "no"}})

    p._keys[0].client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=refused)))
    with pytest.raises(errors.ClientError):
        call(p)
