"""tools/scene_graph_pin_params.py: pure planning, the guarded UPDATE, dry run / apply / idempotence on fakes."""
import json
import os

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import contextlib  # noqa: E402

import pytest  # noqa: E402

from tools import scene_graph_pin_params as t  # noqa: E402

pytestmark = pytest.mark.unit

ON = {"robot_half_side": 0.3, "lattice": 0.1, "chart_radius": 2.5, "near_range_m": 1.5, "near_min_clearance_m": 0.2,
      "place_peak_m": 0.4, "place_path_m": 0.8}
OFF = {"robot_half_side": 0.25, "lattice": 0.1, "chart_radius": 2.0}


class TestParamsFromManifest:
    def test_full_mapping(self):
        p, reason, unpinned = t.params_from_manifest(ON)
        assert (p.chart_radius_m, p.robot_half_side_m, p.close_evidence_range_m, p.place_rule) == (2.5, 0.3, 1.5, True)
        assert reason == "" and unpinned == ["lattice", "near_min_clearance_m", "place_path_m"]

    def test_absent_rules_mean_off_not_schema_default(self):
        p, _, _ = t.params_from_manifest(OFF)
        assert p.close_evidence_range_m is None and p.place_rule is False

    @pytest.mark.parametrize("bad", [None, {}, "x", {"chart_radius": 2.0}, {"robot_half_side": 0.2}])
    def test_unusable(self, bad):
        p, reason, _ = t.params_from_manifest(bad)
        assert p is None and reason

    def test_validation_failure(self):
        p, reason, _ = t.params_from_manifest(dict(OFF, chart_radius=-1))
        assert p is None and "validation" in reason and "chart_radius_m" in reason


class TestPlan:
    def test_pin(self):
        r = t.plan_map("m", {}, OFF, True)
        assert r["action"] == "pin" and r["params"]["chart_radius_m"] == 2.0 and r["params"]["close_evidence_range_m"] is None

    def test_pin_when_explicit_null(self):
        assert t.plan_map("m", {"scene_graph_params": None}, OFF, True)["action"] == "pin"

    def test_skips(self):
        assert t.plan_map("m", {"scene_graph_params": {"a": 1}}, OFF, True)["reason"] == "already pinned"
        assert t.plan_map("m", {}, None, False)["reason"] == "no manifest"
        assert t.plan_map("m", {}, {}, True)["reason"] == "manifest has no usable params"


class FakeConn:
    """Rows keyed by name; mimics the guard of UPDATE_SQL and the transaction."""
    def __init__(self, specs):
        self.specs = specs
        self.updates = 0

    @contextlib.contextmanager
    def transaction(self):
        yield

    def execute(self, sql, args):
        conn = self
        if sql.startswith("SELECT"):
            names = args["names"]
            rows = [(n, s) for n, s in sorted(self.specs.items()) if not names or n in names]
            return type("C", (), {"fetchall": lambda s: rows})()
        n = 0
        if self.specs.get(args["name"], {}).get("scene_graph_params") is None and args["name"] in self.specs:
            self.specs[args["name"]]["scene_graph_params"] = json.loads(args["params"]); n = 1
            conn.updates += 1
        return type("C", (), {"rowcount": n})()


class FakeStore:
    def __init__(self, metas):
        self.docs = type("D", (), {"meta": staticmethod(lambda m: metas.get(m))})()


def test_sql_guard_text():
    assert "scene_graph_params' IS NULL" in t.UPDATE_SQL and "WHERE name" in t.UPDATE_SQL
    assert t.UPDATE_SQL.index("WHERE") < t.UPDATE_SQL.index("IS NULL")


def run(argv, specs, metas, capsys):
    conn = FakeConn(specs)
    rc = t.main(argv, conn=conn, store=FakeStore(metas))
    return rc, conn, [l for l in capsys.readouterr().out.splitlines() if l]


def test_dry_run_changes_nothing(capsys):
    specs = {"a": {}, "b": {}}
    _, conn, lines = run([], specs, {"a": {"params": OFF}}, capsys)
    assert conn.updates == 0 and specs["a"] == {} and len(lines) == 2
    assert '"action": "pin"' in lines[0] and "no manifest" in lines[1]


def test_apply_and_idempotent(capsys):
    specs = {"a": {}, "b": {"scene_graph_params": {"x": 1}}}
    metas = {"a": {"params": OFF}, "b": {"params": OFF}}
    run(["--apply"], specs, metas, capsys)
    assert specs["a"]["scene_graph_params"]["robot_half_side_m"] == 0.25 and specs["b"]["scene_graph_params"] == {"x": 1}
    _, conn, lines = run(["--apply"], specs, metas, capsys)
    assert conn.updates == 0 and all("already pinned" in l for l in lines)


def test_race_lost(capsys):
    specs = {"a": {}}
    conn = FakeConn(specs)
    specs["a"]["scene_graph_params"] = {"y": 2}      # pinned between planning and update
    assert t.apply_pin(conn, "a", {"z": 1}) is False
    assert specs["a"]["scene_graph_params"] == {"y": 2}


def test_map_id_filter(capsys):
    _, _, lines = run(["--map-id", "b"], {"a": {}, "b": {}}, {}, capsys)
    assert len(lines) == 1 and '"map_id": "b"' in lines[0]
