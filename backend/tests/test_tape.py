import pytest

from app.core import tape
from app.core.config import get_settings


@pytest.fixture
def tape_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "tape_dir", tmp_path)
    return tmp_path


def use(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(get_settings(), "tape_mode", mode)


async def test_record_then_replay_without_calling_the_service(tape_dir, monkeypatch):
    calls = []

    async def live():
        calls.append(1)
        return {"answer": 42}

    use(monkeypatch, "record")
    assert await tape.call("metacritic", {"path": "/x", "params": {"a": 1}}, live) == {"answer": 42}
    use(monkeypatch, "replay")
    assert await tape.call("metacritic", {"params": {"a": 1}, "path": "/x"}, live) == {"answer": 42}
    assert calls == [1]  # key order in the request does not matter, the second answer came from the tape


async def test_replay_miss_is_loud(tape_dir, monkeypatch):
    use(monkeypatch, "replay")

    async def live():
        raise AssertionError("replay must not call the service")

    with pytest.raises(tape.TapeMiss):
        await tape.call("youtube", {"path": "/search", "params": {"q": "never recorded"}}, live)


async def test_auto_records_only_what_is_missing_and_never_failures(tape_dir, monkeypatch):
    use(monkeypatch, "auto")
    n = {"calls": 0}

    async def live():
        n["calls"] += 1
        return n["calls"]

    async def broken():
        raise RuntimeError("503")

    assert await tape.call("gemini", {"p": 1}, live) == 1
    assert await tape.call("gemini", {"p": 1}, live) == 1
    with pytest.raises(RuntimeError):
        await tape.call("gemini", {"p": 2}, broken)
    assert await tape.call("gemini", {"p": 2}, live) == 2  # the failure was not kept


async def test_live_mode_writes_nothing(tape_dir, monkeypatch):
    use(monkeypatch, "live")

    async def live():
        return "x"

    await tape.call("metacritic", {"path": "/y"}, live)
    assert not any(tape_dir.rglob("*.gz"))
