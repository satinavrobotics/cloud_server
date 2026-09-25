"""Steps of the WP10 read-endpoints integration test (run.sh drives them, each in a throwaway
container on the test's private network).

    checks.py init        object tables and the robots
    checks.py scenario    level switches through the real routes (site, robot, assignment,
                          global) around a seeded run; seeded runs/events/time series/
                          trajectory; then the read routes over HTTP (the real FastAPI app on
                          an ASGI transport, no lifespan): pagination, filters, 404/422,
                          timeline (events, raw/rollup tracks, trajectory, not_recorded
                          intervals across the switches), effective level, READ ONLY +
                          statement_timeout

Environment: PGHOST/PGPASSWORD/PGDATABASE (postgres user), WORK (a writable dir).
"""
import asyncio
import datetime
import random
import sys
import types
import uuid
from urllib.parse import urlencode

import httpx

import cloud_common.objects as api_objects
from tests.integration.recording_policy.checks import check, conninfo, database, query

import psycopg

ROBOT, OTHER = "fr_bot", "fr_other"
CHANGED = "TELEMETRY.RECORDING_CHANGED"
UTC = datetime.timezone.utc


def now():
    return datetime.datetime.now(UTC)


def last_change_ts():
    return query("SELECT max(ts) FROM fleet_events WHERE code = %s", (CHANGED,))[0][0]


async def init():
    db = database()
    await db.async_init()
    for name in (ROBOT, OTHER):
        await db.create_object(api_objects.RobotObjectV1(name=name, status={}), uuid.uuid4())
    print("init done")


# --- seeding -----------------------------------------------------------------------------------

def insert_run(conn, run_id, robot, started, ended, state, level, site=None, sw="v1",
               mission="m"):
    conn.execute(
        "INSERT INTO mission_runs (run_id, mission_name, robot_name, site_id, map_id, "
        "sw_version, recording_level, state, passes_completed, mission_tree, started_at, "
        "ended_at) VALUES (%s, %s, %s, %s, 'map1', %s, %s, %s, 0, '[]', %s, %s)",
        (run_id, mission, robot, site, sw, level, state, started, ended))


MISSION_NAMES = [
    "x", "x-rerun-1", "x-rerun-1-rerun-2", "x-rerun-1727179200000", "x", "x-rerun-3",
    "x-rerun-abc", "xy-rerun-1", "xy", "x-rerun-", "x-rerun-1-", "x-Rerun-1", " x",
    "x.", "xa", "xa-rerun-1", "x.-rerun-3", "a+b (1)", "aab (1)", "a+b (1)-rerun-9",
    ".*", ".*-rerun-1", "anything", "x\\d", "x\\d-rerun-1", "x7", "Ünï 名", "Ünï 名-rerun-5",
    "100%_done", "100%_done-rerun-2", "1000_done", "x-rerun-1\n",
]
MISSION_CASES = {  # base -> the names above it must return (each as often as seeded)
    "x": ["x", "x", "x-rerun-1", "x-rerun-1-rerun-2", "x-rerun-1727179200000", "x-rerun-3"],
    "x-rerun-1": ["x-rerun-1", "x-rerun-1-rerun-2"],
    "x.": ["x.", "x.-rerun-3"],
    "a+b (1)": ["a+b (1)", "a+b (1)-rerun-9"],
    ".*": [".*", ".*-rerun-1"],
    "x\\d": ["x\\d", "x\\d-rerun-1"],
    "Ünï 名": ["Ünï 名", "Ünï 名-rerun-5"],
    "100%_done": ["100%_done", "100%_done-rerun-2"],
    "xy": ["xy", "xy-rerun-1"],
    "nope": [],
}


def insert_event(conn, ts, robot, code, severity="info", run=None, site=None, payload="{}"):
    eid = uuid.uuid4()
    conn.execute(
        "INSERT INTO fleet_events (ts, event_id, robot_name, run_id, site_id, code, severity, "
        "payload, source) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, 'dispatch')",
        (ts, eid, robot, run, site, code, severity, payload))
    return eid


