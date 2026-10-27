"""packages/utils/region_cost.py (pure region pricing: terms, shrinkage chain, cap, quantum, hash, snapshot) and
weights.region_costs (the edge cost it feeds)."""
import dataclasses

import numpy as np
import pytest

from packages import config
from packages.scene_graph import weights
from packages.utils import region_cost as rc
from packages.utils.region_stats_load import RegionStat, RegionStats

pytestmark = pytest.mark.unit

CLS = "CARRIER|0.50x0.50"
KEYS = ["A", "B", "C"]


def stat(key, runs, len_m, moving_m, stopped_m=0.0, cls=CLS, norm_runs=None):
    """A region_stats row; `moving_m` / `stopped_m` are the speed-normalised sums (the pace ratio is moving_m / len_m)."""
    return RegionStat(chart_key=key, robot_class=cls, runs=runs, len_m=len_m, time_s=0.0, stopped_s=0.0, recovery_s=0.0,
                      blocks=0, pace_median_s_per_m=None, pace_p90_s_per_m=None, moving_pace_median_s_per_m=None,
                      moving_speed_mps=None, stopped_share=None, recovery_s_per_100m=None, blocks_per_run=None,
                      speed_max=None, cells=100, visited=10, first_at=None, last_at=None, stamp="st",
                      norm_runs=runs if norm_runs is None else norm_runs, norm_len_m=len_m, norm_moving_m=moving_m,
                      norm_stopped_m=stopped_m)


def stats(rows, cls=CLS, version=3, stamp="st", status="ok"):
    """A load of one build: its rows carry the build's stamp, as region_stats.build_map writes them."""
    return RegionStats("m", version, cls, {r.chart_key: dataclasses.replace(r, stamp=stamp) for r in rows}, stamp, status,
                       None)


def snap(class_rows, pooled_rows=None, model=None, keys=KEYS, version=3, speed_max=0.24, basis="factsheet", **kw):
    pooled_rows = class_rows if pooled_rows is None else pooled_rows
    return rc.build_snapshot("m", version, CLS, keys, stats(class_rows, **kw), stats(pooled_rows, cls="*", **kw),
                             model or rc.CostModel(), speed_max, basis)


A = stat("A", 2, 10.0, 20.0)         # pace 2.0 x the ideal, 2 samples
B = stat("B", 10, 100.0, 100.0)      # at speed_max, 10 samples
MAP_P = 120.0 / 110.0                # the class map-wide pace ratio of A + B


class TestShrinkage:
    def test_a_chart_with_samples_is_pulled_to_the_map_wide_figure(self):
        p = snap([A, B])
        x = (2 * 2.0 + 5 * MAP_P) / 7
        assert p.rho_of("A") == round((x - 1) * 1000)
        assert p.levels[0] == "class" and p.status == "ok"

    def test_many_samples_dominate_the_prior(self):
        heavy = stat("A", 500, 1000.0, 2000.0)
        p = snap([heavy, B])
        assert abs(p.rho_of("A") - 1000) < 15                       # about 2.0 - 1 = 1.0

    def test_n0_zero_is_the_plain_ratio_of_sums(self):
        p = snap([A, B], model=rc.CostModel(n0=0))
        assert p.rho_of("A") == 1000

    def test_a_chart_at_or_below_speed_max_costs_nothing(self):
        fast = stat("A", 4, 10.0, 8.0)                               # 0.8 x the ideal time: faster than speed_max
        p = snap([fast], model=rc.CostModel(n0=0))
        assert p.rho_of("A") == 0

    def test_shrinkage_is_in_pace_space_so_a_fast_run_offsets_a_slow_one(self):
        p = snap([stat("A", 2, 20.0, 20.0), stat("B", 2, 20.0, 60.0)], model=rc.CostModel(n0=0))
        assert p.rho_of("A") == 0 and p.rho_of("B") == 2000


