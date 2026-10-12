import logging
from types import SimpleNamespace

from packages.topomap_dbs.graph_db.server import GraphDatabaseService as G


class _Col:
    def __init__(self):
        self.indexes = []

    def add_persistent_index(self, fields):
        self.indexes.append(fields)


class _Db:
    def __init__(self, exists=True):
        self.exists, self.col, self.calls = exists, _Col(), []
        self.aql = SimpleNamespace(execute=self._execute)

    def has_collection(self, name):
        return self.exists

    def collection(self, name):
        return self.col

    def _execute(self, aql, bind_vars=None):
        self.calls.append((aql, bind_vars))
        return iter([{"_key": "k1", "costmap_stored_ms": 5}])


def _svc(db):
    return SimpleNamespace(db=db, logger=logging.getLogger("t"), _costmap_indexed=set(),
                           COSTMAP_SINCE_AQL=G.COSTMAP_SINCE_AQL)


def test_set_costmap_updates_stamp_the_database_clock():
    assert "costmap_stored_ms: DATE_NOW()" in G.SET_NODE_COSTMAP_AQL
    assert "costmap_stored_ms: DATE_NOW()" in G.SET_NODE_COSTMAPS_AQL
    assert "DATE_NOW" not in G.SET_NODE_DEPTH_AQL


def test_cursor_query_binds_and_ensures_index_once():
    db = _Db()
    svc = _svc(db)
    rows = G.costmap_nodes_since(svc, "m", 10, "k0", limit=7)
    assert rows == [{"_key": "k1", "costmap_stored_ms": 5}]
    aql, binds = db.calls[0]
    assert binds == {"@col": "nodes_m", "since": 10, "after": "k0", "limit": 7}
    assert "SORT n.costmap_stored_ms, n._key" in aql
    G.costmap_nodes_since(svc, "m")
    assert db.col.indexes == [["costmap_stored_ms"]]


def test_cursor_query_without_collection():
    db = _Db(exists=False)
    assert G.costmap_nodes_since(_svc(db), "m") == []
    assert db.calls == []


def test_set_scene_stamps_first_write_wins_query_and_binds():
    db = _Db()
    svc = SimpleNamespace(db=db, SET_SCENE_STAMPS_AQL=G.SET_SCENE_STAMPS_AQL)
    n = G.set_scene_stamps(svc, "m", {"a": 5, "b": 7})
    aql, binds = db.calls[0]
    assert "d.scene_stamp_ms == null" in aql
    assert binds == {"@col": "nodes_m", "items": [{"key": "a", "ms": 5}, {"key": "b", "ms": 7}]}
    assert n == 1                      # the fake cursor yields one row
    assert G.set_scene_stamps(svc, "m", {}) == 0