def seed_lists(conn, base):
    """120 runs (two robots, all states, three sw versions, one site) and 300 events with
    repeated timestamps, all before `base`."""
    rng = random.Random(7)
    states = ["COMPLETED", "FAILED", "CANCELED", "ABORTED", "TIMEOUT"]
    for i in range(120):
        started = base - datetime.timedelta(hours=200 - i, seconds=i % 3)
        if i % 10 == 0:  # same started_at for several runs: keyset ties broken by run_id
            started = base - datetime.timedelta(hours=100)
        insert_run(conn, uuid.uuid4(), ROBOT if i % 2 else OTHER, started,
                   started + datetime.timedelta(minutes=5), states[i % 5], "events_only",
                   site="site-a" if i % 3 == 0 else None, sw=f"v{i % 3}", mission=f"m{i}")
    codes = [("NAV.GOAL_BLOCKED", "warning"), ("NAV.RECOVERY_ENTERED", "warning"),
             ("BATTERY.LOW", "warning"), ("ROBOT.ERROR_RAISED", "error"),
             ("ROBOT.ONLINE", "info")]
    for i in range(300):
        ts = base - datetime.timedelta(minutes=300 - (i // 3))  # 3 events per timestamp
        code, severity = codes[rng.randrange(len(codes))]
        insert_event(conn, ts, ROBOT if i % 2 else OTHER, code, severity,
                     site="site-a" if i % 4 == 0 else None)


# --- HTTP --------------------------------------------------------------------------------------

async def get(http, url, status=200):
    response = await http.get(url)
    check(response.status_code == status,
          f"GET {url} -> {status} (got {response.status_code}: {response.text[:200]})")
    return response.json()


async def all_pages(http, url, limit):
    items, cursor, pages = [], None, 0
    while True:
        sep = "&" if "?" in url else "?"
        page_url = f"{url}{sep}limit={limit}" + (f"&cursor={cursor}" if cursor else "")
        response = await http.get(page_url)
        assert response.status_code == 200, response.text
        body = response.json()
        items.extend(body["items"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            return items, pages
        assert len(body["items"]) == limit


def iso(ts):
    return ts.astimezone(UTC).isoformat()


def q(ts):
    return iso(ts).replace("+00:00", "Z")


async def scenario():
    import packages.api.main as main
    from packages.api import fleet_reads

    db = database()
    await db.async_init()
    main.service = types.SimpleNamespace(database=db)

    print("== A: level switches through the routes around a run", flush=True)
    await main.create_site({"name": "site-a", "telemetry_recording": "full"})
    await main.create_site({"name": "site-b"})
    await main.assign_robot_site(ROBOT, main.AssignRobotSiteRequest(site_id="site-a"))
    await asyncio.sleep(0.3)
    run_id = uuid.uuid4()
    start = now()
    with psycopg.connect(conninfo(), autocommit=True) as conn:
        insert_run(conn, run_id, ROBOT, start, None, "RUNNING", "full", site="site-a",
                   mission="timeline-mission")
    marks = {}
    steps = [
        ("T1 site-a off", lambda: main.update_site("site-a", {"telemetry_recording": "off"})),
        ("T2 robot events_only", lambda: main.update_robot(
            ROBOT, {"telemetry_recording": "events_only"})),
        ("T3 robot unset", lambda: main.update_robot(ROBOT, {"telemetry_recording": None})),
        ("T4 move to site-b", lambda: main.assign_robot_site(
            ROBOT, main.AssignRobotSiteRequest(site_id="site-b"))),
        ("T5 global full", lambda: main.update_settings({"telemetry_recording": "full"})),
        ("T6 global off", lambda: main.update_settings({"telemetry_recording": "off"})),
    ]
    for label, step in steps:
        await asyncio.sleep(0.4)
        before = last_change_ts()
        await step()
        marks[label] = last_change_ts()
        check(marks[label] is not None and marks[label] != before,
              f"{label}: RECORDING_CHANGED written")
    await asyncio.sleep(0.4)
    end = now()
    t = [start] + [marks[label] for label, _ in steps] + [end]

    print("== B: seed the run's rows", flush=True)
    with psycopg.connect(conninfo(), autocommit=True) as conn:
        conn.execute("UPDATE mission_runs SET state = 'COMPLETED', ended_at = %s "
                     "WHERE run_id = %s", (end, run_id))
        # 3000 robot_state rows over the window (forces downsampling), 50 diagnostics rows
        span = (end - start).total_seconds()
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO robot_state_ts (ts, robot_name, run_id, x, y, yaw, battery, "
                "state, driving) VALUES (%s, %s, %s, %s, %s, 0, 80, 'DRIVING', true)",
                [(start + datetime.timedelta(seconds=span * i / 3000), ROBOT, run_id,
                  float(i), float(-i)) for i in range(3000)])
            cur.executemany(
                "INSERT INTO diagnostics_ts (ts, robot_name, cpu, temp_max) "
                "VALUES (%s, %s, %s, 50)",
                [(start + datetime.timedelta(seconds=span * i / 50), ROBOT, float(i))
                 for i in range(50)])
            cur.executemany(
                "INSERT INTO mission_trajectory (mission_id, robot_name, node_id, seq, x, y, "
                "yaw, map_id, ts, run_id) VALUES ('timeline-mission', %s, %s, %s, %s, 0, 0, "
                "'map1', %s, %s)",
                [(ROBOT, f"n{i}", i, float(i),
                  start + datetime.timedelta(seconds=span * i / 10), run_id)
                 for i in range(10)])
        e_run = insert_event(conn, start, ROBOT, "MISSION.RUN_STARTED", run=run_id,
                             site="site-a")
        e_mid = insert_event(conn, t[2] + datetime.timedelta(milliseconds=50), ROBOT,
                             "NAV.GOAL_BLOCKED", "warning", run=run_id)
        e_untagged = insert_event(conn, t[1] + datetime.timedelta(milliseconds=50), ROBOT,
                                  "ROBOT.ERROR_RAISED", "error")
        insert_event(conn, t[3], OTHER, "ROBOT.ONLINE")          # another robot: not shown
        seed_lists(conn, start - datetime.timedelta(days=1))
        # `mission` filter: reruns (chained), lookalikes and regex metacharacters
        mbase = start - datetime.timedelta(days=5)
        for i, name in enumerate(MISSION_NAMES):
            insert_run(conn, uuid.uuid4(), OTHER, mbase + datetime.timedelta(minutes=i // 2),
                       mbase + datetime.timedelta(minutes=i // 2, seconds=30), "COMPLETED",
                       "events_only", mission=name)

        # an old run whose raw telemetry is gone (retention), only the 1-minute rollup left
        old_run = uuid.uuid4()
        old_start = start - datetime.timedelta(days=10)
        old_end = old_start + datetime.timedelta(minutes=30)
        insert_run(conn, old_run, ROBOT, old_start, old_end, "FAILED", "full")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO robot_state_ts (ts, robot_name, x, y, battery) "
                "VALUES (%s, %s, %s, 0, %s)",
                [(old_start + datetime.timedelta(seconds=5 * i), ROBOT, float(i), 90 - i / 10)
                 for i in range(360)])
        conn.execute("CALL refresh_continuous_aggregate('robot_state_1m', %s, %s)",
                     (old_start - datetime.timedelta(hours=1),
                      old_end + datetime.timedelta(hours=1)))
        conn.execute("DELETE FROM robot_state_ts WHERE ts < %s",
                     (old_end + datetime.timedelta(hours=1),))

        # a running run whose trajectory is not tagged yet
        live_run = uuid.uuid4()
        live_start = now() - datetime.timedelta(seconds=30)
        insert_run(conn, live_run, OTHER, live_start, None, "RUNNING", "events_only",
                   mission="live-mission")
        conn.execute(
            "INSERT INTO mission_trajectory (mission_id, robot_name, node_id, seq, x, y, yaw, "
            "map_id, ts) VALUES ('live-mission', %s, 'a', 0, 1, 1, 0, 'map1', %s), "
            "('live-mission', %s, 'b', 1, 2, 2, 0, 'map1', %s), "
            "('live-mission', %s, 'old', 0, 9, 9, 0, 'map1', %s)",
            (OTHER, live_start + datetime.timedelta(seconds=1), OTHER,
             live_start + datetime.timedelta(seconds=2), OTHER,
             live_start - datetime.timedelta(hours=1)))
    check(True, "seeded")

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://it") as http:
        print("== C: /runs pagination and filters", flush=True)
        expected = query("SELECT run_id::text FROM mission_runs "
                         "ORDER BY started_at DESC, run_id DESC")
        items, pages = await all_pages(http, "/api/v1/runs", 7)
        check([i["run_id"] for i in items] == [r[0] for r in expected],
              f"all {len(expected)} runs over {pages} pages, newest first, no dups/gaps")
        first = (await get(http, "/api/v1/runs?limit=1"))["items"][0]
        check(first["run_id"] == str(run_id) and first["ended_at"] == iso(end)
              and first["started_at"] == iso(start) and first["duration_s"] ==
              round((end - start).total_seconds(), 3), "newest run first, ISO UTC")
        for url, sql, params in [
            ("robot=" + ROBOT, "robot_name = %s", (ROBOT,)),
            ("site=site-a", "site_id = %s", ("site-a",)),
            ("state=FAILED", "state = %s", ("FAILED",)),
            ("sw_version=v2", "sw_version = %s", ("v2",)),
            (f"robot={ROBOT}&state=COMPLETED&sw_version=v1", "robot_name = %s AND "
             "state = %s AND sw_version = %s", (ROBOT, "COMPLETED", "v1")),
            (f"from={q(start - datetime.timedelta(days=2))}&to={q(start)}",
             "started_at >= %s AND started_at < %s",
             (start - datetime.timedelta(days=2), start)),
        ]:
            want = [r[0] for r in query(
                f"SELECT run_id::text FROM mission_runs WHERE {sql} "
                "ORDER BY started_at DESC, run_id DESC", params)]
            got, pages = await all_pages(http, "/api/v1/runs?" + url, 5)
            check([g["run_id"] for g in got] == want and want,
                  f"runs {url}: {len(want)} rows in {pages} pages")
        for base, names in MISSION_CASES.items():
            url = "/api/v1/runs?" + urlencode({"mission": base})
            got, pages = await all_pages(http, url, 2)
            want = [r[0] for r in query(
                "SELECT run_id::text FROM mission_runs WHERE mission_name = ANY(%s) "
                "ORDER BY started_at DESC, run_id DESC", (names,))]
            check([g["run_id"] for g in got] == want
                  and sorted(g["mission_name"] for g in got) == sorted(names),
                  f"runs mission={base!r}: {len(want)} rows in {pages} pages")
        got, _ = await all_pages(http, "/api/v1/runs?mission=x&robot=" + OTHER
                                 + "&state=COMPLETED", 4)
        check(sorted(g["mission_name"] for g in got) == sorted(MISSION_CASES["x"]),
              "runs mission=x combined with robot and state")
        await get(http, "/api/v1/runs?mission=x&robot=" + ROBOT, 200)
        await get(http, "/api/v1/runs?mission=", 422)
        await get(http, "/api/v1/runs?state=SUCCEEDED", 422)
        await get(http, "/api/v1/runs?from=2026-09-24T12:00:00", 422)
        await get(http, "/api/v1/runs?limit=501", 422)
        await get(http, "/api/v1/runs?cursor=nonsense", 422)
        await get(http, f"/api/v1/runs/{uuid.uuid4()}", 404)
        await get(http, "/api/v1/runs/nope", 422)

        print("== D: /events pagination and filters", flush=True)
        expected = query("SELECT event_id::text FROM fleet_events "
                         "ORDER BY ts DESC, event_id DESC")
        items, pages = await all_pages(http, "/api/v1/events", 11)
        check([i["event_id"] for i in items] == [r[0] for r in expected],
              f"all {len(expected)} events over {pages} pages (tied timestamps), no dups/gaps")
        for url, sql, params in [
            ("code=NAV.*", "code LIKE 'NAV.%%'", ()),
            ("code=NAV.GOAL_BLOCKED&code=BATTERY.LOW", "code IN ('NAV.GOAL_BLOCKED', "
             "'BATTERY.LOW')", ()),
            ("code=TELEMETRY.*", "code = %s", (CHANGED,)),
            ("severity=error", "severity = 'error'", ()),
            ("severity=error&severity=warning&robot=" + OTHER,
             "severity IN ('error', 'warning') AND robot_name = %s", (OTHER,)),
            ("site=site-a", "site_id = 'site-a'", ()),
            (f"run={run_id}", "run_id = %s", (run_id,)),
            (f"robot={ROBOT}&from={q(start)}&to={q(end)}",
             "robot_name = %s AND ts >= %s AND ts < %s", (ROBOT, start, end)),
        ]:
            want = [r[0] for r in query(
                f"SELECT event_id::text FROM fleet_events WHERE {sql} "
                "ORDER BY ts DESC, event_id DESC", params)]
            got, pages = await all_pages(http, "/api/v1/events?" + url, 6)
            check([g["event_id"] for g in got] == want and want,
                  f"events {url}: {len(want)} rows in {pages} pages")
        await get(http, "/api/v1/events?code=NAV", 422)
        await get(http, "/api/v1/events?severity=fatal", 422)
        await get(http, "/api/v1/events?run=123", 422)

        print("== E: /runs/{id}", flush=True)
        body = await get(http, f"/api/v1/runs/{run_id}")
        check(body["run"]["state"] == "COMPLETED" and body["run"]["mission_tree"] == [],
              "run with mission_tree")
        check([e["event_id"] for e in body["events"]] == [str(e_run), str(e_mid)],
              "the run's events, oldest first")

        print("== F: timeline", flush=True)
        tl = await get(http, f"/api/v1/runs/{run_id}/timeline")
        check(tl["window"] == {"from": iso(start), "to": iso(end), "open": False},
              f"window = run: {tl['window']}")
        ev_ids = [e["event_id"] for e in tl["events"]]
        check(str(e_run) in ev_ids and str(e_mid) in ev_ids and str(e_untagged) in ev_ids,
              "timeline events: the run's plus the robot's untagged ones in the window")
        check(all(e["robot_name"] == ROBOT for e in tl["events"]), "no other robot's events")
        check([e["ts"] for e in tl["events"]] == sorted(e["ts"] for e in tl["events"]),
              "timeline events oldest first")
        state = tl["tracks"]["robot_state"]
        check(state["source"] == "raw" and state["downsampled"]
              and 0 < len(state["points"]) <= 2000,
              f"robot_state raw, downsampled to {len(state['points'])} points "
              f"(bucket {state['bucket_s']} s)")
        diag = tl["tracks"]["diagnostics"]
        check(diag["source"] == "raw" and not diag["downsampled"] and len(diag["points"]) == 50,
              "diagnostics raw, all 50 points")
        traj = tl["trajectory"]
        check(traj["source"] == "run_id" and [p["seq"] for p in traj["points"]] ==
              list(range(10)), "trajectory by run_id")

        rec = tl["recording"]
        segs = [(s["from"], s["to"], s["level"], s["source"], s["site_id"])
                for s in rec["segments"]]
        want = [(iso(t[0]), iso(t[1]), "full", "site", "site-a"),
                (iso(t[1]), iso(t[2]), "off", "site", "site-a"),
                (iso(t[2]), iso(t[3]), "events_only", "robot", "site-a"),
                (iso(t[3]), iso(t[4]), "off", "site", "site-a"),
                (iso(t[4]), iso(t[5]), "events_only", "default", "site-b"),
                (iso(t[5]), iso(t[6]), "full", "global", "site-b"),
                (iso(t[6]), iso(t[7]), "off", "global", "site-b")]
        for s in segs:
            print(f"    segment {s}", flush=True)
        check(segs == want, "level segments across site, robot, assignment and global "
                            "switches")
        nr = [(n["from"], n["to"], n["level"], n["missing"]) for n in rec["not_recorded"]]
        check(nr == [(iso(t[1]), iso(t[2]), "off", ["events", "time_series"]),
                     (iso(t[2]), iso(t[3]), "events_only", ["time_series"]),
                     (iso(t[3]), iso(t[4]), "off", ["events", "time_series"]),
                     (iso(t[4]), iso(t[5]), "events_only", ["time_series"]),
                     (iso(t[6]), iso(t[7]), "off", ["events", "time_series"])],
              "not_recorded intervals")
        check(rec["level_at_start"] == "full" and rec["approximate"] is False,
              "level at start = mission_runs.recording_level, exact decode")
        check(len(rec["changes"]) == 6, f"6 RECORDING_CHANGED in the window "
                                        f"({len(rec['changes'])})")

        old = await get(http, f"/api/v1/runs/{old_run}/timeline")
        st = old["tracks"]["robot_state"]
        check(st["source"] == "rollup_1m" and len(st["points"]) in (30, 31)
              and "battery_min" in st["points"][0] and st["bucket_s"] == 60,
              f"raw gone -> 1-minute rollup ({len(st['points'])} points)")
        check(old["tracks"]["diagnostics"]["source"] == "none", "no diagnostics at all")
        check(old["trajectory"]["source"] == "none", "no trajectory")

        live = await get(http, f"/api/v1/runs/{live_run}/timeline")
        check(live["window"]["open"] is True and live["trajectory"]["source"] ==
              "mission_window" and [p["node_id"] for p in live["trajectory"]["points"]] ==
              ["a", "b"], "running run: untagged trajectory in the window")
        check(live["recording"]["segments"][0]["level"] == "events_only",
              "running run level from mission_runs")
        await get(http, f"/api/v1/runs/{uuid.uuid4()}/timeline", 404)

        print("== F2: downsampling with a small point cap", flush=True)
        cfg = fleet_reads.config
        saved = cfg.FLEET_TIMELINE_MAX_POINTS
        cfg.FLEET_TIMELINE_MAX_POINTS = 5
        try:
            tl = await get(http, f"/api/v1/runs/{run_id}/timeline")
            pts = tl["tracks"]["robot_state"]["points"]
            check(tl["tracks"]["robot_state"]["downsampled"] and len(pts) <= 5
                  and pts == sorted(pts, key=lambda p: p["ts"]),
                  f"raw: {len(pts)} points, one per bucket, in time order")
            last_raw = query("SELECT x FROM robot_state_ts WHERE robot_name = %s AND ts <= %s "
                             "ORDER BY ts DESC LIMIT 1", (ROBOT, end))[0][0]
            check(pts[-1]["x"] == last_raw, "each bucket keeps its last sample")
            traj = tl["trajectory"]
            check(traj["downsampled"] and len(traj["points"]) <= 5
                  and traj["points"][-1]["seq"] == 9, "trajectory strided, last point kept")
            old = await get(http, f"/api/v1/runs/{old_run}/timeline")
            st = old["tracks"]["robot_state"]
            check(st["source"] == "rollup_1m" and st["downsampled"] and st["bucket_s"] == 360
                  and len(st["points"]) <= 6, f"rollup downsampled to {len(st['points'])} "
                                              f"points of {st['bucket_s']} s")
        finally:
            cfg.FLEET_TIMELINE_MAX_POINTS = saved

        print("== G: effective level", flush=True)
        body = await get(http, f"/api/v1/robots/{ROBOT}/recording")
        check((body["level"], body["source"], body["site_id"]) == ("off", "global", "site-b"),
              f"global: {body}")
        await main.update_site("site-b", {"telemetry_recording": "full"})
        body = await get(http, f"/api/v1/robots/{ROBOT}/recording")
        check((body["level"], body["source"]) == ("full", "site"), f"site: {body}")
        await main.update_robot(ROBOT, {"telemetry_recording": "events_only"})
        body = await get(http, f"/api/v1/robots/{ROBOT}/recording")
        check((body["level"], body["source"]) == ("events_only", "robot")
              and body["configured"] == {"robot": "events_only", "site": "full",
                                         "global": "off"}, f"robot: {body}")
        await main.assign_robot_site(OTHER, main.AssignRobotSiteRequest(site_id=None))
        await main.update_settings({"telemetry_recording": None})
        body = await get(http, f"/api/v1/robots/{OTHER}/recording")
        check((body["level"], body["source"], body["site_id"]) ==
              ("events_only", "default", None), f"default: {body}")
        await get(http, "/api/v1/robots/ghost/recording", 404)
        await main.update_site("site-b", {"display_name": "Site B"})
        bulk = await get(http, "/api/v1/recording")
        check(bulk == [
            {"robot_name": ROBOT, "level": "events_only", "source": "robot",
             "site_id": "site-b", "site_name": "Site B"},
            {"robot_name": OTHER, "level": "events_only", "source": "default", "site_id": None,
             "site_name": None}], f"bulk levels, by name: {bulk}")
        for item in bulk:
            one = await get(http, f"/api/v1/robots/{item['robot_name']}/recording")
            check((one["level"], one["source"], one["site_id"]) ==
                  (item["level"], item["source"], item["site_id"]),
                  f"bulk agrees with the single route for {item['robot_name']}")

    print("== H: read-only transaction with a statement timeout", flush=True)
    async with fleet_reads.read_cursor(db) as cur:
        await cur.execute("SHOW transaction_read_only")
        read_only = (await cur.fetchone())[0]
        await cur.execute("SHOW statement_timeout")
        timeout = (await cur.fetchone())[0]
    check(read_only == "on" and timeout == "5s", f"read only ({read_only}), timeout {timeout}")
    try:
        async with fleet_reads.read_cursor(db) as cur:
            await cur.execute("DELETE FROM fleet_events")
        raise AssertionError("write accepted")
    except psycopg.errors.ReadOnlySqlTransaction:
        check(True, "writes are refused")
    old_timeout = fleet_reads.config.FLEET_READ_STATEMENT_TIMEOUT_MS
    fleet_reads.config.FLEET_READ_STATEMENT_TIMEOUT_MS = 100
    try:
        from fastapi import HTTPException
        async with fleet_reads.read_cursor(db) as cur:
            await cur.execute("SELECT pg_sleep(1)")
        raise AssertionError("no timeout")
    except HTTPException as exc:
        check(exc.status_code == 503, f"slow statement cancelled -> 503 ({exc.detail})")
    finally:
        fleet_reads.config.FLEET_READ_STATEMENT_TIMEOUT_MS = old_timeout
    check(query("SELECT count(*) FROM fleet_events")[0][0] > 0, "nothing was deleted")
    print("scenario done")


def main_(argv):
    step = argv[1]
    if step == "init":
        asyncio.run(init())
    elif step == "scenario":
        asyncio.run(scenario())
    else:
        raise SystemExit(f"unknown step {step}")


if __name__ == "__main__":
    main_(sys.argv)