class TestFallbackLevels:
    def test_a_chart_nobody_drove_gets_the_class_map_wide_figure(self):
        p = snap([A, B])
        assert p.levels[2] == "map" and p.rho_of("C") == round((MAP_P - 1) * 1000)

    def test_a_chart_only_another_class_drove_gets_the_pooled_figure_relative_to_the_map(self):
        other = "TRUCK|1.00x1.00"
        pooled = [stat("A", 2, 10.0, 20.0, cls="*"), stat("B", 10, 100.0, 100.0, cls="*"),
                  stat("C", 3, 10.0, 30.0, cls="*")]
        p = snap([A, B], pooled)
        pool_map = (20.0 + 100.0 + 30.0) / 120.0
        expect = MAP_P * (3.0 / pool_map) - 1
        assert p.levels[2] == "pooled" and p.rho_of("C") == round(expect * 1000)
        assert other                                                  # (the other class is only in the pooled rows)

    def test_pooled_figures_are_used_as_is_when_the_class_has_no_data_on_the_map(self):
        pooled = [stat("A", 2, 10.0, 20.0, cls="*")]
        p = snap([], pooled)
        assert p.levels[0] == "pooled" and p.rho_of("A") == 1000
        assert p.levels[1] == "prior" and p.rho_of("B") == 0

    def test_no_data_anywhere_is_the_factsheet_rho_zero(self):
        p = snap([stat("X", 1, 10.0, 10.0)], [stat("X", 1, 10.0, 10.0, cls="*")], keys=["A"])
        assert p.status == "ok" and p.levels == ("map",) and p.rho_of("A") == 0       # the class map-wide figure is 1.0
        q = snap([], [stat("X", 1, 10.0, 30.0, cls="*")], keys=["A"])
        assert q.status == "ok" and q.levels == ("prior",) and q.rho_of("A") == 0     # nothing on the chart or the class

    def test_a_chart_with_class_data_does_not_borrow_the_pool_even_if_it_is_thin(self):
        pooled = [stat("A", 20, 100.0, 400.0, cls="*"), B_pool()]
        p = snap([stat("A", 1, 5.0, 5.0), B], pooled, model=rc.CostModel(n0=1))
        assert p.levels[0] == "class" and p.rho_of("A") < 100

    def test_zero_length_samples_are_no_data(self):
        p = snap([stat("A", 3, 0.0, 0.0), B])
        assert p.levels[0] == "map"

    def test_runs_without_a_real_speed_are_no_data(self):
        p = snap([stat("A", 3, 10.0, 30.0, norm_runs=0), B])
        assert p.levels[0] == "map"


def B_pool():
    return stat("B", 10, 100.0, 100.0, cls="*")


class TestCapAndQuantum:
    def test_rho_is_capped(self):
        p = snap([stat("A", 50, 10.0, 1000.0)], model=rc.CostModel(n0=0, rho_cap=3.0), keys=["A"])
        assert p.rho_of("A") == 3000 and p.rho_max == 3000

    def test_the_cap_is_a_model_setting(self):
        p = snap([stat("A", 50, 10.0, 1000.0)], model=rc.CostModel(n0=0, rho_cap=0.5), keys=["A"])
        assert p.rho_of("A") == 500

    def test_rho_is_rounded_half_up_to_the_quantum(self):
        for ratio, expect in ((1.0004, 0), (1.00051, 1), (1.0006, 1), (1.00149, 1), (1.00151, 2), (1.1234, 123)):
            p = snap([stat("A", 5, 1000.0, 1000.0 * ratio)], model=rc.CostModel(n0=0), keys=["A"])
            assert p.rho_of("A") == expect, ratio

    def test_a_coarser_quantum(self):
        m = rc.CostModel(n0=0, quantum=0.01)
        p = snap([stat("A", 5, 1000.0, 1237.0)], model=m, keys=["A"])
        assert p.rho_of("A") == 24 and p.scale == 100

    def test_the_array_is_int32_and_read_only(self):
        p = snap([A, B])
        assert p.rho.dtype == np.int32 and not p.rho.flags.writeable
        with pytest.raises(ValueError):
            p.rho[0] = 5
        with pytest.raises(dataclasses.FrozenInstanceError):
            p.status = "x"

    def test_a_quantum_that_is_not_one_over_an_integer_is_refused(self):
        with pytest.raises(ValueError):
            rc.CostModel(quantum=0.3)
        with pytest.raises(ValueError):
            rc.CostModel(quantum=0)


