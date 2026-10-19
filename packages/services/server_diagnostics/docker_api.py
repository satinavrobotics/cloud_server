"""A read-only view of the Docker engine over its unix socket (standard library only).

Only GET requests exist here: the service can list containers, inspect them and read their logs, never start, stop or
change anything, and nothing a caller sends is put into a path except a container id taken from the engine's own list.
"""

import http.client
import json
import socket
import struct
from typing import Dict, List, Optional

DEFAULT_SOCKET = "/var/run/docker.sock"
_HEADER = struct.Struct(">BxxxL")     # stream type, 3 padding bytes, payload size


class DockerUnavailable(Exception):
    """The engine could not be reached or answered with an error."""


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


def demultiplex(raw: bytes) -> str:
    """Docker's log stream of a container without a TTY is framed (8-byte header + payload); join the payloads."""
    out, pos = [], 0
    while pos + _HEADER.size <= len(raw):
        stream, size = _HEADER.unpack_from(raw, pos)
        if stream not in (0, 1, 2) or pos + _HEADER.size + size > len(raw):
            break
        out.append(raw[pos + _HEADER.size:pos + _HEADER.size + size])
        pos += _HEADER.size + size
    else:
        return b"".join(out).decode("utf-8", errors="replace")
    if not out:                       # not framed at all (a TTY container): the bytes are the text
        return raw.decode("utf-8", errors="replace")
    return b"".join(out).decode("utf-8", errors="replace")


class DockerReader:
    def __init__(self, socket_path: str = DEFAULT_SOCKET, timeout: float = 8.0):
        self.socket_path = socket_path
        self.timeout = timeout

    def _get(self, path: str) -> bytes:
        conn = _UnixConnection(self.socket_path, self.timeout)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            body = response.read()
        except (OSError, http.client.HTTPException) as err:
            raise DockerUnavailable(f"cannot reach the Docker socket at {self.socket_path}: {err}") from err
        finally:
            conn.close()
        if response.status != 200:
            raise DockerUnavailable(f"Docker answered {response.status} for {path.split('?')[0]}")
        return body

    def list_containers(self) -> List[Dict]:
        return json.loads(self._get("/containers/json?all=1"))

    def inspect(self, container_id: str) -> Dict:
        return json.loads(self._get(f"/containers/{container_id}/json"))

    def logs(self, container_id: str, tail: int, since: Optional[int] = None) -> List[str]:
        """The last `tail` lines (each starting with its timestamp), optionally only those after epoch `since`."""
        query = f"stdout=1&stderr=1&timestamps=1&tail={int(tail)}" + (f"&since={int(since)}" if since else "")
        text = demultiplex(self._get(f"/containers/{container_id}/logs?{query}"))
        return [line for line in text.splitlines() if line.strip()]
