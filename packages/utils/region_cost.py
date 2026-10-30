"""Region pricing for the mission planner: one non-negative excess time per metre for every chart of a graph version.

Design: docs/planner_region_stats_2026-10-27.md sections 3 and 4 (and the decisions of section 10). Pure: no I/O, no
API imports; the inputs are the planner-side loader's `RegionStats` (packages/utils/region_stats_load.py), the output an
immutable `RegionPricing` the planner indexes by chart.

    model = CostModel.from_config()
    pricing = build_snapshot(map_id, graph_version, robot_class, graph.keys, stats_class, stats_pooled, model,
                             speed_max, speed_basis)
    pricing.rho[graph_index]        # int32 in units of 1 / pricing.scale: excess time per metre x speed_max

rho_c is the time a metre of chart c takes over driving it at the robot's factsheet speed_max, minus that ideal time,
times speed_max (so 0.5 = a metre takes 1.5 x as long as at speed_max). region_stats stores the sums already
normalised by each run's OWN speed_max (`norm_*` columns, decision 2), so the figures here need no speed: the planning
robot's speed_max and `speed_basis` only decide whether the term is on (a placeholder or missing speed_max: off,
decision 5).

Estimator (per chart c and term t), a ratio of sums shrunk towards a prior:

    x_c = (n_c * xbar_c + n0 * prior_c) / (n_c + n0)        n_c = the chart's samples of the planning class

* xbar_c is the length-weighted mean (expected time is additive; a median is not).
* The prior is the map-wide figure of the same class, never the factsheet, so a chart nobody drove does not look faster
  than every driven one. Chain: the chart's class data (level "class", prior = map-wide class) -> no class data on the
  chart: the pooled chart figure relative to the pooled map figure, scaled by the class map-wide figure (level
  "pooled"; absolute pace does not transfer between classes, relative slowness does) -> the class map-wide figure
  (level "map") -> the factsheet, rho 0 (level "prior").
* rho = clamp(sum_t w_t * value_t, 0, cap), rounded to the quantum, so costs are exact integers and equal across
  processes. `model_hash` identifies the configuration (terms with weights and versions, n0, cap, quantum).

Only the exact graph version is priced: stats of another version, or no build, give a neutral snapshot (all 0) whose
`status` says why.
"""
import dataclasses
import hashlib
import json
import math
from typing import Callable, Dict, Mapping, NamedTuple, Optional, Sequence, Tuple

import numpy as np

from packages import config
from packages.utils.region_stats_load import RegionStat, RegionStats

# quality of a chart's figure, best first: a chart without class data prefers the pooled figure to the map-wide one (Term.value)
LEVELS = ("class", "pooled", "map", "prior")
USABLE_BUILDS = ("ok", "stale")          # a stale build (a rebuild is pending) is still real data
MODES = ("off", "shadow", "on")


class Sums(NamedTuple):
    """Normalised sums of a chart (or of a whole map): samples, metres, and the normalised moving / stopped time
    (seconds x the run's speed_max, i.e. metres at speed_max)."""
    runs: float = 0.0
    len_m: float = 0.0
    moving_m: float = 0.0
    stopped_m: float = 0.0

    @classmethod
    def of(cls, stat: Optional[RegionStat]) -> "Sums":
        if stat is None or not stat.norm_runs or stat.norm_len_m <= 0:
            return cls()
        return cls(float(stat.norm_runs), stat.norm_len_m, stat.norm_moving_m, stat.norm_stopped_m)

    def __add__(self, other: "Sums") -> "Sums":      # type: ignore[override]
        return Sums(*(a + b for a, b in zip(self, other)))


@dataclasses.dataclass(frozen=True)
class StatChain:
    """What a term sees for one chart: its class and pooled figures and the two map-wide sums."""
    chart_class: Sums
    chart_pooled: Sums
    map_class: Sums
    map_pooled: Sums


@dataclasses.dataclass(frozen=True)
class Contribution:
    rho: float               # >= 0, excess time per metre x speed_max
    n_eff: float             # samples of the chart behind it (0 = all prior)
    level: str               # one of LEVELS


