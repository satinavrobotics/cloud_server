"""Recordings registry ingest: rosbag uploads (MinIO `rosbags/<robot>/<bag_id>` + sidecar) -> `recordings`.

Table, views, dedupe rule and clock-skew tolerance: migration 20261027_01_recordings. The CLI is
scripts/backfill_recordings.py.

The true UTC interval of a bag is in its rosbag2 `metadata.yaml` (starting_time + duration). The bag
object is a tar of the rosbag2 directory (several GB), so `find_tar_member` walks the tar headers with
ranged reads (one 512-byte header per member, skipping the member's data) and reads only that file.
When the object or the file is missing the interval falls back to the sidecar's `recorded_at`
(robot LOCAL time, Europe/Budapest) as a zero-length range.

`collect` is pure apart from the reads it is given; `UPSERT_SQL` is idempotent (see its comment).
"""
import datetime
import json
import re
import uuid
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

BUCKET = "rosbags"
ROBOT_TZ = ZoneInfo("Europe/Budapest")
# The views' tolerance (migration 20261027_01_recordings), for the dry run's overlap count.
CLOCK_SKEW_S = 5
METADATA_NAME = "metadata.yaml"
MAX_MEMBER_BYTES = 1 << 20           # metadata.yaml is ~20 kB; refuse to read more than 1 MiB
MAX_HEADERS = 10000                  # a rosbag2 directory has a handful of files
BLOCK = 512

# `20261018_190021_navstack_sati_odin` / `..._raw` (the description the orchestrator uploads with)
RUN_PREFIX = re.compile(r"^(\d{8}_\d{6}_navstack_sati_odin)(_raw)?(?:\s|$)")
NAMESPACE = uuid.UUID("5a7b1c4e-6f0e-4b53-9a39-0d7e2f1a8c01")

ReadRange = Callable[[int, int], bytes]       # (offset, length) -> up to `length` bytes at `offset`


# --- tar walker ------------------------------------------------------------------------------------

def _tar_size(field: bytes) -> int:
    """A header's size field: octal text, or base-256 (high bit set) for members over 8 GiB."""
    if field[0] & 0x80:
        return int.from_bytes(bytes([field[0] & 0x7F]) + field[1:], "big")
    text = field.rstrip(b"\0 ").strip()
    return int(text, 8) if text else 0


def _pax_path(data: bytes) -> Optional[str]:
    """`path` of a pax extended header ('<len> path=<value>\\n' records)."""
    while data:
        sp = data.find(b" ")
        if sp < 0:
            return None
        n = int(data[:sp])
        rec = data[sp + 1:n]
        if rec.startswith(b"path="):
            return rec[5:].rstrip(b"\n").decode("utf-8", "replace")
        data = data[n:]
    return None


def _is_metadata(path: str) -> bool:
    parts = [p for p in path.split("/") if p and p != "."]
    return 0 < len(parts) <= 2 and parts[-1] == METADATA_NAME


def find_tar_member(read: ReadRange, total: Optional[int] = None) -> Optional[bytes]:
    """The content of the rosbag2 `metadata.yaml` (top level of the tar or one directory down), or None.

    Reads one header per member and jumps over the data (size rounded up to 512), so it does not
    matter where metadata.yaml sits in the tar. Handles pax ('x') and GNU long-name ('L') headers.
    `total`: the object size, to stop at the end of a truncated upload."""
    pos = 0
    long_name: Optional[str] = None
    for _ in range(MAX_HEADERS):
        if total is not None and pos + BLOCK > total:
            return None
        head = read(pos, BLOCK)
        if len(head) < BLOCK or not head.strip(b"\0"):
            return None                                   # end-of-archive block / short read
        size = _tar_size(head[124:136])
        kind = head[156:157]
        data_pos = pos + BLOCK
        padded = (size + BLOCK - 1) // BLOCK * BLOCK
        if kind in (b"x", b"L"):                          # the next header's path, in this one's data
            if size > MAX_MEMBER_BYTES:
                return None
            body = read(data_pos, size)
            long_name = (body.rstrip(b"\0").decode("utf-8", "replace") if kind == b"L"
                         else _pax_path(body))
        elif kind == b"g":
            pass
        else:
            name = long_name
            if name is None:
                name = head[0:100].split(b"\0")[0].decode("utf-8", "replace")
                prefix = head[345:500].split(b"\0")[0].decode("utf-8", "replace")
                if head[257:262] == b"ustar" and prefix:
                    name = f"{prefix}/{name}"
            long_name = None
            if kind in (b"0", b"\0") and _is_metadata(name):
                if size > MAX_MEMBER_BYTES:
                    return None
                return read(data_pos, size)
        pos = data_pos + padded
    return None


