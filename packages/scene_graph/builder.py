"""Incremental construction of the level-0 scene graph from topo-node costmaps.

A leaf is a cell (where the robot stood) plus its costmap window. It is folded into the cell map once and is not
queried again. The cell map is partitioned into regions (partition.py); the cells of a region are its anchor's
mcmap, so the mcmaps slice the map. An anchor is a cell of its region's wide places that is nearest, in traversal
cost, to all the other cells of the region.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy import ndimage
from scipy.sparse.csgraph import connected_components, dijkstra

from .cellmap import CellMap, NO_OWNER
from .graph import bottleneck, coarsen, fields, lattice_graph
from .params import Params
from .partition import settle


@dataclass
class Leaf:
    key: str
    x: float                # robot position in the map frame (m)
    y: float
    i0: int                 # lattice index of window[0, 0]
    j0: int
    window: np.ndarray      # (rows = y, cols = x) uint8: 0..100 occupancy, 255 unknown
    session: Optional[str] = None   # leaves of one session follow each other along the robot's path


@dataclass
class Anchor:
    id: int
    ci: int                 # lattice cell of the anchor
    cj: int
    n: int = 0              # cells in its mcmap


@dataclass
class Link:
    a: int
    b: int
    cost: float             # shortest traversal cost anchor a -> portal -> anchor b inside the two mcmaps
    length: float           # geometric length of that path
    width: float            # 2 x the smallest clearance on it (m)
    portal: Tuple[float, float]


class _Region:
    """A dense crop of the cell map with its fields and the coarse graph the partition runs on."""

    def __init__(self, cells: CellMap, box, p: Params):
        self.p = p
        self.i0, self.j0, self.i1, self.j1 = box
        self.val, self.own, self.vis = cells.crop(*box)
        self.f = fields(self.val, p)
        self.k = p.block
        self.res_c = self.k * p.res
        self.c = coarsen(self.f, self.k)
        self.g, self.idx = lattice_graph(self.c.nav, self.c.cost, self.res_c)
        self._keep_reachable()
        self.rows, self.cols = np.nonzero(self.c.nav)            # block coordinates of the nodes
        self.shape = self.c.nav.shape
        self.h = self.c.clear[self.rows, self.cols]
        self.fr = self.c.best_r[self.rows, self.cols]            # best fine cell of each node, within the crop
        self.fc = self.c.best_c[self.rows, self.cols]

    def _keep_reachable(self) -> None:
        """Drop navigable pockets the robot has no way into: a component of the navigable space with no visited cell
        is not known to be reachable, and gets no anchor."""
        h, w = self.vis.shape
        k = self.k
        hc, wc = self.c.nav.shape
        pad = np.zeros((hc * k, wc * k), bool)
        pad[:h, :w] = self.vis
        vis_c = pad.reshape(hc, k, wc, k).any(axis=(1, 3)) & self.c.nav
        ncomp, comp = connected_components(self.g.length, directed=False)
        rows, cols = np.nonzero(self.c.nav)
        good = np.zeros(ncomp, bool)
        good[comp[self.idx[vis_c]]] = True
        if good.all():
            return
        self.c.nav[rows[~good[comp]], cols[~good[comp]]] = False
        self.g, self.idx = lattice_graph(self.c.nav, self.c.cost, self.res_c)

    @property
    def coarse_params(self) -> Params:
        return replace(self.p, res=self.res_c, budget=max(1, self.p.budget // (self.k * self.k)))

    def node(self, ci: int, cj: int) -> int:
        r, c = (cj - self.j0) // self.k, (ci - self.i0) // self.k
        if 0 <= r < self.shape[0] and 0 <= c < self.shape[1]:
            return int(self.idx[r, c])
        return -1

    def cell(self, node: int) -> Tuple[int, int]:
        return int(self.fc[node]) + self.i0, int(self.fr[node]) + self.j0


class SceneGraph:
    def __init__(self, params: Params = Params()):
        self.p = params
        self.cells = CellMap()
        self.anchors: Dict[int, Anchor] = {}
        self._next = 0
        self._last: Dict[str, Tuple[int, int]] = {}   # last leaf cell of each session
        self.log: List[dict] = []   # one record per leaf, for the simulator and the metrics

    # ------------------------------------------------------------------------------------------------ public
    def add_leaf(self, leaf: Leaf) -> dict:
        p = self.p
        new, flips = self.cells.fold(leaf.i0, leaf.j0, leaf.window)
        ci, cj = math.floor(leaf.x / p.res), math.floor(leaf.y / p.res)
        r = max(1, round(p.robot_radius / p.res))
        prev = self._last.get(leaf.session) if leaf.session is not None else None
        if prev is not None and math.hypot(ci - prev[0], cj - prev[1]) * p.res <= p.drive_gap_m:
            self.cells.clear_path(prev[0], prev[1], ci, cj, r)      # the body swept the path between the two leaves
        else:
            self.cells.clear_body(ci, cj, r)
        self._last[leaf.session] = (ci, cj)
        self.cells.mark_visited(ci, cj)
        before = {a.id: (a.ci, a.cj) for a in self.anchors.values()}
        self._rebuild()
        rec = dict(key=leaf.key, cell=(ci, cj), new_cells=new, flips=flips, anchors=len(self.anchors),
                   promoted=[i for i in self.anchors if i not in before],
                   removed=[i for i in before if i not in self.anchors],
                   moved=[i for i in self.anchors if i in before and before[i] != (self.anchors[i].ci, self.anchors[i].cj)])
        self.log.append(rec)
        return rec

    def owner_at(self, ci: int, cj: int) -> int:
        return int(self.cells.crop(ci, cj, ci + 1, cj + 1)[1][0, 0])

    def region(self) -> Optional[_Region]:
        """A dense crop of everything known, with a margin."""
        box = self.cells.known_box()
        if box is None:
            return None
        m = int(math.ceil(self.p.margin_m / self.p.res))
        return _Region(self.cells, (box[0] - m, box[1] - m, box[2] + m, box[3] + m), self.p)

    # ------------------------------------------------------------------------------------------------ rebuild
    def _rebuild(self) -> None:
        reg = self.region()
        if reg is None or reg.g.n == 0:
            return
        prior = reg.own[reg.fr, reg.fc]
        label = settle(reg.g, reg.rows, reg.cols, reg.shape, reg.h, reg.coarse_params, prior, self._next)
        ids = sorted(int(i) for i in np.unique(label))
        self._next = max([self._next] + [i + 1 for i in ids])
        previous, self.anchors = self.anchors, {}
        for aid in ids:
            nodes = np.nonzero(label == aid)[0]
            node = self._centre(reg, nodes, int(nodes[np.argmax(reg.h[nodes])]), previous.get(aid))
            ci, cj = reg.cell(node)
            self.anchors[aid] = Anchor(aid, ci, cj, 0)
        self._write_back(reg, label)

    def _centre(self, reg: _Region, S: np.ndarray, peak: int, old: Optional[Anchor]) -> int:
        """Where the anchor of region S sits: among its wide places (clearance >= gamma x the peak), the node nearest
        to all the others in traversal cost (the medoid). The anchor moves only if that is better by `slack`."""
        p = self.p
        h = reg.h[S]
        core = np.nonzero(h >= p.gamma * h.max())[0]
        sub = reg.g.subgraph(S)
        cur = None
        if old is not None:
            node = reg.node(old.ci, old.cj)
            at = int(np.searchsorted(S, node))
            if node >= 0 and at < len(S) and S[at] == node:
                cur = at
        centroid = sub.xy[core].mean(0)
        snap = int(core[np.argmin(np.hypot(*(sub.xy[core] - centroid).T))])
        cand = set(core[np.linspace(0, len(core) - 1, min(len(core), 40)).astype(int)].tolist())
        cand |= {snap, int(np.searchsorted(S, peak))}
        if cur is not None:
            cand.add(cur)
        cand = sorted(cand)
        d = dijkstra(sub.cost, directed=False, indices=cand)
        mean = np.where(np.isfinite(d), d, np.nan)
        mean = np.nanmean(mean, axis=1)
        best = int(np.argmin(mean))
        if cur is not None and mean[best] > mean[cand.index(cur)] * (1.0 - p.slack):
            return int(S[cur])
        return int(S[cand[best]])

    def _write_back(self, reg: _Region, label: np.ndarray) -> None:
        p, k = self.p, reg.k
        coarse = np.full(reg.shape, NO_OWNER, np.int64)
        coarse[reg.rows, reg.cols] = label
        h, w = reg.val.shape
        own = np.repeat(np.repeat(coarse, k, 0), k, 1)[:h, :w].astype(np.int32)
        own[~reg.f.nav] = NO_OWNER
        # observed blocked cells join the region of the nearest navigable cell
        known = (reg.val != 255) & ~reg.f.nav
        r, c = reg.f.near
        dist = np.hypot(r - np.arange(h)[:, None], c - np.arange(w)[None, :]) * p.res
        take = known & (dist <= p.knn_attach_m)
        own[take] = own[r[take], c[take]]
        self.cells.write_own(reg.i0, reg.j0, own)
        counts = np.bincount(own[own >= 0], minlength=max(self.anchors) + 1)
        for aid, a in self.anchors.items():
            a.n = int(counts[aid])

    # ------------------------------------------------------------------------------------------------ links
    def links(self) -> List[Link]:
        """Edges of the anchor graph: one per connected stretch of shared boundary between two mcmaps."""
        reg = self.region()
        if reg is None or len(self.anchors) < 2:
            return []
        own_n = reg.own[reg.fr, reg.fc]
        field = {}
        for aid, a in self.anchors.items():
            S = np.nonzero(own_n == aid)[0]
            root = reg.node(a.ci, a.cj)
            if len(S) == 0 or root < 0:
                continue
            sub = reg.g.subgraph(S)
            r = int(np.searchsorted(S, root))
            dc, pr = dijkstra(sub.cost, directed=False, indices=r, return_predecessors=True)
            dl = dijkstra(sub.length, directed=False, indices=r)
            field[aid] = (S, dc, dl, bottleneck(pr, reg.h[S], r))
        coo = reg.g.length.tocoo()
        a, b = own_n[coo.row], own_n[coo.col]
        m = (a >= 0) & (b >= 0) & (a < b)
        out: List[Link] = []
        if not m.any():
            return out
        u, v, ln = coo.row[m], coo.col[m], coo.data[m]
        key = a[m].astype(np.int64) * 100003 + b[m]
        for kk in np.unique(key):
            sel = key == kk
            ia, ib = int(a[m][sel][0]), int(b[m][sel][0])
            if ia not in field or ib not in field:
                continue
            uu, vv, ll = u[sel], v[sel], ln[sel]
            mask = np.zeros(reg.shape, bool)
            mask[reg.rows[uu], reg.cols[uu]] = True
            lab, ncomp = ndimage.label(mask, structure=np.ones((3, 3)))
            Sa, dca, dla, ba = field[ia]
            Sb, dcb, dlb, bb = field[ib]
            pa = np.full(reg.g.n, -1, np.int64); pa[Sa] = np.arange(len(Sa))
            pb = np.full(reg.g.n, -1, np.int64); pb[Sb] = np.arange(len(Sb))
            tot = dca[pa[uu]] + dcb[pb[vv]] + ll
            comp = lab[reg.rows[uu], reg.cols[uu]]
            for c in range(1, ncomp + 1):
                e = np.nonzero(comp == c)[0]
                if len(e) == 0:
                    continue
                j = e[int(np.argmin(tot[e]))]
                width = 2.0 * min(ba[pa[uu[j]]], bb[pb[vv[j]]])
                length = dla[pa[uu[j]]] + dlb[pb[vv[j]]] + ll[j]
                ci, cj = reg.cell(int(uu[j]))
                out.append(Link(ia, ib, float(tot[j]), float(length), float(width),
                                ((ci + 0.5) * self.p.res, (cj + 0.5) * self.p.res)))
        return out
