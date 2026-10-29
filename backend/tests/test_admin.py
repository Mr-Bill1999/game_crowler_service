import fakeredis
import pytest
from httpx import ASGITransport, AsyncClient

from app.api import main


class Queue:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, name, **kw):
        self.jobs.append(name)
        return object()


@pytest.fixture
async def api(monkeypatch):
    monkeypatch.setattr(main.settings, "admin_password", "s3cret-pass")
    main.app.state.redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    main.app.state.arq = Queue()
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://t") as c:
        yield c


async def run(c, password=None, ip="1.1.1.1"):
    headers = {"X-Real-IP": ip}
    if password is not None:
        headers["X-Admin-Password"] = password
    return await c.post("/api/pipeline/run", json={"kind": "sweep"}, headers=headers)


async def test_a_run_needs_the_right_password(api):
    assert (await run(api)).status_code == 401
    assert (await run(api, "nope")).json()["detail"] == "Неверный пароль"
    assert (await run(api, "s3cret-pass")).status_code == 202
    assert main.app.state.arq.jobs == ["run_sweep"]


async def test_guessing_is_paused_after_five_misses_per_address(api):
    for _ in range(main.ADMIN_TRIES):
        assert (await run(api, "guess")).status_code == 401
    assert (await run(api, "s3cret-pass")).status_code == 429  # even the right one waits out the pause
    assert (await run(api, "s3cret-pass", ip="2.2.2.2")).status_code == 202  # other addresses are not punished


async def test_without_a_configured_password_runs_stay_open(api, monkeypatch):
    monkeypatch.setattr(main.settings, "admin_password", "")
    assert (await run(api)).status_code == 202
