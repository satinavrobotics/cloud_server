"""The robot server's tunable settings, for the app: list them with their effective values, validate a change and save it.

A change is saved to `local/overrides.env` on the machine (merged over the profile by scripts/render_env.py, never committed), so it
cannot conflict with git. Saving changes nothing that runs: applying is a redeploy (operations.deploy with apply_config), which
renders the file again. Only the settings in SETTINGS can be changed, each with a type and bounds; a caller never names a file.
"""
import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROFILES_DIR = Path(os.getenv("ROBOT_PROFILES_DIR", "/deploy/profiles"))
LOCAL_DIR = Path(os.getenv("ROBOT_LOCAL_DIR", "/deploy/local"))
OVERRIDES = "overrides.env"
SECRET_NAME = re.compile(r"(PASSWORD|_SECRET|_KEY|_TOKEN|ROOT_USER)$")
_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_MEMORY = re.compile(r"^(\d+)([mMgG])$")
_UNIT = {"m": 1 << 20, "g": 1 << 30}
NOT_HASHED = {"IMAGE_TAG", "CONFIG_PROFILE", "CONFIG_HASH", "CONFIG_OVERRIDES"}


class ConfigError(Exception):
    def __init__(self, status: int, code: str, message: str, key: Optional[str] = None):
        super().__init__(message)
        self.status, self.code, self.message, self.key = status, code, message, key


def _s(key, group, label, help, typ, default, restarts, **kw) -> Dict:
    return {"key": key, "group": group, "label": label, "help": help, "type": typ, "default": default, "restarts": restarts, **kw}


MEM = "Memory limits"
PERSON = "Person removal"
_MEM_HELP = "The container is killed and restarted by Docker if it needs more than this."
SETTINGS: List[Dict] = [
    _s("MEM_API", MEM, "API", _MEM_HELP, "memory", "384m", ["api-delegation-service"], min="64m", max="4g"),
    _s("MEM_DISPATCH", MEM, "Mission dispatch", _MEM_HELP, "memory", "160m", ["mission-dispatch"], min="64m", max="4g"),
    _s("MEM_PLANNER", MEM, "Mission planner", _MEM_HELP, "memory", "192m", ["mission-planner-service"], min="64m", max="4g"),
    _s("MEM_GRAPH_BUILDER", MEM, "Graph builder (scene graph)", _MEM_HELP + " The scene graph is the biggest user.", "memory", "896m",
       ["graph-builder-service"], min="256m", max="6g"),
    _s("MEM_POSTGRES", MEM, "Postgres", _MEM_HELP, "memory", "448m", ["postgres"], min="128m", max="6g"),
    _s("MEM_ARANGO", MEM, "ArangoDB", _MEM_HELP, "memory", "512m", ["arangodb"], min="128m", max="6g"),
    _s("MEM_MINIO", MEM, "MinIO (maps, recordings)", _MEM_HELP, "memory", "384m", ["minio"], min="64m", max="4g"),
    _s("MEM_MOSQUITTO", MEM, "MQTT broker", _MEM_HELP, "memory", "256m", ["mosquitto"], min="32m", max="1g"),
    _s("RECON_MEM_LIMIT", MEM, "Reconstruction", _MEM_HELP + " Counted in the budget only where the profile sets it (the machine runs reconstruction).", "memory", "768m",
       ["reconstruction-service"], min="256m", max="6g"),
    _s("MEM_LIVEKIT", MEM, "Video server (LiveKit SFU)", _MEM_HELP, "memory", "512m", ["livekit-sfu"], min="64m", max="2g"),
    _s("MEM_LIVEKIT_TOKENS", MEM, "Video token service", _MEM_HELP, "memory", "128m", ["livekit-sfu-tokens"], min="32m", max="1g"),
    _s("MEM_GATEWAY", MEM, "Gateway (nginx)", _MEM_HELP, "memory", "64m", ["nginx-gateway"], min="32m", max="1g"),
    _s("MEM_MINIO_PROXY", MEM, "MinIO forwarder", _MEM_HELP, "memory", "64m", ["minio-tailnet"], min="32m", max="1g"),
    _s("MEM_DIAGNOSTICS", MEM, "Diagnostics (this service)", _MEM_HELP, "memory", "96m", ["server-diagnostics"], min="64m", max="1g",
       note="Changing this restarts the service that shows these settings."),
    _s("ARANGO_BLOCK_CACHE_BYTES", "Database tuning", "ArangoDB block cache", "Cache for reads; keep it well under the ArangoDB memory limit.",
       "bytes", "100000000", ["arangodb"], min="16777216", max="4294967296"),
    _s("ARANGO_WRITE_BUFFER_BYTES", "Database tuning", "ArangoDB write buffer", "Memory for writes before they are flushed.",
       "bytes", "100000000", ["arangodb"], min="16777216", max="4294967296"),
    _s("ARANGO_QUERY_MEMORY_BYTES", "Database tuning", "ArangoDB query memory", "The most one query may use.",
       "bytes", "200000000", ["arangodb"], min="16777216", max="4294967296"),
    _s("UPLOAD_BUFFER_MAX_BYTES", "Database tuning", "Upload buffer (graph builder)", "The largest upload kept in memory at once.",
       "bytes", "100000000", ["graph-builder-service"], min="8388608", max="2147483648"),
    _s("SCENE_GRAPH_STEP_BUDGET_S", "Scene graph", "Step time budget (s)", "How long one scene-graph step may take.",
       "float", "5", ["graph-builder-service"], min="0.5", max="60"),
    _s("SCENE_GRAPH_BATCH_MAX_LEAVES", "Scene graph", "Largest batch (leaves)", "Smaller uses less memory per step.",
       "int", "8", ["graph-builder-service"], min="1", max="64"),
    _s("SCENE_GRAPH_PREFETCH_QUEUE", "Scene graph", "Prefetch queue", "Smaller uses less memory.",
       "int", "16", ["graph-builder-service"], min="1", max="128"),
    # Person removal (docs/reconstruction/person_removal.md). The service is optional (person_mask_service_robot.yaml);
    # its memory limit counts against the budget only on a machine that uses it (PERSON_MASK_SERVICE_URL set).
    _s("PERSON_REMOVAL_DEFAULT", PERSON, "Remove people by default",
       "Reconstruction and mesh jobs use the person masks unless the map or the run says otherwise. The masks are made "
       "by the map's 'Remove people' run.", "bool", "false", ["api-delegation-service"]),
    _s("PERSON_REMOVAL_DILATE_PX", PERSON, "Mask margin (px)", "How far each person mask is grown, in depth pixels.",
       "int", "6", ["api-delegation-service"], min="0", max="64"),
    _s("MEM_PERSON_MASK", MEM, "Person removal (segmentation)", _MEM_HELP + " The model is loaded only while a run goes "
       "on.", "memory", "768m", ["person-mask"], min="256m", max="4g", budget_if="PERSON_MASK_SERVICE_URL"),
]
BY_KEY = {s["key"]: s for s in SETTINGS}


