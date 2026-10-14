"""How long the MAPPING of a map took, per map, per session and per square metre: GET /api/v1/maps/{map_id}/mapping-stats.

Contract and rules: docs/mapping_stats_2026-10-14.md (and docs/API_REFERENCE.md). Read only.

Time. The mapping sessions of the map (`map_sessions`, purpose `mapping`): wall = started_at .. ended_at (an open session up
to now), paused = the MAP.SESSION_PAUSED / _RESUMED intervals of the session (events; the open pause of `paused_at`),
active = wall - paused.

Space. The time between two consecutive nodes of a session (the topomap nodes the session laid down, capture time when
every node has one, else the cloud receipt time) is spread along the straight segment between the two nodes, over 0.1 m
cells like drive-stats (same sampler, same tile contract). Paused seconds are taken out of a pair; a pair that is longer
than MAPPING_STATS_MAX_GAP_S, a pose jump or a segment longer than DRIVE_STATS_LEG_MAX_M is not attributed. A session
with map-frame `robot_track_ts` rows (`map_session_id`) uses those rows instead of its nodes (never both).

Area. `mapped_area_m2` = known cells (free + blocked, not unknown) of the current fused occupancy x (0.05 m)^2, read from the
scene-graph store tile by tile (no engine); `scene_graph_area_m2` = the summed `area_m2` of the charts (free space the
planner can use) as a second figure.
"""
import asyncio
import collections
import datetime
import hashlib
import json
import logging
import math
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from fastapi import HTTPException

from packages import config
from packages.api import drive_stats as ds
from packages.api import fleet_reads, maps
from packages.api import session_trace as st_api
from packages.events.codes import EventCode
from packages.utils import map_sessions as ms
from packages.utils import session_trace as trace

logger = logging.getLogger("ApiDelegationService.mapping_stats")

CELL_M = ds.CELL_M
OCC_CELL_M = 0.05
CACHE = ds.ResultCache(config.DRIVE_STATS_CACHE_ENTRIES, config.DRIVE_STATS_CACHE_TTL_S)
GAP_CHUNK_M = ds.REGION_CHUNK_CELLS * CELL_M


# --- pure core -----------------------------------------------------------------------------------

