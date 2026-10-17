"""Per-map view settings (the client's map window): GET / PUT /api/v1/maps/{map}/view_settings.

What a map's own 3D view shows, kept per map so it survives a reload and follows the user to another
browser: the vertical range (`height_min_m`, `height_max_m`, metres above the floor; null = no limit
on that side) shared by the reconstruction cloud and the semantic layer, and the semantic classes
left out (`ignored_classes`). Nothing here reaches a robot, a pose or a job; it is only a view.

    GET  -> {"map_name", "settings": {...} | null, "updated_at"}     (null: never saved)
    PUT  -> the same, with what was stored; the body replaces the whole document.

404 `map_not_found` for an unknown map, 409 `map_deleting` while it is being deleted, 422 for a body
that does not validate. Stored in Postgres `map_view_settings` (migration 20261017_01).
"""
import datetime
import json
from typing import Any, Dict, List, Optional

import pydantic
from fastapi import HTTPException

TABLE = "map_view_settings"
MAP_SQL = "SELECT lifecycle FROM mapobjectv1 WHERE name = %s AND lifecycle <> 'DELETED'"
GET_SQL = f"SELECT settings, updated_at FROM {TABLE} WHERE map_name = %s"
UPSERT_SQL = (f"INSERT INTO {TABLE} (map_name, settings, updated_at) VALUES (%s, %s::jsonb, now()) "
              "ON CONFLICT (map_name) DO UPDATE SET settings = EXCLUDED.settings, updated_at = now() "
              "RETURNING settings, updated_at")
DELETE_SQL = f"DELETE FROM {TABLE} WHERE map_name = %s"

MAX_CLASSES = 200
MAX_CLASS_NAME = 80
MAX_HEIGHT_M = 30.0


class ViewSettings(pydantic.BaseModel):
    ignored_classes: List[pydantic.constr(min_length=1, max_length=MAX_CLASS_NAME)] = []
    height_min_m: Optional[float] = None
    height_max_m: Optional[float] = None

    class Config:
        extra = pydantic.Extra.forbid

    @pydantic.validator("ignored_classes")
    def _classes(cls, value):  # noqa: N805 - pydantic v1 validator
        if len(value) > MAX_CLASSES:
            raise ValueError(f"at most {MAX_CLASSES} classes")
        return sorted(set(value))

    @pydantic.validator("height_min_m", "height_max_m")
    def _height(cls, value):  # noqa: N805
        if value is None:
            return value
        if not 0.0 <= value <= MAX_HEIGHT_M:   # NaN fails both comparisons
            raise ValueError(f"must be between 0 and {MAX_HEIGHT_M:g} m")
        return value

    @pydantic.root_validator(skip_on_failure=True)
    def _order(cls, values):  # noqa: N805
        low, high = values.get("height_min_m"), values.get("height_max_m")
        if low is not None and high is not None and low >= high:
            raise ValueError("height_min_m must be below height_max_m")
        return values


def _iso(ts: Optional[datetime.datetime]) -> Optional[str]:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return ts.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def parse(body: Any) -> ViewSettings:
    """The validated body; a bad one is 422 in the API's usual shape."""
    try:
        return ViewSettings.parse_obj(body if body is not None else {})
    except pydantic.ValidationError as exc:
        raise HTTPException(422, [{"loc": ["body"] + list(e["loc"]), "type": e["type"], "msg": e["msg"]}
                                  for e in exc.errors()]) from exc


class ViewSettingsStore:
    """map_view_settings on the API's PostgresDatabase (one pooled connection per call)."""

    def __init__(self, db: Any):
        self._db = db

    async def _map(self, conn: Any, map_name: str) -> None:
        cur = await conn.execute(MAP_SQL, (map_name,))
        row = await cur.fetchone()
        if row is None:
            raise _err(404, "map_not_found", f"Map '{map_name}' not found")
        if row[0] == "DELETING":
            raise _err(409, "map_deleting", f"Map '{map_name}' is being deleted")

    @staticmethod
    def _view(map_name: str, row: Optional[Any]) -> Dict[str, Any]:
        if row is None:
            return {"map_name": map_name, "settings": None, "updated_at": None}
        settings = row[0] if isinstance(row[0], dict) else json.loads(row[0] or "{}")
        return {"map_name": map_name, "settings": ViewSettings.parse_obj(settings).dict(),
                "updated_at": _iso(row[1])}

    async def get(self, map_name: str) -> Dict[str, Any]:
        async with self._db.connection() as conn:
            await self._map(conn, map_name)
            cur = await conn.execute(GET_SQL, (map_name,))
            return self._view(map_name, await cur.fetchone())

    async def put(self, map_name: str, body: Any) -> Dict[str, Any]:
        settings = parse(body)
        async with self._db.connection() as conn:
            await self._map(conn, map_name)
            cur = await conn.execute(UPSERT_SQL, (map_name, json.dumps(settings.dict())))
            return self._view(map_name, await cur.fetchone())
