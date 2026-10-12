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
