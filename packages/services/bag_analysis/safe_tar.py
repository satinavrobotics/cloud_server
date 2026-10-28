"""Safe extraction of an uploaded bag tar, from a stream (never a seekable file, never the whole tar on disk).

The tar comes from a robot over a presigned URL and is untrusted. Only regular files and directories are accepted;
every other member type (symlink, hardlink, device, fifo) rejects the archive, which is stricter than "links that
point outside" and costs nothing: a rosbag2 directory has none. Also rejected: absolute names, '..', NUL or
backslash in a name, a duplicate file, more than `max_members` members, more than `max_bytes` of data (header sums
first, then the bytes really written), and a stream longer than `max_stream_bytes` (a tar can lie about nothing:
the counter is on the bytes read). Files are created with O_EXCL|O_NOFOLLOW so nothing is overwritten or followed.
"""
import os
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Optional

CHUNK = 1 << 20


class UnsafeArchive(Exception):
    """The archive violates a rule (message says which); nothing it extracted is to be trusted."""


class CountingReader:
    """Wraps a stream; raises UnsafeArchive once more than `limit` bytes were read."""

    def __init__(self, stream: Any, limit: int):
        self._stream, self._limit, self.count = stream, limit, 0

    def read(self, n: int = -1) -> bytes:
        data = self._stream.read(n)
        self.count += len(data)
        if self.count > self._limit:
            raise UnsafeArchive(f"stream longer than the {self._limit} byte limit")
        return data


def clean_name(name: str) -> Optional[PurePosixPath]:
    """The member name as a relative path, None for '.' / empty; UnsafeArchive when it is not acceptable."""
    if "\x00" in name or "\\" in name:
        raise UnsafeArchive(f"bad character in member name {name!r}")
    p = PurePosixPath(name)
    if p.is_absolute():
        raise UnsafeArchive(f"absolute member name {name!r}")
    if ".." in p.parts:
        raise UnsafeArchive(f"'..' in member name {name!r}")
    parts = [x for x in p.parts if x != "."]
    return PurePosixPath(*parts) if parts else None


def extract_stream(stream: Any, dest: Path, *, max_members: int, max_bytes: int,
                   max_stream_bytes: Optional[int] = None) -> Dict[str, Any]:
    """Extract a tar stream into the (existing, empty) directory `dest`. Returns {members, bytes, bag_dir}:
    bag_dir is the directory holding metadata.yaml (the top directory of the tar, or `dest` itself)."""
    dest = dest.resolve()
    reader = CountingReader(stream, max_stream_bytes) if max_stream_bytes else stream
    members = total = declared = 0
    try:
        with tarfile.open(fileobj=reader, mode="r|*") as tf:
            for m in tf:
                members += 1
                if members > max_members:
                    raise UnsafeArchive(f"more than {max_members} members")
                rel = clean_name(m.name)
                if rel is None:
                    continue
                target = dest.joinpath(*rel.parts)
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not m.isreg():
                    raise UnsafeArchive(f"member {m.name!r} is a {_kind(m)}, only files and directories are accepted")
                declared += m.size
                if declared > max_bytes:
                    raise UnsafeArchive(f"members declare more than {max_bytes} bytes")
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    target.parent.resolve().relative_to(dest)
                except ValueError:
                    raise UnsafeArchive(f"member {m.name!r} escapes the destination")
                try:
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
                except FileExistsError:
                    raise UnsafeArchive(f"member {m.name!r} appears twice or collides with a directory")
                with os.fdopen(fd, "wb") as out:
                    src = tf.extractfile(m)
                    while True:
                        block = src.read(CHUNK)
                        if not block:
                            break
                        total += len(block)
                        if total > max_bytes:
                            raise UnsafeArchive(f"more than {max_bytes} bytes extracted")
                        out.write(block)
    except tarfile.TarError as e:
        raise UnsafeArchive(f"not a readable tar: {e}") from e
    except OSError as e:
        raise UnsafeArchive(f"extraction failed: {e}") from e
    return {"members": members, "bytes": total, "bag_dir": find_bag_dir(dest)}


def _kind(m: tarfile.TarInfo) -> str:
    return ("symlink" if m.issym() else "hardlink" if m.islnk() else "device" if m.isdev() else
            "fifo" if m.isfifo() else f"type {m.type!r}")


def find_bag_dir(dest: Path) -> Path:
    """The rosbag2 directory: the one with metadata.yaml, at the top of dest or one level down."""
    if (dest / "metadata.yaml").is_file():
        return dest
    found = sorted(p for p in dest.iterdir() if p.is_dir() and (p / "metadata.yaml").is_file())
    if len(found) != 1:
        raise UnsafeArchive(f"expected one rosbag2 directory with metadata.yaml, found {len(found)}")
    return found[0]


def make_read_only(root: Path) -> None:
    """Strip the write bits (files 0444, directories 0555): the bag is read, never modified."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for f in filenames:
            os.chmod(os.path.join(dirpath, f), 0o444)
        os.chmod(dirpath, 0o555)


def remove_tree(path: Path) -> None:
    """rmtree that also removes read-only trees (restores the write bit on the way)."""
    import shutil

    def fix(func, p, _exc):
        os.chmod(os.path.dirname(p), 0o755)
        os.chmod(p, 0o755)
        func(p)
    if path.exists() or path.is_symlink():
        for dirpath, dirnames, _ in os.walk(path):
            try:
                os.chmod(dirpath, 0o755)
            except OSError:
                pass
        shutil.rmtree(path, onerror=fix)
