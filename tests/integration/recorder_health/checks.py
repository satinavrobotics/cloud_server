"""Steps of the recorder health integration test (run.sh drives them, each in a throwaway
container on the test's private network).

    checks.py schema [head|down]  recorder_health exists (head) or is gone (down)
    checks.py init                object tables (the API's read path needs them)
    checks.py scenario            dispatch's real fleet_recorder.write_health() on a real
                                  pool -> recorder_health -> the API's real ApiTelemetry
                                  (election, writer term, RecorderHealthMonitor on the lock
                                  connection) -> GET /api/v1/health/recording (FastAPI app on an
                                  ASGI transport, no lifespan):
                                  healthy snapshot; dispatch stops reporting -> report_stale
                                  RAISED once, endpoint alerting; reports again -> CLEARED once;
                                  the sweep stalls -> heartbeat_sweep_lag RAISED / CLEARED;
                                  dispatch spill pending > threshold -> spill_pending RAISED;
                                  API restart while active -> restored, not raised again; a
                                  non-writer worker sees the stored alerts; spill drained ->
                                  CLEARED; exactly one event per transition, robot_name NULL.

Environment: PGHOST/PGPASSWORD/PGDATABASE (postgres user), WORK (a writable dir).
"""
import asyncio
import datetime
import os
import sys
import time
from unittest.mock import patch

import httpx

from tests.integration.recording_policy.checks import check, conninfo, database, query

HEAD = "20260926_02_recorder_health"
PREVIOUS = "20260926_01_run_archive"
UTC = datetime.timezone.utc

# Fast thresholds so the scenario takes seconds, not minutes.
STALE_S = 3.0
SPILL_S = 30.0
EVAL_S = 0.2


def schema(which):
    exists = query("SELECT to_regclass('recorder_health') IS NOT NULL")[0][0]
    version = query("SELECT version_num FROM alembic_version")[0][0]
    if which == "head":
        check(exists, "recorder_health exists")
        cols = [r[0] for r in query(
            "SELECT column_name FROM information_schema.columns WHERE table_name = "
            "'recorder_health' ORDER BY ordinal_position")]
        check(cols == ["process", "pid", "hostname", "role", "started_at", "reported_at",
                       "report", "alerts"], f"columns {cols}")
        check(version == HEAD, f"alembic_version at {HEAD}")
    else:
        check(not exists, "downgrade dropped recorder_health")
        check(version == PREVIOUS, f"alembic_version at {PREVIOUS}")


async def init():
    db = database()
    await db.async_init()
    print("init done")


def alert_events():
    return query("SELECT code, robot_name, payload->>'alert', payload->>'process', source, "
                 "severity FROM fleet_events WHERE code LIKE 'SYSTEM.RECORDER_ALERT_%' "
                 "ORDER BY ts, code")


