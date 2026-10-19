"""Active checks: instead of waiting for a log line, ask the running server the questions whose wrong answer has hurt before.

- the gateway answers, and reaches the API and the services behind it (a gateway that points at the wrong address
  answers 502 for that route and nothing is logged until somebody uses it);
- what server-info says this server has is what actually runs (and the other way round);
- the platform's own services run one version (one container on an older image than its neighbours).

Pure functions over an injected `fetch`, so they are tested without a network.
"""

import json
from dataclasses import asdict, dataclass
from typing import Callable, Dict, List, Optional, Tuple

# fetch(method, url) -> (status or None when nothing answered, body text, error text)
Fetch = Callable[[str, str], Tuple[Optional[int], str, str]]

# What each advertised capability needs to be running. `extra_is_odd`: a service running while the capability is off is
# worth a note (graph-builder always runs, so scene_graph is not in that group).
CAPABILITY_SERVICES: Dict[str, Tuple[List[str], bool]] = {
    "scene_graph": (["graph-builder-service"], False),
    "reconstruction": (["reconstruction-service"], True),
    "semantic": (["semantic-service"], True),
    "agent": (["agent-orchestrator-service"], True),
    "diagnostics": (["server-diagnostics"], True),
    "video": (["livekit-sfu-tokens", "livekit-sfu"], False),
}
# Our own images (the ones build_push.sh builds): these must agree on a version. Third-party images are not compared.
PLATFORM_SERVICES = {"mission-dispatch", "graph-builder-service", "mission-planner-service", "livekit-service",
                     "livekit-sfu-tokens", "server-diagnostics", "agent-orchestrator-service", "api-delegation-service"}
_BAD_GATEWAY = {502, 503, 504}


@dataclass
class Check:
    id: str
    title: str
    ok: bool
    severity: str          # 'ok' when ok, else 'warn' or 'error'
    detail: str
    hint: str

    def to_dict(self) -> Dict:
        return asdict(self)


def _result(id_: str, title: str, problem: Optional[str], severity: str, hint: str, fine: str) -> Check:
    return Check(id_, title, problem is None, "ok" if problem is None else severity, fine if problem is None else problem,
                 "" if problem is None else hint)


def image_tag(image: str) -> str:
    """The tag of an image reference ("host:5000/sati/x:abc123" -> "abc123"; no tag -> "latest")."""
    ref = image.split("@")[0]
    last = ref.rsplit("/", 1)[-1]
    return last.split(":", 1)[1] if ":" in last else "latest"


def _server_info(fetch: Fetch, gateway: str):
    status, body, err = fetch("GET", gateway + "/api/v1/server-info")
    if status == 200:
        try:
            return status, json.loads(body)
        except ValueError:
            return status, None
    return status, None


def gateway_checks(fetch: Fetch, gateway: str) -> Tuple[List[Check], Optional[Dict]]:
    """The gateway answers; it reaches the API; each route of a service this server advertises is reachable."""
    checks: List[Check] = []
    status, _, err = fetch("GET", gateway + "/health")
    checks.append(_result(
        "gateway-answers", "The gateway answers", None if status == 200 else
        (f"GET {gateway}/health gave {status}" if status else f"nothing answered at {gateway} ({err or 'no reply'})"), "error",
        "The nginx gateway container is down or not listening on this address; start it (docker compose ... up -d nginx-gateway) "
        "and read its log. Everything the app shows goes through it.", f"{gateway}/health answers 200"))
    if status != 200:
        return checks, None
    api_status, info = _server_info(fetch, gateway)
    checks.append(_result(
        "gateway-reaches-api", "The gateway reaches the API", None if info is not None else f"GET /api/v1/server-info gave {api_status}", "error",
        "502 means the gateway cannot reach the API container (port 8000): check api-delegation-service is healthy, and that the "
        "gateway's upstream address is one the API listens on. 404 means the API image is older than this check (version skew).",
        "server-info answers through the gateway"))
    if info is None:
        return checks, None
    video = ((info.get("livekit") or {}).get("available"))
    if video:
        path = (info.get("livekit") or {}).get("token_path") or "/api/livekit-selfhost/createToken"
        st, _, err = fetch("GET", gateway + path)
        bad = st is None or st in _BAD_GATEWAY or st == 404
        checks.append(_result(
            "gateway-reaches-token-service", "The gateway reaches the video token service",
            f"GET {path} gave {st if st else 'no answer'}" if bad else None, "error",
            "502 here means the gateway sends the token route to an address the token service does not listen on (it listens on "
            "the machine's tailnet address on a robot, so the gateway's upstream must use that address, not localhost). "
            "Recreate the gateway after fixing the config; the app gets no video token until then.",
            f"{path} reaches the token service"))
    if (info.get("capabilities") or {}).get("agent"):
        st, _, err = fetch("GET", gateway + "/api/agent/health")
        bad = st is None or st in _BAD_GATEWAY
        checks.append(_result("gateway-reaches-agent", "The gateway reaches the agent service",
                              f"GET /api/agent/health gave {st if st else 'no answer'}" if bad else None, "warn",
                              "The agent service (port 8007) is down or the gateway points elsewhere.", "the agent route answers"))
    return checks, info


