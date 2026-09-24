"""Per-robot recording level (docs/satinav-fleet-agent-phase0-v2.md §4).

Resolution order (§4.2), first set value wins:
  1. robotobjectv1.spec.telemetry_recording
  2. siteobjectv1.spec.telemetry_recording of the robot's current site
     (the robot_site_assignments row whose `valid` range contains now)
  3. settingsobjectv1.spec.telemetry_recording (the "global" row)
  4. DEFAULT_LEVEL (events_only)

`RecordingPolicy` keeps a snapshot of those sources and answers `level_for()`
synchronously from memory, so the check can run before enqueueing inside MQTT
handlers. Hosts keep it fresh from their existing NOTIFY handlers, either by
pushing the new value (`set_robot_level()` etc.) or by calling `invalidate()`,
after which the writer (or the host) reloads everything with `refresh()`.
Until the first refresh every robot resolves to the default.
"""

import dataclasses
import datetime
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from packages.events.codes import EventCode
from packages.events.schemas import RecordingLevel  # the one definition of the levels
from packages.telemetry_ingest import tables
from packages.telemetry_ingest._db import connection_scope

logger = logging.getLogger(__name__)

SPEC_FIELD = "telemetry_recording"
GLOBAL_SETTINGS_NAME = "global"  # cloud_common/objects/settings.py GLOBAL_SETTINGS_NAME
ROBOT_TABLE = "robotobjectv1"
SITE_TABLE = "siteobjectv1"
SETTINGS_TABLE = "settingsobjectv1"
ASSIGNMENTS_TABLE = "robot_site_assignments"


DEFAULT_LEVEL = RecordingLevel.EVENTS_ONLY

# Always written, at every level (§4.1, §3.3).
ALWAYS_RECORDED_CODES = frozenset({EventCode.TELEMETRY_RECORDING_CHANGED.value})


def parse_level(value: Any) -> Optional[RecordingLevel]:
    """A RecordingLevel, or None for unset/unknown values (unknown ones are logged)."""
    if value is None or value == "":
        return None
    try:
        return RecordingLevel(value)
    except ValueError:
        logger.warning("Ignoring unknown %s value %r", SPEC_FIELD, value)
        return None


def resolve(robot_level: Any = None, site_level: Any = None,
            global_level: Any = None) -> RecordingLevel:
    """§4.2 precedence: robot, then site, then global, then DEFAULT_LEVEL."""
    for value in (robot_level, site_level, global_level):
        level = parse_level(value)
        if level is not None:
            return level
    return DEFAULT_LEVEL


def allows(level: RecordingLevel, table: str, code: Optional[str] = None) -> bool:
    """§4.1 gate: may a row for `table` (and event `code`) be stored at `level`?"""
    if table == tables.LATEST_TABLE:
        return True
    if table == tables.EVENTS_TABLE:
        if code is not None and str(getattr(code, "value", code)) in ALWAYS_RECORDED_CODES:
            return True
        return level in (RecordingLevel.FULL, RecordingLevel.EVENTS_ONLY)
    if table in tables.TIMESERIES_COLUMNS:
        return level is RecordingLevel.FULL
    raise ValueError(f"unknown table {table!r}")


@dataclasses.dataclass
class PolicySources:
    """Raw per-layer values as stored (strings or None)."""
    robot_levels: Dict[str, Optional[str]] = dataclasses.field(default_factory=dict)
    site_levels: Dict[str, Optional[str]] = dataclasses.field(default_factory=dict)
    robot_sites: Dict[str, Optional[str]] = dataclasses.field(default_factory=dict)
    global_level: Optional[str] = None


