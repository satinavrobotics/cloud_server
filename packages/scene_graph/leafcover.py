"""The smallest set of leaves whose views cover a set of mcmaps (for a reconstruction service).

Pose only: no image or depth is read. A leaf is a view (camera position, camera yaw, field of view, range). A view sees
a cell when the cell is inside its cone and the ray from the camera reaches it through known-free cells; the first
blocked cell a ray meets is seen too (it is the surface). The range is capped where the expected number of valid depth
pixels per lattice cell drops below one: z <= fx * h * sqrt(p), p the camera's valid-pixel fraction.

    views = [view_from_node(...), ...]
    keys, ceiling, optimal = select_views(views, free, solid, res, target_cells, W, k=2)

`target_cells` are lattice cells (row * W + col, lattice step 2 * res): the cells of the chosen mcmaps and the blocked
cells that bound them. `ceiling` is the fraction of target cells that k views can cover at all; the returned set
k-covers all of those and is minimal (integer program; greedy order if the solver does not finish).
"""
import math
from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree


@dataclass(frozen=True)
class View:
    key: str
    x: float        # camera position in the map frame (m)
    y: float
    yaw: float      # optical axis, map frame (rad)
    fov: float      # horizontal field of view (rad)
    rng: float      # maximum range (m)


def cam_yaw_offset(q):
    """Yaw of the optical axis (+z of the camera) in the robot base frame, from T_base_cam's quaternion (qx,qy,qz,qw)."""
    x, y, z, w = q
    return math.atan2(2 * (y * z - w * x), 2 * (x * z + w * y))


def range_cap(fx, h, p):
    """Largest range at which a lattice cell of side h still holds one valid depth pixel on average:
    p * (fx * h / z)^2 >= 1  =>  z <= fx * h * sqrt(p)."""
    return fx * h * math.sqrt(p)


def view_from_node(key, pose, t_base_cam, fx, width, valid_range_m, h, p):
    """pose: {x, y, yaw} of base_link in the map frame; t_base_cam: {x, y, qx, qy, qz, qw}."""
    c, s = math.cos(pose["yaw"]), math.sin(pose["yaw"])
    cx = pose["x"] + t_base_cam["x"] * c - t_base_cam["y"] * s
    cy = pose["y"] + t_base_cam["x"] * s + t_base_cam["y"] * c
    q = (t_base_cam["qx"], t_base_cam["qy"], t_base_cam["qz"], t_base_cam["qw"])
    return View(key, cx, cy, pose["yaw"] + cam_yaw_offset(q), 2 * math.atan(width / (2 * fx)),
                min(valid_range_m, range_cap(fx, h, p)))


def visible(view, free, solid, res, origin):
    """Full-resolution cells (row * W + col) seen by `view`. free / solid: bool planes of the fused map (rows = y),
    `origin` = (x, y) in metres of cell (0, 0)."""
    H, W = free.shape
    n_rays = max(8, int(math.ceil(view.fov * view.rng / res)))
    ang = view.yaw + (np.arange(n_rays) / (n_rays - 1) - 0.5) * view.fov
    t = np.arange(1, 2 * int(view.rng / res) + 1) * 0.5      # half-cell steps: no cell is skipped
    X = (view.x - origin[0]) / res + np.cos(ang)[:, None] * t[None, :]
    Y = (view.y - origin[1]) / res + np.sin(ang)[:, None] * t[None, :]
    xi, yi = np.floor(X).astype(int), np.floor(Y).astype(int)
    inb = (xi >= 0) & (xi < W) & (yi >= 0) & (yi < H)
    fr = np.zeros(inb.shape, bool); so = np.zeros(inb.shape, bool)
    fr[inb] = free[yi[inb], xi[inb]]; so[inb] = solid[yi[inb], xi[inb]]
    alive = np.logical_and.accumulate(fr, axis=1)             # free all the way from the camera
    term = np.zeros_like(alive)                               # the first non-free cell of a ray
    term[:, 0] = ~fr[:, 0]; term[:, 1:] = alive[:, :-1] & ~fr[:, 1:]
    seen = alive | (term & so)                                # a blocked surface is seen, unknown is not
    return np.unique(yi[seen] * W + xi[seen])


