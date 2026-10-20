"""Pi-like end-to-end benchmark of graph-builder's scene-graph path (see README.md).

    python3 -I bench.py ingest  --repo <worktree> --rate 1.0 --factor 1 --store /work/store --out /work/r.json
    python3 -I bench.py restart --repo <worktree> --store /work/store --out /work/r2.json
    python3 -I bench.py idle    --repo <worktree> --out /work/idle.json

`ingest` drives the REAL SceneRunner -> SceneWorker -> scene_adapter (PNG decode, to_leaf) -> ChartGraph.fold_many ->
SceneGraphStore.save chain, built the way scene_host.build_host builds it (same WorkerConfig / RunnerConfig functions, the
robot compose file's graph-builder environment). Only the I/O edges are fakes: the node source (in-memory, nodes become
visible at their arrival time), the PNG fetch (bytes in memory), the guard (always owner) and the store backend (a plain
directory, so a restart is a new process reading it back).

Time is simulated: the clock the worker, runner and source see is `virtual = offset + real_elapsed * factor`, and idle waits
are skipped by jumping `offset`. factor 1 = this CPU's speed; factor 6 = "a CPU 6x slower" (an ESTIMATE of a Pi: every
second of compute counts as 6 simulated seconds against the arrival rate). Memory is not scaled.
"""
import argparse, bisect, collections, cProfile, datetime, dataclasses, inspect, io, json, os, pstats, re, resource, sys, threading, time

