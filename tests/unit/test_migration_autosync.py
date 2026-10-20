"""Automatic sync: what it selects, the cycle through the ordinary transfer, and the status it leaves."""
import asyncio

import httpx
import pytest

from packages.migration.autosync import AutoSync, clean_settings, missing_bags, missing_missions
from packages.migration.jobs import TransferError, TransferManager


class MemStore:
    def __init__(self, settings=None):
        self.data = {"settings": settings or {}, "status": {}}

    async def load(self):
        return {"settings": dict(self.data["settings"]), "status": dict(self.data["status"])}

    async def save(self, settings=None, status=None):
        if settings is not None:
            self.data["settings"] = settings
        if status is not None:
            self.data["status"] = status


def row(mission, runs, last):
    return {"mission": mission, "runs": runs, "last": last}


def test_missions_the_destination_lacks_or_holds_fewer_runs_of_are_selected_oldest_first():
    local = [row("a", 3, "2026-10-03T00:00:00Z"), row("b", 2, "2026-10-01T00:00:00Z"), row("c", 1, "2026-10-05T00:00:00Z"), row("d", 2, "2026-10-09T00:00:00Z")]
    remote = [row("a", 3, "2026-10-03T00:00:00Z"), row("b", 1, "2026-09-30T00:00:00Z"), row("d", 2, "2026-10-08T00:00:00Z")]
    assert missing_missions(local, remote) == ["b", "c", "d"]          # a is complete; b has fewer; c is absent; d's newest run is newer


def test_only_recordings_the_destination_does_not_hold_are_selected():
    local = [{"robot": "r", "bag_id": "1"}, {"robot": "r", "bag_id": "2"}]
    assert missing_bags(local, [{"robot": "r", "bag_id": "1"}]) == [{"robot": "r", "bag_id": "2"}]


def test_settings_are_validated():
    assert clean_settings({"enabled": True, "destination_url": "http://100.85.3.47/", "interval_min": "30"})["destination_url"] == "http://100.85.3.47"
    for bad in ({"enabled": True}, {"interval_min": 1}, {"interval_min": 5000}, {"destination_url": "ftp://x"}, {"interval_min": "soon"}):
        with pytest.raises(TransferError):
            clean_settings(bad)


def make(tmp_path, handler, settings=None, local_runs=None, local_bags=None):
    mgr = TransferManager(None, None, None, str(tmp_path / "spool"), {"name": "pi"})
    mgr.http_transport = httpx.MockTransport(handler)

    async def identity():
        return {"instance_id": "pi-1"}
    mgr.identity = identity

    async def runs():
        return local_runs or []

    async def bags():
        return local_bags or []

    async def nosleep(_):
        return None
    return AutoSync(mgr, MemStore(settings or {"enabled": True, "destination_url": "http://cloud", "interval_min": 15, "history": True, "rosbags": True}),
                    runs, bags, sleep=nosleep), mgr


def destination(listing, steps, seen):
    """A fake destination: its listing, then the import job walks through `steps` (states) as it is polled."""
    states = iter(steps)

    def handler(request):
        path = request.url.path
        seen.append((request.method, path))
        if request.method == "GET" and path == "/api/v1/transfer/maps":
            return httpx.Response(200, json=listing)
        if path == "/api/v1/transfer/imports/pull":
            import json
            seen.append(("BODY", json.loads(request.content)))
            return httpx.Response(202, json={"id": "j1", "state": "fetching"})
        if request.method == "GET" and path == "/api/v1/transfer/imports/j1":
            state = next(states)
            body = {"id": "j1", "state": state, "plan": {"ok": True, "errors": []}, "error": None}
            if state == "done":
                body["report"] = {"ok": True, "runs": [{"runs_inserted": 4}], "rosbags": [{"copied": True}]}
            return httpx.Response(200, json=body)
        if request.method == "POST" and path.endswith("/apply"):
            return httpx.Response(202, json={"id": "j1", "state": "applying"})
        if request.method == "DELETE":
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404)
    return handler