@dataclasses.dataclass(frozen=True)
class Term:
    """A pure function from a chart's statistics chain to an excess rho. `figure` reads the ratio of sums that is
    shrunk (e.g. normalised moving time per metre); `baseline` is what the figure is at the factsheet speed (1 for a
    pace, 0 for a stop share); `kind` "excess_s_per_m" terms are additive inside the search."""
    name: str
    version: int
    figure: Callable[[Sums], float]
    baseline: float = 0.0
    kind: str = "excess_s_per_m"

    def value(self, chain: StatChain, n0: float) -> Contribution:
        n = chain.chart_class.runs
        if n > 0:
            prior, level = self._ratio(chain.map_class), "class"
            if prior is None:
                prior = self._ratio(chain.chart_class)
        elif chain.chart_pooled.runs > 0 and chain.map_pooled.runs > 0:
            prior, level = self._pooled_relative(chain), "pooled"
        elif chain.map_class.runs > 0:
            prior, level = self._ratio(chain.map_class), "map"
        else:
            prior, level = None, "prior"
        prior = self.baseline if prior is None else prior
        if n > 0:
            xbar = self._ratio(chain.chart_class)
            x = (n * xbar + n0 * prior) / (n + n0)
        else:
            x = prior
        return Contribution(max(0.0, x - self.baseline), n, level)

    def _ratio(self, s: Sums) -> Optional[float]:
        return self.figure(s) if s.len_m > 0 else None

    def _pooled_relative(self, chain: StatChain) -> float:
        """The chart's pooled figure relative to the pooled map figure, applied to the class map figure (the pooled
        map figure itself when the class has no data on the map)."""
        chart, whole = self._ratio(chain.chart_pooled), self._ratio(chain.map_pooled)
        scale = self._ratio(chain.map_class)
        if chart is None or whole is None:
            return self.baseline if scale is None else scale
        if scale is None:
            return chart
        # relative slowness of the chart: its pooled figure over the pooled figure of the whole map (0 whole: no scale)
        return scale * (chart / whole) if whole > 0 else scale


TERMS: Dict[str, Term] = {
    # moving pace over the ideal pace at speed_max: the normalised moving time per metre is 1 at speed_max
    "moving": Term("moving", 1, lambda s: s.moving_m / s.len_m if s.len_m > 0 else 0.0, baseline=1.0),
    # time spent stopped in transit per metre (waits at the start, goal and action nodes are dwell_s, not here)
    "stop": Term("stop", 1, lambda s: s.stopped_m / s.len_m if s.len_m > 0 else 0.0, baseline=0.0),
}


@dataclasses.dataclass(frozen=True)
class CostModel:
    """The configuration of the pricing. `terms` are (name, weight); `n0` the shrinkage strength (samples of prior);
    `rho_cap` the largest rho of a chart; `quantum` the resolution rho is rounded to (1 / quantum must be an integer)."""
    terms: Tuple[Tuple[str, float], ...] = (("moving", 1.0),)
    n0: float = 5.0
    rho_cap: float = 3.0
    quantum: float = 0.001

    def __post_init__(self) -> None:
        for name, weight in self.terms:
            if name not in TERMS:
                raise ValueError(f"unknown region cost term {name!r}")
            if not (math.isfinite(weight) and weight >= 0):
                raise ValueError(f"region cost term {name!r}: weight {weight!r} is not a finite number >= 0")
        if not (math.isfinite(self.n0) and self.n0 >= 0):
            raise ValueError("n0 must be finite and >= 0")
        if not (math.isfinite(self.rho_cap) and self.rho_cap >= 0):
            raise ValueError("rho_cap must be finite and >= 0")
        if not (0 < self.quantum <= 1) or abs(round(1.0 / self.quantum) * self.quantum - 1.0) > 1e-9:
            raise ValueError("quantum must be 1 / an integer")

    @classmethod
    def from_config(cls) -> "CostModel":
        return cls(terms=parse_terms(config.REGION_COST_TERMS), n0=config.REGION_COST_N0,
                   rho_cap=config.REGION_COST_RHO_CAP, quantum=config.REGION_COST_QUANTUM)

    @property
    def scale(self) -> int:
        """Integer rho units per 1.0 (1000 for the default quantum)."""
        return int(round(1.0 / self.quantum))

    @property
    def model_hash(self) -> str:
        """16 hex of the canonical configuration (terms sorted by name, with their versions): stable across processes."""
        canon = {"terms": sorted((n, TERMS[n].version, float(w)) for n, w in self.terms if w > 0),
                 "n0": float(self.n0), "cap": float(self.rho_cap), "scale": self.scale}
        return hashlib.sha1(json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]

    def rho(self, chain: StatChain) -> Tuple[float, str]:
        """(rho of a chart before the cap and the rounding, its level: the worst level of the terms in use)."""
        total, worst = 0.0, 0
        for name, weight in self.terms:
            if weight <= 0:
                continue
            c = TERMS[name].value(chain, self.n0)
            total += weight * c.rho
            worst = max(worst, LEVELS.index(c.level))
        return total, LEVELS[worst]


