"""The map as a sparse lattice of cells.

Three planes per cell: the costmap value (255 unknown), the owning anchor (-1 none) and a visited bit.
The owner plane is the partition: one value per cell, so a cell cannot belong to two mcmaps by construction,
and an anchor's mcmap is the set of cells it owns. Nothing is stored for cells never observed.
"""
from __future__ import annotations

from typing import Dict, Tuple

import numpy as np

UNKNOWN = 255
NO_OWNER = -1
CHUNK = 64


class CellMap:
    def __init__(self) -> None:
        self._val: Dict[Tuple[int, int], np.ndarray] = {}
        self._own: Dict[Tuple[int, int], np.ndarray] = {}
        self._vis: Dict[Tuple[int, int], np.ndarray] = {}
        self._seen: set = set()          # chunks holding at least one observed cell

    # ---- chunk plumbing ------------------------------------------------------------------------------------
    def _chunk(self, key):
        if key not in self._val:
            self._val[key] = np.full((CHUNK, CHUNK), UNKNOWN, np.uint8)
            self._own[key] = np.full((CHUNK, CHUNK), NO_OWNER, np.int32)
            self._vis[key] = np.zeros((CHUNK, CHUNK), bool)
        return key

    @staticmethod
    def _spans(i0, j0, i1, j1):
        """Yield (chunk key, array slice, chunk slice) for the half-open cell box [i0,i1) x [j0,j1)."""
        for cj in range(j0 // CHUNK, (j1 - 1) // CHUNK + 1):
            for ci in range(i0 // CHUNK, (i1 - 1) // CHUNK + 1):
                a0, a1 = max(i0, ci * CHUNK), min(i1, (ci + 1) * CHUNK)
                b0, b1 = max(j0, cj * CHUNK), min(j1, (cj + 1) * CHUNK)
                yield ((ci, cj),
                       (slice(b0 - j0, b1 - j0), slice(a0 - i0, a1 - i0)),
                       (slice(b0 - cj * CHUNK, b1 - cj * CHUNK), slice(a0 - ci * CHUNK, a1 - ci * CHUNK)))

    # ---- access --------------------------------------------------------------------------------------------
    def crop(self, i0: int, j0: int, i1: int, j1: int):
        """Dense (val, own, vis) arrays for the cell box [i0,i1) x [j0,j1); rows are j, columns are i."""
        shape = (j1 - j0, i1 - i0)
        val = np.full(shape, UNKNOWN, np.uint8)
        own = np.full(shape, NO_OWNER, np.int32)
        vis = np.zeros(shape, bool)
        for key, a, c in self._spans(i0, j0, i1, j1):
            if key in self._val:
                val[a], own[a], vis[a] = self._val[key][c], self._own[key][c], self._vis[key][c]
        return val, own, vis

    def write_own(self, i0: int, j0: int, own: np.ndarray) -> None:
        h, w = own.shape
        for key, a, c in self._spans(i0, j0, i0 + w, j0 + h):
            if key in self._val:            # never create a chunk for cells nobody observed
                self._own[key][c] = own[a]

    def fold(self, i0: int, j0: int, window: np.ndarray):
        """Newest observation wins; unknown never overwrites. Returns (new cells, class flips)."""
        h, w = window.shape
        new = flips = 0
        for key, a, c in self._spans(i0, j0, i0 + w, j0 + h):
            self._chunk(key)
            old, obs = self._val[key][c], window[a]
            seen = obs != UNKNOWN
            was = old != UNKNOWN
            new += int((seen & ~was).sum())
            flips += int((seen & was & (_cls(old) != _cls(obs))).sum())
            self._val[key][c] = np.where(seen, obs, old)
            if seen.any():
                self._seen.add(key)
        return new, flips

    def clear_body(self, ci: int, cj: int, radius_cells: int) -> None:
        """The robot stood here: the cells under its body are free, whatever the costmap said."""
        r = radius_cells
        yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
        disk = (xx * xx + yy * yy) <= r * r
        for key, a, c in self._spans(ci - r, cj - r, ci + r + 1, cj + r + 1):
            self._chunk(key)
            self._val[key][c][disk[a]] = 0
            self._seen.add(key)

    def clear_path(self, i0: int, j0: int, i1: int, j1: int, radius_cells: int) -> None:
        """The robot drove from (i0, j0) to (i1, j1): everything under its body on the way is free."""
        n = max(abs(i1 - i0), abs(j1 - j0), 1)
        r = radius_cells
        yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
        disk = (xx * xx + yy * yy) <= r * r
        for t in range(n + 1):
            ci = i0 + int(round((i1 - i0) * t / n))
            cj = j0 + int(round((j1 - j0) * t / n))
            for key, a, c in self._spans(ci - r, cj - r, ci + r + 1, cj + r + 1):
                self._chunk(key)
                self._val[key][c][disk[a]] = 0
                self._seen.add(key)

    def mark_visited(self, ci: int, cj: int) -> None:
        """A leaf (topo node) is this cell."""
        key, a, c = next(self._spans(ci, cj, ci + 1, cj + 1))
        self._chunk(key)
        self._vis[key][c] = True

    def known_box(self):
        """(i0, j0, i1, j1) of the chunks holding observed cells, or None."""
        if not self._seen:
            return None
        cs = np.array(list(self._seen))
        return (int(cs[:, 0].min()) * CHUNK, int(cs[:, 1].min()) * CHUNK,
                (int(cs[:, 0].max()) + 1) * CHUNK, (int(cs[:, 1].max()) + 1) * CHUNK)


def _cls(v: np.ndarray) -> np.ndarray:
    """free / soft / lethal, so noise in the soft values is not counted as a change."""
    return np.where(v >= 100, 2, np.where(v > 0, 1, 0))
