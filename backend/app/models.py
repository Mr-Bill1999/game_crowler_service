import datetime as dt
from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

FACET_DIM = 768  # gemini-embedding-001 cut to 768 dims, as in the similarity experiment


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Metacritic product id
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    release_date: Mapped[date | None] = mapped_column(Date, index=True)
    genres: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    developer: Mapped[str | None] = mapped_column(String(500))
    publisher: Mapped[str | None] = mapped_column(String(500))
    rating: Mapped[str | None] = mapped_column(String(20))
    must_play: Mapped[bool] = mapped_column(Boolean, default=False)
    cover_url: Mapped[str | None] = mapped_column(String(1000))
    video_title: Mapped[str | None] = mapped_column(String(500))
    video_embed_url: Mapped[str | None] = mapped_column(String(1000))
    video_manifest_url: Mapped[str | None] = mapped_column(String(1000))

    metascore: Mapped[int | None] = mapped_column(Integer, index=True)
    critic_count: Mapped[int] = mapped_column(Integer, default=0)
    userscore: Mapped[float | None] = mapped_column(Float, index=True)
    user_count: Mapped[int] = mapped_column(Integer, default=0)

    sources: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)  # catalog / new_releases
    tier: Mapped[str] = mapped_column(String(20), default="new", index=True)
    hot_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_growth_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Review-count snapshot for spike detection, rolled over every 24h
    snapshot_count: Mapped[int] = mapped_column(Integer, default=0)
    snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Text reviews (critics + substantive player reviews) at the last analysis; drives the re-analysis trigger.
    analyzed_review_count: Mapped[int] = mapped_column(Integer, default=0)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Place in Metacritic's "New Releases" block (1–20) at the last hourly check; None when not in it.
    top_rank: Mapped[int | None] = mapped_column(Integer, index=True)
    top_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    platforms: Mapped[list["GamePlatform"]] = relationship(
        back_populates="game", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def total_reviews(self) -> int:
        return (self.critic_count or 0) + (self.user_count or 0)


class GamePlatform(Base):
    __tablename__ = "game_platforms"

    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    release_date: Mapped[date | None] = mapped_column(Date)
    metascore: Mapped[int | None] = mapped_column(Integer)
    critic_count: Mapped[int | None] = mapped_column(Integer)

    game: Mapped[Game] = relationship(back_populates="platforms")


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (Index("ix_reviews_game_kind_date", "game_id", "kind", "date"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # u:<uuid> or c:<hash>
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(10))  # critic | user
    platform: Mapped[str | None] = mapped_column(String(100))
    author: Mapped[str | None] = mapped_column(String(300))
    publication: Mapped[str | None] = mapped_column(String(300))
    score: Mapped[float | None] = mapped_column(Float)
    date: Mapped[dt.date | None] = mapped_column(Date)
    quote: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(1000))
    spoiler: Mapped[bool] = mapped_column(Boolean, default=False)
    word_count: Mapped[int | None] = mapped_column(Integer)  # player reviews under 50 words are not sent to the LLM
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # critic | user | essence
    mode: Mapped[str] = mapped_column(String(20), default="full")  # first | update | full (eval runs)
    # Newest fetched_at among the reviews this analysis has seen; the next update reads only reviews after it.
    watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    texts_seen: Mapped[int] = mapped_column(Integer, default=0)  # text reviews of this kind covered so far
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONB)
    reviews_seen: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GameFacets(Base):
    """Light essence of a game and its facet vectors: what "similar games" is computed from."""

    __tablename__ = "game_facets"

    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), primary_key=True)
    essence: Mapped[dict] = mapped_column(JSONB)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)  # "mechanics:shooting", ...
    series: Mapped[str | None] = mapped_column(String(200))
    confidence: Mapped[str | None] = mapped_column(String(10))
    source_hash: Mapped[str] = mapped_column(String(64))  # title + genres + description it was built from
    model: Mapped[str] = mapped_column(String(100))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    essence_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    embed_model: Mapped[str | None] = mapped_column(String(100))
    embed_hash: Mapped[str | None] = mapped_column(String(64))  # source_hash the vectors belong to
    gameplay_vec: Mapped[list[float] | None] = mapped_column(Vector(FACET_DIM))
    world_vec: Mapped[list[float] | None] = mapped_column(Vector(FACET_DIM))
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class SimilarGame(Base):
    """Precomputed neighbours, rebuilt for the whole catalogue at once: centring, tag weights and per-query
    z-scores all depend on every game, so one new game can reorder others' lists."""

    __tablename__ = "similar_games"

    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(10), primary_key=True)  # similar | series
    rank: Mapped[int] = mapped_column(Integer, primary_key=True)
    other_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"))
    score: Mapped[float] = mapped_column(Float)
    reasons: Mapped[dict] = mapped_column(JSONB, default=dict)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DeepAnalysis(Base):
    __tablename__ = "deep_analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    video_id: Mapped[str | None] = mapped_column(String(50))
    video_title: Mapped[str | None] = mapped_column(String(500))
    views: Mapped[int | None] = mapped_column(BigInteger)
    duration_s: Mapped[int | None] = mapped_column(Integer)  # full video: in case Google counts its 8 h/day by it
    clip_s: Mapped[int | None] = mapped_column(Integer)  # seconds actually sent to the model
    requested_by: Mapped[str | None] = mapped_column(String(64))  # anonymous client id, for the per-user cap
    requested_ip: Mapped[str | None] = mapped_column(String(64))  # the per-address cap: client ids cost nothing to forge
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    facts: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RawResponse(Base):
    """Latest raw JSON per (kind, key), so new fields can be backfilled without re-scraping."""

    __tablename__ = "raw_responses"
    __table_args__ = (UniqueConstraint("kind", "key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    key: Mapped[str] = mapped_column(String(255))
    body: Mapped[dict] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # bootstrap | hourly | sweep
    owner: Mapped[str] = mapped_column(String(20), default="worker")  # worker | cli: who may declare it lost
    status: Mapped[str] = mapped_column(String(20), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)


class TopRelease(Base):
    """Snapshot of the "New Releases" block per day (UTC): rewritten every hour, so a past day keeps its last state."""

    __tablename__ = "top_releases"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    rank: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"))
    metascore: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
