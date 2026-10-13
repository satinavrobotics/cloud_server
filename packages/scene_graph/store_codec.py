"""Byte format of the snapshot objects: immutable, content-addressed, deterministic.

An object is `SGT1 | u32 header length | header JSON | zlib(payload)`; the payload is the C-contiguous bytes of the named
arrays in header order. The object's name is the hash of (header + raw payload), so equal content is one object whatever
compressor produced the bytes, and a save writes only what is new."""
import hashlib
import json
import struct
import zlib
from typing import Dict, Tuple

import numpy as np

MAGIC = b"SGT1"


def hash_arrays(arrays: Dict[str, np.ndarray], meta: dict = None) -> Tuple[str, bytes, bytes]:
    """(name, header, raw payload) of an object: the name needs no compression, so a caller that already holds this name
    skips the compressor and the write."""
    names = list(arrays)
    head = json.dumps({"a": [[n, arrays[n].dtype.str, list(arrays[n].shape)] for n in names], "m": meta or {}},
                      sort_keys=True, separators=(",", ":")).encode()
    raw = b"".join(np.ascontiguousarray(arrays[n]).tobytes() for n in names)
    return hashlib.sha256(head + raw).hexdigest()[:32], head, raw


def compress_object(head: bytes, raw: bytes) -> bytes:
    return MAGIC + struct.pack("<I", len(head)) + head + zlib.compress(raw, 6)


def pack(arrays: Dict[str, np.ndarray], meta: dict = None) -> Tuple[str, bytes]:
    h, head, raw = hash_arrays(arrays, meta)
    return h, compress_object(head, raw)


def unpack(blob: bytes) -> Tuple[Dict[str, np.ndarray], dict]:
    if blob[:4] != MAGIC:
        raise ValueError("not a scene-graph object")
    (n,) = struct.unpack("<I", blob[4:8])
    head = json.loads(blob[8:8 + n])
    raw = zlib.decompress(blob[8 + n:])
    out, off = {}, 0
    for name, dt, shape in head["a"]:
        dt = np.dtype(dt); size = int(np.prod(shape)) * dt.itemsize
        out[name] = np.frombuffer(raw, dt, count=int(np.prod(shape)), offset=off).reshape(shape).copy()
        off += size
    return out, head["m"]


def canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def params_hash(params: dict, pipeline: str) -> str:
    """the hash of the VALUES of the params: numbers are hashed as floats, so 2 and 2.0 (which a document store returns in
    either spelling after a round trip) give the same hash"""
    norm = {k: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v for k, v in params.items()}
    return hashlib.sha256(canonical_json({"p": norm, "v": pipeline})).hexdigest()[:16]


def pack_json(obj) -> Tuple[str, bytes]:
    """A page of records: canonical JSON, compressed; named by the hash of the canonical bytes."""
    raw = canonical_json(obj)
    return hashlib.sha256(raw).hexdigest()[:32], b"SGJ1" + zlib.compress(raw, 6)


def unpack_json(blob: bytes):
    if blob[:4] != b"SGJ1":
        raise ValueError("not a scene-graph page")
    return json.loads(zlib.decompress(blob[4:]))