ap = argparse.ArgumentParser()
ap.add_argument("mode", choices=["ingest", "restart", "idle"])
ap.add_argument("--repo", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--store", default="/work/store")
ap.add_argument("--nodes", type=int, default=800)
ap.add_argument("--rate", type=float, default=1.0, help="seconds between nodes (simulated)")
ap.add_argument("--factor", type=float, default=1.0, help="CPU slowdown applied to the simulated clock")
ap.add_argument("--extra", type=int, default=8, help="restart: new nodes that force the engine load")
ap.add_argument("--sample-s", type=float, default=10.0)
ap.add_argument("--drain-cap-s", type=float, default=3600.0, help="simulated seconds allowed after the last arrival")
ap.add_argument("--max-real-s", type=float, default=2400.0)
ap.add_argument("--profile", action="store_true", help="cProfile the fold_many calls (slows them)")
ap.add_argument("--world-x", type=float, default=60.0)
ap.add_argument("--world-y", type=float, default=40.0)
ap.add_argument("--world", default="store", choices=["store", "aisles", "diag", "office8"],
                help="store: the 60x40 m supermarket; aisles/diag: the engine harness worlds (aisles 40x30 m step 0.5, diag 90x90 m "
                     "with pillars, a diagonal path); office8: the real export (--office8-dir)")
ap.add_argument("--office8-dir", default="/office8")
ap.add_argument("--clutter", type=float, default=0.0, help="0 = clean shelves; 1 = pallets/carts in the aisles and ragged shelf faces")
a = ap.parse_args()
REPO = os.path.abspath(a.repo)
sys.path.insert(0, REPO)


# ------------------------------------------------------------------------------------------------ environment
def robot_env():
    """The graph-builder environment of docker_compose/mission_dispatch_services_robot.yaml (defaults of ${VAR:-default})."""
    text = open(os.path.join(REPO, "docker_compose/mission_dispatch_services_robot.yaml")).read()
    block = text.split("graph-builder-service:", 1)[1].split("mem_limit", 1)[0]
    env = {m.group(1): m.group(2) for m in re.finditer(r"-\s+([A-Z_]+)=\$\{[A-Z_]+:-([^}]*)\}", block)}
    env["SCENE_GRAPH_WORKER_ENABLED"] = "true"
    return env


ENV = robot_env()
for k, v in ENV.items():
    os.environ.setdefault(k, v)


def rss_mb(key="VmRSS"):
    try:
        for line in open("/proc/self/status"):
            if line.startswith(key + ":"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def cgroup_mb(name):
    try:
        return int(open("/sys/fs/cgroup/" + name).read()) / 1048576
    except (OSError, ValueError):
        return None


def dump(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=1)


if a.mode == "idle":
    out = {"python_bare_rss_mb": round(rss_mb(), 1)}
    import numpy  # the engine's own dependency
    out["after_numpy_mb"] = round(rss_mb(), 1)
    import packages.services.graph_builder.main as gbmain   # FastAPI app, uvicorn, MQTT, Postgres, Arango, MinIO clients
    out["after_main_import_mb"] = round(rss_mb(), 1)
    from packages.scene_graph.engine import ChartGraph
    from packages.scene_graph.store import SceneGraphStore
    from packages.services.graph_builder import scene_host, scene_runner, scene_worker
    out["after_scene_imports_mb"] = round(rss_mb(), 1)
    pf = scene_worker.PngPrefetch(int(os.environ.get("SCENE_GRAPH_PREFETCH_THREADS", "4")),
                                  int(os.environ["SCENE_GRAPH_PREFETCH_QUEUE"]))
    from packages.scene_graph.api import SceneParams
    g = ChartGraph(SceneParams())
    out["after_pool_and_empty_engine_mb"] = round(rss_mb(), 1)
    out["peak_mb"] = round(rss_mb("VmHWM"), 1)
    out["note"] = ("imports + empty engine + prefetch pool. No MQTT connection, no DB clients, no upload buffer content "
                   "(UPLOAD_BUFFER_MAX_BYTES=%s is a CAP, not an idle cost), no uvicorn event loop." % ENV.get("UPLOAD_BUFFER_MAX_BYTES"))
    dump(a.out, out)
    print(json.dumps(out))
    sys.exit(0)

import numpy as np
from PIL import Image
from packages.scene_graph.engine import ChartGraph
from packages.scene_graph.api import BBox, SceneParams
from packages.scene_graph.store import SceneGraphStore, StaleVersion
from packages.services.graph_builder import scene_host, scene_runner, scene_worker as sw
from packages import config

MAP = "bench"
T0 = 1_760_000_000_000
RES = 0.05


# ------------------------------------------------------------------------------------------------ store backend on a directory
class DirBackend:
    """Blobs + Docs protocols (packages/scene_graph/store_backends.py) on a directory: a restart is a new process."""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _p(self, map_id, *parts):
        return os.path.join(self.root, map_id, *parts)

    def put(self, map_id, key, data):
        p = self._p(map_id, "blobs", key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(bytes(data))

    def get(self, map_id, key):
        try:
            with open(self._p(map_id, "blobs", key), "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def exists(self, map_id, key):
        return os.path.exists(self._p(map_id, "blobs", key))

    def list(self, map_id, prefix=""):
        base = self._p(map_id, "blobs")
        out = []
        for d, _, fs in os.walk(base):
            for f in fs:
                k = os.path.relpath(os.path.join(d, f), base).replace(os.sep, "/")
                if k.startswith(prefix):
                    out.append(k)
        return sorted(out)

    def delete(self, map_id, keys):
        for k in list(keys):
            try:
                os.remove(self._p(map_id, "blobs", k))
            except FileNotFoundError:
                pass

    def meta(self, map_id):
        try:
            with open(self._p(map_id, "meta.json")) as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def cas_meta(self, map_id, expected_version, meta):
        cur = self.meta(map_id)
        if (cur["version"] if cur else None) != expected_version:
            return False
        os.makedirs(self._p(map_id), exist_ok=True)
        tmp = self._p(map_id, "meta.json.tmp")
        with open(tmp, "w") as f:
            json.dump(meta, f)
        os.replace(tmp, self._p(map_id, "meta.json"))
        return True

    def put_index(self, map_id, version, anchors, links):
        os.makedirs(self._p(map_id), exist_ok=True)
        with open(self._p(map_id, "index.json"), "w") as f:
            json.dump(dict(version=version, anchors=anchors, links=links), f)

    def index_version(self, map_id):
        try:
            with open(self._p(map_id, "index.json")) as f:
                return json.load(f)["version"]
        except FileNotFoundError:
            return None

    def drop(self, map_id):
        import shutil
        shutil.rmtree(self._p(map_id), ignore_errors=True)


# ------------------------------------------------------------------------------------------------ synthetic supermarket
def world(WX, WY, n_min, fixed_step=None):
    """Perimeter walls, shelf rows every 3.6 m (aisles 2.4 m), a boustrophedon survey path through the aisles. The step is
    chosen so the path has at least n_min nodes (aldi scale: ~60x40 m, 800 nodes => ~0.7 m between nodes)."""
    T = np.zeros((int(WY / RES), int(WX / RES)), np.uint8)
    T[:8, :] = 100; T[-8:, :] = 100; T[:, :8] = 100; T[:, -8:] = 100
    pitch, shelf, aisle = 3.6, 1.2, 2.4
    y, ycs = 2.0, []
    while y + pitch < WY - 1:
        T[int((y + aisle) / RES):int((y + aisle + shelf) / RES), int(3 / RES):int((WX - 3) / RES)] = 100
        ycs.append(y + aisle / 2); y += pitch
    T[int(1.2 / RES):int(1.6 / RES), int(WX / 4 / RES):int(WX / 2 / RES)] = 60       # a soft strip
    if a.clutter > 0:        # static clutter (same in every capture): ragged shelf faces and boxes off the survey line
        r2 = np.random.default_rng(7)
        for yc in ycs:
            for face in (yc - aisle / 2 - 0.0, yc + aisle / 2):
                for x in np.arange(3.0, WX - 3.0, 0.15):
                    if r2.random() < 0.5 * a.clutter:
                        d = int(r2.integers(1, 5))
                        yy0 = int(face / RES) + (-d if face < yc else 0)
                        T[yy0:yy0 + d, int(x / RES):int(x / RES) + 3] = 100
            for _ in range(int(60 * a.clutter)):
                sg = r2.choice([-1, 1])
                bx, by = r2.uniform(3.5, WX - 3.5), yc + sg * r2.uniform(0.55, 1.0)
                bw, bh = r2.integers(4, 14, 2)
                if sg < 0:
                    by -= bh * RES
                T[int(by / RES):int(by / RES) + bh, int(bx / RES):int(bx / RES) + bw] = 100
    step = fixed_step or 1.0
    while True:
        path = []
        for i, yc in enumerate(ycs):
            xs = np.arange(1.5, WX - 1.5, step)
            if i % 2:
                xs = xs[::-1]
            path += [(float(x), float(yc)) for x in xs]
            if i + 1 < len(ycs):
                xe = xs[-1]
                path += [(float(xe), float(yy)) for yy in np.arange(yc + step, ycs[i + 1], step)]
        if len(path) >= n_min or fixed_step:
            return T, path, step
        step -= 0.01


def make_nodes(n, n_min=None):
    """(node documents, {key: png bytes}). Transient 'people' blobs seen only on some captures, speckle noise, 10x10 m
    windows at 5 cm like the robot's occupancy layer, two sessions."""
    if a.world == "office8":
        return office8_nodes(n)
    rng = np.random.default_rng(12345)
    if a.world == "diag":
        T = np.zeros((int(90 / RES), int(90 / RES)), np.uint8)
        T[:8, :] = 100; T[-8:, :] = 100; T[:, :8] = 100; T[:, -8:] = 100
        for _ in range(12):
            cy, cx = rng.integers(200, 1600, 2)
            T[cy:cy + 12, cx:cx + 12] = 100
        path, step = [(3 + 0.53 * i, 3 + 0.53 * i) for i in range(n)], 0.53 * 2 ** 0.5
    elif a.world == "aisles":
        T, path, step = world(40.0, 30.0, 1, fixed_step=0.5)
    else:
        T, path, step = world(a.world_x, a.world_y, a.nodes + a.extra)   # one step for ingest and restart
    path = path[:n]
    yy, xx = np.mgrid[0:200, 0:200]
    far = np.hypot(yy - 100, xx - 100) * RES > 4.5
    blobs = [(path[k][0] + 0.8, path[k][1] + 0.3) for k in range(5, (n_min or n), 37) if k < len(path)]
    docs, pngs = [], {}
    for i, (x, y) in enumerate(path):
        x0, y0 = x - 5.0, y - 5.0
        gx, gy = int(round(x0 / RES)), int(round(y0 / RES))
        w = np.full((200, 200), 255, np.uint8)
        sy0, sx0 = max(gy, 0), max(gx, 0)
        sy1, sx1 = min(gy + 200, T.shape[0]), min(gx + 200, T.shape[1])
        if sy0 < sy1 and sx0 < sx1:
            w[sy0 - gy:sy1 - gy, sx0 - gx:sx1 - gx] = T[sy0:sy1, sx0:sx1]
        for j, (bx, by) in enumerate(blobs):
            if (i // 3 + j) % 4 == 0:
                cy, cx = int((by - y0) / RES), int((bx - x0) / RES)
                if 0 <= cy < 196 and 0 <= cx < 196:
                    w[cy:cy + 5, cx:cx + 5] = 100
        w[(rng.random((200, 200)) < 0.002) & (w < 100)] = 100
        w[far] = 255
        key = f"n{i:05d}"
        buf = io.BytesIO()
        Image.fromarray(w, mode="L").save(buf, format="PNG")
        pngs[key] = buf.getvalue()
        docs.append({"_key": key, "session_id": "s1" if i < (n_min or n) * 2 // 3 else "s2", "robot_name": "r1",
                     "pose": {"x": x, "y": y, "yaw": 0.0},
                     "costmap": {"occupancy": {"width": 200, "height": 200, "resolution": RES,
                                               "origin_map": {"x": gx * RES, "y": gy * RES, "yaw": 0.0}}}})
    return docs, pngs, step


def office8_nodes(n):
    """The real office8 export: node documents + their occupancy PNGs, ordered by session_node_id."""
    d = a.office8_dir
    rows = json.load(open(os.path.join(d, "nodes.json")))
    rows = sorted([x for x in rows if "costmap" in x and "occupancy" in x["costmap"]], key=lambda x: x["session_node_id"])[:n]
    docs, pngs = [], {}
    for x in rows:
        c = x["costmap"]["occupancy"]
        key = x["_key"]
        pngs[key] = open(os.path.join(d, key + ".png"), "rb").read()
        docs.append({"_key": key, "session_id": x.get("session_id", "s"), "robot_name": x.get("robot_name", "r1"),
                     "pose": x["pose"],
                     "costmap": {"occupancy": {"width": c["width"], "height": c["height"], "resolution": c["resolution"],
                                               "origin_map": c["origin_map"]}}})
    return docs, pngs, 0.0


class Clock:
    """virtual seconds = offset + real elapsed * factor; idle waits are skipped with jump()."""

    def __init__(self, factor):
        self.f, self.p0, self.off = factor, time.perf_counter(), 0.0

    def s(self):
        return self.off + (time.perf_counter() - self.p0) * self.f

    def jump(self, dt):
        self.off += dt


class Source:
    """Nodes become visible at their arrival time (costmap_stored_ms = arrival), in (stored_ms, key) order."""

    def __init__(self, docs, arrivals_s, clock):
        self.clock = clock
        self.docs = []
        for d, at in zip(docs, arrivals_s):
            recv = T0 + int(at * 1000)
            d = dict(d)
            d["costmap_stored_ms"] = recv
            d["timestamp"] = datetime.datetime.fromtimestamp(recv / 1000, datetime.timezone.utc).replace(tzinfo=None).isoformat()
            d["costmap"] = {"occupancy": dict(d["costmap"]["occupancy"], costmap_stamp_ms=recv - 500)}
            self.docs.append(d)
        self.docs.sort(key=lambda n: (n["costmap_stored_ms"], n["_key"]))
        self.keys = [(n["costmap_stored_ms"], n["_key"]) for n in self.docs]
        self.by_key = {n["_key"]: n for n in self.docs}

    def set_scene_stamps(self, map_id, stamps):
        for k, ms in stamps.items():
            self.by_key[k].setdefault("scene_stamp_ms", ms)

    def costmap_nodes_since(self, map_id, since_ms=0, after_key="", limit=200):
        now = T0 + int(self.clock.s() * 1000)
        i = bisect.bisect_right(self.keys, (since_ms, after_key))
        out = []
        while i < len(self.docs) and len(out) < limit and self.docs[i]["costmap_stored_ms"] <= now:
            out.append(self.docs[i]); i += 1
        return out

    def arrived(self):
        return bisect.bisect_right(self.keys, (T0 + int(self.clock.s() * 1000), "￿"))


class Guard:
    def acquire(self, m): return True
    def held(self, m): return True
    def release(self, m): pass
    def is_deleting(self, m): return False


# ------------------------------------------------------------------------------------------------ the production wiring
def spec_params():
    """What scene_host.spec_params builds for a map whose spec carries the defaults (SceneGraphParamsV1())."""
    try:
        from cloud_common.objects.map import SceneGraphParamsV1
        spec = SceneGraphParamsV1()
        near = spec.close_evidence_range_m
        mapped = SceneParams(chart_radius=spec.chart_radius_m, robot_half_side=spec.robot_half_side_m, near_range_m=near,
                             near_min_clearance_m=config.SCENE_GRAPH_NEAR_MIN_CLEARANCE_M if near is not None else None)
        return mapped, spec.robot_half_side_m
    except Exception:   # noqa: BLE001  an old tree without the spec object
        return SceneParams(near_range_m=config.SCENE_GRAPH_NEAR_RANGE_M,
                           near_min_clearance_m=config.SCENE_GRAPH_NEAR_MIN_CLEARANCE_M), None


class Rec:
    """Instrumentation shared by ingest and restart."""

    def __init__(self):
        self.folds, self.saves, self.loads, self.steps, self.events = [], [], [], [], collections.Counter()
        self.solves = 0
        self.prof = cProfile.Profile() if a.profile else None


def build(source, pngs, clock, backend_root, rec):
    params, half_side = spec_params()
    store = SceneGraphStore(DirBackend(backend_root), DirBackend(backend_root))
    real_save, real_load = store.save, store.load

    def save(*args, **kw):
        t = time.perf_counter()
        try:
            return real_save(*args, **kw)
        finally:
            rec.saves.append((clock.s(), time.perf_counter() - t))

    def load(*args, **kw):
        t = time.perf_counter()
        try:
            return real_load(*args, **kw)
        finally:
            rec.loads.append((clock.s(), time.perf_counter() - t, rss_mb()))

    store.save, store.load = save, load

    def instrument(eng):
        real_fold = eng.fold_many

        def fold_many(leaves, *x, **k):
            t = time.perf_counter()
            if rec.prof:
                rec.prof.enable()
            try:
                return real_fold(leaves, *x, **k)
            finally:
                if rec.prof:
                    rec.prof.disable()
                rec.folds.append((clock.s(), len(leaves), time.perf_counter() - t))

        eng.fold_many = fold_many
        if hasattr(eng, "_solve"):
            real_solve = eng._solve

            def _solve(*x, **k):
                rec.solves += 1
                return real_solve(*x, **k)

            eng._solve = _solve
        return eng

    cfg = scene_host.worker_config(params)
    cfg = dataclasses.replace(cfg, expected_params_hash=scene_host.expected_params_hash(params))
    prefetch = (sw.PngPrefetch(config.SCENE_GRAPH_PREFETCH_THREADS, config.SCENE_GRAPH_PREFETCH_QUEUE)
                if config.SCENE_GRAPH_PREFETCH_THREADS > 0 else None)
    cursors = sw.MemoryCursorStore()
    workers = []

    def fetch_png(map_id, key, layer):
        return pngs.get(key)

    def make_worker(map_id):
        def load_engine(version):
            return instrument(store.load(map_id, version) if version else ChartGraph(params))

        kw = dict(source=source, fetch_png=fetch_png, footprint=lambda r: (-1.0, -1.0), engine_factory=load_engine,
                  store=store, cursor_store=cursors, guard=Guard(), config=cfg,
                  now_ms=lambda: T0 + int(clock.s() * 1000), mono_ms=lambda: int(clock.s() * 1000),
                  stored_leaves=lambda version: scene_host.stored_leaf_index(store, map_id, version),
                  publish=lambda version, dirty: None,
                  new_engine=lambda: instrument(ChartGraph(params)), fence_errors=(StaleVersion,), prefetch=prefetch,
                  half_side=half_side, on_event=lambda kind, payload: rec.events.update([kind]))
        ok = inspect.signature(sw.SceneWorker.__init__).parameters
        w = sw.SceneWorker(map_id, **{k: v for k, v in kw.items() if k in ok})
        real_step = w.step

        def step():
            t = time.perf_counter()
            res = real_step()
            rec.steps.append((clock.s(), time.perf_counter() - t, getattr(res, "state", "")))
            return res

        w.step = step
        workers.append(w)
        return w

    rcfg = scene_host.runner_config(config)
    runner = scene_runner.SceneRunner(lambda: [MAP], make_worker, now=clock.s, config=rcfg, prefetch=prefetch,
                                      on_event=lambda m, kind, payload: rec.events.update([kind]))
    return runner, workers, store, params


def pump(runner, clock, until, deadline_real=None, on_tick=None):
    """tick the runner until until() is true; idle waits are jumped over (skipped), never slept."""
    while not until():
        if deadline_real is not None and time.perf_counter() > deadline_real:
            return False
        n = runner.tick()
        if on_tick:
            on_tick()
        if n == 0:
            if runner._inflight:
                time.sleep(0.002)
            else:
                due = runner._due.get(MAP)
                now = clock.s()
                if due is not None and due > now:
                    clock.jump(min(due - now, 5.0))     # never skip far past a sample / arrival
                else:
                    time.sleep(0.001)
    return True


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0.0


def top_functions(prof, n=22):
    ps = pstats.Stats(prof, stream=io.StringIO())
    rows = []
    for (fn, line, name), (cc, nc, tt, ct, _) in ps.stats.items():
        if "/packages/" in fn or "scene_graph" in fn:
            rows.append((ct, tt, nc, f"{os.path.basename(fn)}:{line}:{name}"))
    rows.sort(reverse=True)
    return [dict(cum_s=round(c, 2), self_s=round(t, 2), calls=n_, fn=f) for c, t, n_, f in rows[:n]]


def handled_of(workers):
    w = workers[0] if workers else None
    if w is None:
        return 0
    st = w.stats
    return int(st.folded) + sum((getattr(st, "skipped", {}) or {}).values())     # already_folded is a skip reason


# ------------------------------------------------------------------------------------------------ ingest
if a.mode == "ingest":
    import shutil
    shutil.rmtree(a.store, ignore_errors=True)
    docs, pngs, step = make_nodes(a.nodes)
    clock = Clock(a.factor)
    arrivals = [1.0 + i * a.rate for i in range(len(docs))]
    source = Source(docs, arrivals, clock)
    rec = Rec()
    runner, workers, store, params = build(source, pngs, clock, a.store, rec)
    rss0 = rss_mb()
    samples_path = a.out + ".samples.jsonl"
    samples = open(samples_path, "w")
    next_sample = [0.0]
    N = len(docs)
    last_arrival = arrivals[-1]
    peak_backlog = [0]
    t_real0 = time.perf_counter()

    def sample(force=False):
        vs = clock.s()
        if not force and vs < next_sample[0]:
            return
        next_sample[0] = vs + a.sample_s
        w = workers[0] if workers else None
        arrived = source.arrived()
        h = handled_of(workers)
        rec_ = dict(t=round(vs, 1), arrived=arrived, handled=h, backlog=arrived - h,
                    buffered=len(getattr(w, "_buf", []) or []) if w else 0, folds=len(rec.folds), solves=rec.solves,
                    fold_s_total=round(sum(f[2] for f in list(rec.folds)), 2),
                    version=getattr(w.stats, "version", None) if w else None,
                    engine_mb=round((getattr(w.stats, "resident_bytes", 0) or 0) / 1048576, 1) if w else 0,
                    rss_mb=round(rss_mb(), 1), real_s=round(time.perf_counter() - t_real0, 1),
                    evictions=getattr(runner, "evictions", 0))
        peak_backlog[0] = max(peak_backlog[0], rec_["backlog"])
        samples.write(json.dumps(rec_) + "\n"); samples.flush()

    def done():
        w = workers[0] if workers else None
        return (w is not None and source.arrived() >= N and handled_of(workers) >= N and not runner._inflight
                and getattr(w, "_dirty", 1) is None and not getattr(w, "_buf", [1]))

    def over():
        return done() or clock.s() > last_arrival + a.drain_cap_s

    finished = pump(runner, clock, over, deadline_real=time.perf_counter() + a.max_real_s, on_tick=sample)
    sample(force=True)
    samples.close()
    caught = done()
    end_vs = clock.s()
    w = workers[0]
    v = store.meta(MAP) or {}
    try:
        n_leaves = len(scene_host.stored_leaf_index(store, MAP, v["version"]))
    except Exception as e:   # noqa: BLE001
        n_leaves = f"error {e}"
    try:
        view = store.view(MAP).query(BBox(-1e4, -1e4, 1e4, 1e4), raster=False)
        shape = dict(charts=len(view.charts), links=len(view.links))
    except Exception as e:   # noqa: BLE001
        shape = dict(error=str(e))
    backlogs = [json.loads(l) for l in open(samples_path)]
    fold_t = [f[2] for f in rec.folds]
    fold_n = [f[1] for f in rec.folds]
    lag = [s["backlog"] for s in backlogs]
    res = dict(
        mode="ingest", repo=REPO, params=repr(params),
        env={k: os.environ.get(k) for k in sorted(ENV)}, nodes=N, world_xy=[a.world_x, a.world_y], node_spacing_m=round(step, 2),
        rate_s=a.rate, factor=a.factor, finished=bool(finished), caught_up=bool(caught),
        virtual_s_total=round(end_vs, 1), last_arrival_s=last_arrival,
        drain_after_last_arrival_s=round(max(0.0, end_vs - last_arrival), 1),
        real_s_total=round(time.perf_counter() - t_real0, 1),
        peak_backlog_nodes=peak_backlog[0],
        backlog_at_last_arrival=next((s["backlog"] for s in backlogs if s["t"] >= last_arrival), None),
        backlog_final=backlogs[-1]["backlog"], backlog_mean=round(sum(lag) / max(1, len(lag)), 1),
        folds=len(rec.folds), leaves_per_fold_mean=round(sum(fold_n) / max(1, len(fold_n)), 2),
        solves=rec.solves, fold_s_sum=round(sum(fold_t), 1), fold_s_mean=round(sum(fold_t) / max(1, len(fold_t)), 3),
        fold_s_p95=round(pct(fold_t, .95), 3), fold_s_max=round(max(fold_t, default=0), 3),
        fold_s_last10_mean=round(sum(fold_t[-10:]) / max(1, len(fold_t[-10:])), 3),
        fold_s_first10_mean=round(sum(fold_t[:10]) / max(1, len(fold_t[:10])), 3),
        saves=len(rec.saves), save_s_sum=round(sum(s[1] for s in rec.saves), 1),
        save_s_max=round(max((s[1] for s in rec.saves), default=0), 3),
        steps=len(rec.steps), step_s_sum=round(sum(s[1] for s in rec.steps), 1),
        step_s_max=round(max((s[1] for s in rec.steps), default=0), 3),
        step_states=dict(collections.Counter(s[2] for s in rec.steps)),
        loads=len(rec.loads), evictions=getattr(runner, "evictions", 0),
        rss_start_mb=round(rss0, 1), rss_peak_mb=round(rss_mb("VmHWM"), 1), rss_end_mb=round(rss_mb(), 1),
        cgroup_peak_mb=cgroup_mb("memory.peak"), cgroup_oom_kills=None,
        final_version=v.get("version"), stored_leaves=n_leaves, final_shape=shape,
        events=dict(rec.events), skipped=dict(getattr(w.stats, "skipped", {}) or {}),
        quarantined=list(getattr(w, "_quarantined", [])),
        engine_nbytes_mb=round((getattr(w.stats, "resident_bytes", 0) or 0) / 1048576, 1))
    try:
        for line in open("/sys/fs/cgroup/memory.events"):
            if line.startswith("oom_kill"):
                res["cgroup_oom_kills"] = int(line.split()[1])
    except OSError:
        pass
    # where the worker thread's time goes: fold vs save vs the rest (PNG decode + adapt, scan, bookkeeping)
    res["split_s"] = dict(step_total=res["step_s_sum"], fold=res["fold_s_sum"], save=res["save_s_sum"],
                          other=round(res["step_s_sum"] - res["fold_s_sum"] - res["save_s_sum"], 1))
    if rec.prof:
        res["profile_top"] = top_functions(rec.prof)
    res["fold_series"] = [[round(t, 1), n_, round(s, 3)] for t, n_, s in rec.folds][::max(1, len(rec.folds) // 80)]
    runner.close()
    dump(a.out, res)
    print(json.dumps({k: v_ for k, v_ in res.items() if k not in ("fold_series", "profile_top", "env", "params")}))
    sys.exit(0)

# ------------------------------------------------------------------------------------------------ restart (cold start from the store)
docs, pngs, step = make_nodes(a.nodes + a.extra, a.nodes)
clock = Clock(1.0)
arrivals = [1.0 + i * a.rate for i in range(len(docs))]
source = Source(docs, arrivals, clock)
clock.jump(arrivals[-1] + 5.0)        # every node, old and new, is already in the database when the process starts
rec = Rec()
rss_before_build = rss_mb()
t_start = time.perf_counter()
runner, workers, store, params = build(source, pngs, clock, a.store, rec)
meta0 = store.meta(MAP) or {}
marks = {}


def over():
    w = workers[0] if workers else None
    # a restart re-reads only the replay margin (60 s of nodes, all already folded): done = the new nodes folded and saved
    return (w is not None and int(w.stats.folded) >= a.extra and not runner._inflight
            and getattr(w, "_dirty", 1) is None and not getattr(w, "_buf", [1]))


def on_tick():
    if rec.loads and "engine_loaded_s" not in marks:
        marks["engine_loaded_s"] = round(time.perf_counter() - t_start, 2)


ok = pump(runner, clock, over, deadline_real=time.perf_counter() + a.max_real_s, on_tick=on_tick)
total = time.perf_counter() - t_start
w = workers[0]
v = store.meta(MAP) or {}
res = dict(mode="restart", repo=REPO, ok=bool(ok), store_version_before=meta0.get("version"),
           store_version_after=v.get("version"),
           rss_before_worker_mb=round(rss_before_build, 1), rss_peak_mb=round(rss_mb("VmHWM"), 1),
           rss_end_mb=round(rss_mb(), 1), cgroup_peak_mb=cgroup_mb("memory.peak"),
           total_s=round(total, 2), engine_load_calls=[[round(t, 2), round(d, 2), round(r, 1)] for t, d, r in rec.loads],
           engine_loaded_at_s=marks.get("engine_loaded_s"), folds=len(rec.folds),
           fold_s_sum=round(sum(f[2] for f in rec.folds), 2),
           already_folded=int(getattr(w.stats, "already_folded", 0)), folded=int(w.stats.folded),
           saves=len(rec.saves), steps=len(rec.steps), events=dict(rec.events),
           engine_nbytes_mb=round((getattr(w.stats, "resident_bytes", 0) or 0) / 1048576, 1))
if not ok:
    res["state_at_timeout"] = dict(handled=handled_of(workers), dirty=getattr(w, "_dirty", None) is not None,
                                   buffered=len(getattr(w, "_buf", []) or []), inflight=len(runner._inflight),
                                   due=runner._due.get(MAP), now=clock.s(), last=getattr(runner, "last_state", {}),
                                   stats=repr(w.stats)[:600])
runner.close()
dump(a.out, res)
print(json.dumps(res))