def parse_terms(text: str) -> Tuple[Tuple[str, float], ...]:
    """"moving:1.0,stop:0.5" -> (("moving", 1.0), ("stop", 0.5)); a bare name has weight 1."""
    out = []
    for part in (p.strip() for p in (text or "").split(",")):
        if not part:
            continue
        name, _, w = part.partition(":")
        out.append((name.strip(), float(w) if w.strip() else 1.0))
    return tuple(out)


@dataclasses.dataclass(frozen=True, eq=False)
class RegionPricing:
    """Immutable snapshot for one (map, graph version, robot class, build, model). `rho[i]` belongs to `keys[i]` (the
    keys of the SAME graph version, in the planner's order), an int32 in units of 1 / `scale`; all 0 unless `status`
    is "ok". `levels[i]` is the fallback level of chart i, `provenance` the count per level."""
    map_id: str
    graph_version: int
    robot_class: str
    stamp: Optional[str]
    model_hash: str
    status: str                       # ok | no_speed_max | no_build_for_version | version_mismatch | stamp_mismatch | no_stats
    keys: Tuple[str, ...]
    rho: np.ndarray
    scale: int
    levels: Tuple[str, ...]
    provenance: Tuple[Tuple[str, int], ...]
    rho_min: int
    rho_max: int

    @property
    def key(self) -> Tuple[str, int, str, Optional[str], str]:
        """The cache key of the snapshot."""
        return (self.map_id, self.graph_version, self.robot_class, self.stamp, self.model_hash)

    @property
    def active(self) -> bool:
        return self.status == "ok"

    def rho_of(self, chart_key: str) -> int:
        try:
            return int(self.rho[self.keys.index(chart_key)])
        except ValueError:
            return 0

    def record(self) -> Dict[str, object]:
        """The part of the plan record that identifies the snapshot (docs/planner_region_stats_2026-10-27.md 7)."""
        return {"status": self.status, "model_hash": self.model_hash, "stamp": self.stamp,
                "graph_version": self.graph_version, "robot_class": self.robot_class,
                "rho_min": self.rho_min / self.scale, "rho_max": self.rho_max / self.scale,
                "levels": dict(self.provenance)}


def _quantise(x: float, cap: float, scale: int) -> int:
    return int(math.floor(min(max(x, 0.0), cap) * scale + 0.5))


def _snapshot(map_id: str, graph_version: int, robot_class: str, stamp: Optional[str], model: CostModel,
              status: str, keys: Sequence[str], rho: Sequence[int], levels: Sequence[str]) -> RegionPricing:
    arr = np.asarray(rho, np.int32).reshape(-1)
    arr.setflags(write=False)
    counts = [(lv, sum(1 for x in levels if x == lv)) for lv in LEVELS]
    return RegionPricing(map_id, int(graph_version), robot_class, stamp, model.model_hash, status, tuple(keys), arr,
                         model.scale, tuple(levels), tuple((lv, n) for lv, n in counts if n),
                         int(arr.min()) if arr.size else 0, int(arr.max()) if arr.size else 0)


