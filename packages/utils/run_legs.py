"""Pure helpers for run legs, shared by mission-dispatch (writes them) and the API (reads them).

A leg is one robot move from one topomap node to the next. Nothing here touches a database.

- `expected_seconds`: t_exp = d / v_max + v_max / a_max + |d_psi| / omega_max from the robot's
  factsheet limits; a term whose limit is unknown (<= 0, i.e. the factsheet's -1) is dropped.
- `summarize`: the `mission_runs.summary_metrics` of a run from its legs.
- `identity` / `aggregate`: the per-leg-identity statistics of GET /api/v1/missions/{name}/legs.
"""
import math
import re
import statistics
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# The run-independent tail of a VDA node id we generate: "{prefix}-n{tree node}-s{sequence}".
# The prefix carries the run id, so two runs of one mission only share this tail.
_VDA_TAIL = re.compile(r"-n(\d+)(?:-s(\d+))?$")


def known(limit: Any) -> bool:
    """A factsheet limit the robot has reported (the objects use -1 for 'unknown')."""
    return isinstance(limit, (int, float)) and not isinstance(limit, bool) \
        and math.isfinite(limit) and limit > 0


def wrap_angle(angle: float) -> float:
    return (angle + math.pi) % (2 * math.pi) - math.pi


def expected_seconds(distance_m: Optional[float], heading_change_rad: Optional[float],
                     speed_max: Any, acceleration_max: Any, angular_speed_max: Any
                     ) -> Optional[float]:
    """Expected time of a leg: d / v + v / a + |dpsi| / w. Each term needs its inputs and its
    limit: a limit the robot has not reported (-1) or a missing distance / heading change
    drops that term. None when no term is left (nothing is known to estimate from)."""
    total, terms = 0.0, 0
    if known(speed_max) and distance_m is not None:
        total += max(0.0, distance_m) / speed_max
        terms += 1
    if known(speed_max) and known(acceleration_max):
        total += speed_max / acceleration_max
        terms += 1
    if known(angular_speed_max) and heading_change_rad is not None:
        total += abs(wrap_angle(heading_change_rad)) / angular_speed_max
        terms += 1
    return round(total, 3) if terms else None


def node_tail(vda_node: Optional[str]) -> Optional[str]:
    """'n<tree node>-s<seq>' of a VDA node id: the part that is the same in every run."""
    if not vda_node:
        return None
    match = _VDA_TAIL.search(vda_node)
    return match.group(0)[1:] if match else vda_node


def identity(leg: Mapping[str, Any]) -> Tuple[str, str, bool]:
    """(from, to, topomap) of a leg: the topomap node ids when it has them, else the
    run-independent tail of its VDA node ids. `topomap` says whether both ends are topomap
    nodes (then the identity is comparable across any route through the same nodes)."""
    ends = []
    for side in ("from", "to"):
        ends.append(leg.get(f"{side}_topomap_node") or node_tail(leg.get(f"{side}_vda_node"))
                    or "?")
    topomap = bool(leg.get("from_topomap_node")) and bool(leg.get("to_topomap_node"))
    return ends[0], ends[1], topomap


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Linear-interpolated percentile (q in [0, 100]) of a non-empty sequence."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * q / 100.0
    low = int(math.floor(pos))
    high = min(low + 1, len(ordered) - 1)
    return float(ordered[low] + (ordered[high] - ordered[low]) * (pos - low))


def _round(value: Optional[float], digits: int = 3) -> Optional[float]:
    return None if value is None else round(value, digits)