def coverage(views, free, solid, res, origin, wanted, lat_w, lat_shift=1):
    """{view key: sorted lattice ids of `wanted` that the view sees}; candidates are the views within range of a wanted cell."""
    H, W = free.shape
    wanted = np.unique(np.asarray(wanted))
    ly, lx = wanted // lat_w, wanted % lat_w
    pts = np.stack([origin[0] + (lx * (1 << lat_shift) + 0.5 * (1 << lat_shift)) * res,
                    origin[1] + (ly * (1 << lat_shift) + 0.5 * (1 << lat_shift)) * res], 1)
    tree = cKDTree(pts); out = {}
    for v in views:
        if not tree.query_ball_point([v.x, v.y], v.rng):
            continue                                          # nothing of the target within range
        s = visible(v, free, solid, res, origin)
        lat = np.unique((s // W >> lat_shift) * lat_w + ((s % W) >> lat_shift))
        hit = np.intersect1d(lat, wanted, assume_unique=True)
        if len(hit): out[v.key] = hit
    return out


def cover(sights, wanted, k=1):
    """Greedy: the leaf adding most newly k-covered cells first. Returns ([(leaf, cumulative fraction)], ceiling)."""
    wanted = np.unique(np.asarray(wanted)); m = len(wanted)
    pos = {int(c): i for i, c in enumerate(wanted)}
    hit = {}
    for lf, s in sights.items():
        idx = np.fromiter((pos[c] for c in s.tolist() if c in pos), int)
        if len(idx): hit[lf] = idx
    cnt = np.zeros(m, int)
    for idx in hit.values(): cnt[idx] += 1
    reach = cnt >= k
    if not reach.any(): return [], 0.0
    need = np.where(reach, k, 0); got = np.zeros(m, int); left = dict(hit); out = []
    total = int(reach.sum()); done = 0
    while done < total and left:
        gain = {lf: int((got[idx] < need[idx]).sum()) for lf, idx in left.items()}
        lf = max(sorted(gain), key=lambda q: gain[q])
        if gain[lf] == 0: break
        got[left.pop(lf)] += 1
        done = int(((got >= need) & reach).sum()); out.append((lf, done / m))
    return out, total / m


def cover_exact(sights, wanted, k=1, time_limit=10.0):
    """Smallest set of leaves that k-covers every coverable wanted cell (integer program). Returns (leaves, optimal);
    falls back to the greedy set, optimal False, when the solver does not finish."""
    from scipy.optimize import milp, LinearConstraint, Bounds
    wanted = np.unique(np.asarray(wanted)); pos = {int(c): i for i, c in enumerate(wanted)}
    keys = list(sights); A = np.zeros((len(wanted), len(keys)), np.int8)
    for j, kk in enumerate(keys):
        for c in sights[kk].tolist():
            i = pos.get(c)
            if i is not None: A[i, j] = 1
    reach = A.sum(1) >= k
    if not reach.any(): return [], True
    res = milp(np.ones(len(keys)), constraints=LinearConstraint(A[reach], lb=k, ub=np.inf),
               integrality=np.ones(len(keys)), bounds=Bounds(0, 1), options=dict(time_limit=time_limit))
    if res.x is None or res.status != 0:
        g, _ = cover(sights, wanted, k); return [lf for lf, _ in g], False
    return [keys[j] for j in np.nonzero(res.x > 0.5)[0]], True


def select_views(views, free, solid, res, origin, wanted, lat_w, k=2, lat_shift=1):
    """The leaf keys to hand to a reconstruction: minimal set that k-covers the wanted lattice cells.
    Returns (keys, ceiling, optimal)."""
    s = coverage(views, free, solid, res, origin, wanted, lat_w, lat_shift)
    _, ceiling = cover(s, wanted, k)
    keys, optimal = cover_exact(s, wanted, k)
    return keys, ceiling, optimal