class TestTerms:
    def test_the_stop_term_is_the_normalised_stopped_time_per_metre(self):
        rows = [stat("A", 5, 100.0, 100.0, stopped_m=25.0)]
        p = snap(rows, model=rc.CostModel(terms=(("stop", 1.0),), n0=0), keys=["A"])
        assert p.rho_of("A") == 250

    def test_terms_add_with_their_weights(self):
        rows = [stat("A", 5, 100.0, 150.0, stopped_m=20.0)]
        m = rc.CostModel(terms=(("moving", 1.0), ("stop", 0.5)), n0=0)
        assert snap(rows, model=m, keys=["A"]).rho_of("A") == 500 + 100

    def test_a_zero_weight_term_is_off(self):
        rows = [stat("A", 5, 100.0, 150.0, stopped_m=20.0)]
        m = rc.CostModel(terms=(("moving", 1.0), ("stop", 0.0)), n0=0)
        assert snap(rows, model=m, keys=["A"]).rho_of("A") == 500

    def test_no_terms_prices_nothing(self):
        assert snap([A, B], model=rc.CostModel(terms=())).rho.tolist() == [0, 0, 0]

    def test_unknown_term_and_bad_weight_are_refused(self):
        with pytest.raises(ValueError):
            rc.CostModel(terms=(("nope", 1.0),))
        with pytest.raises(ValueError):
            rc.CostModel(terms=(("moving", -1.0),))
        with pytest.raises(ValueError):
            rc.CostModel(terms=(("moving", float("nan")),))

    def test_parse_terms(self):
        assert rc.parse_terms("moving:1.0, stop:0.5") == (("moving", 1.0), ("stop", 0.5))
        assert rc.parse_terms("moving") == (("moving", 1.0),) and rc.parse_terms("") == ()

    def test_the_default_model_comes_from_the_config(self, monkeypatch):
        assert rc.CostModel.from_config() == rc.CostModel()
        monkeypatch.setattr(config, "REGION_COST_TERMS", "moving:2,stop:1")
        monkeypatch.setattr(config, "REGION_COST_N0", 3.0)
        m = rc.CostModel.from_config()
        assert m.terms == (("moving", 2.0), ("stop", 1.0)) and m.n0 == 3.0

    def test_the_level_of_a_chart_is_the_worst_of_its_terms(self):
        # both terms read the same sums here, so the levels agree; with weights 0 the term is not consulted
        p = snap([A, B], model=rc.CostModel(terms=(("moving", 1.0), ("stop", 1.0))))
        assert p.levels[0] == "class" and p.levels[2] == "map"


class TestModelHash:
    def test_stable_across_instances_and_term_order(self):
        a = rc.CostModel(terms=(("moving", 1.0), ("stop", 0.5)))
        b = rc.CostModel(terms=(("stop", 0.5), ("moving", 1.0)))
        assert a.model_hash == b.model_hash == rc.CostModel(terms=(("moving", 1.0), ("stop", 0.5))).model_hash

    def test_pinned_value_of_the_default_model(self):
        """A change here changes the plan record of every plan: do it on purpose (bump a term version or a setting)."""
        assert rc.CostModel().model_hash == "2321dfce4b2cb095"

    @pytest.mark.parametrize("other", [rc.CostModel(n0=4.0), rc.CostModel(rho_cap=2.0), rc.CostModel(quantum=0.01),
                                       rc.CostModel(terms=(("moving", 2.0),)),
                                       rc.CostModel(terms=(("moving", 1.0), ("stop", 1.0)))])
    def test_every_setting_changes_it(self, other):
        assert other.model_hash != rc.CostModel().model_hash

    def test_a_zero_weight_term_does_not_change_it(self):
        assert rc.CostModel(terms=(("moving", 1.0), ("stop", 0.0))).model_hash == rc.CostModel().model_hash

    def test_the_term_version_is_part_of_it(self, monkeypatch):
        before = rc.CostModel().model_hash
        monkeypatch.setitem(rc.TERMS, "moving", dataclasses.replace(rc.TERMS["moving"], version=2))
        assert rc.CostModel().model_hash != before

    def test_the_snapshot_carries_it_in_its_key(self):
        p = snap([A, B])
        assert p.key == ("m", 3, CLS, "st", rc.CostModel().model_hash)
        assert snap([A, B], model=rc.CostModel(n0=1)).key != p.key