async def wait_for(predicate, what, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


async def scenario():
    import packages.api.main as main
    from packages.api import recorder_health as rh
    from packages.api.telemetry import ApiTelemetry
    from packages.controllers.mission import fleet_recorder as fr
    from packages.telemetry_ingest import create_pool

    th = rh.Thresholds(stale_s=STALE_S, spill_s=SPILL_S, raise_s=0.0, clear_s=0.5)
    db = database()
    await db.async_init()

    # --- dispatch side: the real recorder code on a real pool --------------------------------
    work = os.environ["WORK"]
    recorder = fr.FleetRecorder(conninfo(), spill_path=os.path.join(work, "dispatch.jsonl"))
    recorder._pool = await create_pool(conninfo(), name="it_dispatch")
    recorder._started_at = datetime.datetime.now(UTC)
    recorder.sweep_completed()
    check(await recorder.write_health(), "dispatch wrote its recorder_health row")
    row = query("SELECT role, report->'heartbeat_sweep'->>'period_s', "
                "report->'queue'->>'capacity' FROM recorder_health WHERE process='dispatch'")
    check(row == [("recorder", "1.0", "10000")], f"dispatch row {row}")

    stop_dispatch = asyncio.Event()
    stop_sweep = asyncio.Event()

    async def dispatch_reports():
        while not stop_dispatch.is_set():
            await recorder.write_health()
            await asyncio.sleep(0.3)

    async def sweeps():
        # stands in for fleet_recorder._sweep_loop (its sleep shortened; period_s stays 1 s)
        while True:
            if not stop_sweep.is_set():
                recorder.sweep_completed()
            await asyncio.sleep(0.2)

    loop = asyncio.get_running_loop()
    sweeper = loop.create_task(sweeps())
    reporter = loop.create_task(dispatch_reports())

    # --- API side: the real election + writer + monitor --------------------------------------
    def make_api():
        return ApiTelemetry(conninfo(), os.path.join(work, "api-spill"), retry_s=0.2,
                            check_s=0.1,
                            health_monitor=rh.RecorderHealthMonitor(th, eval_s=EVAL_S))

    api = make_api()
    api.start()
    await wait_for(lambda: api.is_writer, "the API to become the telemetry writer")

    async def get(telemetry):
        svc = type("Svc", (), {"database": db, "telemetry": telemetry})()
        with patch.object(main, "service", svc), \
                patch.object(rh.Thresholds, "from_config", classmethod(lambda cls: th)):
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
                resp = await http.get("/api/v1/health/recording")
        check(resp.status_code == 200, "GET /api/v1/health/recording -> 200")
        return resp.json()

    await wait_for(lambda: query("SELECT 1 FROM recorder_health WHERE process='api'"),
                   "the api row")
    body = await get(api)
    check(body["status"] == "ok" and body["alerts"] == [], f"healthy: {body['alerts']}")
    check(body["processes"]["api"]["role"] == "writer"
          and body["processes"]["api"]["election"]["role"] == "writer", "api role writer")
    d = body["processes"]["dispatch"]
    check(d["present"] and not d["stale"] and d["report_age_s"] < STALE_S,
          f"dispatch fresh ({d['report_age_s']} s)")
    check(d["heartbeat_sweep"]["lag_threshold_s"] == 3.0, "sweep lag threshold 3 x 1 s")
    check(d["queue"]["capacity"] == 10000 and d["spill"]["pending"] == 0, "dispatch numbers")
    check(alert_events() == [], "no alert events yet")

    # --- dispatch goes silent -> report_stale -------------------------------------------------
    stop_dispatch.set()
    await reporter
    await wait_for(lambda: alert_events(), "report_stale RAISED", timeout=STALE_S + 10)
    await asyncio.sleep(1.0)
    events = alert_events()
    check(events == [("SYSTEM.RECORDER_ALERT_RAISED", None, "report_stale", "dispatch", "api",
                      "warning")], f"one RAISED report_stale event: {events}")
    body = await get(api)
    check(body["status"] == "alerting" and [(a["alert"], a["process"], a["source"])
                                            for a in body["alerts"]]
          == [("report_stale", "dispatch", "evaluator")], f"endpoint alerting: {body['alerts']}")
    stored = query("SELECT alerts FROM recorder_health WHERE process='api'")[0][0]
    check([a["alert"] for a in stored] == ["report_stale"], "api row stores the active alert")

    # --- dispatch reports again -> CLEARED once -----------------------------------------------
    stop_dispatch.clear()
    reporter = asyncio.get_running_loop().create_task(dispatch_reports())
    await wait_for(lambda: len(alert_events()) == 2, "report_stale CLEARED")
    await asyncio.sleep(1.0)
    check([e[0] for e in alert_events()] == ["SYSTEM.RECORDER_ALERT_RAISED",
                                             "SYSTEM.RECORDER_ALERT_CLEARED"],
          "exactly one CLEARED")
    payload = query("SELECT payload FROM fleet_events WHERE code = "
                    "'SYSTEM.RECORDER_ALERT_CLEARED'")[0][0]
    check(payload["raised_at"] and payload["duration_s"] > 0, f"CLEARED payload {payload}")
    check((await get(api))["status"] == "ok", "endpoint ok again")

    # --- the heartbeat sweep stalls (dispatch still reports) -> heartbeat_sweep_lag -----------
    stop_sweep.set()
    await wait_for(lambda: len(alert_events()) == 3, "heartbeat_sweep_lag RAISED")
    check(alert_events()[-1][:4] == ("SYSTEM.RECORDER_ALERT_RAISED", None,
                                     "heartbeat_sweep_lag", "dispatch"),
          "heartbeat_sweep_lag RAISED for dispatch")
    body = await get(api)
    sweep = body["processes"]["dispatch"]["heartbeat_sweep"]
    check(sweep["lag_s"] > 3.0 and ("heartbeat_sweep_lag", "dispatch") in
          [(a["alert"], a["process"]) for a in body["alerts"]], f"endpoint shows lag {sweep}")
    stop_sweep.clear()
    await wait_for(lambda: len(alert_events()) == 4, "heartbeat_sweep_lag CLEARED")
    check(alert_events()[-1][:3] == ("SYSTEM.RECORDER_ALERT_CLEARED", None,
                                     "heartbeat_sweep_lag"), "heartbeat_sweep_lag CLEARED")
    query("DELETE FROM fleet_events WHERE code LIKE 'SYSTEM.RECORDER_ALERT_%' AND "
          "payload->>'alert' = 'heartbeat_sweep_lag'")   # keep the counts below simple

    # --- dispatch spill pending too long -> spill_pending -------------------------------------
    from packages.events.codes import EventCode
    from packages.events.emit import Event, build_row
    spill = recorder.queue.spill
    spill.append([build_row(Event(EventCode.ROBOT_ONLINE, datetime.datetime.now(UTC),
                                  robot_name="it_bot",
                                  payload={"connection_state": "ONLINE"}))])
    spill.pending_since = time.time() - (SPILL_S + 5)
    await wait_for(lambda: len(alert_events()) == 3, "spill_pending RAISED")
    check(alert_events()[-1][:4] == ("SYSTEM.RECORDER_ALERT_RAISED", None, "spill_pending",
                                     "dispatch"), "spill_pending RAISED for dispatch")
    body = await get(api)
    check(body["processes"]["dispatch"]["spill"]["pending"] == 1
          and body["processes"]["dispatch"]["spill"]["pending_age_s"] > SPILL_S,
          "endpoint shows the pending spill and its age")

    # --- API restart while active: restored, not raised again ---------------------------------
    await api.stop()
    api = make_api()
    api.start()
    await wait_for(lambda: api.is_writer, "the new API process to become the writer")
    await asyncio.sleep(1.5)
    check(len(alert_events()) == 3, "no second RAISED after the API restart")
    check([a["alert"] for a in api.health.active_alerts()] == ["spill_pending"],
          "the new writer restored the active alert")

    # --- a non-writer worker answers from the stored row --------------------------------------
    body = await get(None)
    check([(a["alert"], a["source"]) for a in body["alerts"]] == [("spill_pending", "stored")],
          f"non-writer sees the stored alert: {body['alerts']}")
    check(body["served_by"]["is_writer"] is False, "served by a non-writer")

    # --- spill drained -> CLEARED -------------------------------------------------------------
    spill.consume(spill.pending_lines)
    await wait_for(lambda: len(alert_events()) == 4, "spill_pending CLEARED")
    await asyncio.sleep(1.0)
    codes = [(e[0], e[2]) for e in alert_events()]
    check(codes == [("SYSTEM.RECORDER_ALERT_RAISED", "report_stale"),
                    ("SYSTEM.RECORDER_ALERT_CLEARED", "report_stale"),
                    ("SYSTEM.RECORDER_ALERT_RAISED", "spill_pending"),
                    ("SYSTEM.RECORDER_ALERT_CLEARED", "spill_pending")],
          f"one event per transition: {codes}")
    body = await get(api)
    check(body["status"] == "ok", "healthy at the end")
    dup = query("SELECT count(*) - count(DISTINCT event_id) FROM fleet_events")[0][0]
    check(dup == 0, "no duplicate event ids")

    stop_dispatch.set()
    await reporter
    sweeper.cancel()
    await api.stop()
    await recorder._pool.close()
    print("scenario done")


if __name__ == "__main__":
    step = sys.argv[1]
    if step == "schema":
        schema(sys.argv[2])
    elif step == "init":
        asyncio.run(init())
    elif step == "scenario":
        asyncio.run(scenario())
    else:
        raise SystemExit(f"unknown step {step}")