def neutral(map_id: str, graph_version: int, robot_class: str, keys: Sequence[str], model: CostModel,
            status: str, stamp: Optional[str] = None) -> RegionPricing:
    """The snapshot that prices nothing (every rho 0), with the reason."""
    return _snapshot(map_id, graph_version, robot_class, stamp, model, status, keys, [0] * len(keys),
                     ["prior"] * len(keys))


def build_snapshot(map_id: str, graph_version: int, robot_class: str, keys: Sequence[str],
                   stats_class: Optional[RegionStats], stats_pooled: Optional[RegionStats], model: CostModel,
                   speed_max: Optional[float], speed_basis: str = "factsheet") -> RegionPricing:
    """The pricing of the charts `keys` of graph version `graph_version` for the planning robot (pure).

    `stats_class` / `stats_pooled` are the loader's results for the robot's class and for '*' of the SAME build;
    `speed_max` / `speed_basis` the planning robot's (robot_class.speed_basis): anything but a real factsheet speed
    switches the term off. Status, in this order: "no_speed_max"; "no_build_for_version" (no stats object or a version
    never built); "version_mismatch" (the stats are of another graph version); "stamp_mismatch" (the two loads
    straddled a rebuild, or a load's rows are of another build than its build row); "no_stats" (a build that is not
    usable, or no row for the class or the pool)."""
    def neutral_with(status: str, stamp: Optional[str] = None) -> RegionPricing:
        return neutral(map_id, graph_version, robot_class, keys, model, status, stamp)

    if speed_basis != "factsheet" or speed_max is None or not (math.isfinite(speed_max) and speed_max > 0):
        return neutral_with("no_speed_max")
    present = [s for s in (stats_class, stats_pooled) if s is not None]
    if not present or any(s.stamp is None for s in present):
        return neutral_with("no_build_for_version")
    if any(s.graph_version != graph_version for s in present):
        return neutral_with("version_mismatch")
    if len({s.stamp for s in present}) > 1:
        return neutral_with("stamp_mismatch")
    # rows and build row are two statements (region_stats_load.aload, READ COMMITTED): a rebuild committed between them
    # gives the rows of one build under the stamp of the next. Every row carries its own build's stamp: check it.
    if any(c.stamp != s.stamp for s in present for c in s.charts.values()):
        return neutral_with("stamp_mismatch")
    stamp = present[0].stamp
    if any(s.status not in USABLE_BUILDS for s in present):
        return neutral_with("no_stats", stamp)
    class_charts: Mapping[str, RegionStat] = stats_class.charts if stats_class is not None else {}
    pooled_charts: Mapping[str, RegionStat] = stats_pooled.charts if stats_pooled is not None else {}
    if not class_charts and not pooled_charts:
        return neutral_with("no_stats", stamp)
    map_class = _total(class_charts)
    map_pooled = _total(pooled_charts)
    rho, levels = [], []
    for key in keys:
        chain = StatChain(Sums.of(class_charts.get(key)), Sums.of(pooled_charts.get(key)), map_class, map_pooled)
        x, level = model.rho(chain)
        rho.append(_quantise(x, model.rho_cap, model.scale))
        levels.append(level)
    return _snapshot(map_id, graph_version, robot_class, stamp, model, "ok", keys, rho, levels)


def _total(charts: Mapping[str, RegionStat]) -> Sums:
    total = Sums()
    for stat in charts.values():
        total = total + Sums.of(stat)
    return total


def mode_for(map_id: str) -> str:
    """off | shadow | on for a map: REGION_COST_MODE, and only for the maps in REGION_COST_MAPS ('*' = all)."""
    mode = config.REGION_COST_MODE
    if mode not in MODES or mode == "off":
        return "off"
    allowed = config.REGION_COST_MAPS
    return mode if ("*" in allowed or map_id in allowed) else "off"
