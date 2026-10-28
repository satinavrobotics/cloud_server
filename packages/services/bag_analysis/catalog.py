"""The declared catalog: what the runner's `--list` manifests say about analyzers and their metrics, stored so
the HTTP API can serve unit / direction / headline / description / title (table `analysis_catalog`).

The manifests are UNTRUSTED text (they come from the analysis image). `entries_from_catalog` therefore
re-validates the parts that are stored, independently of the JSON schema pass in `contract`: metric and analyzer
names by the schema's patterns, unit / direction by the schema's enums, every string bounded and stripped of
control characters (PostgreSQL jsonb refuses NUL), counts bounded. An analyzer whose declaration does not pass is
skipped whole (logged), never stored half.

`CatalogRecorder.observe(catalog)` is the worker's hook: it upserts through the store when the catalog differs
from the one it stored last. A failure to store is logged and swallowed (the next observation retries); it never
reaches the caller. `RecordingExecutor` wraps an executor so every `list_analyzers()` goes through it.
"""
import hashlib
import json
import logging
import re
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger("bag_analysis.catalog")

ANALYZER_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
PROFILE_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
METRIC_RE = re.compile(r"^[a-z][a-z0-9_]{1,59}$")
UNITS = ("s", "m", "rad", "share", "count", "ratio", "m/s", "none")
DIRECTIONS = ("lower_is_better", "higher_is_better", "neutral")
SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")

MAX_TITLE = 200
MAX_OWNER = 200
MAX_DESCRIPTION = 2000
MAX_METRIC_DESCRIPTION = 1000
MAX_IMAGE = 300
MAX_METRICS = 200
MAX_PROFILES = 50
MAX_ANALYZERS = 100

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class Rejected(ValueError):
    pass


def clean_text(value: Any, limit: int, what: str, required: bool = False) -> Optional[str]:
    """A bounded, control-character-free string; None for an absent optional value."""
    if value is None:
        if required:
            raise Rejected(f"{what}: missing")
        return None
    if not isinstance(value, str):
        raise Rejected(f"{what}: not a string")
    text = _CONTROL.sub("", value).strip()
    if required and not text:
        raise Rejected(f"{what}: empty")
    return text[:limit]


def declaration_hash(metrics: Mapping[str, Any]) -> str:
    """sha256 of the canonical JSON of a normalised metrics declaration."""
    blob = json.dumps(metrics, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("ascii")).hexdigest()


def normalise_metrics(raw: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(raw, dict) or not raw:
        raise Rejected("metrics: not a non-empty object")
    if len(raw) > MAX_METRICS:
        raise Rejected(f"metrics: more than {MAX_METRICS}")
    out: Dict[str, Dict[str, Any]] = {}
    for name, m in raw.items():
        if not isinstance(name, str) or not METRIC_RE.match(name):
            raise Rejected(f"metric name {str(name)[:60]!r} does not match the schema pattern")
        if not isinstance(m, dict):
            raise Rejected(f"metric {name}: not an object")
        if m.get("unit") not in UNITS:
            raise Rejected(f"metric {name}: unit not one of the schema's")
        if m.get("direction") not in DIRECTIONS:
            raise Rejected(f"metric {name}: direction not one of the schema's")
        headline = m.get("headline", False)
        if not isinstance(headline, bool):
            raise Rejected(f"metric {name}: headline is not a boolean")
        out[name] = {"unit": m["unit"], "direction": m["direction"],
                     "description": clean_text(m.get("description", ""), MAX_METRIC_DESCRIPTION,
                                               f"metric {name} description") or "",
                     "headline": headline}
    return out


def entry_from_manifest(manifest: Any, *, extractor_version: Optional[int], analysis_image: Optional[str],
                        tool_git_sha: Optional[str]) -> Dict[str, Any]:
    """One catalog entry (the columns of analysis_catalog), or `Rejected`."""
    if not isinstance(manifest, dict):
        raise Rejected("manifest: not an object")
    aid = manifest.get("id")
    if not isinstance(aid, str) or not ANALYZER_RE.match(aid):
        raise Rejected("id: does not match the schema pattern")
    version = manifest.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or not 1 <= version <= 2 ** 31 - 1:
        raise Rejected("version: not a positive integer")
    profiles = manifest.get("profiles")
    if not isinstance(profiles, dict) or not profiles or len(profiles) > MAX_PROFILES:
        raise Rejected("profiles: not an object of 1.. entries")
    for p in profiles:
        if not isinstance(p, str) or not PROFILE_RE.match(p):
            raise Rejected("profiles: a name does not match the schema pattern")
    metrics = normalise_metrics(manifest.get("metrics"))
    ev = extractor_version if isinstance(extractor_version, int) and not isinstance(extractor_version, bool) \
        and 1 <= extractor_version <= 2 ** 31 - 1 else None
    sha = tool_git_sha if isinstance(tool_git_sha, str) and SHA_RE.match(tool_git_sha) else None
    return {"analyzer": aid, "analyzer_version": version, "decl_hash": declaration_hash(metrics),
            "title": clean_text(manifest.get("title"), MAX_TITLE, "title"),
            "owner": clean_text(manifest.get("owner"), MAX_OWNER, "owner"),
            "description": clean_text(manifest.get("description"), MAX_DESCRIPTION, "description"),
            "profiles": sorted(profiles), "metrics": metrics, "extractor_version": ev,
            "analysis_image": clean_text(analysis_image, MAX_IMAGE, "analysis_image") if isinstance(analysis_image, str) else None,
            "tool_git_sha": sha}


def entries_from_catalog(catalog: Any) -> List[Dict[str, Any]]:
    """The storable entries of an executor `Catalog` (rejected manifests are logged and skipped)."""
    out: List[Dict[str, Any]] = []
    for m in list(catalog.analyzers)[:MAX_ANALYZERS]:
        try:
            out.append(entry_from_manifest(m, extractor_version=catalog.extractor_version,
                                           analysis_image=catalog.analysis_image,
                                           tool_git_sha=getattr(catalog, "tool_git_sha", None)))
        except Rejected as e:
            logger.warning("catalog: manifest %r not stored: %s", m.get("id") if isinstance(m, dict) else None, e)
    if len(catalog.analyzers) > MAX_ANALYZERS:
        logger.warning("catalog: %d manifests, only the first %d considered", len(catalog.analyzers), MAX_ANALYZERS)
    return out


class CatalogRecorder:
    def __init__(self, store: Any):
        self.store = store
        self._stored: Optional[str] = None

    def observe(self, catalog: Any) -> Optional[int]:
        """Upsert the catalog's entries unless it equals the last stored one. Returns the number of entries
        written, or None when nothing was attempted / it failed. Never raises."""
        try:
            entries = entries_from_catalog(catalog)
            fingerprint = json.dumps(entries, sort_keys=True, default=str)
            if fingerprint == self._stored:
                return None
            if entries:
                self.store.upsert_catalog(entries)
            self._stored = fingerprint
            return len(entries)
        except Exception:  # noqa: BLE001 - the catalog is metadata; analysis must go on
            logger.exception("catalog: could not store the declared catalog (continuing)")
            return None


class RecordingExecutor:
    """Delegates to `inner`; every catalog it lists is offered to the recorder."""

    def __init__(self, inner: Any, recorder: CatalogRecorder):
        self.inner, self.recorder = inner, recorder

    def list_analyzers(self) -> Any:
        catalog = self.inner.list_analyzers()
        self.recorder.observe(catalog)
        return catalog

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)
