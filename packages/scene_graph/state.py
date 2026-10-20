"""What an engine needs to be resumed: the folded planes and the leaf evidence (the truth), plus the owner plane and the key
table so that the rebuilt partition can be checked cell for cell and given its keys. Everything else (F, the partition's
anchors, links, widths, evidence fractions, the hierarchy) is derived and is rebuilt, never stored."""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np


class StateMismatch(Exception):
    """a restored engine does not reproduce the stored state (key table or owner plane): the snapshot or the code that read it is wrong."""


class TimePlane:
    """Read-only int64 view of a time plane the engine keeps as int32 offsets from an epoch (-1 = never stays -1).
    Slicing returns the original int64 values, so a tile reader (`store._slice`) sees exactly what a full int64 plane holds,
    without the engine widening the whole canvas. `np.asarray(plane)` widens all of it."""
    dtype = np.dtype(np.int64)
    ndim = 2

    def __init__(self, raw: np.ndarray, epoch: int):
        self.raw, self.epoch = raw, int(epoch)

    @property
    def shape(self):
        return self.raw.shape

    @property
    def size(self):
        return self.raw.size

    @property
    def nbytes(self):
        return self.raw.size * 8

    def __len__(self):
        return len(self.raw)

    def __getitem__(self, key):
        r = self.raw[key]
        if np.ndim(r) == 0:
            return int(r) + self.epoch if r >= 0 else -1
        return widen_time(r, self.epoch)

    def __array__(self, dtype=None, copy=None):
        a = widen_time(self.raw, self.epoch)
        return a if dtype is None else a.astype(dtype, copy=False)


def widen_time(r: np.ndarray, epoch: int) -> np.ndarray:
    """int32 offsets -> the int64 stamps they stand for (negative = never -> -1)"""
    out = r.astype(np.int64)
    out += epoch
    out[r < 0] = -1
    return out


@dataclass
class LeafRecord:
    key: str
    session: str
    t: int                                         # capture time, ms
    x: float
    y: float
    r_d: float
    view: Optional[Tuple[float, float, float, float, float]] = None   # camera x, y, yaw, fov, range


@dataclass
class EngineState:
    params: Dict[str, float]                       # robot_half_side, lattice, chart_radius
    cx0: int                                       # global full-resolution index of canvas cell (0, 0); even
    cy0: int
    V: np.ndarray                                  # u8 [H, W] value, 255 unknown
    T: np.ndarray                                  # i8 [H, W] capture time of the winning leaf, -1 none
    B: np.ndarray                                  # i8 [H, W] latest capture time reported blocked, -1 none
    chan: Dict[str, Tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)
    Fn: Optional[np.ndarray] = None                # i8 [H, W] latest FREE time from within near range; only with near_range_m
    Bn: Optional[np.ndarray] = None                # i8 [H, W] latest LETHAL time from within near range (the held test); only with near_range_m
    Hold: Optional[np.ndarray] = None              # bool [H, W] cells the rule holds blocked after the away-from-walls filter; only with
                                                   # near_min_clearance_m (a derived plane, stored so tile readers need no component pass)
    Val: Optional[np.ndarray] = None               # bool [H/2, W/2] validity per lattice cell; only with near_range_m. A derived plane
                                                   # (engine `_build_F`), stored so tile readers need no close-evidence arithmetic
    leaves: List[LeafRecord] = field(default_factory=list)
    keys: List[Tuple[str, int]] = field(default_factory=list)         # chart keys (birth leaf, n), chart order
    plane: Optional[np.ndarray] = None             # i4 [H/2, W/2] chart index per lattice cell, -1 outside F
    version: int = 0
    watermark: Tuple[int, str] = (-1, "")
