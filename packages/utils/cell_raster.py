"""The 0.1 m cell rasteriser shared by the drive statistics and the per-run cell summaries.

Pure numpy, no I/O, no FastAPI. Moved unchanged out of packages/api/drive_stats.py (which re-exports every name
here, so its imports and tests keep working); `block_cells` is new (docs/region_stats_2026-10-27.md 6.1).

Contract:
* A movement is a straight segment between two consecutive pose rows of one run and one frame group
  (`track_segments`: dt in (0, max_gap_s], speed <= max_speed), or a leg (`leg_movements`: leaf node to leaf
  node). It is sampled every CELL_M / SAMPLE_DIV along the segment, each sample carrying an equal share of the
  segment's length, time, recovery seconds and stopped seconds, and summed per absolute cell
  (ix, iy) = floor(x / CELL_M), floor(y / CELL_M) (the scene graph's lattice / owner raster cell).
* `Accumulator` sums per cell and keeps the distinct (cell, run) pairs; `Tiles` is its result: sorted cell
  keys, a 4 x N array of sums in the order (len, time, recovery, stopped), the run count per cell and the pairs.
* `block_cells` places point events (blocks) on the segments by time.
"""
import math
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from packages import config

CELL_M = 0.1                  # = the scene graph's lattice step (the owner raster cell)
SAMPLE_DIV = 4                # samples per cell along a segment
MAX_SAMPLES_PER_SEGMENT = 4000
CHUNK_SEGMENTS = 40_000       # segments rasterised per numpy round (bounds memory)
_OFF = 1 << 30                # cell keys: (ix + _OFF) << 32 | (iy + _OFF)


class Params:
    """The movement rules of one request."""

    def __init__(self, min_speed: Optional[float] = None, max_gap_s: Optional[float] = None,
                 max_speed: Optional[float] = None, leg_max_m: Optional[float] = None) -> None:
        self.min_speed = config.DRIVE_STATS_MIN_SPEED_MS if min_speed is None else float(min_speed)
        self.max_gap_s = config.DRIVE_STATS_MAX_GAP_S if max_gap_s is None else float(max_gap_s)
        self.max_speed = config.DRIVE_STATS_MAX_SPEED_MS if max_speed is None else float(max_speed)
        self.leg_max_m = config.DRIVE_STATS_LEG_MAX_M if leg_max_m is None else float(leg_max_m)


def cell_key(ix: np.ndarray, iy: np.ndarray) -> np.ndarray:
    return ((ix.astype(np.int64) + _OFF) << 32) | (iy.astype(np.int64) + _OFF)


