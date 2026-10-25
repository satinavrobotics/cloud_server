"""A bounded cloud.ply: `GET .../reconstruction/files/cloud.ply?max_points=N`.

The reconstruction service writes one big binary_little_endian PLY (fixed-size vertex records,
no other elements; see reconstruction_topview.read_ply_header). A client that only draws a few
million points should not download all of it. This module builds, once per (file, bucket), a
valid PLY holding every k-th vertex, k = ceil(total / bucket), and keeps it on disk.

Rules (docs/reconstruction/point_budget.md):
  * N is rounded UP to a bucket (BUCKETS). The answer has at most `bucket` vertices, exactly
    ceil(total / k); when total <= bucket the file is returned as it is.
  * Streaming: the source is read in READ_CHUNK pieces and only the kept records are written;
    memory is one chunk plus the kept slices of that chunk, never the file.
  * The result is written to a temp file and renamed (atomic); the cache is bounded in files
    and bytes, least recently used first. A concurrent request for the same entry waits for the
    first one (a per-entry lock) and then reads the cache.
  * Fail soft: anything that is not what the writer produces (ASCII, big endian, other
    elements, size not matching the header, a truncated stream) gives `None` and the caller
    sends the full file.

No numpy here (the API process has none); a kept record is one bytes slice.
"""
import hashlib
import logging
import os
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, Iterable, Iterator, List, NamedTuple, Optional, Tuple

from packages.api.reconstruction_topview import MAX_HEADER, PLY_TYPES, PlyError, read_ply_header

logger = logging.getLogger(__name__)

MIN_POINTS, MAX_POINTS = 1000, 20_000_000
BUCKETS = (1_000, 2_000, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000,
           1_000_000, 2_000_000, 3_000_000, 5_000_000, 10_000_000, 20_000_000)
READ_CHUNK = 4 * 1024 * 1024
STALE_TMP_S = 3600


def bucket_for(n: int) -> int:
    """The smallest bucket >= n (n is 1000..20 M, so one always exists)."""
    for b in BUCKETS:
        if b >= n:
            return b
    return BUCKETS[-1]


def etag_for(job_id: str, n: int) -> str:
    """The ETag of the answer for `n` of the job's cloud: a function of (job, bucket) only, so a
    304 needs no work. The job id changes with every rebuild of the cloud."""
    return f'"{job_id}-lod{bucket_for(n)}"'


class Lod(NamedTuple):
    total: int
    returned: int
    path: Optional[str]  # the cached decimated file, or None: send the source as it is


# --- the cache ---------------------------------------------------------------------------------

