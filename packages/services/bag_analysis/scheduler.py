"""Which recordings need which analyzers, and the two ways jobs get queued.

- auto (`BAG_ANALYSIS_AUTO`, default OFF): `schedule_once` enqueues, for every run recording (part nav, the only
  part analysed: raw / params / host / incident parts and legacy bags are not bags to analyse) with an object in
  the store, one job naming the analyzers that apply and have no result for the CURRENT key
  (analyzer, analyzer_version, profile 'default', extractor_version). A version bump of an analyzer or of the
  extractor therefore re-queues the old bags; a recording that already has a queued/running job, or whose auto job
  failed within BAG_ANALYSIS_FAILED_RETRY_AFTER_S, is left alone, so the same work is never queued twice.
- on request: `request` queues a job for one recording (and optional analyzers / 'id:profile' entries); it does not
  need the auto flag. API routes will call it; none exist yet.

`applies_to`: description_regex is searched in the recording's sidecar description and in its group_key (the run
prefix, e.g. 20261004_200002_navstack_sati_odin); bag_id_regex in the sidecar bag_id. topics_all cannot be judged
from the registry (it holds no topic list): it is left to the runner, which answers n/a for a bag without them.
"""
import logging
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .executor import Catalog

logger = logging.getLogger("bag_analysis.scheduler")
DEFAULT_PROFILE = "default"
Key = Tuple[str, str, int, str, int]


def has_object(recording: Mapping[str, Any]) -> bool:
    """The bag object exists in the store (the registry can hold a sidecar whose upload never finished)."""
    return bool(recording.get("object_present")) and recording.get("size_bytes") is not None


def applies(manifest: Mapping[str, Any], recording: Mapping[str, Any]) -> bool:
    """Whether the manifest's `applies_to` matches the recording (False for a regex that does not compile)."""
    rule = manifest.get("applies_to") or {}
    try:
        rx = rule.get("description_regex")
        if rx and not any(re.search(rx, t) for t in (recording.get("description"), recording.get("group_key")) if t):
            return False
        rx = rule.get("bag_id_regex")
        if rx and not re.search(rx, recording.get("bag_id") or ""):
            return False
    except re.error:
        logger.warning("analyzer %s: applies_to regex does not compile", manifest.get("id"))
        return False
    return True


def runnable(manifest: Mapping[str, Any], catalog: Catalog) -> bool:
    need = (manifest.get("requires") or {}).get("extractor")
    return need is None or catalog.extractor_version >= need


def needed_analyzers(catalog: Catalog, recording: Mapping[str, Any], existing: Set[Key]) -> List[str]:
    """Ids of the analyzers that apply to the recording and have no result for the current key."""
    out = []
    for m in catalog.analyzers:
        if not runnable(m, catalog) or not applies(m, recording):
            continue
        key = (recording["id"], m["id"], m["version"], DEFAULT_PROFILE, catalog.extractor_version)
        if key not in existing:
            out.append(m["id"])
    return sorted(out)


def schedule_once(store: Any, catalog: Catalog, *, auto: bool, retry_failed_after_s: float) -> Dict[str, int]:
    """One scheduler pass. With `auto` False nothing is enqueued (and nothing is read)."""
    counts = {"recordings": 0, "enqueued": 0, "up_to_date": 0, "missing_object": 0, "busy": 0,
              "recently_failed": 0, "duplicate": 0}
    if not auto:
        return counts
    existing = store.result_keys()
    busy = store.busy_recordings()
    failed = store.recently_failed_recordings(retry_failed_after_s)
    for rec in store.candidate_recordings():
        counts["recordings"] += 1
        if not has_object(rec):
            counts["missing_object"] += 1
            continue
        needed = needed_analyzers(catalog, rec, existing)
        if not needed:
            counts["up_to_date"] += 1
        elif rec["id"] in busy:
            counts["busy"] += 1
        elif rec["id"] in failed:
            counts["recently_failed"] += 1
        elif store.enqueue(rec["id"], needed, "auto") is None:
            counts["duplicate"] += 1
        else:
            counts["enqueued"] += 1
    return counts


def request(store: Any, catalog: Catalog, recording_id: str, analyzer_ids: Optional[Sequence[str]] = None,
            requested_by: Optional[str] = None) -> Optional[str]:
    """Queue an on-request job. Returns the job id, None when the same job is already queued/running.
    ValueError: unknown recording, no object, an unknown analyzer / profile, or nothing that applies."""
    rec = store.get_recording(recording_id)
    if rec is None:
        raise ValueError("unknown recording")
    if rec["part"] != "nav" or rec["kind"] != "run":
        raise ValueError("only run recordings of part nav are analysed")
    if not has_object(rec):
        raise ValueError("the recording has no stored bag object")
    by_id = catalog.by_id()
    ids = sorted(set(analyzer_ids)) if analyzer_ids else None
    for entry in ids or ():
        aid, _, profile = entry.partition(":")
        if aid not in by_id or (profile and profile not in by_id[aid]["profiles"]):
            raise ValueError(f"unknown analyzer or profile {entry!r}")
    if ids is None and not any(applies(m, rec) and runnable(m, catalog) for m in catalog.analyzers):
        raise ValueError("no analyzer applies to this recording")
    return store.enqueue(recording_id, ids, "request", requested_by)
