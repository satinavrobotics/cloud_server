"""Bag analysis API routes (packages/api/bag_analysis.py): the real FastAPI app over ASGI, an in-memory store
(tests/unit/bag_analysis/fakes.FakeStore plus the read methods the API needs) and a fake object reader.
The SQL itself is covered by tests/integration/test_bag_analysis_postgres.py."""
import datetime
import os
from types import SimpleNamespace
from unittest.mock import patch

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx  # noqa: E402
import psycopg  # noqa: E402
import pytest  # noqa: E402

import packages.api.main as main  # noqa: E402
from packages.api import bag_analysis as ba  # noqa: E402
from tests.unit.bag_analysis.fakes import FakeStore, recording  # noqa: E402

pytestmark = pytest.mark.unit

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
REC = recording(1)
RID = REC["id"]
BUCKET = "analysis"
KEY = "odin1/bag1/lost_time/default/v1_x1/ledger.json"


def _stamp(j):
    """FakeStore keeps float clocks; the API gets datetimes from Postgres."""
    return dict(j, created_at=T0, started_at=T0 if j.get("started_at") else None,
                finished_at=T0 if j.get("finished_at") else None)


def result(analyzer="lost_time", av=1, ev=1, profile="default", status="warn", rid=RID, **over):
    row = {"recording_id": rid, "analyzer": analyzer, "analyzer_version": av, "profile": profile,
           "extractor_version": ev, "status": status, "metrics": {"lost_s": 590.8, "goals_n": 24, "bad": None},
           "tables": {"by_state": {"columns": ["state", "s"], "rows": [["<b>x</b>", 1.5]]}},
           "notes": [{"code": "o2.fallback", "level": "warn", "text": "<script>alert(1)</script>"}],
           "producer": {"tool_git_sha": "abc"}, "inputs": {"present": {"params": False}},
           "artifacts": [{"name": "ledger", "path": "ledger.json", "media_type": "application/json", "size": 7,
                          "sha256": "a" * 64, "bucket": BUCKET, "key": KEY},
                         {"name": "page", "path": "p.html", "media_type": "text/html", "size": 3,
                          "bucket": BUCKET, "key": "odin1/bag1/lost_time/default/v1_x1/p.html"},
                         {"name": "nokey", "path": "n.md", "media_type": "text/markdown"}],
           "envelope": {}, "created_at": T0}
    row.update(over)
    return row


class ApiStore(FakeStore):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fail_with = None

    def _maybe_fail(self):
        if self.fail_with is not None:
            raise self.fail_with

    def get_recording(self, rid):
        self._maybe_fail()
        return super().get_recording(rid)

    def jobs_for_recording(self, rid, limit=20):
        rows = [_stamp(j) for j in self.jobs.values() if j["recording_id"] == rid]
        return list(reversed(rows))[:limit]

    def active_job(self, rid, analyzer_ids, trigger):
        from packages.services.bag_analysis.store import scope_of
        return next((_stamp(j) for j in self.jobs.values() if j["recording_id"] == rid
                     and j["trigger"] == trigger and j["scope"] == scope_of(analyzer_ids)
                     and j["status"] in ("queued", "running")), None)

    def catalog_summary(self):
        out = {}
        for r in self.results.values():
            k = (r["analyzer"], r["profile"])
            o = out.setdefault(k, {"analyzer": k[0], "profile": k[1], "analyzer_version": 0, "extractor_version": 0,
                                   "analyzer_versions": set(), "recordings": set(), "last_result_at": T0, "metrics": set()})
            o["analyzer_version"] = max(o["analyzer_version"], r["analyzer_version"])
            o["extractor_version"] = max(o["extractor_version"], r["extractor_version"])
            o["analyzer_versions"].add(r["analyzer_version"])
            o["recordings"].add(r["recording_id"])
            o["metrics"].update(r["metrics"])
        return [dict(o, analyzer_versions=sorted(o["analyzer_versions"]), recordings=len(o["recordings"]),
                     metrics=sorted(o["metrics"])) for o in out.values()]

    def recordings_page(self, limit, robot=None, after=None):
        recs = sorted((r for r in self.recordings.values() if r["part"] == "nav"),
                      key=lambda r: r["id"], reverse=True)
        if robot:
            recs = [r for r in recs if r["robot_name"] == robot]
        if after:
            recs = [r for r in recs if r["id"] < after[1]]
        out = []
        for r in recs[:limit]:
            jobs = [j for j in self.jobs.values() if j["recording_id"] == r["id"]]
            out.append(dict(r, created_at=T0, latest=self.latest_results(r["id"]),
                            last_job=_stamp(jobs[-1]) if jobs else None))
        return out


