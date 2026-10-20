"""The bundle: a tar holding a manifest and the exported entities (DATA_MIGRATION.md §3, principle 2).

    manifest.json                       first member; what is inside, where it came from, a SHA-256 per file
    maps/<q>/map.json                   the map's Postgres row (spec, status), view settings, sessions
    maps/<q>/nodes.ndjson               one ArangoDB node document per line
    maps/<q>/edges.ndjson               one edge document per line
    maps/<q>/objects/<key>              MinIO objects of the map (images, depth, costmaps, ...), byte for byte

`<q>` is the map name URL-quoted, so any name is a safe path. Large things are files, never embedded in JSON.
The reader refuses a bundle with a newer `schema_version` than it knows, and `verify()` re-hashes every file.
"""
import datetime
import hashlib
import io
import json
import os
import tarfile
import tempfile
from typing import Any, BinaryIO, Dict, Iterable, Iterator, List, Optional
from urllib.parse import quote

SCHEMA_VERSION = 1
KIND = "satibundle"
MANIFEST = "manifest.json"
CHUNK = 1 << 20


class BundleError(Exception):
    """The bundle is unreadable, newer than this code, or fails verification."""


def map_dir(name: str) -> str:
    return "maps/" + quote(name, safe="")


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class BundleWriter:
    """Collects files in a scratch directory (hashing as they are written), then packs the tar.

    Use as a context manager: the scratch directory is removed on exit."""

    def __init__(self, source: Optional[Dict[str, Any]] = None, scope: Optional[Dict[str, Any]] = None,
                 tmp_dir: Optional[str] = None):
        self._tmp = tempfile.TemporaryDirectory(prefix="satibundle-", dir=tmp_dir)
        self.root = self._tmp.name
        self.source = dict(source or {})
        self.scope = dict(scope or {})
        self.files: Dict[str, Dict[str, Any]] = {}
        self.items: List[Dict[str, Any]] = []

    def __enter__(self) -> "BundleWriter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._tmp.cleanup()

    def _path(self, rel: str) -> str:
        if rel.startswith("/") or ".." in rel.split("/") or rel == MANIFEST:
            raise BundleError(f"bad bundle path {rel!r}")
        if rel in self.files:
            raise BundleError(f"{rel} is added twice")
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def add_stream(self, rel: str, chunks: Iterable[bytes]) -> Dict[str, Any]:
        """Write `chunks` as `rel`; returns {size, sha256}. A failing iterator leaves nothing behind."""
        path = self._path(rel)
        digest, size = hashlib.sha256(), 0
        try:
            with open(path, "wb") as f:
                for chunk in chunks:
                    f.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        except BaseException:
            if os.path.exists(path):
                os.remove(path)
            raise
        entry = {"size": size, "sha256": digest.hexdigest()}
        self.files[rel] = entry
        return entry

    def add_bytes(self, rel: str, data: bytes) -> Dict[str, Any]:
        return self.add_stream(rel, [data])

    def add_json(self, rel: str, doc: Any) -> Dict[str, Any]:
        return self.add_bytes(rel, json.dumps(doc, sort_keys=True, indent=1, default=str).encode())

    def add_ndjson(self, rel: str, docs: Iterable[Any]) -> Dict[str, Any]:
        """One compact JSON document per line, in the order given. Returns {size, sha256, count}."""
        count = 0

        def lines() -> Iterator[bytes]:
            nonlocal count
            for doc in docs:
                count += 1
                yield json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str).encode() + b"\n"
        entry = self.add_stream(rel, lines())
        entry["count"] = count
        return entry

    def manifest(self) -> Dict[str, Any]:
        return {"kind": KIND, "schema_version": SCHEMA_VERSION, "created_at": _utcnow(),
                "source": self.source, "scope": self.scope, "items": self.items,
                "files": {k: {"size": v["size"], "sha256": v["sha256"]} for k, v in sorted(self.files.items())}}

    def pack(self, out: BinaryIO) -> Dict[str, Any]:
        """Write the tar (manifest first) to `out`; returns the manifest."""
        manifest = self.manifest()
        raw = json.dumps(manifest, sort_keys=True, indent=1).encode()
        with tarfile.open(fileobj=out, mode="w|") as tar:
            info = tarfile.TarInfo(MANIFEST)
            info.size = len(raw)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(raw))
            for rel in sorted(self.files):
                info = tar.gettarinfo(os.path.join(self.root, rel), arcname=rel)
                info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
                with open(os.path.join(self.root, rel), "rb") as f:
                    tar.addfile(info, f)
        return manifest


class BundleReader:
    """Random access to a bundle tar on disk (a bundle is spooled to a file before it is read)."""

    def __init__(self, path: str):
        self.path = path
        try:
            self._tar = tarfile.open(path, "r:")
        except (tarfile.TarError, OSError) as exc:
            raise BundleError(f"not a bundle: {exc}") from exc
        self._members = {m.name: m for m in self._tar.getmembers() if m.isfile()}
        self.manifest = self._load_manifest()

    def close(self) -> None:
        self._tar.close()

    def __enter__(self) -> "BundleReader":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _load_manifest(self) -> Dict[str, Any]:
        member = self._members.get(MANIFEST)
        if member is None:
            raise BundleError("not a bundle: no manifest.json")
        try:
            manifest = json.load(self._tar.extractfile(member))
        except ValueError as exc:
            raise BundleError(f"manifest.json is not valid JSON: {exc}") from exc
        if not isinstance(manifest, dict) or manifest.get("kind") != KIND:
            raise BundleError("not a bundle: wrong kind")
        version = manifest.get("schema_version")
        if not isinstance(version, int) or version < 1:
            raise BundleError("manifest has no valid schema_version")
        if version > SCHEMA_VERSION:
            raise BundleError(f"bundle schema {version} is newer than this server understands "
                              f"({SCHEMA_VERSION}); update the server that imports it")
        return manifest

    def has(self, rel: str) -> bool:
        return rel in self._members

    def open(self, rel: str) -> BinaryIO:
        member = self._members.get(rel)
        if member is None:
            raise BundleError(f"{rel} is not in the bundle")
        return self._tar.extractfile(member)

    def read_json(self, rel: str) -> Any:
        with self.open(rel) as f:
            return json.load(f)

    def iter_ndjson(self, rel: str) -> Iterator[Any]:
        with self.open(rel) as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)

    def verify(self, only: Optional[Iterable[str]] = None) -> List[str]:
        """Re-hash files against the manifest; the list of problems (empty = the bundle is intact).

        Also reports files in the tar the manifest does not list, and listed files that are missing."""
        listed = self.manifest.get("files") or {}
        wanted = set(only) if only is not None else set(listed)
        problems: List[str] = []
        for rel in sorted(wanted):
            entry = listed.get(rel)
            if entry is None:
                problems.append(f"{rel}: not listed in the manifest")
                continue
            if rel not in self._members:
                problems.append(f"{rel}: listed but missing")
                continue
            digest, size = hashlib.sha256(), 0
            with self.open(rel) as f:
                for chunk in iter(lambda: f.read(CHUNK), b""):
                    digest.update(chunk)
                    size += len(chunk)
            if size != entry.get("size") or digest.hexdigest() != entry.get("sha256"):
                problems.append(f"{rel}: size or hash differs from the manifest")
        if only is None:
            for rel in sorted(set(self._members) - set(listed) - {MANIFEST}):
                problems.append(f"{rel}: in the bundle but not listed in the manifest")
        return problems
