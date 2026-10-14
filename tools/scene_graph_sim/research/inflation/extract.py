import json, struct, urllib.request, math, base64, sys
import numpy as np
from scipy import ndimage
B = "http://localhost:8000"
def jget(p): return json.load(urllib.request.urlopen(B + p, timeout=30))
def rget(p):
    r = urllib.request.urlopen(B + p, timeout=60); return r.read(), dict(r.headers)
def occupancy(mid):
    ext = jget(f"/api/v1/maps/{mid}/scene-graph/occupancy/extent"); v = ext["version"]; T = 128
    tiles = {}
    for c in ext["chunks"]:
        raw, _ = rget(f"/api/v1/maps/{mid}/scene-graph/occupancy/chunk/{c['cx']}/{c['cy']}?version={v}")
        n = struct.unpack("<I", raw[:4])[0]; hdr = json.loads(raw[4:4 + n]); body = raw[4 + n:]
        for slot, h, off in hdr["slots"]:
            tiles[(hdr["cx"] * 8 + slot % 8, hdr["cy"] * 8 + slot // 8)] = np.frombuffer(body[off:off + T * T], np.uint8).reshape(T, T)
    xs = [t[0] for t in tiles]; ys = [t[1] for t in tiles]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    V = np.full(((y1 - y0 + 1) * T, (x1 - x0 + 1) * T), 255, np.uint8)
    for (tx, ty), a in tiles.items(): V[(ty - y0) * T:(ty - y0 + 1) * T, (tx - x0) * T:(tx - x0 + 1) * T] = a
    return V, x0 * T, y0 * T, v      # V, global full-res index of V[0,0] (cells of 0.05 m)
out = {}
for mid in sys.argv[1:]:
    V, gx0, gy0, occ_v = occupancy(mid)
    H, W = V.shape
    x0m, y0m = gx0 * 0.05, gy0 * 0.05
    bbox = f"{x0m - 1},{y0m - 1},{x0m + W * 0.05 + 1},{y0m + H * 0.05 + 1}"
    view = jget(f"/api/v1/maps/{mid}/scene-graph?bbox={bbox}&level=0&raster=true")
    print(mid, "occupancy", V.shape, "bbox", bbox, "| available", view.get("available"), "version", view.get("version"), "charts", len(view.get("charts", [])), "links", len(view.get("links", [])), "keys", list(view)[:12])
    ch = view["charts"]; lk = view["links"]
    print("  chart keys:", sorted(ch[0])[:14]); print("  link keys:", sorted(lk[0])[:14]); print("  raster meta:", str(view.get("raster"))[:300])
    out[mid] = dict(V=V, gx0=gx0, gy0=gy0, view=view, bbox=bbox)
import pickle; pickle.dump(out, open("raw.pkl", "wb"))
