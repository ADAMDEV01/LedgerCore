"""Single source of "now" so tests can freeze time by patching these functions."""

from datetime import UTC, date, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def today() -> date:
    return utcnow().date()


def as_utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes; treat them as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
