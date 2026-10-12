"""Where snapshot objects and the small per-map documents live.

`Blobs`: immutable objects by name under a map. `Docs`: one meta document per map (compare-and-set on its version) and an
index of the current version (anchors and links, for AQL and display; the manifest is the truth for pinned reads).
`MemoryBackend` serves tests; `ArangoMinioBackend` is the real one (MinIO `map-{id}/scene_graph/...`, ArangoDB per-map
`sg_anchors_{id}`, `sg_links_{id}`, `sg_meta_{id}`, the same pattern as `nodes_{id}` so the delete saga drops them).
"""
import io
from typing import Dict, Iterable, List, Optional, Protocol

PREFIX = "scene_graph/"


class Blobs(Protocol):
    def put(self, map_id: str, key: str, data: bytes) -> None: ...
    def get(self, map_id: str, key: str) -> Optional[bytes]: ...
    def exists(self, map_id: str, key: str) -> bool: ...
    def list(self, map_id: str, prefix: str = "") -> List[str]: ...
    def delete(self, map_id: str, keys: Iterable[str]) -> None: ...


class Docs(Protocol):
    def meta(self, map_id: str) -> Optional[dict]: ...
    def cas_meta(self, map_id: str, expected_version: Optional[int], meta: dict) -> bool: ...
    def put_index(self, map_id: str, version: int, anchors: List[dict], links: List[dict]) -> None: ...
    def index_version(self, map_id: str) -> Optional[int]: ...
    def drop(self, map_id: str) -> None: ...


class MemoryBackend:
    """Both protocols in memory. `fail_after` makes the n-th next write raise (crash injection)."""

    def __init__(self):
        self.blobs: Dict[str, Dict[str, bytes]] = {}
        self._meta: Dict[str, dict] = {}
        self._index: Dict[str, dict] = {}
        self.puts = 0
        self.fail_on = None            # callable(kind: str, name: str) -> raise to inject a crash

    # Blobs
    def put(self, map_id, key, data):
        if self.fail_on: self.fail_on("blob", key)
        self.blobs.setdefault(map_id, {})[key] = bytes(data); self.puts += 1

    def get(self, map_id, key): return self.blobs.get(map_id, {}).get(key)
    def exists(self, map_id, key): return key in self.blobs.get(map_id, {})
    def list(self, map_id, prefix=""): return sorted(k for k in self.blobs.get(map_id, {}) if k.startswith(prefix))

    def delete(self, map_id, keys):
        for k in list(keys): self.blobs.get(map_id, {}).pop(k, None)

    # Docs
    def meta(self, map_id):
        m = self._meta.get(map_id)
        return None if m is None else dict(m)

    def cas_meta(self, map_id, expected_version, meta):
        if self.fail_on: self.fail_on("meta", str(meta.get("version")))
        cur = self._meta.get(map_id)
        if (cur["version"] if cur else None) != expected_version:
            return False
        self._meta[map_id] = dict(meta)
        return True

    def put_index(self, map_id, version, anchors, links):
        self._index[map_id] = dict(version=version, anchors=anchors, links=links)

    def index_version(self, map_id):
        i = self._index.get(map_id)
        return None if i is None else i["version"]

    def drop(self, map_id):
        self.blobs.pop(map_id, None); self._meta.pop(map_id, None); self._index.pop(map_id, None)


class ArangoMinioBackend:
    """Adapter over a `TopomapDatabaseClient` (duck-typed: `.graph.db` python-arango Database, `.image.client` Minio,
    `.image._bucket_name`, `.image._ensure_map_bucket`). Writes only under `scene_graph/` of the map's bucket and to
    the three per-map collections."""

    CAS_UPDATE = ("FOR d IN @@c FILTER d._key == 'meta' AND d.version == @exp "
                  "UPDATE d WITH @meta IN @@c OPTIONS {mergeObjects: false} RETURN NEW._key")

    def __init__(self, client):
        self.c = client

    # -- blobs
    def _bucket(self, map_id): return self.c.image._bucket_name(map_id)

    def put(self, map_id, key, data):
        if not self.c.image._ensure_map_bucket(map_id):
            raise IOError(f"bucket of {map_id} unavailable")
        self.c.image.client.put_object(self._bucket(map_id), PREFIX + key, io.BytesIO(data), length=len(data),
                                       content_type="application/octet-stream")

    def get(self, map_id, key):
        try:
            r = self.c.image.client.get_object(self._bucket(map_id), PREFIX + key)
            try: return r.read()
            finally: r.close(); r.release_conn()
        except Exception as e:
            if getattr(e, "code", "") in ("NoSuchKey", "NoSuchBucket"): return None
            raise

    def exists(self, map_id, key):
        try:
            self.c.image.client.stat_object(self._bucket(map_id), PREFIX + key); return True
        except Exception as e:
            if getattr(e, "code", "") in ("NoSuchKey", "NoSuchBucket", "NotFound"): return False
            raise

    def list(self, map_id, prefix=""):
        try:
            return sorted(o.object_name[len(PREFIX):] for o in
                          self.c.image.client.list_objects(self._bucket(map_id), prefix=PREFIX + prefix, recursive=True))
        except Exception as e:
            if getattr(e, "code", "") == "NoSuchBucket": return []
            raise

    def delete(self, map_id, keys):
        for k in keys: self.c.image.client.remove_object(self._bucket(map_id), PREFIX + k)

    # -- docs
    def _col(self, map_id, kind, edge=False):
        name = f"sg_{kind}_{map_id}"; db = self.c.graph.db
        if not db.has_collection(name): db.create_collection(name, edge=edge)
        return db.collection(name)

    def meta(self, map_id):
        db = self.c.graph.db; name = f"sg_meta_{map_id}"
        if not db.has_collection(name): return None
        d = db.collection(name).get("meta")
        return None if d is None else {k: v for k, v in d.items() if not k.startswith("_")}

    def cas_meta(self, map_id, expected_version, meta):
        col = self._col(map_id, "meta")
        if expected_version is None:
            try: col.insert({**meta, "_key": "meta"}, overwrite=False); return True
            except Exception as e:
                if getattr(e, "http_code", None) == 409 or "unique" in str(e).lower(): return False
                raise
        cur = self.c.graph.db.aql.execute(self.CAS_UPDATE, bind_vars={"@c": col.name, "exp": expected_version, "meta": meta})
        return bool(list(cur))

    def put_index(self, map_id, version, anchors, links):
        a, l = self._col(map_id, "anchors"), self._col(map_id, "links", edge=True)
        a.truncate(); l.truncate()
        if anchors: a.insert_many([{**d, "_key": d["key"], "version": version} for d in anchors], overwrite=True)
        if links:
            l.insert_many([{**d, "_key": f"{i}", "_from": f"sg_anchors_{map_id}/{d['a']}", "_to": f"sg_anchors_{map_id}/{d['b']}",
                            "version": version} for i, d in enumerate(links)], overwrite=True)
        self._col(map_id, "meta").update({"_key": "meta", "index_version": version})

    def index_version(self, map_id):
        m = self.meta(map_id)
        return None if m is None else m.get("index_version")

    def drop(self, map_id):
        db = self.c.graph.db
        for kind in ("anchors", "links", "meta"):
            if db.has_collection(f"sg_{kind}_{map_id}"): db.delete_collection(f"sg_{kind}_{map_id}")
        try: self.delete(map_id, self.list(map_id))
        except Exception: pass