class Objects:
    def __init__(self, data=None, error=None):
        self.data, self.error, self.opened = data or {}, error, []

    def open(self, bucket, key):
        self.opened.append((bucket, key))
        if self.error:
            raise self.error
        if (bucket, key) not in self.data:
            raise FileNotFoundError(key)
        blob = self.data[(bucket, key)]
        return iter([blob[:3], blob[3:]]), len(blob)


def make(recs=(REC,), results=(), objects=None):
    store = ApiStore(list(recs))
    for r in results:
        store.results[tuple(r[c] for c in ("recording_id", "analyzer", "analyzer_version", "profile",
                                           "extractor_version"))] = r
    return store, ba.AnalysisApi(store, objects or Objects({(BUCKET, KEY): b'{"a":1}'}))


async def call(api, method, url, **kw):
    svc = SimpleNamespace(bag_analysis=api, database=None)
    with patch.object(main, "service", svc):
        transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            return await http.request(method, url, **kw)


BASE = f"/api/v1/recordings/{RID}"


# --- catalog ----------------------------------------------------------------------------------------------

async def test_catalog_is_derived_from_stored_results():
    _, api = make(results=[result(), result(av=2), result(analyzer="other", profile="fast", metrics={"x": 1})])
    r = await call(api, "GET", "/api/v1/analyses/catalog")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "derived" and body["declared"] is False
    lt = next(a for a in body["analyzers"] if a["analyzer"] == "lost_time")
    assert lt["analyzer_version"] == 2 and lt["analyzer_versions"] == [1, 2] and lt["recordings"] == 1
    assert [m["name"] for m in lt["metrics"]] == ["bad", "goals_n", "lost_s"]
    assert lt["metrics"][0]["unit"] is None and lt["metrics"][0]["direction"] is None
    assert len(body["analyzers"]) == 2


async def test_empty_catalog():
    _, api = make()
    assert (await call(api, "GET", "/api/v1/analyses/catalog")).json()["analyzers"] == []


# --- results ----------------------------------------------------------------------------------------------

async def test_results_latest_and_history():
    _, api = make(results=[result(av=1), result(av=2, status="ok"), result(profile="legacy")])
    latest = (await call(api, "GET", f"{BASE}/analyses")).json()
    assert latest["history"] is False and len(latest["results"]) == 2
    lt = next(x for x in latest["results"] if x["profile"] == "default")
    assert lt["analyzer_version"] == 2 and lt["status"] == "ok" and lt["latest"] is True
    hist = (await call(api, "GET", f"{BASE}/analyses?history=1")).json()
    assert hist["history"] is True and len(hist["results"]) == 3
    flags = {(x["profile"], x["analyzer_version"]): x["latest"] for x in hist["results"]}
    assert flags == {("default", 1): False, ("default", 2): True, ("legacy", 1): True}


async def test_result_fields_text_is_data_and_artifact_urls():
    _, api = make(results=[result()])
    res = (await call(api, "GET", f"{BASE}/analyses")).json()["results"][0]
    assert res["metrics"] == {"lost_s": 590.8, "goals_n": 24.0, "bad": None}
    assert res["notes"][0]["text"] == "<script>alert(1)</script>"      # returned verbatim, as JSON data
    assert res["tables"]["by_state"]["rows"][0][0] == "<b>x</b>"
    assert "envelope" not in res and res["created_at"].startswith("2026-10-28T12:00:00")
    urls = {a["path"]: a["download_url"] for a in res["artifacts"]}
    assert urls["ledger.json"] == f"{BASE}/analyses/lost_time/default/1/1/artifacts/ledger.json"
    assert urls["n.md"] is None


async def test_results_unknown_and_malformed_recording():
    _, api = make()
    assert (await call(api, "GET", "/api/v1/recordings/00000000-0000-0000-0000-0000000000ff/analyses")).status_code == 404
    assert (await call(api, "GET", "/api/v1/recordings/not-a-uuid/analyses")).status_code == 422


async def test_database_errors_are_503_without_internals():
    store, api = make()
    store.fail_with = psycopg.errors.UndefinedTable("relation analysis_results does not exist")
    r = await call(api, "GET", f"{BASE}/analyses")
    assert r.status_code == 503 and "migration" in r.json()["detail"]
    store.fail_with = psycopg.OperationalError("connection to 10.0.0.5 refused password=hunter2")
    r = await call(api, "GET", f"{BASE}/analyses")
    assert r.status_code == 503 and "hunter2" not in r.text and "10.0.0.5" not in r.text