def key_cells(key: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    return (key >> 32) - _OFF, (key & 0xFFFFFFFF) - _OFF


class Segments:
    """Movements: run code, start/end time (epoch s), start/end position (m, map frame)."""

    def __init__(self, run, t0, t1, x0, y0, x1, y1):
        self.run, self.t0, self.t1 = run, t0, t1
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1

    def __len__(self) -> int:
        return int(self.run.size)


def track_segments(run: np.ndarray, group: np.ndarray, ts: np.ndarray, x: np.ndarray, y: np.ndarray,
                   p: Params) -> Tuple[Segments, int]:
    """Segments between consecutive rows of the same run and group (session + run epoch), the rows sorted by
    (run, ts). Returns (segments, rejected pairs): a pair is rejected for dt <= 0, dt > max_gap_s or a speed
    above max_speed (a pose jump)."""
    if run.size < 2:
        e = np.empty(0)
        return Segments(np.empty(0, np.int64), e, e, e, e, e, e), 0
    same = (run[:-1] == run[1:]) & (group[:-1] == group[1:])
    dt = ts[1:] - ts[:-1]
    dx, dy = x[1:] - x[:-1], y[1:] - y[:-1]
    dist = np.hypot(dx, dy)
    finite = np.isfinite(dist)
    with np.errstate(divide="ignore", invalid="ignore"):
        fast = dist / dt > p.max_speed
    ok = same & finite & (dt > 0) & (dt <= p.max_gap_s) & ~fast
    rejected = int(np.count_nonzero(same)) - int(np.count_nonzero(ok))
    i = np.nonzero(ok)[0]
    return Segments(run[i], ts[i], ts[i + 1], x[i], y[i], x[i + 1], y[i + 1]), rejected


def recovery_overlap(seg: Segments, intervals: Dict[int, List[Tuple[float, float]]]) -> Tuple[np.ndarray, float]:
    """Seconds of each segment inside its run's recovery intervals, and the recovery seconds of the intervals
    that overlapped no segment (a pose gap)."""
    rec = np.zeros(len(seg))
    attributed = 0.0
    total = 0.0
    for run, spans in intervals.items():
        merged: List[List[float]] = []
        for s, e in sorted(spans):
            if e <= s:
                continue
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        if not merged:
            continue
        total += sum(e - s for s, e in merged)
        sel = np.nonzero(seg.run == run)[0]
        if not sel.size:
            continue
        pts = np.array([v for s, e in merged for v in (s, e)])
        # the value at s_i is the recovery time before the interval, at e_i the time after it
        before = np.cumsum([0.0] + [e - s for s, e in merged[:-1]])
        vals = np.empty(pts.size)
        vals[0::2], vals[1::2] = before, before + np.array([e - s for s, e in merged])
        over = np.interp(seg.t1[sel], pts, vals) - np.interp(seg.t0[sel], pts, vals)
        rec[sel] = np.clip(over, 0.0, seg.t1[sel] - seg.t0[sel])
        attributed += float(rec[sel].sum())
    return rec, max(0.0, total - attributed)


class Accumulator:
    """Sums per cell and the distinct (cell, run) pairs, built chunk by chunk."""

    def __init__(self, n_runs: int) -> None:
        self.n_runs = max(1, n_runs)
        self.keys: List[np.ndarray] = []
        self.sums: List[np.ndarray] = []         # rows: len, time, recovery, stopped
        self.pair_cell: List[np.ndarray] = []
        self.pair_run: List[np.ndarray] = []

    def add(self, run: np.ndarray, x0, y0, x1, y1, time_s: np.ndarray, rec_s: np.ndarray,
            stopped_s: np.ndarray, step: float = CELL_M) -> None:
        """One movement per element; its length is |p1 - p0|, spread evenly along the segment."""
        for lo in range(0, run.size, CHUNK_SEGMENTS):
            sl = slice(lo, lo + CHUNK_SEGMENTS)
            self._add_chunk(run[sl], x0[sl], y0[sl], x1[sl], y1[sl], time_s[sl], rec_s[sl], stopped_s[sl], step)

    def _add_chunk(self, run, x0, y0, x1, y1, time_s, rec_s, stopped_s, step) -> None:
        if not run.size:
            return
        length = np.hypot(x1 - x0, y1 - y0)
        nsub = np.clip(np.ceil(length / (step / SAMPLE_DIV)), 1, MAX_SAMPLES_PER_SEGMENT).astype(np.int64)
        total = int(nsub.sum())
        idx = np.repeat(np.arange(run.size), nsub)
        first = np.cumsum(nsub) - nsub
        k = np.arange(total) - np.repeat(first, nsub)
        frac = (k + 0.5) / nsub[idx]
        px = x0[idx] + frac * (x1 - x0)[idx]
        py = y0[idx] + frac * (y1 - y0)[idx]
        key = cell_key(np.floor(px / step), np.floor(py / step))
        uniq, inv = np.unique(key, return_inverse=True)
        w = 1.0 / nsub[idx]
        sums = np.stack([np.bincount(inv, weights=(w * length[idx]), minlength=uniq.size),
                         np.bincount(inv, weights=(w * time_s[idx]), minlength=uniq.size),
                         np.bincount(inv, weights=(w * rec_s[idx]), minlength=uniq.size),
                         np.bincount(inv, weights=(w * stopped_s[idx]), minlength=uniq.size)])
        self.keys.append(uniq)
        self.sums.append(sums)
        pk = np.unique(inv.astype(np.int64) * self.n_runs + run[idx].astype(np.int64))
        self.pair_cell.append(uniq[pk // self.n_runs])
        self.pair_run.append(pk % self.n_runs)

    def merged_with(self, other: "Accumulator") -> "Accumulator":
        """Both accumulators over disjoint runs (a run is never in both): sums and run counts add."""
        out = Accumulator(max(self.n_runs, other.n_runs))
        for name in ("keys", "sums", "pair_cell", "pair_run"):
            setattr(out, name, getattr(self, name) + getattr(other, name))
        return out

    def tiles(self) -> "Tiles":
        if not self.keys:
            return Tiles(np.empty(0, np.int64), np.empty((4, 0)), np.empty(0, np.int64),
                         np.empty(0, np.int64), np.empty(0, np.int64))
        key = np.concatenate(self.keys)
        uniq, inv = np.unique(key, return_inverse=True)
        sums = np.stack([np.bincount(inv, weights=np.concatenate([s[r] for s in self.sums]), minlength=uniq.size)
                         for r in range(4)])
        pc, pr = np.concatenate(self.pair_cell), np.concatenate(self.pair_run)
        pi = np.searchsorted(uniq, pc)
        pair = np.unique(pi.astype(np.int64) * self.n_runs + pr)
        n = np.bincount(pair // self.n_runs, minlength=uniq.size)
        return Tiles(uniq, sums, n, (pair // self.n_runs), (pair % self.n_runs))


class Tiles:
    """Sorted cell keys, the 4 x N sums (len, time, recovery, stopped), N run counts, and the distinct
    (tile index, run) pairs."""

    def __init__(self, key, sums, n, pair_tile, pair_run) -> None:
        self.key, self.sums, self.n, self.pair_tile, self.pair_run = key, sums, n, pair_tile, pair_run

    def __len__(self) -> int:
        return int(self.key.size)


def leg_movements(legs: Sequence[Dict[str, Any]], pos: Callable[[str], Optional[Tuple[float, float]]],
                  p: Params) -> Tuple[Dict[str, np.ndarray], int]:
    """Legs as movements; a leg whose node positions are unknown, whose duration is not positive or whose
    straight length exceeds leg_max_m is skipped (returned count)."""
    rows, skipped = [], 0
    for leg in legs:
        a, b = pos(leg["from"]), pos(leg["to"])
        dur = leg["duration_s"]
        if a is None or b is None or not (dur and dur > 0) or math.hypot(b[0] - a[0], b[1] - a[1]) > p.leg_max_m:
            skipped += 1
            continue
        stopped = min(max(float(leg.get("stopped_s") or 0.0), 0.0), dur)
        rec = min(max(float(leg.get("recovery_s") or 0.0), 0.0), dur)
        rows.append((leg["run"], a[0], a[1], b[0], b[1], float(dur), rec, stopped))
    if not rows:
        return {}, skipped
    arr = np.array(rows, dtype=float)
    return {"run": arr[:, 0].astype(np.int64), "x0": arr[:, 1], "y0": arr[:, 2], "x1": arr[:, 3], "y1": arr[:, 4],
            "time": arr[:, 5], "rec": arr[:, 6], "stopped": arr[:, 7]}, skipped


# --- block events --------------------------------------------------------------------------------

def block_cells(seg: Segments, run: np.ndarray, ts: np.ndarray, max_gap_s: float
                ) -> Tuple[np.ndarray, int]:
    """Cell keys of point events (blocks) placed on the movements, one key per placed event, and the number of
    events that found no place. `run`/`ts` are the events' run code and epoch s; `seg` must be sorted by
    (run, t0) as `track_segments` returns it.

    An event inside a segment's [t0, t1] sits at the position interpolated at its time. One in a gap between
    two segments of its run sits at the nearest segment end (the previous one's end or the next one's start) if
    that is within `max_gap_s`; otherwise (no segment of the run, or too far) it is not placed."""
    keys: List[int] = []
    unplaced = 0
    for r, t in zip(np.asarray(run).tolist(), np.asarray(ts, dtype=float).tolist()):
        lo, hi = np.searchsorted(seg.run, r, "left"), np.searchsorted(seg.run, r, "right")
        pos = None
        if hi > lo:
            t0, t1 = seg.t0[lo:hi], seg.t1[lo:hi]
            i = int(np.searchsorted(t0, t, "right")) - 1       # last segment starting at or before t
            if i >= 0 and t <= t1[i]:
                f = (t - t0[i]) / (t1[i] - t0[i]) if t1[i] > t0[i] else 0.0
                j = lo + i
                pos = (seg.x0[j] + f * (seg.x1[j] - seg.x0[j]), seg.y0[j] + f * (seg.y1[j] - seg.y0[j]))
            else:
                best = max_gap_s
                if i >= 0 and t - t1[i] <= best:
                    best, pos = t - t1[i], (seg.x1[lo + i], seg.y1[lo + i])
                if i + 1 < t0.size and t0[i + 1] - t <= best:
                    pos = (seg.x0[lo + i + 1], seg.y0[lo + i + 1])
        if pos is None or not (math.isfinite(pos[0]) and math.isfinite(pos[1])):
            unplaced += 1
            continue
        keys.append(int(cell_key(np.array([math.floor(pos[0] / CELL_M)]), np.array([math.floor(pos[1] / CELL_M)]))[0]))
    return np.array(keys, np.int64), unplaced


def leg_block_cells(events: Sequence[Tuple[Optional[float], Optional[int]]], legs: Sequence[Dict[str, Any]]
                    ) -> Tuple[np.ndarray, int]:
    """The leg-source counterpart of `block_cells`: `events` are (epoch s, leg seq or None); `legs` the legs
    that became movements: {seq, t0, t1 (epoch s, may be None), a, b (start / end node position)}. An event with
    a known leg seq sits at that leg's end node; otherwise at the position interpolated along the straight leg
    whose [t0, t1] contains its time. Returns (cell keys, unplaced count)."""
    by_seq = {l["seq"]: l for l in legs}
    keys: List[int] = []
    unplaced = 0
    for t, seq in events:
        pos = None
        if seq is not None and seq in by_seq:
            pos = by_seq[seq]["b"]
        elif t is not None:
            for l in legs:
                if l.get("t0") is not None and l.get("t1") is not None and l["t0"] <= t <= l["t1"]:
                    f = (t - l["t0"]) / (l["t1"] - l["t0"]) if l["t1"] > l["t0"] else 1.0
                    pos = (l["a"][0] + f * (l["b"][0] - l["a"][0]), l["a"][1] + f * (l["b"][1] - l["a"][1]))
                    break
        if pos is None:
            unplaced += 1
            continue
        keys.append(int(cell_key(np.array([math.floor(pos[0] / CELL_M)]), np.array([math.floor(pos[1] / CELL_M)]))[0]))
    return np.array(keys, np.int64), unplaced
