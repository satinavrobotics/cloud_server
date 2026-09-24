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
import json
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

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
# NOTIFY channel for assignment changes (WP9). The API's PUT /api/v1/robots/{name}/site sends
# pg_notify(ASSIGNMENTS_CHANNEL, assignment_payload(robot, site)) in the transaction that
# changes the assignment, so it is delivered on commit only. robot_site_assignments is not an
# object table and has no object NOTIFY of its own, hence the dedicated channel.
ASSIGNMENTS_CHANNEL = "robot_site_assignments"


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

    def site_for(self, robot_name: Optional[str]) -> Optional[str]:
        """The robot's current site as of the last refresh (None if unassigned/unknown)."""
        if robot_name is None:
            return None
        return self._sources.robot_sites.get(robot_name)

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

    # The set_* helpers push one value straight from a NOTIFY (no database round trip).
    # They return True if the value changed. A change also bumps the generation, so a
    # refresh() already in flight (whose snapshot may predate the change) leaves the policy
    # stale and the next refresh picks the change up again; an unchanged value (e.g. a
    # status-only robot write) does not.
    def _set(self, changed: bool) -> bool:
        self._cache.clear()
        if changed:
            self._generation += 1
        return changed

    def set_robot_level(self, robot_name: str, value: Optional[str]) -> bool:
        levels = self._sources.robot_levels
        changed = robot_name not in levels or levels[robot_name] != value
        levels[robot_name] = value
        return self._set(changed)

    def forget_robot(self, robot_name: str) -> bool:
        changed = (robot_name in self._sources.robot_levels
                   or robot_name in self._sources.robot_sites)
        self._sources.robot_levels.pop(robot_name, None)
        self._sources.robot_sites.pop(robot_name, None)
        return self._set(changed)

    def set_robot_site(self, robot_name: str, site_id: Optional[str]) -> bool:
        sites = self._sources.robot_sites
        changed = robot_name not in sites or sites[robot_name] != site_id
        sites[robot_name] = site_id
        return self._set(changed)

    def set_site_level(self, site_id: str, value: Optional[str]) -> bool:
        levels = self._sources.site_levels
        changed = site_id not in levels or levels[site_id] != value
        levels[site_id] = value
        return self._set(changed)

    def forget_site(self, site_id: str) -> bool:
        changed = site_id in self._sources.site_levels
        self._sources.site_levels.pop(site_id, None)
        return self._set(changed)

    def set_global_level(self, value: Optional[str]) -> bool:
        changed = self._sources.global_level != value
        self._sources.global_level = value
        return self._set(changed)

    # --- whole objects, as the hosts' NOTIFY watchers deliver them ------------------------
    def apply_robot_object(self, robot: Any) -> bool:
        """A robotobjectv1 object (spec fields flattened, as cloud_common builds it); a
        DELETED one is forgotten. Returns True if the robot's configured level changed."""
        if _is_deleted(robot):
            return self.forget_robot(robot.name)
        return self.set_robot_level(robot.name, getattr(robot, SPEC_FIELD, None))

    def apply_settings_object(self, settings: Any) -> bool:
        """A settingsobjectv1 object. Only the global row counts; a DELETED one unsets the
        global level (as load_sources would). Returns True if the global level changed."""
        if getattr(settings, "name", None) != GLOBAL_SETTINGS_NAME:
            return False
        value = None if _is_deleted(settings) else getattr(settings, SPEC_FIELD, None)
        return self.set_global_level(value)

    def apply_site_object(self, site: Any) -> bool:
        """A siteobjectv1 object; a DELETED one is forgotten (its robots then resolve to the
        global level, as load_sources would). Returns True if the site's level changed."""
        if _is_deleted(site):
            return self.forget_site(site.name)
        return self.set_site_level(site.name, getattr(site, SPEC_FIELD, None))

    def apply_assignment_payload(self, payload: str) -> bool:
        """A NOTIFY payload from ASSIGNMENTS_CHANNEL. An unreadable one marks the policy
        stale (full reload) instead of raising. Returns True if the robot's site changed."""
        try:
            robot_name, site_id = parse_assignment_payload(payload)
        except ValueError:
            logger.warning("Unreadable %s payload %r; reloading", ASSIGNMENTS_CHANNEL, payload)
            self.invalidate()
            return False
        return self.set_robot_site(robot_name, site_id)

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


def assignment_payload(robot_name: str, site_id: Optional[str]) -> str:
    """The ASSIGNMENTS_CHANNEL payload: the robot's site from now on (None = unassigned)."""
    return json.dumps({"robot_name": robot_name, "site_id": site_id}, separators=(",", ":"))


def parse_assignment_payload(payload: str) -> Tuple[str, Optional[str]]:
    """(robot_name, site_id) from assignment_payload(); ValueError if malformed."""
    try:
        data = json.loads(payload)
        robot_name, site_id = data["robot_name"], data["site_id"]
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"bad assignment payload {payload!r}") from exc
    if not isinstance(robot_name, str) or not robot_name or \
            (site_id is not None and not isinstance(site_id, str)):
        raise ValueError(f"bad assignment payload {payload!r}")
    return robot_name, site_id


def _is_deleted(obj: Any) -> bool:
    lifecycle = getattr(obj, "lifecycle", None)
    return getattr(lifecycle, "value", lifecycle) == "DELETED"


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

