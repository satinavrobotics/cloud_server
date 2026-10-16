"""The map spec's scene_graph_params: read by the guard, and the robot half side it fixes (scene_worker._robot_half_side)."""
import pytest

from packages.services.graph_builder import scene_guard, scene_worker


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, row, seen):
        self._row, self._seen = row, seen

    def execute(self, sql, params=()):
        self._seen.append((sql, params))
        return _Result(self._row)

    def close(self):
        pass


def _guard(row):
    seen = []
    return scene_guard.PgGuard(lambda: _Conn(row, seen)), seen


def test_the_stored_object_is_read_by_map_name():
    guard, seen = _guard(({"schema_version": 1, "robot_half_side_m": 0.3},))
    assert guard.scene_graph_params("office8") == {"schema_version": 1, "robot_half_side_m": 0.3}
    assert seen[0][1] == ("office8",) and "scene_graph_params" in seen[0][0]


def test_a_map_without_the_object_reads_as_none():
    guard, _ = _guard((None,))
    assert guard.scene_graph_params("old-map") is None
    guard, _ = _guard(None)                       # no row at all
    assert guard.scene_graph_params("gone") is None


def test_a_json_string_value_is_decoded():
    guard, _ = _guard(('{"schema_version": 1, "robot_half_side_m": 0.25}',))
    assert guard.scene_graph_params("m") == {"schema_version": 1, "robot_half_side_m": 0.25}


def _worker(half_side):
    w = object.__new__(scene_worker.SceneWorker)
    w._half_side = half_side
    w.footprint = lambda robot: (_ for _ in ()).throw(AssertionError("the live footprint must not be read"))
    w.config = scene_worker.WorkerConfig()
    w._footprints = {}
    w._mono = lambda: 0
    return w


def test_a_fixed_half_side_is_used_and_the_footprint_is_not_read():
    assert _worker(0.3)._robot_half_side({"robot_name": "r1"}) == (0.3, True)


def test_without_a_fixed_half_side_the_legacy_footprint_is_used():
    w = _worker(None)
    w.footprint = lambda robot: (0.8, 0.5)
    r, known = w._robot_half_side({"robot_name": "r1"})
    assert known and r == pytest.approx(0.25)     # half of the smaller side


def test_a_maps_worker_expects_the_hash_of_its_own_params():
    from types import SimpleNamespace
    from packages import config
    from packages.scene_graph.api import SceneParams
    from packages.services.graph_builder import scene_host as sh
    svc = SimpleNamespace(topomap_db=object(), image_db=SimpleNamespace(list_maps=lambda: []),
                          graph_db=object(), update_publisher=object())
    host = sh.build_host(svc, None, conninfo="dbname=x")
    spec = {"schema_version": 1, "chart_radius_m": 3.0, "close_evidence_range_m": 1.3, "robot_half_side_m": 0.3,
            "place_rule": False}
    host.guard.scene_graph_params = lambda map_id: spec
    worker = host.runner.make_worker("mapA")
    mine = SceneParams(chart_radius=3.0, robot_half_side=0.3, near_range_m=1.3,
                       near_min_clearance_m=config.SCENE_GRAPH_NEAR_MIN_CLEARANCE_M)
    assert worker.config.expected_params_hash == sh.expected_params_hash(mine)
    assert worker.config.expected_params_hash != sh.expected_params_hash(SceneParams(near_range_m=1.3))


def test_a_map_without_a_spec_keeps_the_global_hash():
    from types import SimpleNamespace
    from packages.services.graph_builder import scene_host as sh
    svc = SimpleNamespace(topomap_db=object(), image_db=SimpleNamespace(list_maps=lambda: []),
                          graph_db=object(), update_publisher=object())
    host = sh.build_host(svc, None, conninfo="dbname=x")
    host.guard.scene_graph_params = lambda map_id: None
    worker = host.runner.make_worker("legacy")
    from packages import config
    from packages.scene_graph.api import SceneParams
    glob = SceneParams(near_range_m=config.SCENE_GRAPH_NEAR_RANGE_M,
                       near_min_clearance_m=config.SCENE_GRAPH_NEAR_MIN_CLEARANCE_M)
    assert worker.config.expected_params_hash == sh.expected_params_hash(glob)