def run_cycle(sync):
    return asyncio.new_event_loop().run_until_complete(sync.cycle({**{"history": True, "rosbags": True}, "destination_url": "http://cloud", "interval_min": 15}))


def test_a_cycle_sends_what_is_missing_applies_it_and_records_the_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr("packages.migration.jobs.own_urls", lambda port, host: ["http://100.116.20.49"])
    seen = []
    handler = destination({"runs": [row("b", 1, "2026-09-30T00:00:00Z")], "rosbags": [], "server": {"instance_id": "cloud-1"}}, ["planned", "applying", "done"], seen)
    sync, _ = make(tmp_path, handler, local_runs=[row("a", 2, "2026-10-01T00:00:00Z"), row("b", 1, "2026-09-30T00:00:00Z")],
                   local_bags=[{"robot": "r", "bag_id": "1"}])
    status = run_cycle(sync)
    assert status["ok"] and status["sent"] == {"missions": 1, "recordings": 1}
    body = [b for k, b in seen if k == "BODY"][0]
    assert body["runs"] == ["a"] and body["history"] is True and body["rosbags"] == [{"robot": "r", "bag_id": "1"}] and body["delta"] is True
    assert ("POST", "/api/v1/transfer/imports/j1/apply") in seen
    assert status["counts"] == {"runs_added": 4, "recordings_copied": 1}
    assert sync.store.data["status"]["ok"] and not sync.running


def test_nothing_missing_means_nothing_is_started(tmp_path):
    seen = []
    handler = destination({"runs": [row("a", 2, "2026-10-01T00:00:00Z")], "rosbags": [], "server": {}}, [], seen)
    sync, _ = make(tmp_path, handler, local_runs=[row("a", 2, "2026-10-01T00:00:00Z")])
    status = run_cycle(sync)
    assert status["ok"] and status["note"] == "Up to date"
    assert not any(p == "/api/v1/transfer/imports/pull" for _, p in seen if isinstance(p, str))


def test_an_unreachable_destination_is_recorded_not_raised_and_history_can_be_left_out(tmp_path):
    def down(request):
        raise httpx.ConnectError("no route")
    sync, _ = make(tmp_path, down, local_runs=[row("a", 1, "2026-10-01T00:00:00Z")])
    status = run_cycle(sync)
    assert not status["ok"] and "cannot reach" in status["error"] and status["finished_at"]


def test_a_refused_plan_is_an_error_and_nothing_is_applied(tmp_path, monkeypatch):
    monkeypatch.setattr("packages.migration.jobs.own_urls", lambda port, host: ["http://100.116.20.49"])
    seen = []
    base = destination({"runs": [], "rosbags": [], "server": {}}, ["planned"], seen)

    def handler(request):
        if request.method == "GET" and request.url.path == "/api/v1/transfer/imports/j1":
            return httpx.Response(200, json={"id": "j1", "state": "planned", "plan": {"ok": False, "errors": ["not enough disk"]}})
        return base(request)
    sync, _ = make(tmp_path, handler, local_runs=[row("a", 1, "2026-10-01T00:00:00Z")])
    status = run_cycle(sync)
    assert not status["ok"] and "not enough disk" in status["error"]
    assert not any(isinstance(p, str) and p.endswith("/apply") for _, p in seen)


def test_the_loop_only_runs_a_cycle_when_it_is_on_and_due(tmp_path):
    sync, _ = make(tmp_path, lambda r: httpx.Response(404))
    on = {"enabled": True, "interval_min": 15}
    assert sync._due(on, {}, False)
    assert not sync._due(on, {"finished_at": __import__("time").time(), "ok": True}, False)
    assert sync._due(on, {"finished_at": __import__("time").time(), "ok": True}, True)         # run now
    assert not sync._due({"enabled": False, "interval_min": 15}, {}, True)
