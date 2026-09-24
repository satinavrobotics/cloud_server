"""Deterministic event IDs (docs/satinav-fleet-agent-phase0-v2.md §3.2).

event_id = uuid5(EVENT_NAMESPACE, f"{code}|{robot}|{ts_utc_us}|{discriminator}")

EVENT_NAMESPACE and the key format are part of the stored data: changing either
makes replays produce new IDs and so duplicate events.
"""

import datetime
import enum
import uuid
from typing import Optional, Union

EVENT_NAMESPACE = uuid.UUID("5b0f6c1e-3d0a-4e8e-9a51-7c2f0d6b8e14")


def normalize_ts(ts: datetime.datetime) -> datetime.datetime:
    """Return `ts` as an aware UTC datetime. Naive datetimes are taken to be UTC."""
    if not isinstance(ts, datetime.datetime):
        raise TypeError(f"ts must be a datetime, got {type(ts).__name__}")
    if ts.tzinfo is None:
        return ts.replace(tzinfo=datetime.timezone.utc)
    return ts.astimezone(datetime.timezone.utc)


def ts_key(ts: datetime.datetime) -> str:
    return normalize_ts(ts).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def event_id(code: Union[str, enum.Enum], robot_name: Optional[str],
             ts: datetime.datetime, discriminator: Optional[str] = None) -> uuid.UUID:
    code_str = code.value if isinstance(code, enum.Enum) else str(code)
    key = f"{code_str}|{robot_name or ''}|{ts_key(ts)}|{discriminator or ''}"
    return uuid.uuid5(EVENT_NAMESPACE, key)
