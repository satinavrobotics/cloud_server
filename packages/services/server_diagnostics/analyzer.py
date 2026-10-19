"""Find what is wrong with a service from its container state and its recent log lines.

Pure functions: no Docker, no clock of their own. Each rule names a failure the platform is known to have, says it in
plain words and says what to check. Only the recent window is judged, so a problem that was fixed stops showing.
"""

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

SEVERITY_RANK = {"ok": 0, "warn": 1, "error": 2}
_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?Z?\s")
SAMPLE_CHARS = 300


@dataclass
class Finding:
    id: str
    severity: str
    title: str
    detail: str
    hint: str
    count: int = 0
    sample: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    title: str
    hint: str
    pattern: "re.Pattern"


def _rule(id_: str, severity: str, title: str, hint: str, pattern: str) -> Rule:
    return Rule(id_, severity, title, hint, re.compile(pattern, re.IGNORECASE))


RULES: List[Rule] = [
    _rule("connection-refused", "error", "Cannot reach another service",
          "Something refused the connection. Check that the service it talks to is running (the list shows it) and "
          "listens on the address and port named in the line; a service bound to the wrong address looks exactly like this.",
          r"connection refused|ECONNREFUSED|connect\(\) failed \(111|\[Errno 111\]"),
    _rule("name-resolution", "error", "A host name cannot be resolved",
          "A service or machine name did not resolve. Check the address in the settings of this service and that the "
          "network (or the tailnet) is up.",
          r"name or service not known|temporary failure in name resolution|getaddrinfo|could not resolve host|ENOTFOUND"),
    _rule("timeout", "warn", "Requests are timing out",
          "Something answered too slowly or not at all. Check the CPU, memory and network of the machine and of the "
          "service named in the line.",
          r"timed out|ETIMEDOUT|deadline exceeded|timeout (exceeded|reached|expired|error|while|after|waiting|connecting)|"
          r"(request|read|connect|connection|gateway|upstream)[ _-]timeout(?!\s*[=:]\s*\d)"),
    _rule("auth-failed", "error", "A login or key was refused",
          "A password, key or token was rejected. Check the credentials in the .env of this service against the "
          "service it connects to.",
          r"authentication failed|password authentication failed|invalid credentials|access ?denied|unauthori[sz]ed|"
          r"not authori[sz]ed|\b401\b.*(unauthori[sz]ed|denied)|invalid api key"),
    _rule("out-of-memory", "error", "Out of memory",
          "The service (or the machine) ran out of memory. Raise the container's memory limit or free memory on the host.",
          r"out of memory|MemoryError|\boom\b|killed process|cannot allocate memory"),
    _rule("disk-full", "error", "The disk is full",
          "No space left on a disk. Free space or move the data folder; databases stop working until then.",
          r"no space left on device|disk full|ENOSPC"),
    _rule("port-in-use", "error", "A port is already in use",
          "Another program already listens on this port. Stop it, or change the port of this service.",
          r"address already in use|EADDRINUSE|port is already allocated"),
    _rule("permission-denied", "warn", "Permission denied",
          "The service is not allowed to read or write something. Check file and folder ownership of the mounted paths.",
          r"permission denied|EACCES|operation not permitted"),
    _rule("database", "error", "The database is not usable",
          "A database refused or dropped the connection. Check that the database container is healthy and its "
          "password matches the .env.",
          r"could not connect to server|OperationalError|ArangoError|too many connections|database system is "
          r"(starting up|shutting down)|connection to database failed"),
    _rule("mqtt", "warn", "The message broker connection is unstable",
          "The MQTT broker (mosquitto) disconnected or refused this service. Check that mosquitto is running and "
          "its ports match.",
          r"mqtt.*(disconnect|connection lost|not connected|refused)|connection refused: not authori[sz]ed"),
    _rule("upstream-down", "error", "The gateway cannot reach a service behind it",
          "nginx has no working upstream for a route. The service behind that route is down or listens on another "
          "address than the gateway's config says.",
          r"no live upstreams|upstream (timed out|prematurely closed)|while connecting to upstream"),
    _rule("traceback", "error", "A program crashed with an error",
          "A Python exception was raised. The last lines of the trace below the sample name the cause.",
          r"traceback \(most recent call last\)"),
]

# Words like "timeout" or "permission" are everyday vocabulary in routine lines ("Image buffer timeout: 30s"): for these
# rules an INFO or DEBUG line is never a symptom.
_LEVEL_SENSITIVE = {"timeout", "permission-denied", "mqtt", "name-resolution"}
# Registry clients (docker buildx, pull) ask for tags and blobs that do not exist yet as part of normal work; the registry logs each as an error.
_BENIGN_ERROR = re.compile(r"(manifest|blob|name) unknown", re.IGNORECASE)
_ROUTINE_LINE = re.compile(r"\b(INFO|DEBUG)\b|\[(info|debug|notice)\]")
# Level words: UPPERCASE as in Python/Go logs, or a level field/bracket in any case (`level=error`, "level":"error", [emerg]).
# Plain lowercase prose ("no error", "0 errors") is not a level.
_ERROR_LINE = re.compile(r"(?-i:\b(?:ERROR|CRITICAL|FATAL|PANIC)\b)|\blevel[\"']?\s*[=:]\s*[\"']?(error|fatal|critical|panic)\b|\[(error|emerg|crit|alert)\]", re.IGNORECASE)
_WARNING_LINE = re.compile(r"\b(WARNING|WARN)\b|\[warn\]")
ERROR_LINES_FOR_ERROR = 20


