"""Server diagnostics: which containers this server runs, how they are doing and what their logs say."""

import re
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from .analyzer import Finding, analyze, worst
from .docker_api import DockerReader, DockerUnavailable
from .redact import redact_line

COMPOSE_SERVICE_LABEL = "com.docker.compose.service"
ANALYSIS_TAIL = 400            # lines read from each container to judge it
MAX_LOG_LINES = 2000
MAX_LOG_CHARS = 400_000


class UnknownService(Exception):
    pass


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    """Docker's RFC3339 time with up to nanoseconds; the zero time ("0001-01-01...") means never."""
    if not value or value.startswith("0001-"):
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class DiagnosticsService:
    def __init__(self, docker: DockerReader, include: str = "", window_minutes: float = 15.0,
                 cache_seconds: float = 5.0, clock: Callable[[], float] = time.time):
        self._docker = docker
        self._include = re.compile(include) if include else None
        self._window = max(1.0, window_minutes) * 60.0
        self._ttl = cache_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: Optional[Tuple[float, Dict]] = None

    # ---- which containers belong to this server
    def _visible(self, container: Dict) -> bool:
        labels = container.get("Labels") or {}
        name = (container.get("Names") or ["/"])[0].lstrip("/")
        if self._include is not None:
            return bool(self._include.search(name) or self._include.search(labels.get(COMPOSE_SERVICE_LABEL, "")))
        return COMPOSE_SERVICE_LABEL in labels or name.startswith("sati_")

    def _containers(self) -> List[Dict]:
        """[{name, id, container, tty}] with unique service names, sorted."""
        found = [c for c in self._docker.list_containers() if self._visible(c)]
        counts: Dict[str, int] = {}
        for c in found:
            counts[(c.get("Labels") or {}).get(COMPOSE_SERVICE_LABEL) or c["Names"][0].lstrip("/")] = 0
        out = []
        for c in found:
            container = c["Names"][0].lstrip("/")
            service = (c.get("Labels") or {}).get(COMPOSE_SERVICE_LABEL) or container
            counts[service] = counts.get(service, 0) + 1
            out.append({"service": service, "container": container, "id": c["Id"]})
        for item in out:                          # replicas of one service keep their container names
            if counts[item["service"]] > 1:
                item["service"] = item["container"]
        return sorted(out, key=lambda i: i["service"])

    def _find(self, name: str) -> Dict:
        for item in self._containers():
            if item["service"] == name or item["container"] == name:
                return item
        raise UnknownService(name)

    # ---- state
    def _state(self, container_id: str) -> Dict:
        info = self._docker.inspect(container_id)
        st = info.get("State") or {}
        health = (st.get("Health") or {}).get("Status")
        status = "restarting" if st.get("Restarting") else (st.get("Status") or "unknown")
        if status not in ("running", "exited", "restarting", "paused", "dead", "created"):
            status = "unknown"
        return {
            "state": status,
            "health": health if health in ("healthy", "unhealthy", "starting") else None,
            "restart_count": int(info.get("RestartCount") or 0),
            "exit_code": st.get("ExitCode") if status in ("exited", "dead", "restarting") else None,
            "oom_killed": bool(st.get("OOMKilled")),
            "started_at": _parse_time(st.get("StartedAt")),
            "image": (info.get("Config") or {}).get("Image", ""),
        }

    def _lines(self, container_id: str, tail: int, since: Optional[float] = None) -> List[str]:
        return [redact_line(line) for line in self._docker.logs(container_id, tail, since=int(since) if since else None)]

    def _judge(self, item: Dict) -> Tuple[Dict, List[Finding]]:
        state = self._state(item["id"])
        now = datetime.fromtimestamp(self._clock(), tz=timezone.utc)
        try:
            lines = self._lines(item["id"], ANALYSIS_TAIL, since=self._clock() - self._window)
        except DockerUnavailable:
            lines = []
        return state, analyze(state, lines, now, self._window)

    # ---- the three answers
    def overview(self) -> Dict:
        """Every service with its state and a count of findings; cached for a few seconds (the UI polls)."""
        with self._lock:
            now = self._clock()
            if self._cached and now - self._cached[0] < self._ttl:
                return self._cached[1]
            services = []
            for item in self._containers():
                state, findings = self._judge(item)
                services.append({
                    "name": item["service"], "container": item["container"], "image": state["image"],
                    "state": state["state"], "health": state["health"],
                    "status_text": _status_text(state),
                    "restart_count": state["restart_count"],
                    "started_at": state["started_at"].isoformat() if state["started_at"] else None,
                    "exit_code": state["exit_code"], "oom_killed": state["oom_killed"],
                    "severity": worst(findings), "findings_count": len(findings),
                })
            summary = {"total": len(services),
                       "up": sum(s["severity"] == "ok" for s in services),
                       "warn": sum(s["severity"] == "warn" for s in services),
                       "down": sum(s["severity"] == "error" for s in services)}
            result = {"generated_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                      "host": socket.gethostname(), "summary": summary, "services": services}
            self._cached = (now, result)
            return result

    def logs(self, name: str, lines: int = 200) -> Dict:
        item = self._find(name)
        lines = max(1, min(int(lines), MAX_LOG_LINES))
        out = self._lines(item["id"], lines)
        truncated = False
        total = 0
        kept: List[str] = []
        for line in reversed(out):                # keep the newest lines if the answer would be too large
            total += len(line) + 1
            if total > MAX_LOG_CHARS:
                truncated = True
                break
            kept.append(line)
        kept.reverse()
        return {"name": item["service"], "lines": kept, "truncated": truncated or len(out) >= lines, "redacted": True}

    def analysis(self, name: str, lines: int = 500) -> Dict:
        item = self._find(name)
        state = self._state(item["id"])
        now = datetime.fromtimestamp(self._clock(), tz=timezone.utc)
        text = self._lines(item["id"], max(1, min(int(lines), MAX_LOG_LINES)), since=self._clock() - self._window)
        findings = analyze(state, text, now, self._window)
        return {"name": item["service"], "severity": worst(findings), "scanned_lines": len(text),
                "findings": [f.to_dict() for f in findings]}


def _status_text(state: Dict) -> str:
    text = state["state"]
    if state["health"]:
        text += f" ({state['health']})"
    if state["state"] in ("exited", "dead") and state["exit_code"] is not None:
        text += f", exit code {state['exit_code']}"
    return text
