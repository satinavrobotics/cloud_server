"""Operator actions on this server: restart a service, restart them all, and (on a robot) deploy a tag.

Everything here is a fixed action on this server's own containers. A request names a service from the engine's own list or a
git-sha tag, never a command or a path. Off unless OPERATIONS_ENABLED and a token are both set; the token travels in
X-Operations-Token. One action runs at a time. A robot deploy runs scripts/deploy_robot.sh in a one-shot container made from this
service's own image (which carries the docker CLI and compose): the deploy recreates this service too, and the script must
outlive it. That container is kept (with its log) for the last few deploys and is how the app follows a deploy's progress.
"""
import hmac
import ipaddress
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from .docker_api import DockerActor, DockerUnavailable
from .redact import redact_line
from .service import COMPOSE_SERVICE_LABEL, DiagnosticsService, UnknownService

log = logging.getLogger(__name__)

SELF_SERVICE = "server-diagnostics"          # restarting this service from here would cut the answer; a deploy restarts it
TAG = re.compile(r"^[0-9a-f]{7,40}$")
KEEP_DEPLOYS = 5
HEALTHY_WAIT_S = 90.0
DEPLOY_LABEL = "sati.operation"
INFRA_FIRST = ("postgres", "arangodb", "minio", "mosquitto")      # restart-all order: these, then the rest, the gateway last
GATEWAY = ("nginx-gateway", "sati_nginx_gateway")
ACTIVE_STATES = ("RUNNING", "PENDING")


# Tailscale's own address space: callers from here need no token when the server trusts the tailnet (the tailnet already
# authenticates who is on it). The LAN, the internet and everything else still need the token.
TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


def trusted_caller(ip: Optional[str]) -> bool:
    """A tailnet peer, or the machine itself. `ip` is the caller's address as the gateway saw it (X-Real-IP), never a client claim."""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    return addr.is_loopback or any(addr in net for net in TAILNET)


