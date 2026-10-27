"""Navstack images in the private registry, and what each robot runs (docs/navstack_deploy.md).

    GET /api/v1/navstack/images?branch=humble   -> list_images()
    GET /api/v1/fleet/software?branch=humble    -> fleet_software()

The server reads the registry's HTTP API itself (no docker), so the build list works while a robot is
offline and is fetched once for the fleet. It is the async httpx port of
satibot_orchestrator/isaac_ros_docker/scripts/navstack_versions.py (describe() / versions()): per tag,
the index -> linux/<arch> image manifest -> config blob, whose OCI labels carry the git sha and the
build info. `<branch>-latest` is folded into the row of the image it points to as an alias.

Caching: tags are immutable, so a versioned tag's row is cached for the life of the process and config
blobs (content addressed) too; only `tags/list` and the `*-latest` tags (which move) are refetched, and
the assembled answer is reused for NAVSTACK_IMAGES_CACHE_TTL_S (60 s) per branch. Concurrent refreshes
of one branch share one fetch.

An unreachable registry is an error (`RegistryError`, 502), never an empty list. "Nothing pushed yet"
(tags/list 404 or no tags) is an empty list with `latest_tag: null`.

`parse_sw_version()` is the one parser of the build id a robot reports (factsheet / state `swVersion`
-> robot_latest.sw_version): `sati/navstack:<tag>` is image mode, `dev:<describe>` dev mode.
"""
import asyncio
import datetime
import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
from fastapi import HTTPException

from packages import config

logger = logging.getLogger("ApiDelegationService.navstack_images")

BUILD_INFO_LABEL = "com.satinavrobotics.build-info"
INDEX_TYPES = ("application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json")
MANIFEST_TYPES = ("application/vnd.oci.image.manifest.v1+json",
                  "application/vnd.docker.distribution.manifest.v2+json")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_DEV_SHA_RE = re.compile(r"(?:^|[-_:])g?([0-9a-f]{7,40})(?:-dirty)?$")
IMAGE_PREFIX = "sati/navstack:"


class RegistryError(Exception):
    """The registry could not be read: `detail` is the text for the operator, `status` the HTTP
    status the route answers (always 502 here)."""

    def __init__(self, detail: str, status: int = 502):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def check_branch(branch: Optional[str]) -> str:
    """The branch of a request (default NAVSTACK_DEFAULT_BRANCH); 422 when it is not a branch name."""
    branch = config.NAVSTACK_DEFAULT_BRANCH if branch is None else branch
    if not BRANCH_RE.match(branch):
        raise HTTPException(422, f"'{branch}' is not a branch name")
    return branch


def _utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# --------------------------------------------------------------------------------------------
# the build id a robot reports
# --------------------------------------------------------------------------------------------

def parse_sw_version(sw_version: Optional[str]) -> Dict[str, Optional[str]]:
    """`{"mode", "tag", "sha"}` of a robot's reported build id; pure, never raises.

    - `sati/navstack:<tag>` (any registry host prefix is accepted): mode "image", the tag, and the
      sha7 the tag ends with (`<branch>-<YYYYMMDD>-<sha7>`; None for `<branch>-latest`).
    - `dev:<describe>`: mode "dev", tag None, sha best effort from the describe string (its trailing
      `g<hex>` / `<hex>` token, `-dirty` ignored), else None.
    - anything else, empty or None: mode None, tag None, sha None (shown as is by the client).
    """
    out: Dict[str, Optional[str]] = {"mode": None, "tag": None, "sha": None}
    text = (sw_version or "").strip()
    if not text:
        return out
    if text.startswith("dev:"):
        out["mode"] = "dev"
        match = _DEV_SHA_RE.search(text[4:].strip())
        out["sha"] = match.group(1) if match else None
        return out
    repo, sep, tag = text.rpartition(":")
    if sep and tag and "/" not in tag and repo.endswith(config.NAVSTACK_IMAGE):
        out["mode"] = "image"
        out["tag"] = tag
        last = tag.rsplit("-", 1)[-1]
        out["sha"] = last if _SHA_RE.match(last) and "-" in tag else None
    return out


