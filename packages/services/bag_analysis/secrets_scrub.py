"""Drop the service's credentials from the process once its clients exist.

`os.environ.pop` alone is not enough: Linux serves /proc/<pid>/environ from the ORIGINAL environment block on the
stack, which unsetenv leaves untouched, and an analyzer subprocess running as the same uid can read
/proc/<ppid>/environ. `scrub_environ` therefore pops the names AND overwrites their values in that block
(best effort, Linux only). It does not make analyzers untrusted-safe: same uid, same network namespace
(docs/bag_analysis_server_2026-10-28.md, "Trust model").
"""
import ctypes
import logging
import os
from typing import Iterable, List

logger = logging.getLogger("bag_analysis.secrets")

SECRET_NAMES = ("MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD", "POSTGRES_DATABASE_PASSWORD",
                "ARANGO_PASSWORD")


def _env_block() -> "tuple[int, int]":
    """(start, end) address of this process's initial environment block, from /proc/self/stat fields 50 and 51."""
    with open("/proc/self/stat") as f:
        rest = f.read().rsplit(")", 1)[1].split()      # rest[0] is field 3
    return int(rest[47]), int(rest[48])


def blank_initial_environ(names: Iterable[str]) -> List[str]:
    """Overwrite with NUL the values of NAME=value entries in the initial environment block. Returns the names
    blanked. Never raises (a platform without the block simply blanks nothing)."""
    wanted = {n.encode() + b"=" for n in names}
    done: List[str] = []
    try:
        start, end = _env_block()
        if not start or end <= start:
            return done
        block = ctypes.string_at(start, end - start)
        pos = 0
        for entry in block.split(b"\0"):
            for w in wanted:
                if entry.startswith(w) and len(entry) > len(w):
                    ctypes.memset(start + pos + len(w), 0, len(entry) - len(w))
                    done.append(w[:-1].decode())
            pos += len(entry) + 1
    except Exception:  # noqa: BLE001
        logger.debug("could not blank the initial environment block", exc_info=True)
    return done


def scrub_environ(names: Iterable[str] = SECRET_NAMES) -> None:
    names = tuple(names)
    blank_initial_environ(names)
    for n in names:
        os.environ.pop(n, None)
