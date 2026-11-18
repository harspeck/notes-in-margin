from __future__ import annotations

from datetime import datetime, timezone


def iso_utc(dt: datetime | None) -> str | None:
    """ISO 8601 with an explicit UTC offset. SQLite hands back naive datetimes and Postgres
    aware ones; both were written in UTC, so the two modes serialize the same."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()
