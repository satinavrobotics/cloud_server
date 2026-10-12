import logging
from types import SimpleNamespace

from packages.topomap_dbs.graph_db.server import (GraphDatabaseService,
                                                  SCENE_GRAPH_COLLECTION_PREFIXES)


class _Db:
    def __init__(self, names):
        self.names = set(names)
        self.deleted = []

    def has_graph(self, name):
        return False

    def has_collection(self, name):
        return name in self.names

    def delete_collection(self, name):
        self.deleted.append(name)
        self.names.discard(name)


def _service(db):
    return SimpleNamespace(db=db, logger=logging.getLogger("t"))


def test_delete_map_drops_scene_graph_index():
    names = ["nodes_m", "edges_m"] + [f"{p}m" for p in SCENE_GRAPH_COLLECTION_PREFIXES]
    db = _Db(names + ["sg_meta_other"])
    assert GraphDatabaseService.delete_map(_service(db), "m") is True
    assert sorted(db.deleted) == sorted(names)
    assert db.names == {"sg_meta_other"}


def test_delete_map_without_scene_graph_collections():
    db = _Db(["nodes_m", "edges_m"])
    assert GraphDatabaseService.delete_map(_service(db), "m") is True
    assert sorted(db.deleted) == ["edges_m", "nodes_m"]