def capability_check(info: Optional[Dict], services: List[Dict]) -> Optional[Check]:
    """What server-info advertises against what runs. None when server-info could not be read."""
    if info is None:
        return None
    caps = dict(info.get("capabilities") or {})
    caps["video"] = bool((info.get("livekit") or {}).get("available"))
    running = {s["name"] for s in services if s.get("state") == "running"}
    present = {s["name"] for s in services}
    missing, extra = [], []
    for cap, (names, extra_is_odd) in CAPABILITY_SERVICES.items():
        if caps.get(cap):
            for n in names:
                if n not in running:
                    missing.append(f"{cap} is advertised but {n} is {'not running' if n in present else 'not on this server'}")
        elif extra_is_odd and any(n in running for n in names):
            extra.append(f"{names[0]} runs but this server does not advertise {cap}")
    if missing:
        return Check("capabilities-match", "What the server advertises is running", False, "error", "; ".join(missing + extra),
                     "The app will offer something that fails. Start the missing service, or switch the capability off in the "
                     "server's settings (SERVER_*_AVAILABLE, SCENE_GRAPH_WORKER_ENABLED, RECONSTRUCTION_SERVICE_URL, ...).")
    if extra:
        return Check("capabilities-match", "What the server advertises is running", False, "warn", "; ".join(extra),
                     "A service runs that the app is told does not exist, so it never shows it. Set the matching URL/flag in "
                     "the server's settings (see docs/robot_deploy.md, section on server-info).")
    return Check("capabilities-match", "What the server advertises is running", True, "ok",
                 "every advertised capability has its service running", "")


def version_check(services: List[Dict]) -> Check:
    """The platform's own services should all run the same image tag."""
    by_tag: Dict[str, List[str]] = {}
    for s in services:
        if s["name"] in PLATFORM_SERVICES and s.get("image"):
            by_tag.setdefault(image_tag(s["image"]), []).append(s["name"])
    if len(by_tag) <= 1:
        tag = next(iter(by_tag), "-")
        return Check("same-version", "All services run the same version", True, "ok", f"all on {tag}", "")
    parts = [f"{tag}: {', '.join(sorted(names))}" for tag, names in sorted(by_tag.items(), key=lambda kv: -len(kv[1]))]
    return Check("same-version", "All services run the same version", False, "warn", "; ".join(parts),
                 "Containers were recreated at different times. Redeploy everything on one tag (scripts/deploy_robot.sh <tag>); "
                 "when you recreate single services by hand use scripts/robot_compose.sh so the tag is the deployed one.")


def run_checks(fetch: Fetch, gateway: str, services: List[Dict]) -> List[Check]:
    checks, info = gateway_checks(fetch, gateway)
    cap = capability_check(info, services)
    if cap is not None:
        checks.append(cap)
    checks.append(version_check(services))
    return checks
