"""The pipeline's notion of "now". Frozen on the test stand (GA_FROZEN_NOW) so that discovery windows, refresh tiers
and quota days ask the same questions on every run, and recorded external answers replay one to one.
Durations (how long a run took) and event timestamps stay on the real clock."""

from datetime import UTC, datetime

from app.core.config import get_settings


def now() -> datetime:
    frozen = get_settings().frozen_now
    return frozen.astimezone(UTC) if frozen else datetime.now(UTC)