def aggregate(legs: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Group legs by identity: count, median and p90 duration, expected time, ratio
    (median / expected) and recoveries. Slowest median first."""
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for leg in legs:
        src, dst, topomap = identity(leg)
        group = groups.setdefault((src, dst), {"from": src, "to": dst, "topomap": True,
                                               "durations": [], "expected": [],
                                               "recoveries": 0, "recovery_s": 0.0,
                                               "blocks": 0, "runs": set()})
        group["topomap"] = group["topomap"] and topomap
        if leg.get("duration_s") is not None:
            group["durations"].append(float(leg["duration_s"]))
        if leg.get("expected_s") is not None:
            group["expected"].append(float(leg["expected_s"]))
        group["recoveries"] += int(leg.get("recoveries") or 0)
        group["recovery_s"] += float(leg.get("recovery_s") or 0.0)
        group["blocks"] += int(leg.get("blocks") or 0)
        group["runs"].add(str(leg.get("run_id")))
    items = []
    for group in groups.values():
        durations = group["durations"]
        median = statistics.median(durations) if durations else None
        expected = statistics.median(group["expected"]) if group["expected"] else None
        items.append({
            "from": group["from"], "to": group["to"], "topomap": group["topomap"],
            "count": len(durations), "runs": len(group["runs"]),
            "median_s": _round(median), "p90_s": _round(percentile(durations, 90)),
            "expected_s": _round(expected),
            "ratio": _round(median / expected, 3) if median is not None and expected else None,
            "recoveries": group["recoveries"], "recovery_s": _round(group["recovery_s"]),
            "blocks": group["blocks"],
        })
    items.sort(key=lambda g: (-(g["median_s"] if g["median_s"] is not None else -1.0),
                              g["from"], g["to"]))
    return items


def summarize(legs: Sequence[Mapping[str, Any]], run_duration_s: Optional[float],
              passes_completed: int = 0) -> Dict[str, Any]:
    """mission_runs.summary_metrics from the run's legs (any order).

    - distance_m: sum of planned_m (straight_m where a leg has no planned distance).
    - time_moving_s: leg time while the robot reported driving; time_stopped_s: the rest of
      the run (non-driving time inside legs, plus time outside any leg).
    - time_recovery_s / recovery_count / block_count: from the events tagged with the legs.
      Recovery time overlaps stopped time, it is not a third share of the run.
    - expected_s: sum of the legs' expected_s; actual_vs_expected: actual time of those same
      legs over that sum (None without any expected time).
    - pass_durations_s: per pass, first leg start to last leg end."""
    ordered = sorted(legs, key=lambda l: l.get("seq") or 0)
    leg_time = sum(float(l.get("duration_s") or 0.0) for l in ordered)
    leg_stopped = sum(min(float(l.get("stopped_s") or 0.0), float(l.get("duration_s") or 0.0))
                      for l in ordered)
    moving = leg_time - leg_stopped
    distance = sum(float(l["planned_m"] if l.get("planned_m") is not None else l["straight_m"])
                   for l in ordered if l.get("planned_m") is not None
                   or l.get("straight_m") is not None)
    with_expected = [l for l in ordered if l.get("expected_s") is not None]
    expected = sum(float(l["expected_s"]) for l in with_expected)
    actual_of_expected = sum(float(l.get("duration_s") or 0.0) for l in with_expected)
    passes: Dict[int, List[Mapping[str, Any]]] = {}
    for leg in ordered:
        passes.setdefault(int(leg.get("pass_index") or 0), []).append(leg)
    pass_durations = []
    for index in sorted(passes):
        group = passes[index]
        start = min(l["started_at"] for l in group)
        end = max(l["ended_at"] for l in group)
        pass_durations.append({"pass": index, "legs": len(group),
                               "duration_s": _round(max(0.0, (end - start).total_seconds()))})
    stopped = None
    if run_duration_s is not None:
        stopped = max(0.0, run_duration_s - moving)
    return {
        "leg_count": len(ordered),
        "passes_completed": passes_completed,
        "duration_s": _round(run_duration_s),
        "distance_m": _round(distance),
        "time_moving_s": _round(moving),
        "time_stopped_s": _round(stopped),
        "time_recovery_s": _round(sum(float(l.get("recovery_s") or 0.0) for l in ordered)),
        "recovery_count": sum(int(l.get("recoveries") or 0) for l in ordered),
        "block_count": sum(int(l.get("blocks") or 0) for l in ordered),
        "expected_s": _round(expected) if with_expected else None,
        "actual_vs_expected": (_round(actual_of_expected / expected, 3)
                               if with_expected and expected > 0 else None),
        "pass_durations_s": pass_durations,
    }
