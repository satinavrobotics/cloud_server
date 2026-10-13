"""What an engine needs to be resumed: the folded planes and the leaf evidence (the truth), plus the solve's own memory
(anchors, owner and round planes) so that one re-solve reproduces the same charts and keys. Everything else (F, clearance,
links, widths, evidence fractions, the hierarchy) is derived and is rebuilt, never stored."""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np


class StateMismatch(Exception):
    """a restored engine does not reproduce the stored state (key table or owner plane): the snapshot or the code that read it is wrong."""


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
    params: Dict[str, float]                       # robot_half_side, lattice, place_mult, sight_range
    cx0: int                                       # global full-resolution index of canvas cell (0, 0); even
    cy0: int
    V: np.ndarray                                  # u8 [H, W] value, 255 unknown
    T: np.ndarray                                  # i8 [H, W] capture time of the winning leaf, -1 none
    B: np.ndarray                                  # i8 [H, W] latest capture time reported blocked, -1 none
    chan: Dict[str, Tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)
    leaves: List[LeafRecord] = field(default_factory=list)
    anchors: List[Tuple[int, int]] = field(default_factory=list)      # (y, x) lattice cell, canvas image, chart order
    keys: List[Tuple[str, int]] = field(default_factory=list)         # chart keys (birth leaf, n), chart order
    plane: Optional[np.ndarray] = None             # i4 [H/2, W/2] chart index per lattice cell, -1 outside F
    dep: Optional[np.ndarray] = None               # bool [H/2, W/2] attached by cost, not seen from its anchor
    rnd: Optional[np.ndarray] = None               # i4 [H/2, W/2] round at which the cell was reached
    version: int = 0
    watermark: Tuple[int, str] = (-1, "")
