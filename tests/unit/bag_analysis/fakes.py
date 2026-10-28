"""In-memory stand-ins for the Postgres store and the object store (same interface as store.PgStore / worker.MinioObjects)."""
import io
import itertools
from pathlib import Path

from packages.services.bag_analysis.store import RESULT_COLUMNS, scope_of

_ids = itertools.count(1)


def recording(n=1, **over):
    r = {"id": f"00000000-0000-0000-0000-{n:012d}", "kind": "run", "robot_name": "r", "group_key": f"20261004_20000{n}_navstack_sati_odin",
         "part": "nav", "storage_bucket": "rosbags", "storage_key": f"odin1/bag{n}", "size_bytes": 1000, "map_id": None,
         "description": f"20261004_20000{n}_navstack_sati_odin", "bag_id": f"bag{n}", "object_present": True}
    r.update(over)
    return r


class FakeStore:
    def __init__(self, recordings=()):
        self.recordings = {r["id"]: r for r in recordings}
        self.jobs = {}
        self.results = {}
        self.now = 1_000_000.0

    # jobs
    def enqueue(self, recording_id, analyzer_ids, trigger, requested_by=None):
        scope = scope_of(analyzer_ids)
        if any(j["recording_id"] == recording_id and j["trigger"] == trigger and j["scope"] == scope
               and j["status"] in ("queued", "running") for j in self.jobs.values()):
            return None
        jid = f"job-{next(_ids)}"
        self.jobs[jid] = {"id": jid, "recording_id": recording_id, "analyzer_ids": sorted(set(analyzer_ids)) if analyzer_ids else None,
                          "scope": scope, "trigger": trigger, "requested_by": requested_by, "status": "queued",
                          "attempts": 0, "started_at": None, "finished_at": None, "error": None, "log_tail": None}
        return jid

    def claim_next(self):
        for j in self.jobs.values():
            if j["status"] == "queued":
                j.update(status="running", attempts=j["attempts"] + 1, started_at=self.now)
                return dict(j)
        return None

    def complete(self, job_id, log_tail=None):
        self.jobs[job_id].update(status="done", finished_at=self.now, log_tail=log_tail)

    def fail(self, job_id, error, log_tail=None):
        self.jobs[job_id].update(status="failed", finished_at=self.now, error=error, log_tail=log_tail)

    def release(self, job_id):
        j = self.jobs[job_id]
        j.update(status="queued", started_at=None, attempts=max(j["attempts"] - 1, 0), error=None)

    def requeue_stuck(self, older_than_s, max_attempts):
        requeued = failed = 0
        for j in self.jobs.values():
            if j["status"] == "running" and j["started_at"] < self.now - older_than_s:
                if j["attempts"] < max_attempts:
                    j.update(status="queued", started_at=None)
                    requeued += 1
                else:
                    j.update(status="failed", finished_at=self.now)
                    failed += 1
        return requeued, failed

    # declared catalog (same semantics as the SQL: key (analyzer, version, decl_hash))
    def upsert_catalog(self, entries):
        if getattr(self, "fail_catalog", False):
            raise OSError("database down")
        self.catalog_rows = getattr(self, "catalog_rows", {})
        fresh = 0
        for e in entries:
            key = (e["analyzer"], e["analyzer_version"], e["decl_hash"])
            old = self.catalog_rows.get(key)
            self.now += 1
            if old is None:
                self.catalog_rows[key] = dict(e, first_seen_at=self.now, last_seen_at=self.now)
                fresh += 1
            else:
                old.update(e, last_seen_at=self.now)
        return fresh, len(entries) - fresh

    def declarations(self):
        best = {}
        for (a, v, _h), r in getattr(self, "catalog_rows", {}).items():
            if (a, v) not in best or r["last_seen_at"] >= best[(a, v)]["last_seen_at"]:
                best[(a, v)] = r
        return [best[k] for k in sorted(best, key=lambda k: (k[0], -k[1]))]

    # results
    @staticmethod
    def _key(row):
        return tuple(row[c] for c in RESULT_COLUMNS[:5])

    def insert_result(self, row):
        key = self._key(row)
        old = self.results.get(key)
        if old is not None and old["status"] != "error":
            return False
        self.results[key] = dict(row)
        return True

    def results_for_recording(self, recording_id):
        return [r for r in self.results.values() if r["recording_id"] == recording_id]

    def latest_results(self, recording_id=None):
        best = {}
        for r in self.results.values():
            if recording_id and r["recording_id"] != recording_id:
                continue
            k = (r["recording_id"], r["analyzer"], r["profile"])
            if k not in best or (r["analyzer_version"], r["extractor_version"]) > (best[k]["analyzer_version"], best[k]["extractor_version"]):
                best[k] = r
        return list(best.values())

    # reads
    def get_recording(self, recording_id):
        return self.recordings.get(recording_id)

    def candidate_recordings(self):
        return [r for r in self.recordings.values() if r["kind"] == "run" and r["part"] == "nav"]

    def result_keys(self):
        return set(self.results)

    def busy_recordings(self):
        return {j["recording_id"] for j in self.jobs.values() if j["status"] in ("queued", "running")}

    def recently_failed_recordings(self, since_s):
        return {j["recording_id"] for j in self.jobs.values()
                if j["status"] == "failed" and j["finished_at"] > self.now - since_s}


class FakeObjects:
    def __init__(self, objects=None, fail_put=False):
        self.objects = dict(objects or {})
        self.puts = {}
        self.fail_put = fail_put
        self.opened = 0

    def stat_size(self, bucket, key):
        data = self.objects.get((bucket, key))
        return None if data is None else len(data)

    def open_stream(self, bucket, key):
        self.opened += 1
        stream = io.BytesIO(self.objects[(bucket, key)])
        stream.close = lambda: None
        return stream

    def ensure_bucket(self, bucket):
        pass

    def put_file(self, bucket, key, path, content_type):
        if self.fail_put:
            raise OSError("object store down")
        self.puts[(bucket, key)] = (Path(path).read_bytes(), content_type)
