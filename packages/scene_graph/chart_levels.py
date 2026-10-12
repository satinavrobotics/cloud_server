"""Hierarchy of the charts as a derived view: nested nets of the anchors in the link metric, covering radius
rho_j = 2^j * (median link length); net points are anchors, so levels nest and a parent is the union of its children.

Sparse: a level only needs, per node, the nodes within its covering radius, so each level runs a Dijkstra truncated at
that radius from the nodes of the level (never an all-pairs matrix). The greedy and its tie rules are exactly the dense
definition (`tests/unit/scene_graph/test_levels.py` keeps the dense one as the reference): the centre of a step is the
uncovered node with most uncovered nodes within rho, then the smallest sum of distances to the level's nodes within rho
(summed in the level's node order), then the smallest node index; a node's parent is its nearest centre, ties by index."""
import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra

_CHUNK = 256


def hierarchy(k, links):
    """links: dicts with a, b, length. Returns (levels, None, ell); level 0 is every chart. (The second slot was the dense
    all-pairs matrix of the first implementation; nothing needs it and it is no longer built.)"""
    best = {}
    for l in links:
        a, b, w = int(l["a"]), int(l["b"]), float(l["length"])
        if a == b or not (w > 0 and np.isfinite(w)): continue              # a zero length is "no edge" in the dense definition
        key = (a, b) if a < b else (b, a)
        if key not in best or w < best[key]: best[key] = w
    if best:
        ab = np.array(list(best.keys()), dtype=np.int64); wv = np.array(list(best.values()), dtype=np.float64)
        G = csr_matrix((np.concatenate([wv, wv]), (np.concatenate([ab[:, 0], ab[:, 1]]), np.concatenate([ab[:, 1], ab[:, 0]]))),
                       shape=(k, k))
    else:
        G = csr_matrix((k, k), dtype=np.float64)
    ncomp = connected_components(G, directed=False)[0] if k else 0
    ell = float(np.median([l["length"] for l in links])) if links else 1.0
    levels = [dict(nodes=list(range(k)), parent=list(range(k)), rho=0.0)]
    cur = list(range(k)); j = 0
    while len(cur) > ncomp and j < 12:
        j += 1; rho = (2 ** j) * ell
        centers, par = _level(G, cur, rho)
        levels.append(dict(nodes=centers, parent=[par[p] for p in levels[-1]["parent"]], rho=rho)); cur = centers
    return levels, None, ell


def _level(G, cur, rho):
    """One net level: (centres in selection order, {node of cur -> its centre})."""
    n = len(cur)
    cur_arr = np.asarray(cur, dtype=np.int64)
    lim = rho * (1 + 1e-9)                                   # exact values below; the filter `<= rho` is applied on them
    pos_all, d_all = [], []                                  # per row of cur: positions (in cur order) and distances <= lim
    for s in range(0, n, _CHUNK):
        D = dijkstra(G, directed=False, indices=cur_arr[s:s + _CHUNK], limit=lim)[:, cur_arr]
        for r in range(D.shape[0]):
            m = np.nonzero(np.isfinite(D[r]))[0]
            pos_all.append(m); d_all.append(D[r, m])
    # ball of every row: positions with distance <= rho; sum of those distances in cur order (sequential, as the definition)
    ball = []; sums = np.empty(n)
    for ci in range(n):
        keep = d_all[ci] <= rho
        ball.append(pos_all[ci][keep])
        dd = d_all[ci][keep]
        sums[ci] = np.cumsum(dd)[-1] if dd.size else 0.0
    rows = np.concatenate([np.full(len(b), ci, dtype=np.int64) for ci, b in enumerate(ball)]) if n else np.zeros(0, np.int64)
    cols = np.concatenate(ball) if n else np.zeros(0, np.int64)
    memT = csr_matrix((np.ones(len(rows), np.int8), (cols, rows)), shape=(n, n))   # row u: the rows whose ball holds u
    cnt = np.fromiter((len(b) for b in ball), dtype=np.int64, count=n)
    unc = np.ones(n, bool)
    centers_pos = []
    ids = cur_arr
    while unc.any():
        cand = np.nonzero(unc)[0]
        mc = cnt[cand].max(); c1 = cand[cnt[cand] == mc]
        ms = sums[c1].min(); c2 = c1[sums[c1] == ms]
        b = int(c2[np.argmin(ids[c2])])
        centers_pos.append(b)
        rem = ball[b][unc[ball[b]]]
        if rem.size:
            unc[rem] = False
            for u in rem:
                cnt[memT.indices[memT.indptr[u]:memT.indptr[u + 1]]] -= 1
    cpos = np.array(centers_pos, dtype=np.int64)
    centers = [int(ids[p]) for p in centers_pos]
    is_c = np.zeros(n, bool); is_c[cpos] = True
    par = {}
    for ci in range(n):
        p, d = pos_all[ci], d_all[ci]
        m = is_c[p]
        if m.any():
            pc, dc = p[m], d[m]
            dmin = dc.min(); cands = pc[dc == dmin]
            par[int(ids[ci])] = int(ids[cands[np.argmin(ids[cands])]])
        else:                                                # not within the guard band: ask the whole graph
            dfull = dijkstra(G, directed=False, indices=int(ids[ci]))[ids[cpos]]
            dmin = dfull.min(); cands = np.nonzero(dfull == dmin)[0]
            par[int(ids[ci])] = int(min(centers[c] for c in cands))
    return centers, par