class OperationError(Exception):
    """status is the HTTP status to answer with; code a short machine-readable reason."""
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_missions(url: str, timeout: float = 6.0):
    """The mission list as the API answers it (it is large, so not the diagnostics' small-body fetch), or None."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url + "/api/v1/missions"), timeout=timeout) as response:
            return json.loads(response.read(8_000_000))
    except (OSError, ValueError, urllib.error.URLError):
        return None


class Operations:
    def __init__(self, docker: DockerActor, diagnostics: DiagnosticsService, *, enabled: bool, token: str, kind: str,
                 deploy_root: str, trust_tailnet: bool = False, gateway_url: str = "", fetch_missions: Optional[Callable] = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self._docker, self._diag = docker, diagnostics
        self._token = token or ""
        self._trust_tailnet = bool(trust_tailnet)
        self._enabled = bool(enabled and (self._token or self._trust_tailnet))
        if enabled and not self._enabled:
            log.warning("OPERATIONS_ENABLED is set without OPERATIONS_TOKEN or OPERATIONS_TRUST_TAILNET: operations stay off")
        self._kind, self._root = kind, deploy_root.rstrip("/")
        self._gateway = gateway_url.rstrip("/")
        self._fetch_missions = fetch_missions or _fetch_missions
        self._sleep, self._clock = sleep, clock
        self._lock = threading.Lock()
        self._records: Dict[str, Dict] = {}

    # ---- who may
    def token_required(self, caller_ip: Optional[str]) -> bool:
        return not (self._trust_tailnet and trusted_caller(caller_ip))

    def authorize(self, token: Optional[str], caller_ip: Optional[str] = None) -> None:
        if not self._enabled:
            raise OperationError(403, "operations_disabled", "operator actions are not enabled on this server")
        if not self.token_required(caller_ip):
            return
        if not self._token or not token or not hmac.compare_digest(token.encode(), self._token.encode()):
            raise OperationError(401, "bad_token", "the operations token is missing or wrong")

    def info(self, caller_ip: Optional[str] = None) -> Dict:
        return {"enabled": self._enabled, "kind": self._kind, "can_deploy": self._enabled and self._kind == "robot",
                "token_required": self.token_required(caller_ip), "recent": self.recent()}

    # ---- guards
    def _active_missions(self) -> Optional[int]:
        """How many missions are RUNNING or PENDING; None when the API cannot be asked (it is often what is being fixed)."""
        data = self._fetch_missions(self._gateway) if self._gateway else None
        if not isinstance(data, list):
            return None
        return sum(1 for m in data if isinstance(m, dict) and ((m.get("status") or {}).get("state") in ACTIVE_STATES))

    def _guard(self, force: bool) -> Optional[int]:
        active = self._active_missions()
        if active and not force:
            raise OperationError(409, "missions_active", f"{active} mission(s) are running or pending; stop them or force")
        return active

    # ---- records
    def _new(self, kind: str, target: str, operator: str) -> Dict:
        rec = {"id": uuid.uuid4().hex[:12], "kind": kind, "target": target, "status": "running", "operator": operator[:60],
               "started": _now(), "finished": None, "lines": []}
        self._records[rec["id"]] = rec
        for old in sorted(self._records.values(), key=lambda r: r["started"])[:-20]:
            self._records.pop(old["id"], None)
        log.info("operation %s start: %s %s by %s", rec["id"], kind, target, operator[:60] or "unknown")
        return rec

    def _finish(self, rec: Dict, ok: bool, line: str) -> None:
        rec["lines"].append(line)
        rec["status"], rec["finished"] = ("succeeded" if ok else "failed"), _now()
        log.info("operation %s %s: %s", rec["id"], rec["status"], line)

    @staticmethod
    def _public(rec: Dict) -> Dict:
        return {k: rec[k] for k in ("id", "kind", "target", "status", "operator", "started", "finished", "lines")}

    # ---- restart
    def _wait_up(self, container_id: str) -> str:
        """'' when running (and healthy where it has a check), else why not."""
        deadline = self._clock() + HEALTHY_WAIT_S
        while True:
            state = self._diag._state(container_id)
            if state["state"] == "running" and state["health"] in (None, "healthy"):
                return ""
            if self._clock() >= deadline:
                return f"{state['state']}{' (' + state['health'] + ')' if state['health'] else ''} after {int(HEALTHY_WAIT_S)}s"
            self._sleep(2.0)

    def _restart_one(self, item: Dict, rec: Dict) -> bool:
        rec["lines"].append(f"restarting {item['service']}")
        try:
            self._docker.restart(item["id"])
            problem = self._wait_up(item["id"])
        except DockerUnavailable as err:
            rec["lines"].append(f"{item['service']}: {err}")
            return False
        rec["lines"].append(f"{item['service']}: {problem or 'up'}")
        return not problem

    def restart(self, service: str, *, force: bool = False, operator: str = "") -> Dict:
        if service == SELF_SERVICE:
            raise OperationError(400, "not_allowed", "this service is restarted by a deploy, not from itself")
        try:
            item = self._diag._find(service)
        except UnknownService:
            raise OperationError(404, "unknown_service", f"no service named '{service}' on this server")
        except DockerUnavailable as err:
            raise OperationError(503, "docker_unavailable", str(err))
        return self._start(f"restart", service, operator, force, lambda rec: self._run_restart([item], rec))

    def restart_all(self, *, force: bool = False, operator: str = "") -> Dict:
        try:
            items = [i for i in self._diag._containers() if i["service"] != SELF_SERVICE]
        except DockerUnavailable as err:
            raise OperationError(503, "docker_unavailable", str(err))
        rank = lambda i: (0 if i["service"] in INFRA_FIRST else 2 if i["service"] in GATEWAY else 1, i["service"])
        return self._start("restart_all", f"{len(items)} services", operator, force, lambda rec: self._run_restart(sorted(items, key=rank), rec))

    def _start(self, kind: str, target: str, operator: str, force: bool, work) -> Dict:
        if not self._lock.acquire(blocking=False):
            raise OperationError(409, "busy", "another operator action is still running")
        try:
            active = self._guard(force)
            rec = self._new(kind, target, operator)
        except BaseException:
            self._lock.release()
            raise
        if active:
            rec["lines"].append(f"forced while {active} mission(s) are running or pending")

        def run():
            try:
                work(rec)
            except Exception as err:              # an action must always end in a final state
                self._finish(rec, False, f"failed: {err}")
            finally:
                self._lock.release()
        threading.Thread(target=run, daemon=True, name=f"op-{rec['id']}").start()
        return self._public(rec)

    def _run_restart(self, items: List[Dict], rec: Dict) -> None:
        bad = [i["service"] for i in items if not self._restart_one(i, rec)]
        self._finish(rec, not bad, "all up" if not bad else "not up: " + ", ".join(bad))

    # ---- deploy (robot)
    def _own_image(self) -> str:
        try:
            return self._docker.inspect(self._diag._find(SELF_SERVICE)["id"])["Config"]["Image"]
        except (UnknownService, KeyError, DockerUnavailable):
            raise OperationError(503, "no_deploy_image", "cannot tell which image this service runs, so the deploy tooling is unknown")

    def _helpers(self) -> List[Dict]:
        found = [c for c in self._docker.list_containers() if (c.get("Labels") or {}).get(DEPLOY_LABEL) == "deploy"]
        return sorted(found, key=lambda c: c.get("Created", 0))

    def deploy(self, tag: Optional[str] = None, *, rollback: bool = False, force: bool = False, operator: str = "") -> Dict:
        if self._kind != "robot":
            raise OperationError(409, "not_a_robot", "deploys from the app are for robot servers")
        if rollback == bool(tag):
            raise OperationError(400, "bad_request", "give a tag or ask for a rollback, not both and not neither")
        if tag and not TAG.match(tag):
            raise OperationError(400, "bad_tag", "the tag must be a git sha (7 to 40 hex characters)")
        if not self._lock.acquire(blocking=False):
            raise OperationError(409, "busy", "another operator action is still running")
        try:
            try:
                if any(c.get("State") == "running" for c in self._helpers()):
                    raise OperationError(409, "busy", "a deploy is already running")
            except DockerUnavailable as err:
                raise OperationError(503, "docker_unavailable", str(err))
            active = self._guard(force)
            image = self._own_image()
            name = f"sati-deploy-{int(time.time())}"
            spec = {"Image": image, "Cmd": ["bash", "scripts/deploy_robot.sh"] + (["--rollback"] if rollback else [tag]),
                    "WorkingDir": self._root, "Labels": {DEPLOY_LABEL: "deploy", "sati.operation.target": "rollback" if rollback else tag,
                                                         "sati.operation.operator": operator[:60]},
                    "HostConfig": {"NetworkMode": "host", "Binds": [f"{self._root}:{self._root}", "/var/run/docker.sock:/var/run/docker.sock"]}}
            try:
                cid = self._docker.create(name, spec)
                self._docker.start(cid)
            except DockerUnavailable as err:
                raise OperationError(502, "deploy_not_started", str(err))
            log.info("deploy %s started (%s) by %s%s", name, "rollback" if rollback else tag, operator[:60] or "unknown",
                     f", forced over {active} active mission(s)" if active else "")
            self._prune_helpers()
        finally:
            self._lock.release()          # the deploy runs on in its own container; the lock only covers starting it
        return self.deploy_status(name)

    def _prune_helpers(self) -> None:
        try:
            for old in self._helpers()[:-KEEP_DEPLOYS]:
                if old.get("State") != "running":
                    self._docker.remove(old["Id"])
        except DockerUnavailable:
            pass

    def deploy_status(self, name: str) -> Dict:
        match = next((c for c in self._helpers() if (c.get("Names") or [""])[0].lstrip("/") == name), None)
        if match is None:
            raise OperationError(404, "unknown_operation", "no such deploy (only the last few are kept)")
        info = self._docker.inspect(match["Id"])
        st = info.get("State") or {}
        running = bool(st.get("Running"))
        status = "running" if running else ("succeeded" if st.get("ExitCode") == 0 else "failed")
        labels = info.get("Config", {}).get("Labels") or {}
        lines = [redact_line(l) for l in self._docker.logs(match["Id"], 300)]
        return {"id": name, "kind": "deploy", "target": labels.get("sati.operation.target", ""), "status": status,
                "operator": labels.get("sati.operation.operator", ""), "started": (st.get("StartedAt") or "")[:19] + "Z",
                "finished": None if running else (st.get("FinishedAt") or "")[:19] + "Z", "lines": lines}

    # ---- reading
    def get(self, op_id: str) -> Dict:
        if op_id.startswith("sati-deploy-"):
            try:
                return self.deploy_status(op_id)
            except DockerUnavailable as err:
                raise OperationError(503, "docker_unavailable", str(err))
        rec = self._records.get(op_id)
        if rec is None:
            raise OperationError(404, "unknown_operation", "no such operation (only the last few are kept)")
        return self._public(rec)

    def recent(self) -> List[Dict]:
        out = [self._public(r) | {"lines": []} for r in sorted(self._records.values(), key=lambda r: r["started"], reverse=True)[:10]]
        try:
            for c in reversed(self._helpers()[-5:]):
                st = c.get("State")
                out.append({"id": (c.get("Names") or [""])[0].lstrip("/"), "kind": "deploy", "target": (c.get("Labels") or {}).get("sati.operation.target", ""),
                            "status": "running" if st == "running" else "finished", "operator": (c.get("Labels") or {}).get("sati.operation.operator", ""),
                            "started": None, "finished": None, "lines": []})
        except DockerUnavailable:
            pass
        return out