def to_bytes(value: str) -> int:
    m = _MEMORY.match(value)
    return int(m.group(1)) * _UNIT[m.group(2).lower()] if m else -1


def validate(key: str, value: str) -> str:
    """The value as stored, or a ConfigError naming the key."""
    spec = BY_KEY.get(key)
    if spec is None:
        raise ConfigError(400, "unknown_key", f"{key} cannot be changed from here", key)
    v = str(value).strip()
    bad = lambda why: ConfigError(400, "bad_value", f"{spec['label']}: {why}", key)
    t = spec["type"]
    if t == "memory":
        if to_bytes(v) < 0:
            raise bad("use a number and m or g, like 384m or 1g")
        lo, hi = to_bytes(spec["min"]), to_bytes(spec["max"])
        if not lo <= to_bytes(v) <= hi:
            raise bad(f"between {spec['min']} and {spec['max']}")
    elif t in ("bytes", "int"):
        if not re.fullmatch(r"\d{1,12}", v):
            raise bad("digits only")
        if not int(spec["min"]) <= int(v) <= int(spec["max"]):
            raise bad(f"between {spec['min']} and {spec['max']}")
    elif t == "float":
        if not re.fullmatch(r"\d{1,4}(\.\d{1,3})?", v):
            raise bad("a number like 5 or 0.5")
        if not float(spec["min"]) <= float(v) <= float(spec["max"]):
            raise bad(f"between {spec['min']} and {spec['max']}")
    elif t == "bool":
        if v not in ("true", "false"):
            raise bad("true or false")
    return v


