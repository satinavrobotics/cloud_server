"""Cost fields and the graph the partition runs on.

The partition works on a coarse lattice whose cell is as large as the robot radius: features smaller than the robot
do not matter, and no step of the coarse lattice can cross a wall (opposite sides of a wall are at least twice the
robot radius apart). Nodes are coarse cells, or anchors at the levels above, so everything here is a generic
weighted graph with node positions.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from .params import Params


@dataclass
class Graph:
    """Undirected weighted graph with node positions in metres.

    length: geometric edge length (the detour bound is measured in it)
    cost:   traversal cost of the edge (centres and routes are measured in it)
    """
    xy: np.ndarray          # (n, 2) metres
    length: csr_matrix
    cost: csr_matrix

    @property
    def n(self) -> int:
        return len(self.xy)

    def subgraph(self, nodes: np.ndarray) -> "Graph":
        s = np.asarray(nodes)
        return Graph(self.xy[s], self.length[s][:, s], self.cost[s][:, s])


@dataclass
class Fields:
    """Per-cell fields of a dense crop."""
    nav: np.ndarray         # navigable: known, not lethal, robot fits
    clear: np.ndarray       # distance to the nearest blocked cell (m); unknown counts as blocked
    cost: np.ndarray        # cost of traversing the cell, >= 1 (inf where not navigable)
    near: tuple             # (row index, col index) of the nearest navigable cell, for blocked cells


def fields(val: np.ndarray, p: Params) -> Fields:
    known = val != 255
    free = known & (val < p.v_block)
    clear = ndimage.distance_transform_edt(free) * p.res          # distance to the nearest non-free cell
    nav = free & (clear >= p.robot_radius)
    soft = 1.0 + p.soft_weight * np.where(known, val, 0).clip(0, 99) / 100.0
    near_wall = p.clear_weight * np.exp(-p.clear_decay * np.maximum(clear - p.robot_radius, 0.0))
    cost = np.where(nav, soft + near_wall, np.inf)
    near = ndimage.distance_transform_edt(~nav, return_distances=False, return_indices=True)
    return Fields(nav, clear, cost, (near[0], near[1]))


_STEPS = ((0, 1), (1, 0), (1, 1), (1, -1))


def lattice_graph(nav: np.ndarray, cost: np.ndarray, res: float):
    """Graph over the navigable cells of a crop (8-connected). Returns (Graph, node index per cell)."""
    rows, cols = np.nonzero(nav)
    n = len(rows)
    idx = np.full(nav.shape, -1, np.int64)
    idx[rows, cols] = np.arange(n)
    ii, jj, ll = [], [], []
    h, w = nav.shape
    for dr, dc in _STEPS:
        r2, c2 = rows + dr, cols + dc
        ok = (r2 >= 0) & (r2 < h) & (c2 >= 0) & (c2 < w)
        j = np.full(n, -1, np.int64)
        j[ok] = idx[r2[ok], c2[ok]]
        m = j >= 0
        ii.append(np.nonzero(m)[0]); jj.append(j[m]); ll.append(np.full(int(m.sum()), np.hypot(dr, dc) * res))
    i = np.concatenate(ii); j = np.concatenate(jj); l = np.concatenate(ll)
    c = l * 0.5 * (cost[rows[i], cols[i]] + cost[rows[j], cols[j]])
    mk = lambda d: csr_matrix((np.concatenate([d, d]), (np.concatenate([i, j]), np.concatenate([j, i]))), shape=(n, n))
    xy = np.stack([cols, rows], 1).astype(float) * res
    return Graph(xy, mk(l), mk(c)), idx


def bottleneck(pred: np.ndarray, c: np.ndarray, root: int) -> np.ndarray:
    """Smallest value of c on the tree path from the root to each node (pointer doubling, O(n log n))."""
    p = pred.copy()
    p[p < 0] = root
    p[root] = root
    b = c.copy()
    for _ in range(int(np.ceil(np.log2(max(len(c), 2)))) + 1):
        b = np.minimum(b, b[p])
        p = p[p]
    return b


@dataclass
class Coarse:
    """The navigable cells pooled into blocks of k x k cells."""
    k: int
    nav: np.ndarray         # (Hc, Wc) block holds a navigable cell
    clear: np.ndarray       # best clearance in the block (m)
    cost: np.ndarray        # mean traversal cost of its navigable cells
    best_r: np.ndarray      # fine row (within the crop) of the block's best-clearance navigable cell
    best_c: np.ndarray


def coarsen(f: Fields, k: int) -> Coarse:
    h, w = f.nav.shape
    hc, wc = -(-h // k), -(-w // k)
    def pad(a, fill):
        out = np.full((hc * k, wc * k), fill, a.dtype)
        out[:h, :w] = a
        return out
    nav = pad(f.nav, False)
    clear = np.where(nav, pad(f.clear, 0.0), -1.0)
    cost = np.where(nav, pad(np.where(f.nav, f.cost, 0.0), 0.0), 0.0)
    blk = lambda a: a.reshape(hc, k, wc, k).transpose(0, 2, 1, 3).reshape(hc, wc, k * k)
    cl, cs, nv = blk(clear), blk(cost), blk(nav)
    arg = cl.argmax(-1)
    rows = (np.arange(hc)[:, None] * k) + arg // k
    cols = (np.arange(wc)[None, :] * k) + arg % k
    n = nv.sum(-1)
    return Coarse(k, n > 0, np.maximum(cl.max(-1), 0.0), np.where(n > 0, cs.sum(-1) / np.maximum(n, 1), np.inf), rows, cols)


__all__ = ["Graph", "Fields", "Coarse", "fields", "coarsen", "lattice_graph", "bottleneck", "dijkstra"]
