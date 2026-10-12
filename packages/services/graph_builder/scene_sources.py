"""The scene-graph worker's real data providers (the callables `SceneWorker` is given).

* `png_fetcher(image_db)`: `(map_id, node_key, layer) -> PNG bytes | None` from MinIO
  (`{node}/costmap/{layer}.png`); None = not visible (yet), which the worker retries through its
  replay margin.
* `PgFootprint(connect)`: `robot_name -> (length, width)` from the robot's factsheet in Postgres
  (`status->'factsheet'`, -1 until the robot has sent it); the worker caches per robot and turns
  it into `r_d` with `scene_adapter.half_side`. None, -1 and failures all give `(-1, -1)`, which
  the worker counts as an unknown footprint and replaces by the params default.

Synchronous like the worker; connections come from an injected factory (see scene_guard).
"""
import logging
from typing import Any, Callable, Optional, Tuple

from cloud_common.objects.robot import RobotObjectV1

log = logging.getLogger("GraphBuilderService.scene_sources")

UNKNOWN = (-1.0, -1.0)


def png_fetcher(image_db: Any) -> Callable[[str, str, str], Optional[bytes]]:
    def fetch(map_id: str, node_key: str, layer: str) -> Optional[bytes]:
        return image_db.get_costmap(node_key, layer, map_id)
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
