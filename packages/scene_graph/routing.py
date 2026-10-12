"""Routing on the scene graph: the portal graph.

The anchors are the places; the seams between them are crossed at portals. A route from cell x in region A to cell
y in region B goes x -> portal -> ... -> portal -> y, with the cost inside each region taken from that region's own
cells. Portals are sampled along every seam (up to `per_seam` per connected stretch), so a long open seam does not
force a detour to one point. The cost is exact for the routes that cross each seam at a portal: an upper bound of
the true shortest path, tight when the seams are narrow (doors) and within the portal spacing when they are wide.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np
from scipy import ndimage
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from .builder import SceneGraph, _Region


class Router:
    def __init__(self, sg: SceneGraph, per_seam: int = 6):
        reg = sg.region()
        self.reg: _Region = reg
        self.own = reg.own[reg.fr, reg.fc] if reg is not None else np.zeros(0, int)
        self.ids = sorted(set(self.own[self.own >= 0].tolist())) if reg is not None else []
        self._sub: Dict[int, Tuple[np.ndarray, "csr_matrix"]] = {}
        for i in self.ids:
            S = np.nonzero(self.own == i)[0]
            self._sub[i] = (S, reg.g.subgraph(S).cost)
        self._build(per_seam)

    def _build(self, per_seam: int) -> None:
        reg, own = self.reg, self.own
        coo = reg.g.length.tocoo()
        m = (own[coo.row] >= 0) & (own[coo.col] >= 0) & (own[coo.row] < own[coo.col])
        u, v = coo.row[m], coo.col[m]
        cross: List[Tuple[int, int, float]] = []
        key = own[u].astype(np.int64) * 100003 + own[v]
        for kk in np.unique(key):
            sel = np.nonzero(key == kk)[0]
            mask = np.zeros(reg.shape, bool)
            mask[reg.rows[u[sel]], reg.cols[u[sel]]] = True
            lab, ncomp = ndimage.label(mask, structure=np.ones((3, 3)))
            comp = lab[reg.rows[u[sel]], reg.cols[u[sel]]]
            for c in range(1, ncomp + 1):
                e = sel[comp == c]
                xy = reg.g.xy[u[e]]
                axis = np.argmax(xy.max(0) - xy.min(0))
                order = e[np.argsort(xy[:, axis], kind="stable")]
                take = order[np.unique(np.linspace(0, len(order) - 1, min(per_seam, len(order))).astype(int))]
                for j in take:
                    step = reg.g.cost[u[j], v[j]]
                    cross.append((int(u[j]), int(v[j]), float(step)))
        self.cross = cross
        nodes = sorted({x for a, b, _ in cross for x in (a, b)})
        self.pidx = {n: k for k, n in enumerate(nodes)}
        P = len(nodes)
        rows, cols, vals = [], [], []
        for a, b, c in cross:
            rows += [self.pidx[a], self.pidx[b]]; cols += [self.pidx[b], self.pidx[a]]; vals += [c, c]
        self._rows_in: Dict[int, np.ndarray] = {}
        for i in self.ids:
            S, cost = self._sub[i]
            mine = [n for n in nodes if own[n] == i]
            if len(mine) < 2:
                continue
            loc = [int(np.searchsorted(S, n)) for n in mine]
            D = dijkstra(cost, directed=False, indices=loc)
            for a, na in enumerate(mine):
                for b, nb in enumerate(mine):
                    if a != b and np.isfinite(D[a, loc[b]]):
                        rows.append(self.pidx[na]); cols.append(self.pidx[nb]); vals.append(float(D[a, loc[b]]))
        self.graph = csr_matrix((vals, (rows, cols)), shape=(P, P)) if P else csr_matrix((0, 0))
        self.nodes = nodes

    def _to_portals(self, node: int) -> Dict[int, float]:
        """Cost from a cell's node to every portal node of its own region."""
        i = int(self.own[node])
        S, cost = self._sub[i]
        d = dijkstra(cost, directed=False, indices=int(np.searchsorted(S, node)))
        return {self.pidx[n]: float(d[np.searchsorted(S, n)]) for n in self.nodes
                if self.own[n] == i and np.isfinite(d[np.searchsorted(S, n)])}

    def cost(self, a: int, b: int) -> float:
        """Route cost between two nodes (coarse cells) through the portal graph; the in-region cost if same region."""
        ia, ib = int(self.own[a]), int(self.own[b])
        if ia < 0 or ib < 0:
            return float("inf")
        if ia == ib:
            S, cost = self._sub[ia]
            return float(dijkstra(cost, directed=False, indices=int(np.searchsorted(S, a)))[int(np.searchsorted(S, b))])
        pa, pb = self._to_portals(a), self._to_portals(b)
        if not pa or not pb or self.graph.shape[0] == 0:
            return float("inf")
        src = list(pa)
        D = dijkstra(self.graph, directed=False, indices=src)
        best = float("inf")
        for r, s in enumerate(src):
            for t, ct in pb.items():
                best = min(best, pa[s] + D[r, t] + ct)
        return best
