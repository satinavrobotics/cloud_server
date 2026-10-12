"""Invariants and summary numbers of a scene graph, computed from the cell map alone."""
from __future__ import annotations

from typing import Dict

import numpy as np
from scipy.sparse.csgraph import dijkstra

from .builder import SceneGraph
from .partition import dilation


def invariants(sg: SceneGraph) -> Dict[str, float]:
    """Counts of violations (all should be 0) and shape numbers, from the cell map alone."""
    p = sg.p
    reg = sg.region()
    if reg is None:
        return {}
    own = reg.own[reg.fr, reg.fc]
    vis = reg.vis[reg.fr, reg.fc]
    out: Dict[str, float] = dict(nodes=int(len(own)), owned=int((own >= 0).sum()), unowned=int((own < 0).sum()),
                                 anchors=len(sg.anchors), visited_unowned=int((vis & (own < 0)).sum()))
    bad_root = bad_conn = over_budget = 0
    dil, eccs, offc, sizes = [], [], [], []
    cp = reg.coarse_params
    for aid, a in sg.anchors.items():
        S = np.nonzero(own == aid)[0]
        root = reg.node(a.ci, a.cj)
        if len(S) == 0 or root < 0 or own[root] != aid:
            bad_root += 1
            continue
        sub = reg.g.subgraph(S)
        r = int(np.searchsorted(S, root))
        d = dijkstra(sub.length, directed=False, indices=r)
        bad_conn += int((~np.isfinite(d)).sum())
        over_budget += int(len(S) > cp.budget)
        dil.append(round(dilation(reg.g, reg.shape, reg.rows, reg.cols, S, reg.res_c, p.free_detour_m), 2))
        eccs.append(round(float(d[np.isfinite(d)].max()), 2))
        offc.append(round(float(np.hypot(*(sub.xy[r] - sub.xy.mean(0)))), 2))
        sizes.append(a.n)
    out.update(bad_root=bad_root, disconnected_nodes=bad_conn, over_budget=over_budget,
               dilation=dil, ecc_m=eccs, anchor_to_centroid_m=offc, cells=sizes,
               max_dilation=max(dil) if dil else 1.0)
    return out
