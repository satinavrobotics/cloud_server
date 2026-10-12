"""Hierarchy: the same rules, applied to the anchors.

Level 0 is the partition of the cells (builder.py). Level k+1 groups the anchors of level k with the same two
tests, now on the anchor graph:
  bottleneck  the passage between two groups (half the width of their widest link) is at least gamma_k x the
              narrower of their own clearances; gamma_k = gamma / 2^k, so each level accepts narrower passages,
  chart       any two anchors of the union are within tau of straight from each other by a path of links
              (up to the free detour).
A group's anchor is its member nearest, in link cost, to the others; its mcmap is the union of its members' mcmaps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from .builder import Link, SceneGraph


@dataclass
class Node:
    id: int                  # the anchor id of the member that represents the group
    x: float
    y: float
    cells: int               # cells of the union of the member mcmaps
    clearance: float         # widest place inside (m)
    members: List[int]       # ids of the nodes one level down


@dataclass
class Level:
    index: int
    nodes: List[Node] = field(default_factory=list)
    links: List[Link] = field(default_factory=list)


def _graph(ids: List[int], links: List[Link]):
    k = {a: i for i, a in enumerate(ids)}
    n = len(ids)
    rows, cols, ln, co = [], [], [], []
    for L in links:
        if L.a in k and L.b in k and L.a != L.b:
            rows += [k[L.a], k[L.b]]; cols += [k[L.b], k[L.a]]; ln += [L.length] * 2; co += [L.cost] * 2
    mk = lambda d: csr_matrix((d, (rows, cols)), shape=(n, n))
    return k, mk(ln), mk(co)


def coarsen(nodes: List[Node], links: List[Link], gamma: float, tau: float, free_m: float, budget: int,
            hysteresis: float = 0.0) -> Tuple[List[List[int]], List[Link]]:
    """Group the nodes of one level. Returns (groups as lists of node ids, links among them)."""
    ids = [n.id for n in nodes]
    k, length, cost = _graph(ids, links)
    xy = np.array([[n.x, n.y] for n in nodes])
    peak = {n.id: n.clearance for n in nodes}
    cells = {n.id: n.cells for n in nodes}
    group = {n.id: [n.id] for n in nodes}
    owner = {n.id: n.id for n in nodes}
    width: Dict[Tuple[int, int], float] = {}
    for L in links:
        key = (min(L.a, L.b), max(L.a, L.b))
        width[key] = max(width.get(key, 0.0), L.width / 2.0)

    def dil(members: List[int]) -> float:
        idx = [k[m] for m in members]
        if len(idx) < 2:
            return 1.0
        sub = length[idx][:, idx]
        d = dijkstra(sub, directed=False)
        e = np.hypot(xy[idx, 0][:, None] - xy[idx, 0][None, :], xy[idx, 1][:, None] - xy[idx, 1][None, :])
        r = np.maximum(d - free_m, 0.0) / (e + 1e-9 + 1.0)
        return float(np.where(np.isfinite(r), r, np.inf).max())

    changed = True
    while changed:
        changed = False
        pairs: Dict[Tuple[int, int], float] = {}
        for (a, b), s in width.items():
            ga, gb = owner[a], owner[b]
            if ga != gb:
                key = (min(ga, gb), max(ga, gb))
                pairs[key] = max(pairs.get(key, 0.0), s)
        for (ga, gb), s in sorted(pairs.items(), key=lambda kv: -kv[1]):
            lo = min(peak[ga], peak[gb])
            if s < gamma * (1.0 + hysteresis) * lo or cells[ga] + cells[gb] > budget:
                continue
            if dil(group[ga] + group[gb]) > tau * (1.0 - hysteresis):
                continue
            keep, drop = (ga, gb) if cells[ga] >= cells[gb] else (gb, ga)
            group[keep] += group.pop(drop)
            for m in group[keep]:
                owner[m] = keep
            cells[keep] += cells.pop(drop)
            peak[keep] = max(peak[keep], peak.pop(drop))
            changed = True
            break
    groups = [sorted(g) for g in group.values()]
    return groups, links


def build_levels(sg: SceneGraph, max_levels: int = 4) -> List[Level]:
    """Level 0 from the scene graph, then coarser levels until the grouping stops changing."""
    p = sg.p
    reg = sg.region()
    if reg is None or not sg.anchors:
        return []
    own = reg.own[reg.fr, reg.fc]
    nodes0 = []
    for aid, a in sg.anchors.items():
        S = own == aid
        res = p.res
        nodes0.append(Node(aid, (a.ci + 0.5) * res, (a.cj + 0.5) * res, a.n, float(reg.h[S].max()) if S.any() else 0.0, [aid]))
    links = sg.links()
    levels = [Level(0, nodes0, links)]
    for k in range(1, max_levels + 1):
        cur = levels[-1]
        if len(cur.nodes) < 2:
            break
        groups, _ = coarsen(cur.nodes, cur.links, p.gamma / (2 ** k), p.tau, p.free_detour_m, p.budget)
        if len(groups) == len(cur.nodes):
            continue                      # nothing joins at this strictness: relax further
        ids = [n.id for n in cur.nodes]
        kidx, length, cost = _graph(ids, cur.links)
        by_id = {n.id: n for n in cur.nodes}
        new_nodes, parent_of = [], {}
        for g in groups:
            idx = [kidx[m] for m in g]
            if len(g) == 1:
                rep = g[0]
            else:
                D = dijkstra(cost[idx][:, idx], directed=False)
                mean = np.where(np.isfinite(D), D, np.nan)
                rep = g[int(np.nanargmin(np.nanmean(mean, axis=1)))]
            total = sum(by_id[m].cells for m in g)
            new_nodes.append(Node(rep, by_id[rep].x, by_id[rep].y, total, max(by_id[m].clearance for m in g), g))
            for m in g:
                parent_of[m] = rep
        new_links: Dict[Tuple[int, int], Link] = {}
        for L in cur.links:
            pa, pb = parent_of[L.a], parent_of[L.b]
            if pa == pb:
                continue
            key = (min(pa, pb), max(pa, pb))
            if key not in new_links or L.cost < new_links[key].cost:
                new_links[key] = Link(key[0], key[1], L.cost, L.length, L.width, L.portal)
        levels.append(Level(k, new_nodes, list(new_links.values())))
    return levels
