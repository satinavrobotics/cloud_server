"""Hierarchy of the charts as a derived view: nested nets of the anchors in the link metric, covering radius
rho_j = 2^j * (median link length); net points are anchors, so levels nest and a parent is the union of its children."""
import numpy as np
from scipy.sparse.csgraph import connected_components, dijkstra


def hierarchy(k, links):
    """links: dicts with a, b, length. Returns (levels, Da, ell); level 0 is every chart."""
    La = np.full((k, k), np.inf); np.fill_diagonal(La, 0)
    for l in links:
        La[l["a"], l["b"]] = La[l["b"], l["a"]] = min(La[l["a"], l["b"]], l["length"])
    Da = dijkstra(La, directed=False)
    ncomp = connected_components(np.isfinite(La) & (La > 0), directed=False)[0] if k else 0
    ell = float(np.median([l["length"] for l in links])) if links else 1.0
    levels = [dict(nodes=list(range(k)), parent=list(range(k)), rho=0.0)]
    cur = list(range(k)); j = 0
    while len(cur) > ncomp and j < 12:
        j += 1; rho = (2 ** j) * ell; unc = set(cur); centers = []
        while unc:
            best = max(sorted(unc), key=lambda c: (sum(1 for u in unc if Da[c, u] <= rho), -sum(Da[c, u] for u in cur if Da[c, u] <= rho)))
            centers.append(best); unc -= {u for u in cur if Da[best, u] <= rho}
        par = {u: min(centers, key=lambda c: (Da[u, c], c)) for u in cur}
        levels.append(dict(nodes=centers, parent=[par[p] for p in levels[-1]["parent"]], rho=rho)); cur = centers
    return levels, Da, ell
