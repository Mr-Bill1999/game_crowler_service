"""Event bus: every pipeline action becomes one JSON event in a Redis Stream that DevPanel reads."""

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

log = logging.getLogger(__name__)

current_run_id: ContextVar[str | None] = ContextVar("current_run_id", default=None)


class EventBus:
    def __init__(self, redis: Redis, stream: str, maxlen: int):
        self.redis = redis
        self.stream = stream
        self.maxlen = maxlen

    async def emit(self, stage: str, status: str = "ok", **fields: Any) -> None:
        event = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "run_id": fields.pop("run_id", None) or current_run_id.get(),
            "stage": stage,
            "status": status,
            **{k: v for k, v in fields.items() if v is not None},
        }
        try:
            await self.redis.xadd(
                self.stream, {"data": json.dumps(event, ensure_ascii=False, default=str)},
                maxlen=self.maxlen, approximate=True,
            )
        except Exception:  # the pipeline must never fail because DevPanel is unavailable
            log.exception("event emit failed: %s", event)
        log.info("%s %s %s", stage, status, {k: v for k, v in event.items() if k not in ("ts", "stage", "status")})
