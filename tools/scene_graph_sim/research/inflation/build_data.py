import json, pickle, base64, math, urllib.request
import numpy as np
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra, connected_components
raw = pickle.load(open("raw.pkl", "rb"))
B = "http://localhost:8000"
out = {}
for mid, d in raw.items():
    V, gx0, gy0, view = d["V"], d["gx0"], d["gy0"], d["view"]
    rj = view["raster"]
    meta = {k: v for k, v in rj.items() if k != "keys"}
    print(mid, "raster meta:", meta)
    r = urllib.request.urlopen(f"{B}/api/v1/maps/{mid}/scene-graph/raster?bbox={d['bbox']}&level=0&version={view['version']}&plane=owner", timeout=60)
    body, hdr = r.read(), dict(r.headers)
    hh = {k: v for k, v in hdr.items() if k.lower().startswith("x-raster")}
    print("  headers:", hh)
    shape = [int(x) for x in (hh.get("X-Raster-Shape") or hh.get("x-raster-shape")).replace("x", ",").split(",")]
    origin = [float(x) for x in (hh.get("X-Raster-Origin") or hh.get("x-raster-origin")).split(",")]
    step = float(hh.get("X-Raster-Step") or hh.get("x-raster-step"))
    rows, cols = shape
    own = np.frombuffer(body, "<u2").reshape(rows, cols).copy()
    keys = rj["keys"]
    # lattice <-> full-res alignment: lattice cell (r, c) has global index gy = round(oy/step)+r, gx = round(ox/step)+c
    lgx0, lgy0 = int(round(origin[0] / step)), int(round(origin[1] / step))
    free = (V != 255) & (V < 100)
    dt = ndimage.distance_transform_edt(free) * 0.05
    H, W = V.shape
    occ = np.zeros((rows, cols), np.uint8); clr = np.zeros((rows, cols), np.uint8)
    for rr in range(rows):
        fy = 2 * (lgy0 + rr) - gy0
        for cc in range(cols):
            fx = 2 * (lgx0 + cc) - gx0
            if 0 <= fy < H - 1 and 0 <= fx < W - 1:
                blk = V[fy:fy + 2, fx:fx + 2]
                if (blk != 255).any():
                    occ[rr, cc] = 2 if ((blk >= 100) & (blk != 255)).any() else 1
                clr[rr, cc] = min(255, int(round(dt[fy, fx] * 100)))
    F = own > 0
    print("  F cells", int(F.sum()), "| on lattice-free cells:", float((occ[F] == 1).mean()), "| mean clearance in F %.2f m" % (clr[F].mean() / 100))
    ci = {k: i for i, k in enumerate(keys)}
    charts = []
    chart_by_key = {c["key"]: c for c in view["charts"]}
    for i, k in enumerate(keys):
        c = chart_by_key.get(k, {})
        charts.append(dict(key=k.split(":")[-1], anchor=c.get("anchor"), cells=c.get("cells"), area=c.get("area_m2"), clearance=c.get("clearance"), driven=c.get("driven_frac")))
    links = []
    for l in view["links"]:
        if l["a"] in ci and l["b"] in ci:
            links.append(dict(a=ci[l["a"]], b=ci[l["b"]], length=l["length"], width=l["width"], portal=l["portal"]))
    # default pair: farthest two anchors that are connected on F (8-neighbour)
    ys, xs = np.nonzero(F); idx = -np.ones(F.shape, int); idx[ys, xs] = np.arange(len(ys))
    R, C = [], []
    for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
        y2, x2 = ys + dy, xs + dx
        ok = (y2 >= 0) & (y2 < rows) & (x2 >= 0) & (x2 < cols); ok[ok] &= F[y2[ok], x2[ok]]
        a = np.nonzero(ok)[0]; b = idx[y2[a], x2[a]]
        if dy and dx:
            k2 = F[ys[a], x2[a]] & F[y2[a], xs[a]]; a, b = a[k2], b[k2]
        R.append(a); C.append(b)
    R, C = np.concatenate(R), np.concatenate(C)
    _, lab = connected_components(coo_matrix((np.ones(len(R)), (R, C)), shape=(len(ys),) * 2), directed=False)
    anc = [(i, c["anchor"]) for i, c in enumerate(charts) if c["anchor"]]
    best = (0, None, None)
    for i, (ia, pa) in enumerate(anc):
        for ib, pb in anc[i + 1:]:
            d_ = math.dist(pa, pb)
            if d_ > best[0]:
                ca = (int(round(pa[1] / step)) - lgy0, int(round(pa[0] / step)) - lgx0)
                cb = (int(round(pb[1] / step)) - lgy0, int(round(pb[0] / step)) - lgx0)
                try:
                    if F[ca] and F[cb] and lab[idx[ca]] == lab[idx[cb]]: best = (d_, pa, pb)
                except IndexError: pass
    print("  default pair:", best[1], best[2], "dist %.1f m" % best[0], "| charts", len(charts), "links", len(links))
    out[mid] = dict(map=mid, version=view["version"], rows=rows, cols=cols, origin=origin, step=step,
                    occ=base64.b64encode(occ.tobytes()).decode(), clr=base64.b64encode(clr.tobytes()).decode(),
                    own=base64.b64encode(own.astype("<u2").tobytes()).decode(), charts=charts, links=links,
                    pair=[best[1], best[2]])
json.dump(out, open("data.json", "w"), separators=(",", ":"))
import os; print("data.json bytes:", os.path.getsize("data.json"))