def _parse(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text().splitlines():
        m = _LINE.match(raw.strip()) if raw.strip() and not raw.strip().startswith("#") else None
        if m:
            out[m.group(1)] = m.group(2)
    return out


def read_profile(profile: str, profiles_dir: Path = PROFILES_DIR) -> Dict[str, str]:
    """base.env then the profile: what the repo says for this machine (non-secret)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", profile or ""):
        raise ConfigError(404, "config_unavailable", "this server was not set up from a profile")
    path = profiles_dir / f"{profile}.env"
    if not path.is_file():
        raise ConfigError(404, "config_unavailable", "the profile file is not available to this service")
    return {**_parse(profiles_dir / "base.env"), **_parse(path)}


def read_overrides(local_dir: Path = LOCAL_DIR) -> Dict[str, str]:
    return _parse(local_dir / OVERRIDES)


def overrides_hash(overrides: Dict[str, str]) -> str:
    return hashlib.sha256("\n".join(f"{k}={v}" for k, v in sorted(overrides.items())).encode()).hexdigest()[:12] if overrides else ""


def profile_hash(settings: Dict[str, str]) -> str:
    body = "\n".join(f"{k}={v}" for k, v in sorted(settings.items()) if k not in NOT_HASHED and not SECRET_NAME.search(k))
    return hashlib.sha256(body.encode()).hexdigest()[:12]


def effective(profile: Dict[str, str], overrides: Dict[str, str]) -> Dict[str, str]:
    """Every setting in SETTINGS with the value that will run once applied (override, else profile, else the default)."""
    return {s["key"]: overrides.get(s["key"], profile.get(s["key"], s["default"])) for s in SETTINGS}


def memory_total_bytes(meminfo: str = "/proc/meminfo") -> int:
    try:
        for line in open(meminfo):
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def counts_in_budget(spec: Dict, context: Optional[Dict[str, str]]) -> bool:
    """A memory limit counts unless it belongs to an optional service this machine does not run (`budget_if` names
    the setting that says it does; unknown context = it counts)."""
    flag = spec.get("budget_if")
    if not flag or context is None:
        return True
    return str(context.get(flag, "")).strip().lower() not in ("", "false", "0", "no")


def check_budget(values: Dict[str, str], total: int, context: Optional[Dict[str, str]] = None,
                 skip: Tuple[str, ...] = ()) -> None:
    """The memory limits together must fit the machine: refuse a set that cannot, whoever asks. `context` (the
    profile with the overrides) decides whether an optional service's limit counts; `skip` names limits of services
    this machine does not run."""
    if total <= 0:
        return
    limits = sum(to_bytes(v) for k, v in values.items()
                 if BY_KEY[k]["type"] == "memory" and k not in skip and counts_in_budget(BY_KEY[k], context))
    if limits > total * 0.95:
        raise ConfigError(400, "over_budget", f"the memory limits add up to {limits >> 20} MB but the machine has {total >> 20} MB: lower some limits", None)


def describe(profile_name: str, kind: str, can_edit: bool, running_overrides: str, running_hash: str,
             profiles_dir: Path = PROFILES_DIR, local_dir: Path = LOCAL_DIR) -> Dict:
    profile = read_profile(profile_name, profiles_dir)
    overrides = {k: v for k, v in read_overrides(local_dir).items() if k in BY_KEY}
    values = effective(profile, overrides)
    now = overrides_hash(read_overrides(local_dir))
    settings = []
    for spec in SETTINGS:
        k = spec["key"]
        settings.append({**spec, "value": values[k], "profile_value": profile.get(k), "overridden": k in overrides})
    return {"profile": profile_name, "kind": kind, "can_edit": can_edit, "hash": running_hash, "profile_hash": profile_hash(profile),
            "overrides_hash": now, "running_overrides_hash": running_overrides, "pending_apply": now != running_overrides, "settings": settings}


def save(changes: Dict[str, Optional[str]], profile_name: str, profiles_dir: Path = PROFILES_DIR, local_dir: Path = LOCAL_DIR,
         total_memory: Optional[int] = None) -> None:
    """Validate every change, check the memory budget of the result, then replace local/overrides.env atomically."""
    if not changes:
        return
    clean: Dict[str, Optional[str]] = {}
    for key, value in changes.items():
        clean[key] = None if value is None else validate(key, value)
    profile = read_profile(profile_name, profiles_dir)
    current = read_overrides(local_dir)
    merged = {k: v for k, v in current.items() if k in BY_KEY}
    for key, value in clean.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    skip = () if "RECON_MEM_LIMIT" in profile or "RECON_MEM_LIMIT" in merged else ("RECON_MEM_LIMIT",)     # reconstruction runs only where the profile says so
    check_budget(effective(profile, merged), memory_total_bytes() if total_memory is None else total_memory,
                 {**profile, **merged}, skip)
    others = {k: v for k, v in current.items() if k not in BY_KEY}          # lines this service did not write stay as they are
    final = {**others, **merged}
    local_dir.mkdir(parents=True, exist_ok=True)
    text = "# Local settings changed from the app (docs/SERVER_OPERATIONS.md). Merged over the profile by render_env.sh; not in git.\n" \
           + "".join(f"{k}={v}\n" for k, v in sorted(final.items()))
    target = local_dir / OVERRIDES
    if not final:
        if target.exists():
            target.unlink()
        return
    fd, tmp = tempfile.mkstemp(dir=local_dir, prefix=OVERRIDES + ".")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, target)
