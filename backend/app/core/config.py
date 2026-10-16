from datetime import datetime
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All tunables live here; override any of them with a GA_* environment variable."""

    # backend/.env, wherever the process is started from
    model_config = SettingsConfigDict(env_file=Path(__file__).parents[2] / ".env", env_prefix="GA_", extra="ignore")

    database_url: str = "postgresql+asyncpg://ga:ga@localhost:5432/gameanalytics"
    redis_url: str = "redis://localhost:6379/0"

    # Metacritic
    mc_backend: str = "https://backend.metacritic.com"
    mc_image_base: str = "https://www.metacritic.com/a/img/catalog"
    user_agent: str = "GameAnalytics/0.1 (research crawler)"
    rate_limit_rps: float = 3.0
    max_concurrency: int = 6
    http_timeout_s: float = 60.0
    max_attempts: int = 4
    finder_page_size: int = 50  # API rejects anything above 50
    user_reviews_page_size: int = 500

    # Discovery: windows by release date, each enumerated completely and checked against the API total
    bootstrap_days: int = 30
    discovery_window_days: int = 3  # hourly: today and the two days before; upcoming games are out of scope
    sweep_days: int = 14  # daily: late additions with an older release date

    # Refresh tiers (days since release)
    hot_days: int = 14
    warm_days: int = 45
    cool_days: int = 90
    quiet_days: int = 7  # no new reviews for this long -> archive after cool_days
    dormant_days: int = 30  # no reviews at all for this long -> archive
    spike_ratio: float = 0.10  # +10% reviews in 24h -> back to hot
    spike_min_new: int = 5
    spike_hot_hours: int = 24

    # Analysis triggers count text reviews only: critic reviews and player reviews of 50+ words.
    analysis_min_critic: int = 3
    analysis_min_user: int = 5
    reanalyze_ratio: float = 0.20  # re-analyse after +20% new text reviews...
    reanalyze_min_new: int = 25  # ...or +25, whichever comes first...
    reanalyze_floor: int = 3  # ...but never for fewer than 3; and at most once per calendar day (UTC)
    llm_provider: str = "none"  # none | gemini
    llm_model: str = "gemini-3.5-flash-lite"  # picked by the blind model comparison, docs/model-comparison.html
    llm_thinking_level: str | None = "minimal"
    llm_rpm: float = 12  # free tier answers 429 to bursts; pace below its per-minute limit
    llm_concurrency: int = 2
    gemini_api_key: str | None = None

    # Similar games: light essence of every game with a store description, facet vectors, precomputed neighbours
    embed_model: str = "gemini-embedding-001"  # the model the similarity experiment was judged on
    embed_batch: int = 44  # texts per request: 22 games x 2 facets; free tier allows 100 texts a minute
    embed_daily_texts: int = 900  # free tier: 1000 texts a day per model, every text in a batch counts
    similar_min_gameplay: float = 0.2  # below it a game is not offered as similar; see app/pipeline/similarity.py
    facets_per_run: int = 150  # light essences per job: ~13 min at llm_rpm, the rest waits for the next hour

    # Deep analyze runs on the same free key: a shared daily pool, and the service's own analyses come first.
    youtube_api_key: str | None = None  # YouTube Data API v3: free, 10 000 units a day, a search costs 100
    deep_results_per_query: int = 10  # search.list results for each of the model's 3 queries
    deep_min_views: int = 1000  # below it a video is someone's upload, not the let's play people watch
    deep_clip_total_s: int = 1200  # about 20 min of the video in clips of 5..6.5 min, spread over all of it
    deep_clip_min_s: int = 300
    deep_clip_max_s: int = 390
    deep_fps: float = 0.2  # one frame every 5 s at low resolution: ~38 tokens per second of video with audio
    deep_max_video_s: int = 8 * 3600  # free tier: 8 h of YouTube a day, in case whole videos count
    deep_daily_pool: int = 5  # new deep analyses per quota day for all users together; cached ones are free
    deep_per_user: int = 2
    deep_per_ip: int = 3  # a new X-Client-Id is free to make up; an address is not
    deep_reuse_days: int = 7  # a finished analysis is served to everyone for this long
    deep_est_tokens: int = 48_000  # ~20 min of clips at ~38 tokens/s, the query call and the answer
    llm_daily_tokens: int = 1_160_000  # most the free key has served in a day without refusing: a floor, not the cap
    llm_safety: float = 0.8
    service_reserve_tokens: int = 190_000  # peak day of review analyses + light essence of ~100 new games + 25% retries
    quota_tz: str = "America/Los_Angeles"  # Google resets free quotas at midnight Pacific

    # Test stand: replayable external calls and a frozen clock (see stand/README.md)
    tape_mode: str = "live"  # live | record | replay | auto (replay what is recorded, record the rest)
    tape_dir: Path = Path(__file__).parents[2] / "tapes"
    frozen_now: datetime | None = None  # ISO 8601 with offset; None = real time
    cron_enabled: bool = True  # the stand runs cycles on command, not on the clock
    faults: str = ""  # stand only: injected failures of external services, syntax in app/core/faults.py
    fault_seed: int = 1

    @field_validator("frozen_now", "gemini_api_key", "youtube_api_key", mode="before")
    @classmethod
    def _empty_is_unset(cls, v):
        """Compose passes unset variables as empty strings: treat them as not given."""
        return None if v == "" else v

    # Event bus
    events_stream: str = "ga:events"
    events_maxlen: int = 50_000


@lru_cache
def get_settings() -> Settings:
    return Settings()