class LodCache:
    def __init__(self, directory: Optional[str] = None, max_files: int = 6,
                 max_bytes: int = 1536 * 1024 * 1024):
        self.directory = directory or os.path.join(tempfile.gettempdir(), "satinav-recon-lod")
        self.max_files, self.max_bytes = max(1, max_files), max_bytes
        self._locks: Dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def path(self, bucket_name: str, key: str, size: Optional[int], bucket: int) -> str:
        digest = hashlib.sha1(f"{bucket_name}/{key}/{size}".encode()).hexdigest()[:24]
        return os.path.join(self.directory, f"{digest}-{bucket}.ply")

    def lock(self, path: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(path, threading.Lock())

    def touch(self, path: str) -> None:
        try:
            os.utime(path, None)
        except OSError:
            pass

    def tmp_path(self) -> str:
        os.makedirs(self.directory, exist_ok=True)
        return os.path.join(self.directory, f".tmp-{uuid.uuid4().hex}")

    def evict(self, keep: str) -> List[str]:
        """Delete stale temp files, then the least recently used entries beyond the limits
        (never `keep`, the entry just written). Returns the deleted names."""
        gone, entries, now = [], [], time.time()
        try:
            names = os.listdir(self.directory)
        except OSError:
            return gone
        for name in names:
            full = os.path.join(self.directory, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if name.startswith(".tmp-"):
                if now - st.st_mtime > STALE_TMP_S:
                    self._rm(full, gone)
            elif name.endswith(".ply"):
                entries.append((st.st_mtime, st.st_size, full))
        entries.sort()  # oldest use first
        total = sum(e[1] for e in entries)
        count = len(entries)
        for _, size, full in entries:
            if count <= self.max_files and total <= self.max_bytes:
                break
            if full == keep:
                continue
            count -= 1
            total -= size
            self._rm(full, gone)
        return gone

    @staticmethod
    def _rm(path: str, gone: List[str]) -> None:
        try:
            os.remove(path)
            gone.append(os.path.basename(path))
        except OSError:
            pass


# --- the PLY work (pure, testable without MinIO) -----------------------------------------------

def _record_size(props: List[Tuple[str, str]]) -> int:
    return sum(PLY_TYPES[t][1] for _, t in props)


def _header_and_rest(chunks: Iterator[bytes]) -> Tuple[bytes, bytearray]:
    """(the header bytes up to and including end_header\\n, whatever followed in the chunks read)."""
    buf = bytearray()
    for chunk in chunks:
        buf += chunk
        end = buf.find(b"end_header\n")
        if end >= 0:
            cut = end + len(b"end_header\n")
            return bytes(buf[:cut]), bytearray(buf[cut:])
        if len(buf) > MAX_HEADER:
            break
    raise PlyError("no end_header in the first 64 KiB")


def rewrite_header(header: bytes, total: int, returned: int, stride: int) -> bytes:
    """The header with `element vertex <returned>` and a comment saying what was done."""
    out, done = [], False
    for line in header.split(b"\n")[:-1]:  # the last piece is empty (ends with \n)
        if not done and line.strip().startswith(b"element vertex"):
            out.append(f"comment satinav lod stride={stride} total={total}".encode())
            out.append(f"element vertex {returned}".encode())
            done = True
        else:
            out.append(line)
    if not done:
        raise PlyError("no element vertex line")
    return b"\n".join(out) + b"\n"


def decimate_stream(chunks: Iterator[bytes], out, total: int, rec: int, stride: int,
                    rest: bytearray) -> int:
    """Write every `stride`-th of the `total` records of `rest` + `chunks` to `out`; the number
    of records written. ValueError if the stream ends before `total` records."""
    buf, index, written = rest, 0, 0  # `index` = global number of the record at buf[0]
    chunks = iter(chunks)

    def take() -> None:
        nonlocal index, written
        n = min(len(buf) // rec, total - index)
        if n <= 0:
            return
        view = memoryview(buf)
        first = (-index) % stride
        picked = range(first, n, stride)
        out.write(b"".join([view[j * rec:(j + 1) * rec] for j in picked]))
        written += len(picked)
        view.release()
        del buf[:n * rec]
        index += n

    take()
    while index < total:
        chunk = next(chunks, None)
        if chunk is None:
            raise ValueError(f"PLY truncated: {index} of {total} vertices")
        buf += chunk
        take()
    return written


def build(chunks: Iterable[bytes], source_size: Optional[int], bucket: int, final: str,
          cache: LodCache) -> Optional[Lod]:
    """Decimate the PLY in `chunks` to at most `bucket` vertices into `final`. None = not
    a PLY this module handles (send the source); Lod(path=None) = it is small enough."""
    it = iter(chunks)
    header, rest = _header_and_rest(it)
    header_len, total, props = read_ply_header(header)
    rec = _record_size(props)
    if source_size is not None and source_size != header_len + total * rec:
        raise PlyError(f"size {source_size} does not match the header ({total} x {rec} B)")
    if total <= bucket:
        return Lod(total, total, None)
    stride = -(-total // bucket)
    returned = len(range(0, total, stride))
    tmp = cache.tmp_path()
    try:
        with open(tmp, "wb") as f:
            f.write(rewrite_header(header, total, returned, stride))
            written = decimate_stream(it, f, total, rec, stride, rest)
        if written != returned:
            raise ValueError(f"wrote {written} vertices, expected {returned}")
        os.replace(tmp, final)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    cache.evict(keep=final)
    return Lod(total, returned, final)


def cached_info(path: str) -> Optional[Lod]:
    """(total, returned, path) of a cache file from its header, or None if unreadable."""
    try:
        with open(path, "rb") as f:
            head = f.read(MAX_HEADER + 16)
        _, returned, _ = read_ply_header(head)
        total = returned
        for line in head.split(b"end_header")[0].split(b"\n"):
            if line.startswith(b"comment satinav lod "):
                for word in line.split():
                    if word.startswith(b"total="):
                        total = int(word[6:])
        return Lod(total, returned, path)
    except (OSError, PlyError, ValueError):
        return None


def plan(objects: Any, info: Dict[str, Any], max_points: int, cache: LodCache) -> Optional[Lod]:
    """Blocking (run in a thread). The decimated cloud for `info` (gateway.open_file) with at
    most bucket_for(max_points) vertices: a Lod with a path (send that file), a Lod without
    (send the source, the counts are known), or None (not our kind of PLY: send the source)."""
    bucket = bucket_for(max_points)
    final = cache.path(info["bucket"], info["key"], info.get("bytes"), bucket)
    hit = cached_info(final) if os.path.exists(final) else None
    if hit:
        cache.touch(final)
        return hit
    with cache.lock(final):
        hit = cached_info(final) if os.path.exists(final) else None
        if hit:
            cache.touch(final)
            return hit
        stream = objects.stream(info["bucket"], info["key"], READ_CHUNK)
        try:
            return build(stream, info.get("bytes"), bucket, final, cache)
        except (PlyError, ValueError) as exc:
            logger.info("reconstruction lod: sending %s/%s whole: %s",
                        info["bucket"], info["key"], exc)
            return None
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()
