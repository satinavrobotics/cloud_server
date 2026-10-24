"""Shared time helpers (stdlib only, safe to import from any service)."""
import datetime


def utcnow() -> datetime.datetime:
    """The current time as a timezone-aware UTC datetime."""
    return datetime.datetime.now(datetime.timezone.utc)