Loader = Callable[[Any, datetime.datetime], Awaitable[PolicySources]]


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class RecordingPolicy:
    """Cached recording level per robot.

    `now` is only used to pick the current site assignment on `refresh()`.
    `loader(conn, now)` loads PolicySources; it defaults to `load_sources` (Postgres).
    """

    def __init__(self, loader: Optional[Loader] = None,
                 now: Callable[[], datetime.datetime] = _utcnow,
                 sources: Optional[PolicySources] = None):
        self._loader = loader or load_sources
        self._now = now
        self._sources = sources or PolicySources()
        self._cache: Dict[Optional[str], RecordingLevel] = {}
        self._stale = sources is None
        self._loaded = sources is not None
        self._generation = 0  # bumped by invalidate(); a refresh only clears what it saw

    # --- queries -------------------------------------------------------------------------
    @property
    def stale(self) -> bool:
        """True after invalidate() (and before the first load) until refresh() succeeds."""
        return self._stale

    @property
    def loaded(self) -> bool:
        return self._loaded

    def level_for(self, robot_name: Optional[str]) -> RecordingLevel:
        """Resolved level; robot_name None (fleet-level rows) uses the global layer only."""
        try:
            return self._cache[robot_name]
        except KeyError:
            pass
        src = self._sources
        if robot_name is None:
            level = resolve(None, None, src.global_level)
        else:
            site = src.robot_sites.get(robot_name)
            site_level = src.site_levels.get(site) if site is not None else None
            level = resolve(src.robot_levels.get(robot_name), site_level, src.global_level)
        self._cache[robot_name] = level
        return level

    def allows(self, table: str, robot_name: Optional[str], code: Optional[str] = None) -> bool:
        return allows(self.level_for(robot_name), table, code)

    def snapshot(self) -> Dict[str, Any]:
        """Current sources and cached levels, for debugging/health output."""
        return {
            "loaded": self._loaded, "stale": self._stale,
            "global_level": self._sources.global_level,
            "robot_levels": dict(self._sources.robot_levels),
            "site_levels": dict(self._sources.site_levels),
            "robot_sites": dict(self._sources.robot_sites),
            "resolved": {k: v.value for k, v in self._cache.items()},
        }

    # --- updates from NOTIFY handlers ----------------------------------------------------
    def invalidate(self) -> None:
        """Mark the snapshot stale; the next refresh() reloads every source.

        Levels keep resolving from the previous snapshot until then, so a reload in
        flight never flips robots back to the default.
        """
        self._stale = True
        self._generation += 1

    def set_robot_level(self, robot_name: str, value: Optional[str]) -> None:
        self._sources.robot_levels[robot_name] = value
        self._cache.clear()

    def forget_robot(self, robot_name: str) -> None:
        self._sources.robot_levels.pop(robot_name, None)
        self._sources.robot_sites.pop(robot_name, None)
        self._cache.clear()

    def set_robot_site(self, robot_name: str, site_id: Optional[str]) -> None:
        self._sources.robot_sites[robot_name] = site_id
        self._cache.clear()

    def set_site_level(self, site_id: str, value: Optional[str]) -> None:
        self._sources.site_levels[site_id] = value
        self._cache.clear()

    def set_global_level(self, value: Optional[str]) -> None:
        self._sources.global_level = value
        self._cache.clear()

    def replace_sources(self, sources: PolicySources) -> None:
        self._sources = sources
        self._cache.clear()
        self._stale = False
        self._loaded = True

    async def refresh(self, pool_or_conn: Any) -> bool:
        """Reload all sources. Never raises; returns False (and stays stale) on failure.

        An invalidate() that arrives while the load is in flight keeps the policy stale,
        so the next refresh picks that change up.
        """
        generation = self._generation
        try:
            async with connection_scope(pool_or_conn) as conn:
                sources = await self._loader(conn, self._now())
        except Exception:  # noqa: BLE001 - must never take the host down
            logger.exception("Recording policy refresh failed; keeping the previous snapshot")
            return False
        self.replace_sources(sources)
        self._stale = generation != self._generation
        return True


async def _table_exists(cursor: Any, table: str) -> bool:
    await cursor.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
    row = await cursor.fetchone()
    return bool(row and row[0])


async def load_sources(conn: Any, now: datetime.datetime) -> PolicySources:
    """Read every layer from Postgres. Missing tables (e.g. siteobjectv1 before any site
    exists) count as unset. Read-only; runs in whatever transaction state `conn` has."""
    sources = PolicySources()
    async with conn.cursor() as cursor:
        if await _table_exists(cursor, ROBOT_TABLE):
            await cursor.execute(
                f"SELECT name, spec->>'{SPEC_FIELD}' FROM {ROBOT_TABLE} "
                "WHERE lifecycle <> 'DELETED'")
            sources.robot_levels = {name: value for name, value in await cursor.fetchall()}
        if await _table_exists(cursor, SITE_TABLE):
            await cursor.execute(
                f"SELECT name, spec->>'{SPEC_FIELD}' FROM {SITE_TABLE} "
                "WHERE lifecycle <> 'DELETED'")
            sources.site_levels = {name: value for name, value in await cursor.fetchall()}
        if await _table_exists(cursor, ASSIGNMENTS_TABLE):
            await cursor.execute(
                f"SELECT robot_name, site_id FROM {ASSIGNMENTS_TABLE} WHERE valid @> %s",
                (now,))
            sources.robot_sites = {robot: site for robot, site in await cursor.fetchall()}
        if await _table_exists(cursor, SETTINGS_TABLE):
            await cursor.execute(
                f"SELECT spec->>'{SPEC_FIELD}' FROM {SETTINGS_TABLE} "
                "WHERE name = %s AND lifecycle <> 'DELETED'", (GLOBAL_SETTINGS_NAME,))
            row = await cursor.fetchone()
            sources.global_level = row[0] if row else None
    return sources

