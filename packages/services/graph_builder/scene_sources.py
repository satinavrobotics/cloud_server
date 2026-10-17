"""The scene-graph worker's real data providers (the callables `SceneWorker` is given).

* `png_fetcher(image_db)`: `(map_id, node_key, layer) -> PNG bytes | None` from MinIO
  (`{node}/costmap/{layer}.png`). None = the object is confirmed not there (NoSuchKey /
  NoSuchBucket): the worker keeps the node pending and asks again for a while. Any other failure
  (MinIO down, timeout) RAISES, because `image_db.get_costmap` returns None for those too and the
  worker must not mistake an outage for a missing PNG: it backs off and does not skip the leaf.
* `PgFootprint(connect)`: `robot_name -> (length, width)` from the robot's factsheet in Postgres
  (`status->'factsheet'`, -1 until the robot has sent it); the worker caches per robot and turns
  it into `r_d` with `half_side` (cloud_common.objects.map). None, -1 and failures all give `(-1, -1)`, which
  the worker counts as an unknown footprint and replaces by the params default.

Synchronous like the worker; connections come from an injected factory (see scene_guard).
"""
import logging
from typing import Any, Callable, Optional, Tuple

from cloud_common.objects.robot import RobotObjectV1

log = logging.getLogger("GraphBuilderService.scene_sources")

UNKNOWN = (-1.0, -1.0)


MISSING_CODES = ("NoSuchKey", "NoSuchBucket")


def png_fetcher(image_db: Any) -> Callable[[str, str, str], Optional[bytes]]:
    def fetch(map_id: str, node_key: str, layer: str) -> Optional[bytes]:
        client = getattr(image_db, "client", None)
        bucket_of, key_of = getattr(image_db, "_bucket_name", None), getattr(image_db, "costmap_key", None)
        if client is None or bucket_of is None or key_of is None:
            # no lower-level access: get_costmap cannot tell "missing" from "failed" (both None);
            # the worker's wall-clock expiry bounds how long such a None is waited for
            return image_db.get_costmap(node_key, layer, map_id)
        try:
            response = client.get_object(bucket_of(map_id), key_of(str(node_key), layer))
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "code", "") in MISSING_CODES:
                return None
            raise
        try:
            return response.read()
        finally:
            for name in ("close", "release_conn"):
                try:
                    getattr(response, name)()
                except Exception:  # noqa: BLE001
                    pass
    return fetch


class PgFootprint:
    SQL = (f"SELECT status->'factsheet'->>'length', status->'factsheet'->>'width' "
           f"FROM {RobotObjectV1.table_name()} WHERE name = %s")

    def __init__(self, connect: Callable[[], Any]) -> None:
        self._connect = connect

    def __call__(self, robot_name: str) -> Tuple[float, float]:
        conn = None
        try:
            conn = self._connect()
            row = conn.execute(self.SQL, (robot_name,)).fetchone()
        except Exception:  # noqa: BLE001
            log.exception("footprint of %s: query failed", robot_name)
            return UNKNOWN
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
        if not row:
            return UNKNOWN
        try:
            return float(row[0]), float(row[1])
        except (TypeError, ValueError):
            return UNKNOWN