def line_time(line: str) -> Optional[datetime]:
    """The timestamp Docker puts at the start of every log line (UTC), or None."""
    m = _TIMESTAMP.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def recent_lines(lines: Iterable[str], now: datetime, window_seconds: float) -> List[str]:
    """The lines inside the window (lines without a timestamp stay: they belong to the line before them)."""
    out = []
    for line in lines:
        t = line_time(line)
        if t is None or (now - t).total_seconds() <= window_seconds:
            out.append(line)
    return out


MEMORY_WARN, MEMORY_ERROR = 0.80, 0.92     # fraction of its memory limit a container uses


def _mb(n) -> str:
    return f"{n / 1048576:.0f} MB"


def _memory_findings(state: Dict) -> List[Finding]:
    usage, limit = state.get("mem_usage_bytes"), state.get("mem_limit_bytes")
    if not usage or not limit or state.get("state") != "running":
        return []
    frac = usage / limit
    if frac < MEMORY_WARN:
        return []
    hint = ("Raise this service's memory limit (mem_limit in the compose file, or its MEM_* setting in .env.robot) or reduce its "
            "load. At the limit the kernel kills it (it then shows as 'Stopped for using too much memory').")
    if frac >= MEMORY_ERROR:
        return [Finding("memory-critical", "error", "About to run out of its memory limit",
                        f"Uses {_mb(usage)} of {_mb(limit)} ({frac * 100:.0f}%).", hint)]
    return [Finding("memory-high", "warn", "Using most of its memory limit",
                    f"Uses {_mb(usage)} of {_mb(limit)} ({frac * 100:.0f}%).", hint)]


def _state_findings(state: Dict, now: datetime, window_seconds: float) -> List[Finding]:
    findings: List[Finding] = []
    status = state.get("state")
    exit_code = state.get("exit_code")
    if state.get("oom_killed"):
        findings.append(Finding("oom-killed", "error", "Stopped for using too much memory",
                                f"The container was killed because it hit its memory limit (exit code {exit_code}).",
                                "Raise this service's memory limit (mem_limit in the compose file) or reduce its load."))
    if status == "restarting":
        findings.append(Finding("crash-loop", "error", "Restarting over and over",
                                f"The container keeps crashing and is being restarted (restarted {state.get('restart_count', 0)} times).",
                                "Read the last lines below: the crash reason is printed just before each restart."))
    elif status in ("exited", "dead") and exit_code not in (0, None):
        findings.append(Finding("stopped-with-error", "error", f"Stopped with exit code {exit_code}",
                                "The container is not running and its last run ended with an error.",
                                "Read the last lines below for the reason, fix it, then start the service again."))
    elif status in ("exited", "created", "dead") and state.get("restart_policy") in ("always", "unless-stopped"):
        findings.append(Finding("stopped", "warn", "Not running although it should be",
                                f"The container is {status} but is set to restart automatically, so someone stopped it (or it never started).",
                                "Start it again (docker start / compose up -d), or remove it if it is not wanted."))
    elif status == "paused":
        findings.append(Finding("paused", "warn", "Paused", "The container is paused and does nothing.",
                                "Unpause it with docker unpause."))
    if state.get("health") == "unhealthy":
        findings.append(Finding("unhealthy", "error", "Its health check fails",
                                "The container runs but its own health check does not pass.",
                                "Open its health check command in the compose file and run it by hand; the lines below show what the service says."))
    started = state.get("started_at")
    if status == "running" and state.get("restart_count", 0) > 0 and started and (now - started).total_seconds() <= window_seconds:
        findings.append(Finding("restarted-recently", "warn", "Restarted a moment ago",
                                f"It was restarted {state.get('restart_count')} time(s) and has only been up since {started.isoformat()}.",
                                "Read the lines before the restart for what made it stop."))
    findings.extend(_memory_findings(state))
    return findings


def analyze(state: Dict, lines: List[str], now: datetime, window_seconds: float) -> List[Finding]:
    """Findings for one service: its container state first, then what the recent log lines show.

    `state`: state, health, restart_count, exit_code, oom_killed, started_at (datetime or None).
    """
    findings = _state_findings(state, now, window_seconds)
    window = recent_lines(lines, now, window_seconds)
    matched: Dict[str, Finding] = {}
    explained = set()                       # lines already named by an error-severity rule (never counted twice)
    for index, line in enumerate(window):
        for rule in RULES:
            if rule.id in _LEVEL_SENSITIVE and _ROUTINE_LINE.search(line):
                continue
            if rule.pattern.search(line):
                f = matched.get(rule.id)
                if f is None:
                    f = matched[rule.id] = Finding(rule.id, rule.severity, rule.title, "", rule.hint)
                f.count += 1
                f.sample = line[:SAMPLE_CHARS]
                if rule.severity == "error":
                    explained.add(index)
    for f in matched.values():
        minutes = max(1, round(window_seconds / 60))
        f.detail = f"{f.count} matching line(s) in the last {minutes} minutes; the newest is the sample."
        findings.append(f)
    errors = [ln for i, ln in enumerate(window) if i not in explained and _ERROR_LINE.search(ln) and not _BENIGN_ERROR.search(ln)]
    if errors:
        severity = "error" if len(errors) >= ERROR_LINES_FOR_ERROR else "warn"
        findings.append(Finding("errors-logged", severity, "Errors in the log",
                                f"{len(errors)} line(s) with an error level in the last {max(1, round(window_seconds / 60))} minutes.",
                                "Read the newest error lines in the log below.", len(errors), errors[-1][:SAMPLE_CHARS]))
    findings.sort(key=lambda f: -SEVERITY_RANK[f.severity])
    return findings


def worst(findings: Iterable[Finding]) -> str:
    return max((f.severity for f in findings), key=lambda s: SEVERITY_RANK[s], default="ok")