# --- artifacts --------------------------------------------------------------------------------------------

ART = f"{BASE}/analyses/lost_time/default/1/1/artifacts"


async def test_artifact_streams_with_safe_headers():
    objects = Objects({(BUCKET, KEY): b'{"a":1}'})
    _, api = make(results=[result()], objects=objects)
    r = await call(api, "GET", f"{ART}/ledger.json")
    assert r.status_code == 200 and r.content == b'{"a":1}'
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["content-disposition"] == 'attachment; filename="ledger.json"'
    assert r.headers["x-content-type-options"] == "nosniff" and "sandbox" in r.headers["content-security-policy"]
    assert objects.opened == [(BUCKET, KEY)]


async def test_html_artifact_is_served_as_octet_stream():
    key = "odin1/bag1/lost_time/default/v1_x1/p.html"
    _, api = make(results=[result()], objects=Objects({(BUCKET, key): b"<html>"}))
    r = await call(api, "GET", f"{ART}/p.html")
    assert r.status_code == 200 and r.headers["content-type"] == "application/octet-stream"


@pytest.mark.parametrize("path", ["nope.json", "../ledger.json", "%2e%2e/x", "a//b",
                                  "n.md", "odin1/bag1/lost_time/default/v1_x1/ledger.json"])
async def test_artifact_path_only_from_the_stored_list(path):
    objects = Objects({(BUCKET, KEY): b"x"})
    _, api = make(results=[result()], objects=objects)
    r = await call(api, "GET", f"{ART}/{path}")
    assert r.status_code == 404 and objects.opened == []


async def test_artifact_wrong_result_key_or_bucket_is_404():
    other = result(artifacts=[{"name": "a", "path": "a.json", "media_type": "application/json",
                               "bucket": "rosbags", "key": "odin1/bag1"}])
    objects = Objects({("rosbags", "odin1/bag1"): b"x"})
    _, api = make(results=[other], objects=objects)
    assert (await call(api, "GET", f"{ART}/a.json")).status_code == 404       # bucket is not the analysis bucket
    assert (await call(api, "GET", f"{BASE}/analyses/lost_time/default/9/1/artifacts/a.json")).status_code == 404
    assert objects.opened == []


async def test_artifact_object_missing_and_store_down():
    _, api = make(results=[result()], objects=Objects({}))
    assert (await call(api, "GET", f"{ART}/ledger.json")).status_code == 404
    _, api = make(results=[result()], objects=Objects(error=RuntimeError("minio at 10.1.1.1 secret")))
    r = await call(api, "GET", f"{ART}/ledger.json")
    assert r.status_code == 503 and "10.1.1.1" not in r.text


# --- request ----------------------------------------------------------------------------------------------

async def test_request_enqueues_then_is_idempotent():
    store, api = make()
    r = await call(api, "POST", f"{BASE}/analyses", headers={"X-Sati-Operator": "alice"})
    assert r.status_code == 202
    body = r.json()
    assert body["created"] is True and body["job"]["status"] == "queued" and body["job"]["trigger"] == "request"
    assert body["job"]["requested_by"] == "alice" and body["job"]["analyzer_ids"] is None
    again = await call(api, "POST", f"{BASE}/analyses", json={})
    assert again.status_code == 200 and again.json()["created"] is False
    assert again.json()["job"]["id"] == body["job"]["id"] and len(store.jobs) == 1


async def test_request_with_known_analyzers_and_validation():
    store, api = make(results=[result(rid="x"), ])
    r = await call(api, "POST", f"{BASE}/analyses", json={"analyzers": ["lost_time:default", "lost_time"]})
    assert r.status_code == 202 and r.json()["job"]["analyzer_ids"] == ["lost_time", "lost_time:default"]
    for bad in ({"analyzers": ["ghost"]}, {"analyzers": ["lost_time:nope"]}, {"analyzers": ["Bad Id"]},
                {"analyzers": ["a'; drop table x"]}, {"analyzers": [f"lost_time{i}" for i in range(21)]},
                {"force": True}, {"analyzers": "lost_time"}):
        r = await call(api, "POST", f"{BASE}/analyses", json=bad)
        assert r.status_code == 422, bad
    assert len(store.jobs) == 1