def sw_version_matches(mode: str, tag: Optional[str], reported: Optional[str]) -> bool:
    """Does a reported build id match a deploy target? (image: exact; dev: any `dev:...`)."""
    parsed = parse_sw_version(reported)
    if mode == "image":
        return parsed["mode"] == "image" and parsed["tag"] == tag
    return parsed["mode"] == "dev"


# --------------------------------------------------------------------------------------------
# the registry client
# --------------------------------------------------------------------------------------------

class NavstackRegistry:
    """Async reader of the navstack repository of the registry. `http_factory(timeout=...)` returns
    an httpx.AsyncClient (tests pass one over a MockTransport); `clock` is time.monotonic."""

    def __init__(self, base_url: Optional[str] = None, image: Optional[str] = None,
                 arch: Optional[str] = None, ttl_s: Optional[float] = None,
                 timeout_s: Optional[float] = None,
                 http_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
                 clock: Callable[[], float] = time.monotonic):
        self.base_url = (base_url or config.NAVSTACK_REGISTRY_URL).rstrip("/")
        self.image = image or config.NAVSTACK_IMAGE
        self.arch = arch or config.NAVSTACK_ARCH
        self.ttl_s = config.NAVSTACK_IMAGES_CACHE_TTL_S if ttl_s is None else ttl_s
        self.timeout_s = config.NAVSTACK_REGISTRY_TIMEOUT_S if timeout_s is None else timeout_s
        self._http_factory = http_factory
        self._clock = clock
        self._rows: Dict[str, Dict[str, Any]] = {}       # versioned tag -> row, forever
        self._configs: Dict[str, Dict[str, Any]] = {}    # config blob digest -> blob, forever
        self._answers: Dict[str, Tuple[float, Dict[str, Any]]] = {}   # branch -> (at, answer)
        self._inflight: Dict[str, "asyncio.Task[Dict[str, Any]]"] = {}

    # --- HTTP -------------------------------------------------------------------------------
    async def _get(self, http: httpx.AsyncClient, path: str,
                   accept: Optional[Tuple[str, ...]] = None) -> Tuple[Any, Optional[str]]:
        """(json body, Docker-Content-Digest). HttpStatus on an HTTP error status, RegistryError
        when the registry cannot be reached."""
        headers = {"Accept": ", ".join(accept)} if accept else {}
        try:
            resp = await http.get(f"{self.base_url}/v2/{path}", headers=headers)
        except httpx.TimeoutException:
            raise RegistryError(f"The image registry at {self.base_url} timed out") from None
        except httpx.HTTPError as exc:
            raise RegistryError(f"The image registry at {self.base_url} is not reachable "
                                f"({type(exc).__name__}); is Tailscale up?") from None
        if resp.status_code >= 400:
            raise _HttpStatus(resp.status_code, path)
        try:
            return resp.json(), resp.headers.get("Docker-Content-Digest")
        except ValueError:
            raise _HttpStatus(502, path) from None

    async def _manifest(self, http: httpx.AsyncClient, tag: str) -> Tuple[str, str, Dict[str, Any]]:
        """(digest the tag points to, digest of its image manifest for the arch, that manifest).
        LookupError when the index has no linux/<arch> image."""
        body, pushed = await self._get(http, f"{self.image}/manifests/{tag}",
                                       INDEX_TYPES + MANIFEST_TYPES)
        digest = pushed
        if body.get("mediaType") in INDEX_TYPES or "manifests" in body:
            for entry in body["manifests"]:
                platform = entry.get("platform", {})
                if platform.get("os") == "linux" and platform.get("architecture") == self.arch:
                    digest = entry["digest"]
                    body, _ = await self._get(http, f"{self.image}/manifests/{digest}",
                                              MANIFEST_TYPES)
                    break
            else:
                raise LookupError(f"no linux/{self.arch} image in {tag}")
        return pushed or digest, digest, body

    async def _row(self, http: httpx.AsyncClient, tag: str) -> Dict[str, Any]:
        """The row of one tag (navstack_versions.describe): from the config labels of its image."""
        pushed, digest, manifest = await self._manifest(http, tag)
        config_digest = manifest["config"]["digest"]
        blob = self._configs.get(config_digest)
        if blob is None:
            blob, _ = await self._get(http, f"{self.image}/blobs/{config_digest}")
            self._configs[config_digest] = blob
        labels = (blob.get("config") or {}).get("Labels") or {}
        try:
            info = json.loads(labels.get(BUILD_INFO_LABEL, "{}"))
        except ValueError:
            info = {}
        if not isinstance(info, dict):
            info = {}
        return {
            "tag": tag,
            "digest": digest,
            "git_sha": info.get("git_sha") or labels.get("org.opencontainers.image.revision"),
            "created": blob.get("created"),
            "commit_subject": info.get("commit_subject"),
            "size_bytes": sum(layer.get("size", 0) for layer in manifest.get("layers", [])),
            "aliases": [],
            "build_info": info or None,
        }

    # --- the listing ------------------------------------------------------------------------
    async def list_images(self, branch: str) -> Dict[str, Any]:
        """`{"branch", "latest_tag", "images": [...], "fetched_at"}`, newest first; RegistryError
        when the registry cannot be read."""
        hit = self._answers.get(branch)
        if hit is not None and self._clock() - hit[0] < self.ttl_s:
            return hit[1]
        task = self._inflight.get(branch)
        if task is None:
            task = asyncio.ensure_future(self._fetch(branch))
            self._inflight[branch] = task
            task.add_done_callback(lambda _t, b=branch: self._inflight.pop(b, None))
        return await asyncio.shield(task)

    async def _fetch(self, branch: str) -> Dict[str, Any]:
        async with self._http_factory(timeout=self.timeout_s) as http:
            try:
                body, _ = await self._get(http, f"{self.image}/tags/list")
                tags = sorted(body.get("tags") or [])
            except _HttpStatus as exc:
                if exc.status != 404:      # 404: the repository does not exist yet, nothing pushed
                    raise RegistryError(f"The image registry answered HTTP {exc.status} for the "
                                        f"tag list") from None
                tags = []
            tags = [t for t in tags if t.startswith(branch + "-")]
            latest = f"{branch}-latest"
            rows: Dict[str, Dict[str, Any]] = {}      # arm64 digest -> row
            for tag in tags:
                row = await self._row_of(http, tag, moving=tag.endswith("-latest"))
                if row is None:
                    continue
                if row["digest"] in rows:
                    kept = rows[row["digest"]]
                    # Same image under two tags: the versioned tag names the row, -latest is an alias.
                    if kept["tag"].endswith("-latest"):
                        kept["tag"], tag = tag, kept["tag"]
                    kept["aliases"].append(tag)
                else:
                    rows[row["digest"]] = row
        images = sorted(rows.values(), key=lambda r: r["created"] or "", reverse=True)
        latest_tag = None
        for row in images:
            if latest in [row["tag"]] + row["aliases"]:
                latest_tag = row["tag"]
        answer = {"branch": branch, "latest_tag": latest_tag, "images": images,
                  "fetched_at": _utc_iso()}
        self._answers[branch] = (self._clock(), answer)
        return answer

    async def _row_of(self, http: httpx.AsyncClient, tag: str, moving: bool) -> Optional[Dict[str, Any]]:
        """A tag's row (a copy: callers add aliases), None when this one tag cannot be described
        (skipped with a warning, as navstack_versions.py does); RegistryError when the registry
        itself fails."""
        cached = None if moving else self._rows.get(tag)
        if cached is None:
            try:
                cached = await self._row(http, tag)
            except (LookupError, KeyError, ValueError, TypeError) as exc:
                logger.warning("navstack tag %s skipped: %s", tag, exc)
                return None
            except _HttpStatus as exc:
                if exc.status >= 500:
                    raise RegistryError(f"The image registry answered HTTP {exc.status} for "
                                        f"{tag}") from None
                logger.warning("navstack tag %s skipped: HTTP %s on %s", tag, exc.status, exc.path)
                return None
            if not moving:
                self._rows[tag] = cached
        return {**cached, "aliases": [], "build_info": cached["build_info"]}

    async def has_tag(self, tag: str, branch: Optional[str] = None) -> bool:
        """Is `tag` (a versioned tag or an alias such as `<branch>-latest`) in the registry?
        RegistryError when it cannot be read. The branch is the tag's own (text before the date)."""
        branch = branch or _branch_of(tag)
        answer = await self.list_images(branch)
        return any(tag == row["tag"] or tag in row["aliases"] for row in answer["images"])

    async def resolve(self, tag: str, branch: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The row `tag` names (alias folded), or None when the registry has no such tag."""
        answer = await self.list_images(branch or _branch_of(tag))
        for row in answer["images"]:
            if tag == row["tag"] or tag in row["aliases"]:
                return row
        return None


class _HttpStatus(Exception):
    def __init__(self, status: int, path: str):
        super().__init__(f"HTTP {status} on {path}")
        self.status = status
        self.path = path


def _branch_of(tag: str) -> str:
    """The branch a tag `<branch>-<YYYYMMDD>-<sha7>` / `<branch>-latest` belongs to."""
    match = re.match(r"^(.*)-(?:\d{8}-[0-9a-f]{7,40}|latest)$", tag)
    return match.group(1) if match else tag.split("-", 1)[0]


# --------------------------------------------------------------------------------------------
# the fleet view
# --------------------------------------------------------------------------------------------

def _iso(ts: Any) -> Optional[str]:
    if ts is None:
        return None
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


def software_row(robot_name: str, sw_version: Optional[str], last_seen: Any,
                 branch: str, latest_tag: Optional[str],
                 latest_aliases: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """One robot of GET /fleet/software. `behind_latest`: image mode, a tag of this branch, and a
    tag other than `<branch>-latest`'s (its alias `<branch>-latest` itself counts as current).
    Dev builds, other branches, unknown build ids and an unknown latest are never behind."""
    parsed = parse_sw_version(sw_version)
    tag = parsed["tag"]
    behind = (parsed["mode"] == "image" and tag is not None and latest_tag is not None
              and tag.startswith(branch + "-") and tag != latest_tag
              and tag not in latest_aliases)
    return {"robot_name": robot_name, "sw_version": sw_version, "mode": parsed["mode"],
            "tag": tag, "sha": parsed["sha"], "last_seen": _iso(last_seen),
            "behind_latest": bool(behind)}


async def fleet_software(db: Any, registry: NavstackRegistry, branch: str) -> Dict[str, Any]:
    """`{"branch", "latest_tag", "robots": [...], "registry_error"}`. The robots come from
    robot_latest and are listed even when the registry cannot be read (then `latest_tag` is null,
    nothing is behind and `registry_error` says why; null otherwise)."""
    from packages.api import fleet_reads
    async with fleet_reads.read_cursor(db) as cursor:
        await cursor.execute("SELECT robot_name, sw_version, last_seen FROM robot_latest "
                             "ORDER BY robot_name")
        rows = await cursor.fetchall()
    latest_tag, aliases, error = None, (), None
    try:
        answer = await registry.list_images(branch)
        latest_tag = answer["latest_tag"]
        for row in answer["images"]:
            if row["tag"] == latest_tag:
                aliases = tuple(row["aliases"])
    except RegistryError as exc:
        error = exc.detail
    return {"branch": branch, "latest_tag": latest_tag,
            "robots": [software_row(name, sw, seen, branch, latest_tag, aliases)
                       for name, sw, seen in rows],
            "registry_error": error}
