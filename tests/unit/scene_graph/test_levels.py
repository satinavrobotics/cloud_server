"""The sparse hierarchy equals the dense definition (kept here as the reference) on random link graphs."""
import time

import numpy as np
import pytest
from scipy.sparse.csgraph import connected_components, dijkstra

from packages.scene_graph.chart_levels import hierarchy

pytestmark = pytest.mark.unit


def dense_hierarchy(k, links):
    """the first implementation, verbatim: all-pairs matrix, per-step recount"""
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


def _random_links(rng, k):
    kind = rng.integers(3)
    links = []
    if kind == 0:                                            # integer lengths: many exact ties
        m = int(rng.integers(max(1, k - 1), 3 * k + 1))
        for _ in range(m):
            a, b = rng.integers(k, size=2)
            if a != b: links.append(dict(a=int(a), b=int(b), length=float(rng.integers(1, 4))))
    elif kind == 1:                                          # geometric
        P = rng.random((k, 2)) * 10
        for a in range(k):
            d = np.hypot(*(P - P[a]).T)
            for b in np.argsort(d)[1:4]:
                links.append(dict(a=a, b=int(b), length=float(d[b]) + 0.1))
    else:                                                    # chain pieces, parallel links, disconnected parts
        for a in range(k - 1):
            if rng.random() < 0.8: links.append(dict(a=a, b=a + 1, length=float(rng.choice([0.5, 1.0, 1.5, 1.0]))))
        for _ in range(k // 4):
            a = int(rng.integers(k)); links.append(dict(a=a, b=min(k - 1, a + 1), length=float(rng.random() + 0.5)))
    return links


def test_sparse_hierarchy_equals_the_dense_definition_on_random_graphs():
    rng = np.random.default_rng(7)
    n = 0
    for trial in range(200):
        k = int(rng.choice([1, 2, 3, 5, 12, 40, 90, 160, 300]))
        links = _random_links(rng, k)
        a, _, ella = dense_hierarchy(k, links)
        b, _, ellb = hierarchy(k, links)
        assert ella == ellb and len(a) == len(b), (trial, k)
        for la, lb in zip(a, b):
            assert la["nodes"] == lb["nodes"] and la["parent"] == lb["parent"] and la["rho"] == lb["rho"], (trial, k)
        n += 1
    assert n == 200


def test_edge_cases():
    assert hierarchy(0, [])[0][0]["nodes"] == []
    assert hierarchy(3, [])[0] == dense_hierarchy(3, [])[0]
    zero = [dict(a=0, b=1, length=0.0), dict(a=1, b=2, length=1.0)]            # a zero length is no edge in the definition
    assert hierarchy(3, zero)[0] == dense_hierarchy(3, zero)[0]


@pytest.mark.parametrize("k", [500, 1500, 3000])
def test_scaling_is_not_cubic(k):
    side = int(np.ceil(np.sqrt(k)))
    links = []
    for i in range(k):
        r, c = divmod(i, side)
        if c + 1 < side and i + 1 < k: links.append(dict(a=i, b=i + 1, length=0.8 + 0.01 * (i % 7)))
        if i + side < k: links.append(dict(a=i, b=i + side, length=0.9 + 0.01 * (i % 5)))
    t = time.time(); lv, _, _ = hierarchy(k, links); dt = time.time() - t
    assert lv[-1]["nodes"] and dt < (3.0 if k <= 1500 else 20.0), dt