def merge_intervals(spans: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    out: List[List[float]] = []
    for s, e in sorted(spans):
        if e <= s:
            continue
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def pause_intervals(events: Sequence[Tuple[str, float]], start: float, end: float,
                    paused_at: Optional[float]) -> List[Tuple[float, float]]:
    """The paused intervals of one session inside [start, end] from its (code, epoch s) pause/resume events and the
    open pause `paused_at` (a pause with no resume lasts until `end`; a resume with no pause is ignored)."""
    spans: List[Tuple[float, float]] = []
    opened: Optional[float] = None
    for code, ts in sorted(events, key=lambda e: e[1]):
        if code == EventCode.MAP_SESSION_PAUSED.value:
            opened = ts if opened is None else opened
        elif code == EventCode.MAP_SESSION_RESUMED.value and opened is not None:
            spans.append((opened, ts))
            opened = None
    if opened is None and paused_at is not None and not any(s <= paused_at < e for s, e in spans):
        opened = paused_at                         # paused now, the pause event is missing
    if opened is not None:
        spans.append((opened, end))
    clipped = [(max(s, start), min(e, end)) for s, e in spans]
    return merge_intervals(clipped)


def session_times(session: Dict[str, Any], events: Sequence[Tuple[str, float]], now: float) -> Dict[str, Any]:
    start = session["started_at"].timestamp()
    ended = session["ended_at"]
    end = ended.timestamp() if ended is not None else now
    end = max(end, start)
    paused_at = session["paused_at"].timestamp() if session.get("paused_at") is not None else None
    spans = pause_intervals(events, start, end, paused_at)
    paused = sum(e - s for s, e in spans)
    return {"start": start, "end": end, "open": ended is None, "spans": spans, "wall_s": end - start,
            "paused_s": paused, "active_s": max(0.0, end - start - paused)}


def node_times(nodes: List[Dict[str, Any]]) -> Tuple[List[Optional[float]], str]:
    """Epoch seconds of the ordered trace nodes on ONE clock (capture when every node has one, else receipt time)."""
    source = trace.timestamp_source(nodes)
    key = "capture" if source == trace.SOURCE_CAPTURE else "time"
    return [n[key].timestamp() if n.get(key) is not None else None for n in nodes], source


class SessionSegments:
    """Time-carrying movements of one source: arrays t0, t1 (epoch s), x0, y0, x1, y1."""

    def __init__(self, t0, t1, x0, y0, x1, y1):
        self.t0, self.t1, self.x0, self.y0, self.x1, self.y1 = t0, t1, x0, y0, x1, y1

    def __len__(self) -> int:
        return int(self.t0.size)


def node_segments(times: Sequence[Optional[float]], xs: Sequence[Optional[float]], ys: Sequence[Optional[float]],
                  spans: List[Tuple[float, float]], max_gap_s: float, max_speed: float, max_len_m: float
                  ) -> Tuple[SessionSegments, Dict[str, Any], np.ndarray]:
    """Segments between consecutive nodes. Returns (segments with the paused seconds taken out as `dt`, counts, dt)."""
    e = np.empty(0)
    counts = {"pairs": 0, "pairs_attributed": 0, "pairs_missing": 0, "pairs_gap": 0, "pairs_jump": 0,
              "pairs_long": 0, "pairs_reversed": 0, "gap_s": 0.0}
    n = len(times)
    if n < 2:
        return SessionSegments(e, e, e, e, e, e), counts, e
    t = np.array([np.nan if v is None else v for v in times], float)
    x = np.array([np.nan if v is None else v for v in xs], float)
    y = np.array([np.nan if v is None else v for v in ys], float)
    t0, t1 = t[:-1], t[1:]
    dist = np.hypot(x[1:] - x[:-1], y[1:] - y[:-1])
    dt = t1 - t0
    counts["pairs"] = n - 1
    missing = ~(np.isfinite(dt) & np.isfinite(dist))
    reversed_ = ~missing & (dt <= 0)
    paused = _overlap(t0, t1, spans)
    eff = dt - paused
    ok = ~missing & ~reversed_
    gap = ok & (eff > max_gap_s)
    ok2 = ok & ~gap & (eff > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        jump = ok2 & (dist / eff > max_speed)
    long_ = ok2 & ~jump & (dist > max_len_m)
    keep = ok2 & ~jump & ~long_
    counts.update(pairs_missing=int(missing.sum()), pairs_reversed=int(reversed_.sum()), pairs_gap=int(gap.sum()),
                  pairs_jump=int(jump.sum()), pairs_long=int(long_.sum()), pairs_attributed=int(keep.sum()),
                  gap_s=round(float(eff[gap].sum()), 1))
    i = np.nonzero(keep)[0]
    return SessionSegments(t0[i], t1[i], x[i], y[i], x[i + 1], y[i + 1]), counts, eff[i]


def _overlap(t0: np.ndarray, t1: np.ndarray, spans: List[Tuple[float, float]]) -> np.ndarray:
    """Seconds of each [t0, t1] inside the merged `spans`."""
    out = np.zeros(t0.size)
    if not spans:
        return out
    pts = np.array([v for s, e in spans for v in (s, e)])
    before = np.cumsum([0.0] + [e - s for s, e in spans[:-1]])
    vals = np.empty(pts.size)
    vals[0::2], vals[1::2] = before, before + np.array([e - s for s, e in spans])
    ok = np.isfinite(t0) & np.isfinite(t1)
    over = np.interp(t1[ok], pts, vals) - np.interp(t0[ok], pts, vals)
    out[ok] = np.clip(over, 0.0, np.maximum(t1[ok] - t0[ok], 0.0))
    return out


def track_session_segments(ts: np.ndarray, x: np.ndarray, y: np.ndarray, epoch: np.ndarray,
                           spans: List[Tuple[float, float]], p: ds.Params
                           ) -> Tuple[SessionSegments, Dict[str, Any], np.ndarray]:
    """Segments of one session's track rows (any order): drive-stats rules, the paused seconds taken out."""
    order = np.argsort(ts, kind="stable")
    ts, x, y, epoch = ts[order], x[order], y[order], epoch[order]
    run = np.zeros(ts.size, np.int64)
    seg, rejected = ds.track_segments(run, epoch, ts, x, y, p)
    paused = _overlap(seg.t0, seg.t1, spans)
    eff = (seg.t1 - seg.t0) - paused
    keep = eff > 0
    counts = {"rows": int(ts.size), "pairs_attributed": int(keep.sum()), "pairs_rejected": rejected}
    return (SessionSegments(seg.t0[keep], seg.t1[keep], seg.x0[keep], seg.y0[keep], seg.x1[keep], seg.y1[keep]),
            counts, eff[keep])


class Loaded:
    """What the database and ArangoDB gave, before any numpy."""

    def __init__(self) -> None:
        self.sessions: List[Dict[str, Any]] = []
        self.events: Dict[str, List[Tuple[str, float]]] = {}
        self.nodes: Dict[str, List[Dict[str, Any]]] = {}          # session id -> session_trace_nodes rows
        self.track: Dict[str, List[tuple]] = {}                   # session id -> (epoch s, x_map, y_map, run_epoch)
        self.now: float = 0.0


def compute(loaded: Loaded, p: ds.Params, max_gap_s: float) -> Dict[str, Any]:
    """Everything that is not area: per-session figures, the accumulated tiles and the node cells."""
    acc = ds.Accumulator(len(loaded.sessions))
    node_keys: List[np.ndarray] = []
    per: List[Dict[str, Any]] = []
    totals = collections.Counter()
    for code, s in enumerate(loaded.sessions):
        sid = str(s["session_id"])
        tm = session_times(s, loaded.events.get(sid, []), loaded.now)
        rows = loaded.nodes.get(sid, [])
        ordered = st_api.ordered_nodes(rows)
        times, tsource = node_times(ordered)
        xs = [trace._number(n.get("x")) for n in ordered]  # pylint: disable=protected-access
        ys = [trace._number(n.get("y")) for n in ordered]  # pylint: disable=protected-access
        have_pose = np.array([a is not None and b is not None for a, b in zip(xs, ys)], bool)
        if have_pose.any():
            px = np.array([xs[i] for i in np.nonzero(have_pose)[0]], float)
            py = np.array([ys[i] for i in np.nonzero(have_pose)[0]], float)
            node_keys.append(ds.cell_key(np.floor(px / CELL_M), np.floor(py / CELL_M)))
        images = sum(len(r.get("camera_stamps_ms") or []) for r in rows)
        trk = loaded.track.get(sid) or []
        src = "none"
        counts: Dict[str, Any] = {}
        if trk:
            src = "track"
            a = np.array([[r[0], r[1], r[2]] for r in trk], float)
            ep = np.array([hash(r[3]) % (1 << 40) for r in trk], np.int64)
            seg, counts, eff = track_session_segments(a[:, 0], a[:, 1], a[:, 2], ep, tm["spans"], p)
        elif rows:
            src = "nodes"
            seg, counts, eff = node_segments(times, xs, ys, tm["spans"], max_gap_s, p.max_speed, p.leg_max_m)
        else:
            seg, eff = SessionSegments(*(np.empty(0),) * 6), np.empty(0)
        if len(seg):
            z = np.zeros(len(seg))
            acc.add(np.full(len(seg), code, np.int64), seg.x0, seg.y0, seg.x1, seg.y1, eff, z, z)
        outside = sum(1 for t in times if t is not None and not (tm["start"] - 60 <= t <= tm["end"] + 60))
        no_time = sum(1 for t in times if t is None)
        attributed = float(eff.sum()) if len(seg) else 0.0
        entry = {"session_id": sid, "robot": s["robot_name"], "started_at": _iso(s["started_at"]),
                 "ended_at": _iso(s["ended_at"]), "open": tm["open"], "paused_now": s.get("paused_at") is not None,
                 "wall_s": round(tm["wall_s"], 1), "paused_s": round(tm["paused_s"], 1),
                 "active_s": round(tm["active_s"], 1), "attributed_s": round(attributed, 1),
                 "nodes": len(rows), "images": images, "node_count_recorded": s.get("node_count"),
                 "area_added_m2": None, "time_source": src, "timestamp_source": tsource,
                 "nodes_without_time": no_time, "nodes_outside_session": outside}
        if src == "nodes":
            entry["pairs"] = {k: v for k, v in counts.items() if k != "gap_s"}
            entry["gap_s"] = counts.get("gap_s", 0.0)
        elif src == "track":
            entry["pairs"] = counts
        per.append(entry)
        totals.update(wall=tm["wall_s"], paused=tm["paused_s"], active=tm["active_s"], attributed=attributed,
                      nodes=len(rows), images=images, no_time=no_time, outside=outside,
                      open=1 if tm["open"] else 0, track=1 if src == "track" else 0,
                      node_src=1 if src == "nodes" else 0, empty=1 if not rows and not trk else 0)
    tiles = acc.tiles()
    nodes_key = np.concatenate(node_keys) if node_keys else np.empty(0, np.int64)
    nk, nn = np.unique(nodes_key, return_counts=True)
    return {"per": per, "tiles": tiles, "node_key": nk, "node_n": nn, "totals": totals}


def merge_cells(tile_key: np.ndarray, seconds: np.ndarray, node_key: np.ndarray, node_n: np.ndarray
                ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Union of the cells with time and the cells with nodes: (keys, seconds, nodes)."""
    keys = np.union1d(tile_key, node_key)
    sec = np.zeros(keys.size)
    nod = np.zeros(keys.size, np.int64)
    if tile_key.size:
        sec[np.searchsorted(keys, tile_key)] = seconds
    if node_key.size:
        nod[np.searchsorted(keys, node_key)] = node_n
    return keys, sec, nod


def region_rows(ids: np.ndarray, names: List[str], cells: Dict[str, int], seconds: np.ndarray, nodes: np.ndarray
                ) -> List[Dict[str, Any]]:
    out = []
    for r in np.unique(ids[ids > 0]):
        m = ids == r
        name = names[int(r) - 1]
        sec = float(seconds[m].sum())
        c = cells.get(name, 0)
        area = c * CELL_M * CELL_M
        out.append({"key": name, "seconds": round(sec, 1), "cells": c, "area_m2": round(area, 2),
                    "seconds_per_m2": round(sec / area, 2) if area > 0 else None, "nodes": int(nodes[m].sum()),
                    "visited": int(np.count_nonzero(seconds[m] > 0))})
    out.sort(key=lambda d: d["key"])
    return out


def _iso(when: Any) -> Optional[str]:
    return when.isoformat() if when is not None else None


# --- data access ---------------------------------------------------------------------------------

async def fetch_sessions(store: Any, map_id: str, robot: Optional[str]) -> List[Dict[str, Any]]:
    rows = [s for s in await store.sessions(map_id) if ms.purpose_of(s) == ms.MAPPING]
    if robot:
        rows = [s for s in rows if s["robot_name"] == robot]
    return sorted(rows, key=lambda s: (s["started_at"], str(s["session_id"])))


async def fetch_events(cur: Any, session_ids: Sequence[str], t_min: datetime.datetime) -> Dict[str, List[Tuple[str, float]]]:
    if not session_ids:
        return {}
    await cur.execute(
        "SELECT payload->>'session_id', code, extract(epoch FROM ts)::float8 FROM fleet_events "
        "WHERE code IN ('MAP.SESSION_PAUSED', 'MAP.SESSION_RESUMED') AND ts >= %s "
        "AND payload->>'session_id' = ANY(%s)", (t_min, list(session_ids)))
    out: Dict[str, List[Tuple[str, float]]] = collections.defaultdict(list)
    for sid, code, ts in await cur.fetchall():
        out[str(sid)].append((code, float(ts)))
    return dict(out)


async def fetch_track(cur: Any, sessions: Sequence[Dict[str, Any]], now: datetime.datetime) -> Dict[str, List[tuple]]:
    if not sessions:
        return {}
    lo = min(s["started_at"] for s in sessions)
    hi = max((s["ended_at"] or now) for s in sessions) + datetime.timedelta(minutes=1)
    await cur.execute(
        "SELECT map_session_id, extract(epoch FROM ts)::float8, x_map, y_map, run_epoch FROM robot_track_ts "
        "WHERE ts >= %s AND ts < %s AND map_session_id = ANY(%s::uuid[]) AND x_map IS NOT NULL AND y_map IS NOT NULL "
        "ORDER BY ts LIMIT %s",
        (lo, hi, [str(s["session_id"]) for s in sessions], config.DRIVE_STATS_MAX_ROWS))
    out: Dict[str, List[tuple]] = collections.defaultdict(list)
    for sid, ts, x, y, ep in await cur.fetchall():
        out[str(sid)].append((ts, x, y, str(ep)))
    return dict(out)


async def occupancy_area(occupancy: Any, map_id: str) -> Tuple[Dict[str, Any], str]:
    """({version, tiles, free, blocked, bbox}, status "ok" | "none" | "unavailable"). Never raises."""
    if occupancy is None:
        return {}, "unavailable"
    try:
        return await occupancy.known_area(map_id), "ok"
    except HTTPException:
        return {}, "unavailable"
    except Exception as exc:  # pylint: disable=broad-except
        if type(exc).__name__ == "NotFound":
            return {}, "none"
        logger.warning("Mapping stats of %s: no occupancy area: %s: %s", map_id, type(exc).__name__, exc)
        return {}, "unavailable"


async def chart_area(reads: Any, map_id: str, bbox: Optional[List[float]]) -> Tuple[Optional[float], int, str]:
    """(summed area_m2 of the charts of the current graph version over `bbox`, chart count, status). Never raises."""
    if reads is None or not bbox:
        return None, 0, "none"
    from packages.scene_graph.api import BBox
    seen: Dict[str, float] = {}
    try:
        x = math.floor(bbox[0] / GAP_CHUNK_M)
        while x * GAP_CHUNK_M < bbox[2]:
            y = math.floor(bbox[1] / GAP_CHUNK_M)
            while y * GAP_CHUNK_M < bbox[3]:
                box = BBox(round(x * GAP_CHUNK_M, 6), round(y * GAP_CHUNK_M, 6),
                           round((x + 1) * GAP_CHUNK_M, 6), round((y + 1) * GAP_CHUNK_M, 6))
                view = await reads.view(map_id, box, 0, raster=False, parents=False)
                if view.get("available") is False:
                    return None, 0, "none"
                for ch in view.get("charts", []):
                    seen[ch["key"]] = float(ch.get("area_m2") if ch.get("area_m2") is not None
                                            else (ch.get("cells") or 0) * CELL_M * CELL_M)
                y += 1
            x += 1
    except HTTPException:
        return None, 0, "error"
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Mapping stats of %s: no chart area: %s", map_id, exc)
        return None, 0, "error"
    return round(sum(seen.values()), 2), len(seen), "ok"


def build_body(map_id: str, comp: Dict[str, Any], area: Dict[str, Any], area_status: str,
               chart_m2: Optional[float], charts: int, chart_status: str, regions: List[Dict[str, Any]],
               rstatus: str, version: Optional[int], origin: Tuple[int, int], keys: np.ndarray,
               seconds: np.ndarray, nodes: np.ndarray, window: Dict[str, Any]) -> Dict[str, Any]:
    t = comp["totals"]
    mapped = None
    if area_status == "ok":
        mapped = round((area["free"] + area["blocked"]) * OCC_CELL_M * OCC_CELL_M, 2)
    active = float(t["active"])

    def per_m2(a: Optional[float]) -> Optional[float]:
        return round(active / a, 2) if a else None

    spm = per_m2(mapped)
    spm_sg = per_m2(chart_m2)
    ax, ay = ds.key_cells(keys)
    rows = [[int(a), int(b), round(float(s), 2), int(n)] for a, b, s, n in zip(ax - origin[0], ay - origin[1], seconds, nodes)]
    headline = {
        "sessions": len(comp["per"]), "sessions_open": int(t["open"]), "wall_s": round(float(t["wall"]), 1),
        "paused_s": round(float(t["paused"]), 1), "active_s": round(active, 1),
        "active_h": round(active / 3600.0, 3),
        "mapped_area_m2": mapped, "free_area_m2": round(area["free"] * OCC_CELL_M ** 2, 2) if area_status == "ok" else None,
        "blocked_area_m2": round(area["blocked"] * OCC_CELL_M ** 2, 2) if area_status == "ok" else None,
        "scene_graph_area_m2": chart_m2, "seconds_per_m2": spm,
        "minutes_per_100m2": round(spm * 100 / 60.0, 1) if spm is not None else None,
        "seconds_per_m2_scene_graph": spm_sg,
        "minutes_per_100m2_scene_graph": round(spm_sg * 100 / 60.0, 1) if spm_sg is not None else None,
        "nodes": int(t["nodes"]), "images": int(t["images"]), "attributed_s": round(float(t["attributed"]), 1),
    }
    source = {"sessions_nodes": int(t["node_src"]), "sessions_track": int(t["track"]),
              "sessions_without_data": int(t["empty"]), "nodes_without_time": int(t["no_time"]),
              "nodes_outside_session": int(t["outside"]), "occupancy": area_status, "occupancy_version": area.get("version"),
              "occupancy_tiles": area.get("tiles"), "charts": charts, "charts_status": chart_status,
              "scene_graph": rstatus}
    return {"map_id": map_id, "graph_version": version, "cell_m": CELL_M, "origin": [round(origin[0] * CELL_M, 6),
            round(origin[1] * CELL_M, 6)], "step": CELL_M, "tile_fields": ["ix", "iy", "seconds", "nodes"],
            "headline": headline, "sessions": comp["per"], "tiles": rows, "regions": regions, "source": source,
            "window": window}


# --- the endpoint --------------------------------------------------------------------------------

async def mapping_stats(db: Any, graph: Any, reads: Any, occupancy: Any, map_id: str, robot: Optional[str] = None,
                        cache: Optional[ds.ResultCache] = None, now: Optional[datetime.datetime] = None
                        ) -> Tuple[bytes, str]:
    """(JSON body, ETag) of GET /api/v1/maps/{map_id}/mapping-stats. 404 unknown map."""
    cache = CACHE if cache is None else cache
    p = ds.Params()
    max_gap = config.MAPPING_STATS_MAX_GAP_S
    key = (map_id, robot or None)

    async def run() -> Tuple[bytes, str]:
        clock = now or datetime.datetime.now(datetime.timezone.utc)
        loaded = Loaded()
        loaded.now = clock.timestamp()
        try:
            async with maps.open_store(db, uuid.uuid4()) as store:
                if await store.get_map(map_id) is None:
                    raise HTTPException(404, f"Did not find \"map\" with name \"{map_id}\"")
                loaded.sessions = await fetch_sessions(store, map_id, robot)
        except maps._SCHEMA_ERRORS as exc:
            raise maps._undefined_table(exc) from exc
        if loaded.sessions:
            async with fleet_reads.read_cursor(db) as cur:
                ids = [str(s["session_id"]) for s in loaded.sessions]
                loaded.events = await fetch_events(cur, ids, min(s["started_at"] for s in loaded.sessions))
                loaded.track = await fetch_track(cur, loaded.sessions, clock)
            for s in loaded.sessions:
                sid = str(s["session_id"])
                loaded.nodes[sid] = await asyncio.to_thread(graph.session_trace_nodes, map_id, sid)
        comp = await asyncio.to_thread(compute, loaded, p, max_gap)
        area, area_status = await occupancy_area(occupancy, map_id)
        chart_m2, charts, chart_status = await chart_area(reads, map_id, area.get("bbox"))
        keys, seconds, nodes = merge_cells(comp["tiles"].key, comp["tiles"].sums[1], comp["node_key"], comp["node_n"])
        regions: List[Dict[str, Any]] = []
        version: Optional[int] = None
        if not keys.size:
            rstatus = "ok"
        elif reads is None:
            rstatus = "none"
        else:
            ids_, names, cells, version, rstatus = await ds.regions_of(reads, map_id, *ds.key_cells(keys))
            if ids_ is not None:
                regions = region_rows(ids_, names, cells, seconds, nodes)
        origin = ds.origin_cells(keys, rstatus == "ok" and version is not None)
        window = {"robot": robot or None, "max_gap_s": max_gap, "max_speed": p.max_speed, "max_segment_m": p.leg_max_m,
                  "now": clock.isoformat() if comp["totals"]["open"] else None}
        body = build_body(map_id, comp, area, area_status, chart_m2, charts, chart_status, regions, rstatus, version,
                          origin, keys, seconds, nodes, window)
        raw = json.dumps(body, separators=(",", ":")).encode()
        return raw, '"' + hashlib.sha1(raw).hexdigest()[:20] + '"'
    return await cache.get(key, run)