class TestStatus:
    def test_a_placeholder_speed_switches_the_term_off(self):
        p = snap([A, B], speed_max=1.0, basis="placeholder")
        assert p.status == "no_speed_max" and p.rho.tolist() == [0, 0, 0] and not p.active

    @pytest.mark.parametrize("speed", [None, 0.0, -1.0, float("nan")])
    def test_a_missing_speed_switches_it_off(self, speed):
        assert snap([A, B], speed_max=speed).status == "no_speed_max"

    def test_an_unknown_basis_switches_it_off(self):
        assert snap([A, B], basis="unknown").status == "no_speed_max"

    def test_stats_of_another_graph_version_are_not_used(self):
        p = snap([A, B], version=3)
        assert p.active
        q = rc.build_snapshot("m", 4, CLS, KEYS, stats([A, B], version=3), stats([A, B], cls="*", version=3),
                              rc.CostModel(), 0.24)
        assert q.status == "version_mismatch" and q.rho.tolist() == [0, 0, 0] and q.graph_version == 4

    def test_a_version_that_was_never_built(self):
        none = RegionStats("m", 3, CLS, {}, None, None, None)
        p = rc.build_snapshot("m", 3, CLS, KEYS, none, none, rc.CostModel(), 0.24)
        assert p.status == "no_build_for_version" and p.stamp is None
        assert rc.build_snapshot("m", 3, CLS, KEYS, None, None, rc.CostModel(), 0.24).status == "no_build_for_version"

    def test_two_loads_of_different_builds_are_refused(self):
        p = rc.build_snapshot("m", 3, CLS, KEYS, stats([A, B], stamp="one"), stats([A, B], cls="*", stamp="two"),
                              rc.CostModel(), 0.24)
        assert p.status == "stamp_mismatch"

    def test_rows_of_another_build_than_the_build_row_are_refused(self):
        # a rebuild committed between a load's two statements: old rows, the new build's stamp
        old_rows = stats([A, B], stamp="old")
        mixed = RegionStats("m", 3, CLS, old_rows.charts, "new", "ok", None)
        p = rc.build_snapshot("m", 3, CLS, KEYS, mixed, stats([A, B], cls="*", stamp="new"), rc.CostModel(), 0.24)
        assert p.status == "stamp_mismatch" and p.rho.tolist() == [0, 0, 0]

    def test_a_build_without_a_scene_graph_or_without_rows_has_no_stats(self):
        assert snap([A, B], status="no_scene_graph").status == "no_stats"
        assert snap([], []).status == "no_stats"

    def test_a_stale_build_is_still_used(self):
        assert snap([A, B], status="stale").status == "ok"

    def test_the_class_load_may_be_missing_when_the_pool_is_there(self):
        p = rc.build_snapshot("m", 3, CLS, KEYS, None, stats([A, B], cls="*"), rc.CostModel(), 0.24)
        assert p.status == "ok" and p.levels[0] == "pooled" and p.rho_of("A") == 1000

    def test_a_neutral_snapshot_still_aligns_with_the_keys(self):
        p = snap([A, B], speed_max=None)
        assert p.keys == tuple(KEYS) and len(p.rho) == 3 and p.rho_min == p.rho_max == 0


