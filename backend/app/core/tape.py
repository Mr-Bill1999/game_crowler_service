"""Record and replay of external calls: Metacritic, Gemini and YouTube.

  live    call the service (production)
  record  call the service and keep the answer
  replay  answer only from the tape; a call that was never recorded raises TapeMiss
  auto    replay what is on the tape, record the rest

Only successful answers are recorded: failures are what the stand injects on purpose, not what it replays.
A call is identified by its source and a canonical JSON of what was asked (path and params, model and prompt),
so the same question on the frozen clock finds the same answer. Tapes are gzip JSON files under tape_dir,
one per call, sharded by the first two hex digits of the key.
"""

import gzip
import hashlib
import json
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.core.config import get_settings

MODES = {"live", "record", "replay", "auto"}
stats: Counter = Counter()  # "<source>:<hit|miss|recorded|live>" for this process, reported by the stand


class TapeMiss(Exception):
    """Replay asked for a call nobody recorded: the stand's run left the recorded path."""

    def __init__(self, source: str, key: str, summary: str):
        super().__init__(f"tape miss: {source} {summary} ({key[:12]})")
        self.source, self.key = source, key


def key_of(source: str, request: Any) -> str:
    blob = json.dumps({"source": source, "request": request}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _path(root: Path, source: str, key: str) -> Path:
    return root / source / key[:2] / f"{key}.json.gz"


async def call(source: str, request: Any, live: Callable[[], Awaitable[Any]], *,
               encode: Callable[[Any], Any] = lambda x: x, decode: Callable[[Any], Any] = lambda x: x,
               summary: str = "", on_hit: Callable[[], Awaitable[None]] | None = None) -> Any:
    s = get_settings()
    mode = s.tape_mode
    if mode not in MODES:
        raise ValueError(f"GA_TAPE_MODE must be one of {sorted(MODES)}, not {mode!r}")
    if mode == "live":
        stats[f"{source}:live"] += 1
        return await live()
    key = key_of(source, request)
    path = _path(Path(s.tape_dir), source, key)
    if mode in ("replay", "auto") and path.exists():
        stats[f"{source}:hit"] += 1
        with gzip.open(path, "rt", encoding="utf-8") as f:
            result = decode(json.load(f)["response"])
        if on_hit:
            await on_hit()
        return result
    if mode == "replay":
        stats[f"{source}:miss"] += 1
        raise TapeMiss(source, key, summary)
    result = await live()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump({"source": source, "summary": summary, "request": request, "response": encode(result)},
                  f, ensure_ascii=False, default=str)
    tmp.replace(path)
    stats[f"{source}:recorded"] += 1
    return result