# --- intervals -------------------------------------------------------------------------------------

def parse_bag_metadata(text: bytes) -> Dict[str, Any]:
    """{start_ns, duration_ns, message_count, storage_identifier} from rosbag2 metadata.yaml.
    ValueError when the interval is not there."""
    import yaml
    info = (yaml.safe_load(text) or {}).get("rosbag2_bagfile_information") or {}
    try:
        start = int(info["starting_time"]["nanoseconds_since_epoch"])
        duration = int(info["duration"]["nanoseconds"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"metadata.yaml without starting_time/duration: {e!r}")
    return {"start_ns": start, "duration_ns": duration,
            "message_count": info.get("message_count"),
            "storage_identifier": info.get("storage_identifier")}


def _utc(ns: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(ns // 1_000_000_000, datetime.timezone.utc) + \
        datetime.timedelta(microseconds=(ns % 1_000_000_000) // 1000)


def sidecar_start(recorded_at: Optional[str]) -> Optional[datetime.datetime]:
    """`recorded_at` as a UTC instant: a naive stamp is robot local time (Europe/Budapest)."""
    if not recorded_at:
        return None
    try:
        t = datetime.datetime.fromisoformat(recorded_at)
    except ValueError:
        return None
    return (t.replace(tzinfo=ROBOT_TZ) if t.tzinfo is None else t).astimezone(datetime.timezone.utc)


def interval(sidecar: Mapping[str, Any], metadata: Optional[Mapping[str, Any]]
             ) -> Tuple[Optional[datetime.datetime], Optional[datetime.datetime], str]:
    """(start, end, interval_source): 'metadata' | 'sidecar_start_only' (end == start) | 'none'."""
    if metadata:
        start = _utc(metadata["start_ns"])
        return start, _utc(metadata["start_ns"] + metadata["duration_ns"]), "metadata"
    start = sidecar_start(sidecar.get("recorded_at"))
    if start is not None:
        return start, start, "sidecar_start_only"
    return None, None, "none"


# --- rows ------------------------------------------------------------------------------------------

def classify(sidecar: Mapping[str, Any]) -> Tuple[str, str, str]:
    """(kind, group_key, part). The orchestrator's description carries the run prefix; a legacy
    bag without it is its own group (group_key = bag_id, part 'bag')."""
    m = RUN_PREFIX.match(sidecar.get("description") or "")
    if m:
        return "run", m.group(1), "raw" if m.group(2) else "nav"
    return "run", sidecar["bag_id"], "bag"


def mission_context(mission: Optional[Mapping[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    """(mission_runs.run_id, order_id) from the robot-recorded mission block: incident.yaml's `mission:`
    or the nav bag's /sati/mission_context JSON ({run_id, order_id, order_rev, mission_name,
    last_node_id, node_idx, order_update_id, context_stamp_ns}). Its `run_id` is the dispatcher's SHORT
    run id (8 hex chars), not the mission_runs uuid: that is fleet_recorder.run_uuid(mission_name,
    short). The block itself is kept whole in source.mission, so the derivation can be redone."""
    if not mission:
        return None, None
    from packages.controllers.mission.fleet_recorder import run_uuid
    name, short = mission.get("mission_name"), mission.get("run_id")
    run_id = str(run_uuid(str(name), str(short))) if name and short else None
    order_id = mission.get("order_id")
    return run_id, str(order_id) if order_id else None


def build_row(sidecar: Mapping[str, Any], obj: Optional[Mapping[str, Any]],
              metadata: Optional[Mapping[str, Any]],
              mission: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """One `recordings` row. `obj`: {key, size, etag, last_modified} of the bag object, None when
    only the sidecar exists (the key is then the one the upload would have used). `mission`: the
    robot-recorded mission block (see mission_context); no recording has one yet."""
    kind, group_key, part = classify(sidecar)
    start, end, how = interval(sidecar, metadata)
    robot = sidecar["robot_name"]
    run_id, order_id = mission_context(mission)
    return {
        "id": str(uuid.uuid5(NAMESPACE, f"{robot}/{group_key}/{part}")),
        "kind": kind, "robot_name": robot, "start": start, "end": end,
        "group_key": group_key, "part": part,
        "storage_bucket": BUCKET,
        "storage_key": obj["key"] if obj else f"{robot}/{sidecar['bag_id']}",
        "size_bytes": obj["size"] if obj else None,
        "content_hash": (obj.get("etag") or "").strip('"') or None if obj else None,
        "map_id": sidecar.get("map_id"), "run_id": run_id, "order_id": order_id,
        "source": {"sidecar": dict(sidecar), "interval_source": how,
                   "object_present": obj is not None,
                   "uploaded_at": obj["last_modified"].isoformat() if obj and obj.get("last_modified") else None,
                   "bag_metadata": dict(metadata) if metadata else None,
                   "mission": dict(mission) if mission else None,
                   "duplicates": []},
    }


def _rank(row: Mapping[str, Any]) -> Tuple:
    """Which upload of a duplicated (robot, group_key, part) wins: has its object, then has the
    metadata interval, then the newest upload, then bag_id (deterministic)."""
    src = row["source"]
    return (src["object_present"], src["interval_source"] == "metadata",
            src["uploaded_at"] or "", src["sidecar"]["bag_id"])


def collapse(rows: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """(one row per (robot_name, group_key, part), number of rows dropped). The winner lists the
    dropped bag_ids in source.duplicates."""
    best: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    dropped: Dict[Tuple[str, str, str], List[str]] = {}
    for row in rows:
        key = (row["robot_name"], row["group_key"], row["part"])
        old = best.get(key)
        if old is None:
            best[key] = row
        elif _rank(row) > _rank(old):
            dropped.setdefault(key, []).append(old["source"]["sidecar"]["bag_id"])
            best[key] = row
        else:
            dropped.setdefault(key, []).append(row["source"]["sidecar"]["bag_id"])
    for key, ids in dropped.items():
        best[key]["source"]["duplicates"] = sorted(ids)
    return list(best.values()), sum(len(v) for v in dropped.values())


def collect(sources: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """(rows to upsert, counts). `sources`: sidecars() -> sidecar dicts; stat(robot, bag_id) -> the
    bag object's {key, size, etag, last_modified} or None; read_range(key) -> a ReadRange."""
    counts: Dict[str, Any] = {"sidecars": 0, "bag_missing": 0, "metadata_missing": 0,
                              "interval_source": {"metadata": 0, "sidecar_start_only": 0, "none": 0}}
    rows = []
    for sidecar in sources.sidecars():
        counts["sidecars"] += 1
        obj = sources.stat(sidecar["robot_name"], sidecar["bag_id"])
        metadata = None
        if obj is None:
            counts["bag_missing"] += 1
        else:
            try:
                text = find_tar_member(sources.read_range(obj["key"]), obj["size"])
                metadata = parse_bag_metadata(text) if text is not None else None
            except Exception:                                 # unreadable tar / yaml: fall back
                metadata = None
            if metadata is None:
                counts["metadata_missing"] += 1
        rows.append(build_row(sidecar, obj, metadata))
    kept, dropped = collapse(rows)
    for row in kept:                                          # of the rows that will be stored
        counts["interval_source"][row["source"]["interval_source"]] += 1
    counts["rows"] = len(kept)
    counts["duplicates_collapsed"] = dropped
    return kept, counts


# --- SQL -------------------------------------------------------------------------------------------

# Idempotent on (robot_name, group_key, part). A re-run, or a later upload of the same recording,
# refreshes the row, except that a row with an object is not replaced by an upload without one, and
# the robot-recorded context (map_id, run_id, order_id; git_sha / image_tag are not written here) is
# never blanked.
UPSERT_SQL = """
INSERT INTO recordings (id, kind, robot_name, during, group_key, part, storage_bucket, storage_key,
                        size_bytes, content_hash, map_id, run_id, order_id, source)
VALUES (%(id)s, %(kind)s, %(robot_name)s,
        CASE WHEN %(start)s::timestamptz IS NULL THEN 'empty'::tstzrange
             ELSE tstzrange(%(start)s, %(end)s, '[]') END,
        %(group_key)s, %(part)s, %(storage_bucket)s, %(storage_key)s, %(size_bytes)s,
        %(content_hash)s, %(map_id)s, %(run_id)s, %(order_id)s, %(source)s::jsonb)
ON CONFLICT (robot_name, group_key, part) DO UPDATE SET
  during = EXCLUDED.during, storage_key = EXCLUDED.storage_key, size_bytes = EXCLUDED.size_bytes,
  content_hash = EXCLUDED.content_hash, source = EXCLUDED.source,
  map_id = COALESCE(EXCLUDED.map_id, recordings.map_id),
  run_id = COALESCE(EXCLUDED.run_id, recordings.run_id),
  order_id = COALESCE(EXCLUDED.order_id, recordings.order_id)
WHERE recordings.size_bytes IS NULL OR EXCLUDED.size_bytes IS NOT NULL
RETURNING (xmax = 0) AS inserted
"""


def write(cursor: Any, rows: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    """Upsert `rows` through a cursor. Returns {inserted, updated, kept} ('kept': conflict skipped)."""
    out = {"inserted": 0, "updated": 0, "kept": 0}
    for row in rows:
        cursor.execute(UPSERT_SQL, dict(row, source=json.dumps(row["source"])))
        got = cursor.fetchone()
        out["kept" if got is None else "inserted" if got[0] else "updated"] += 1
    return out


def overlap_counts(rows: Iterable[Mapping[str, Any]], runs: Iterable[Mapping[str, Any]],
                   now: Optional[datetime.datetime] = None) -> Dict[str, int]:
    """How many rows overlap a mission run on their robot (the recording_runs rule, in Python)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    skew = datetime.timedelta(seconds=CLOCK_SKEW_S)
    runs = list(runs)
    hit = 0
    for row in rows:
        if row["start"] is None:
            continue
        if any(r["robot_name"] == row["robot_name"]
               and row["start"] <= (r["ended_at"] or now) + skew
               and row["end"] >= r["started_at"] - skew for r in runs):
            hit += 1
    return {"with_start": sum(1 for r in rows if r["start"] is not None), "overlap_mission_runs": hit}


# --- the real sources (read-only) ----------------------------------------------------------------------

class MinioSources:
    """Sidecars and bag objects of the `rosbags` bucket, through the minio client the services use."""

    def __init__(self, client: Any, bucket: str = BUCKET):
        self.client = client
        self.bucket = bucket
        self._objects = {o.object_name: o for o in client.list_objects(bucket, recursive=True)}

    def sidecars(self):
        for name in sorted(self._objects):
            if not name.endswith(".json"):
                continue
            resp = self.client.get_object(self.bucket, name)
            try:
                data = json.loads(resp.read())
            finally:
                resp.close()
                resp.release_conn()
            if isinstance(data, dict) and data.get("bag_id") and data.get("robot_name"):
                yield data

    def stat(self, robot: str, bag_id: str) -> Optional[Dict[str, Any]]:
        o = self._objects.get(f"{robot}/{bag_id}")
        if o is None:
            return None
        return {"key": o.object_name, "size": o.size, "etag": o.etag, "last_modified": o.last_modified}

    def read_range(self, key: str) -> ReadRange:
        def read(offset: int, length: int) -> bytes:
            resp = self.client.get_object(self.bucket, key, offset=offset, length=length)
            try:
                return resp.read()
            finally:
                resp.close()
                resp.release_conn()
        return read