class TestSnapshotShape:
    def test_rho_follows_the_order_of_the_keys(self):
        p = snap([A, B], keys=["C", "B", "A"])
        assert p.rho_of("A") == snap([A, B]).rho_of("A") and p.rho[2] == p.rho_of("A") and p.rho[1] == p.rho_of("B")

    def test_a_key_the_stats_do_not_know_is_priced_and_a_stat_without_a_key_is_ignored(self):
        p = snap([A, B, stat("Z", 5, 10.0, 40.0)], keys=["A", "B"])
        assert p.keys == ("A", "B") and p.rho_of("Z") == 0

    def test_provenance_counts_the_levels_and_min_max(self):
        p = snap([A, B])
        assert dict(p.provenance) == {"class": 2, "map": 1}
        assert p.rho_min == min(p.rho) and p.rho_max == max(p.rho)
        rec = p.record()
        assert rec["levels"] == {"class": 2, "map": 1} and rec["status"] == "ok" and rec["model_hash"] == p.model_hash

    def test_it_is_deterministic(self):
        a, b = snap([A, B]), snap([B, A])
        assert a.rho.tolist() == b.rho.tolist() and a.key == b.key

    def test_an_empty_graph(self):
        p = snap([A, B], keys=[])
        assert p.rho.size == 0 and p.rho_min == 0


class TestMode:
    def test_default_is_off(self):
        assert config.REGION_COST_MODE == "off" and rc.mode_for("office9") == "off"

    def test_the_mode_applies_only_to_the_allowed_maps(self, monkeypatch):
        monkeypatch.setattr(config, "REGION_COST_MODE", "shadow")
        monkeypatch.setattr(config, "REGION_COST_MAPS", ("office9",))
        assert rc.mode_for("office9") == "shadow" and rc.mode_for("office7") == "off"
        monkeypatch.setattr(config, "REGION_COST_MODE", "on")
        assert rc.mode_for("office9") == "on"

    def test_star_allows_every_map_and_empty_none(self, monkeypatch):
        monkeypatch.setattr(config, "REGION_COST_MODE", "on")
        monkeypatch.setattr(config, "REGION_COST_MAPS", ("*",))
        assert rc.mode_for("anything") == "on"
        monkeypatch.setattr(config, "REGION_COST_MAPS", ())
        assert rc.mode_for("anything") == "off"

    def test_an_unknown_mode_is_off(self, monkeypatch):
        monkeypatch.setattr(config, "REGION_COST_MODE", "yes")
        monkeypatch.setattr(config, "REGION_COST_MAPS", ("*",))
        assert rc.mode_for("m") == "off"


class TestEdgeCost:
    def test_zero_rho_adds_nothing(self):
        assert weights.region_costs([0.1, 0.1414], [0, 0], [0, 0]).tolist() == [0.0, 0.0]

    def test_the_additive_formula(self):
        """A 1000 unit edge between rho 0.5 and 1.0 costs ceil(1000 * 0.75) more."""
        assert weights.region_costs([0.1], [500], [1000]).tolist() == [750.0]
        assert weights.region_costs([0.1414], [500], [500]).tolist() == [np.ceil(1414 * 0.5 - 1e-6)]

    def test_it_is_a_whole_number_never_negative_and_never_below_the_length(self):
        rng = np.random.default_rng(1)
        length = rng.uniform(0.05, 0.2, 200)
        a, b = rng.integers(0, 3000, 200), rng.integers(0, 3000, 200)
        extra = weights.region_costs(length, a, b)
        assert (extra >= 0).all() and (extra == np.round(extra)).all()
        base = weights.edge_costs(length, np.zeros(200), 0.0)
        assert (base + extra >= base).all() and (base + extra >= np.floor(length / weights.ROUTE_UNIT_M)).all()
        assert (extra >= length / weights.ROUTE_UNIT_M * (a + b) / 2000 - 1e-6).all()

    def test_the_scale_follows_the_models_quantum(self):
        m = rc.CostModel(quantum=0.01)
        assert weights.region_costs([0.1], [50], [100], scale=m.scale).tolist() == [750.0]

    def test_a_tiny_rho_does_not_round_to_a_lower_cost_than_zero(self):
        assert weights.region_costs([0.0001], [1], [0]).tolist() == [1.0]


def test_the_module_is_pure_it_imports_no_io_layer():
    import subprocess
    import sys
    code = ("import sys; import packages.utils.region_cost; "
            "bad = [m for m in ('psycopg', 'fastapi', 'asyncio', 'arango', 'minio') if m in sys.modules]; "
            "print(','.join(bad))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         cwd=str(__import__("pathlib").Path(__file__).resolve().parents[2])).stdout.strip()
    assert out == ""