async def test_request_rejects_unknown_unanalysable_and_objectless():
    recs = (REC, recording(2, part="raw"), recording(3, object_present=False), recording(4, kind="incident"))
    store, api = make(recs)
    assert (await call(api, "POST", "/api/v1/recordings/00000000-0000-0000-0000-0000000000ff/analyses")).status_code == 404
    for n in (2, 3, 4):
        r = await call(api, "POST", f"/api/v1/recordings/{recording(n)['id']}/analyses")
        assert r.status_code == 409, n
    assert store.jobs == {}


async def test_request_trims_operator_label():
    store, api = make()
    await call(api, "POST", f"{BASE}/analyses", headers={"X-Sati-Operator": "x" * 500})
    assert len(next(iter(store.jobs.values()))["requested_by"]) == ba.MAX_OPERATOR_LEN


# --- jobs -------------------------------------------------------------------------------------------------

async def test_jobs_newest_first_and_log_only_on_request():
    store, api = make()
    j1 = store.enqueue(RID, None, "auto")
    store.claim_next()
    store.fail(j1, "boom <b>", "tail text")
    j2 = store.enqueue(RID, None, "request", "bob")
    r = (await call(api, "GET", f"{BASE}/analysis-jobs")).json()
    assert [j["id"] for j in r["items"]] == [j2, j1]
    assert r["items"][1]["error"] == "boom <b>" and r["items"][1]["log_tail"] is None
    r = (await call(api, "GET", f"{BASE}/analysis-jobs?log=1&limit=1")).json()
    assert len(r["items"]) == 1
    full = (await call(api, "GET", f"{BASE}/analysis-jobs?log=true")).json()
    assert full["items"][1]["log_tail"] == "tail text"
    assert (await call(api, "GET", f"{BASE}/analysis-jobs?limit=0")).status_code == 422
    assert (await call(api, "GET", f"{BASE}/analysis-jobs?limit=101")).status_code == 422


# --- list -------------------------------------------------------------------------------------------------

async def test_recordings_list_states_metrics_and_pagination():
    recs = [recording(n) for n in (1, 2, 3, 4)]
    store, api = make(recs, results=[result(rid=recs[0]["id"])])
    store.enqueue(recs[1]["id"], None, "request")                  # queued
    j = store.enqueue(recs[2]["id"], None, "auto")
    store.claim_next(), store.claim_next()                          # running (2 and 3 claimed)
    store.fail(j, "x")                                              # 3: failed
    page1 = (await call(api, "GET", "/api/v1/analyses/recordings?limit=2")).json()
    assert [i["bag_id"] for i in page1["items"]] == ["bag4", "bag3"] and page1["next_cursor"]
    assert page1["items"][1]["analysis_state"] == "failed" and page1["items"][1]["last_job"]["error"] == "x"
    assert page1["items"][0]["analysis_state"] == "none" and page1["items"][0]["last_job"] is None
    page2 = (await call(api, "GET", f"/api/v1/analyses/recordings?limit=2&cursor={page1['next_cursor']}")).json()
    assert [i["bag_id"] for i in page2["items"]] == ["bag2", "bag1"] and page2["next_cursor"] is None
    assert page2["items"][0]["analysis_state"] == "running"
    done = page2["items"][1]
    assert done["analysis_state"] == "analysed" and done["latest"][0]["status"] == "warn"
    assert "lost_s" in done["latest"][0]["metrics"]
    sel = (await call(api, "GET", "/api/v1/analyses/recordings?metrics=goals_n,missing")).json()
    assert sel["items"][3]["latest"][0]["metrics"] == {"goals_n": 24.0}


async def test_recordings_list_validation():
    _, api = make()
    assert (await call(api, "GET", "/api/v1/analyses/recordings?cursor=garbage")).status_code == 422
    assert (await call(api, "GET", "/api/v1/analyses/recordings?limit=201")).status_code == 422
    assert (await call(api, "GET", "/api/v1/analyses/recordings?robot=zz")).json()["items"] == []


async def test_service_not_initialised_is_503():
    with patch.object(main, "service", None):
        transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            assert (await http.get("/api/v1/analyses/catalog")).status_code == 503


def test_routes_registered_once():
    paths = [(m, r.path) for r in main.app.routes for m in getattr(r, "methods", ()) if "analys" in r.path]
    assert len(paths) == len(set(paths)) == 6


async def test_artifact_dotdot_is_refused_below_the_http_layer():
    objects = Objects({(BUCKET, KEY): b"x"})
    _, api = make(results=[result()], objects=objects)
    with pytest.raises(ba.HTTPException) as exc:
        await api.open_artifact(RID, "lost_time", "default", 1, 1, "ledger.json/../ledger.json")
    assert exc.value.status_code == 404 and objects.opened == []
