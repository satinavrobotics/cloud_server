import hashlib

from packages.services.graph_builder import scene_guard as sg


class Cur:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class RowsCur:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class Conn:
    def __init__(self, hub):
        self.hub, self.closed, self.locks = hub, False, set()

    def execute(self, sql, params=None):
        if self.closed or self.hub.broken:
            raise RuntimeError("connection lost")
        if "pg_try_advisory_lock" in sql:
            key = params[0]
            if key in self.hub.held_by_other:
                return Cur((False,))
            self.locks.add(key)
            return Cur((True,))
        if "pg_advisory_unlock" in sql:
            self.locks.discard(params[0])
            return Cur((True,))
        if "ANY(" in sql:
            return RowsCur([(n,) for n in params[0]
                            if n in self.hub.lifecycles and self.hub.lifecycles[n][0] not in ("DELETED", "DELETING")])
        if "mapobjectv1" in sql:
            return Cur(self.hub.lifecycles.get(params[0]))
        return Cur((1,))

    def close(self):
        self.closed = True


class Hub:
    def __init__(self):
        self.held_by_other, self.lifecycles, self.broken, self.conns = set(), {}, False, []

    def connect(self):
        c = Conn(self)
        self.conns.append(c)
        return c


def test_lock_key_is_the_sha256_construction():
    d = hashlib.sha256(b"scene_graph:m1").digest()[:8]
    assert sg.lock_key("m1") == int.from_bytes(d, "big", signed=True)
    assert sg.lock_key("m1") != sg.lock_key("m2")


def test_acquire_held_release():
    h = Hub()
    g = sg.PgGuard(h.connect)
    assert g.acquire("m") and g.held("m")
    assert g.acquire("m")                       # idempotent, no second connection
    assert len(h.conns) == 1
    g.release("m")
    assert not g.held("m") and h.conns[0].closed


def test_lock_taken_elsewhere():
    h = Hub()
    h.held_by_other.add(sg.lock_key("m"))
    g = sg.PgGuard(h.connect)
    assert not g.acquire("m") and h.conns[0].closed


def test_lost_connection_means_not_held():
    h = Hub()
    g = sg.PgGuard(h.connect)
    g.acquire("m")
    h.broken = True
    assert not g.held("m")


def test_is_deleting():
    h = Hub()
    g = sg.PgGuard(h.connect)
    assert g.is_deleting("none")                # missing row
    h.lifecycles["m"] = ("DELETING",)
    assert g.is_deleting("m")
    h.lifecycles["m"] = ("ALIVE",)
    assert not g.is_deleting("m")
    h.broken = True
    assert g.is_deleting("m")                   # unanswerable: no save on a guess


def test_registered_maps_is_a_positive_check():
    hub = Hub()
    hub.lifecycles = {"real": ("READY",), "going": ("DELETING",), "gone": ("DELETED",)}
    g = sg.PgGuard(hub.connect)
    got = g.registered_maps(["real", "going", "gone", "map_junk", "", None])
    assert got == ["real"]                    # a bucket with no registry row is not a map
    assert hub.conns[-1].closed
    assert g.registered_maps([]) == []


def test_registered_maps_raises_when_registry_unreachable():
    hub = Hub()
    hub.broken = True
    g = sg.PgGuard(hub.connect)
    try:
        g.registered_maps(["a"])
    except RuntimeError:
        pass
    else:
        raise AssertionError("an unanswered registry must not read as 'no maps'")


# ---- status keys: per-map switch and the rebuild request

class StatusCur:
    def __init__(self, row=None, rows=(), rowcount=0):
        self.row, self.rows, self.rowcount = row, rows, rowcount

    def fetchone(self):
        return self.row

    def fetchall(self):
        return list(self.rows)


class StatusConn:
    """mapobjectv1.status as a dict per map; understands the three guard queries."""

    def __init__(self, status, broken=False):
        self.status, self.broken, self.closed, self.sql = status, broken, False, []

    def execute(self, sql, params=None):
        if self.broken:
            raise RuntimeError("down")
        self.sql.append(sql)
        if sql.startswith("UPDATE"):
            key, name, _, at = params
            doc = self.status.get(name, {})
            if isinstance(doc.get(key), dict) and doc[key].get("at") == at:
                del doc[key]
                return StatusCur(rowcount=1)
            return StatusCur(rowcount=0)
        if "ANY(" in sql:
            names, key = params
            return StatusCur(rows=[(n,) for n in names if self.status.get(n, {}).get(key) is False])
        key, name = params
        if name not in self.status:
            return StatusCur(None)
        return StatusCur((self.status[name].get(key),))

    def close(self):
        self.closed = True


def test_status_keys_match_the_api_constant():
    from packages.api import maps
    assert sg.NEEDS_REBUILD_KEY == maps.SCENE_GRAPH_NEEDS_REBUILD_KEY


def test_disabled_maps_only_an_explicit_false_counts():
    status = {"off": {sg.ENABLED_KEY: False}, "on": {sg.ENABLED_KEY: True}, "unset": {},
              "junk": {sg.ENABLED_KEY: "no"}}
    conn = StatusConn(status)
    g = sg.PgGuard(lambda: conn)
    assert g.disabled_maps(["off", "on", "unset", "junk", ""]) == {"off"}
    assert g.disabled_maps([]) == set()
    conn.broken = True
    try:
        g.disabled_maps(["off"])
    except RuntimeError:
        pass
    else:
        raise AssertionError("an unanswered lookup must raise so the caller keeps its answer")


def test_rebuild_request_read_and_compare_and_clear():
    req = {"at": "2026-10-12T10:00:00+00:00", "reason": "nodes_deleted", "nodes": 3}
    status = {"m": {sg.NEEDS_REBUILD_KEY: dict(req)}, "none": {}}
    g = sg.PgGuard(lambda: StatusConn(status))
    assert g.rebuild_request("m") == req
    assert g.rebuild_request("none") is None and g.rebuild_request("missing") is None
    # a newer delete arrived during the rebuild: clearing the old `at` must keep the key
    status["m"][sg.NEEDS_REBUILD_KEY]["at"] = "2026-10-12T10:05:00+00:00"
    assert g.clear_rebuild_request("m", req["at"]) is False
    assert sg.NEEDS_REBUILD_KEY in status["m"]
    assert g.clear_rebuild_request("m", "2026-10-12T10:05:00+00:00") is True
    assert sg.NEEDS_REBUILD_KEY not in status["m"]
