"""The Docker engine over its unix socket (standard library only).

DockerReader sends GET requests only: it can list containers, inspect them and read their logs, never change anything.
DockerActor (below, used only by operations.py) adds the few writes the operator actions need: restart a container, and
create, start and remove the one-shot container that runs a robot deploy. In both, nothing a caller sends is put into a
path except a container id taken from the engine's own list.
"""

import http.client
import json
import socket
import struct
from typing import Dict, List, Optional

DEFAULT_SOCKET = "/var/run/docker.sock"
MAX_BODY_BYTES = 8_000_000      # a log answer is cut here: a container that writes huge lines cannot exhaust this service's memory
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
            body = response.read(MAX_BODY_BYTES)
        except (OSError, http.client.HTTPException) as err:
            raise DockerUnavailable(f"cannot reach the Docker socket at {self.socket_path}: {err}") from err
        finally:
            conn.close()
        if response.status != 200:
            raise DockerUnavailable(f"Docker answered {response.status} for {path.split('?')[0]}")
        return body

    def _json(self, path: str):
        try:
            return json.loads(self._get(path))
        except ValueError as err:
            raise DockerUnavailable(f"Docker sent something that is not JSON for {path.split('?')[0]}") from err

    def list_containers(self) -> List[Dict]:
        return self._json("/containers/json?all=1")

    def inspect(self, container_id: str) -> Dict:
        return self._json(f"/containers/{container_id}/json")

    def stats(self, container_id: str) -> Dict:
        """One memory sample: {usage_bytes (working set: without reclaimable file cache), engine_limit_bytes}."""
        data = self._json(f"/containers/{container_id}/stats?stream=false&one-shot=true")
        mem = data.get("memory_stats") or {}
        detail = mem.get("stats") or {}
        cache = detail.get("inactive_file", detail.get("total_inactive_file", detail.get("cache", 0))) or 0
        usage = mem.get("usage")
        return {"usage_bytes": max(0, usage - cache) if isinstance(usage, int) else None,
                "engine_limit_bytes": mem.get("limit")}

    def logs(self, container_id: str, tail: int, since: Optional[int] = None) -> List[str]:
        """The last `tail` lines (each starting with its timestamp), optionally only those after epoch `since`."""
        query = f"stdout=1&stderr=1&timestamps=1&tail={int(tail)}" + (f"&since={int(since)}" if since else "")
        text = demultiplex(self._get(f"/containers/{container_id}/logs?{query}"))
        return [line.rstrip("\r") for line in text.split("\n") if line.strip()]


class DockerActor(DockerReader):
    """DockerReader plus restart / create / start / remove, and nothing else."""

    def _send(self, method: str, path: str, body: Optional[Dict] = None, ok=(200, 201, 204)) -> bytes:
        conn = _UnixConnection(self.socket_path, max(self.timeout, 30.0))      # a restart waits for the container to stop
        try:
            payload = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, body=payload, headers={"Content-Type": "application/json"} if payload else {})
            response = conn.getresponse()
            data = response.read(MAX_BODY_BYTES)
        except (OSError, http.client.HTTPException) as err:
            raise DockerUnavailable(f"cannot reach the Docker socket at {self.socket_path}: {err}") from err
        finally:
            conn.close()
        if response.status not in ok:
            detail = data[:300].decode("utf-8", errors="replace")
            raise DockerUnavailable(f"Docker answered {response.status} for {method} {path.split('?')[0]}: {detail}")
        return data

    def restart(self, container_id: str, grace_seconds: int = 10) -> None:
        self._send("POST", f"/containers/{container_id}/restart?t={int(grace_seconds)}")

    def create(self, name: str, spec: Dict) -> str:
        """Create a container (not started); returns its id."""
        data = self._send("POST", f"/containers/create?name={name}", spec)
        return json.loads(data)["Id"]

    def start(self, container_id: str) -> None:
        self._send("POST", f"/containers/{container_id}/start")

    def remove(self, container_id: str) -> None:
        self._send("DELETE", f"/containers/{container_id}?force=1")
