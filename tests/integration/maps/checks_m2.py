"""Steps of the maps M2 integration test (run_m2.sh drives them, each in a throwaway container on
the test's private network). docs/satinav-maps-redesign.md §6, §12, §13.2.

    checks_m2.py seed          fresh database only: the live robot and the 5 legacy nodes of
                               `map` in ArangoDB (the M1 `seed` step made the map row)
    checks_m2.py show          map rows, sessions, robots, `map` nodes
    checks_m2.py upgraded      migration 20260929_01: fleet_events source graph_builder allowed
    checks_m2.py legacy        after tools.maps_m2_legacy_nodes --apply: `map` nodes in the map
                               frame (robot_pose kept), legacy session transform + node_count,
                               datum_* = the origin as a utm datum
    checks_m2.py reverted      after --revert --apply: the nodes and datum as before
    checks_m2.py ingest        graph-builder end to end over MQTT: no session -> dropped +
                               MAP.INGEST_REJECTED; a mapping session (POST .../sessions; the
                               PUT /robots/{r}/map shim was removed in U6) -> image + node stored
                               in the map frame, node_count counted; pause -> dropped within
                               ~1 s; resume; finish -> dropped; a new geo map
    checks_m2.py downgraded    after alembic downgrade -1: the old CHECK, no graph_builder rows

Environment: as checks.py, plus ARANGO_HOST/ARANGO_PASSWORD, MINIO_HOST/MINIO_PORT/keys,
MQTT_HOST, GB_URL (graph-builder).
"""
import asyncio
import json
import math
import os
import sys
import time
import uuid

import paho.mqtt.client as mqtt

from packages.api import maps
from packages.utils import geo, map_geo
from tests.integration.maps.checks import check, database, query

ROBOT = "masked-frigatebird"
MAP = "map"
ENU_DATUM = {"latitude": 47.4979, "longitude": 19.0402, "bearing_deg": 0.0, "frame": "enu",
             "utm_zone": None, "utm_north": None, "utm_easting": None, "utm_northing": None}
LEGACY_POSES = [(2.6263811562342476, 0.09897950453554777, 0.6299589053415888),
                (2.7190450383093356, 0.16983032724271666, 1.0309292403257377),
                (2.658120087242419, 0.4080409102255091, 2.0256896297323763),
                (2.1045091444949224, 0.8075625452384483, 2.7292079179403665),
                (1.4814666818472393, 0.9849484618786741, 2.9303623861822308)]
PUB = uuid.uuid4()


def arango():
    from arango import ArangoClient
    client = ArangoClient(hosts=f"http://{os.environ['ARANGO_HOST']}:8529")
    sys_db = client.db("_system", username="root", password=os.environ["ARANGO_PASSWORD"])
    if not sys_db.has_database("topomap_db"):
        sys_db.create_database("topomap_db")
    return client.db("topomap_db", username="root", password=os.environ["ARANGO_PASSWORD"])


def nodes(db, name=MAP):
    col = f"nodes_{name}"
    if not db.has_collection(col):
        return []
    return list(db.aql.execute("FOR d IN @@c SORT d.created_at RETURN d", bind_vars={"@c": col}))


def seed():
    db = arango()
    for col, edge in (("nodes_map", False), ("edges_map", True)):
        if not db.has_collection(col):
            db.create_collection(col, edge=edge)
    for i, (x, y, yaw) in enumerate(LEGACY_POSES):
        db.collection("nodes_map").insert({
            "_key": f"legacy-{i}", "node_id": f"legacy-{i}", "pose": {"x": x, "y": y, "yaw": yaw},
            "created_at": f"2026-09-28T14:10:{20 + i}", "map_id": MAP, "robot_name": ROBOT,
            "session_node_id": i})
    query("INSERT INTO robotobjectv1 (name, lifecycle, spec, status) VALUES (%s, 'ALIVE', %s, %s)",
          (ROBOT, json.dumps({"datum": ENU_DATUM}),
           json.dumps({"online": True, "state": "IDLE"})))
    print("seed done")


def show():
    for name, spec, status in query("SELECT name, spec, status FROM mapobjectv1 ORDER BY name"):
        print(f"  map {name}: type={spec.get('type')} state={status.get('state')} "
              f"datum={spec.get('datum_latitude')},{spec.get('datum_longitude')} "
              f"frame={spec.get('datum_frame')} utm_e={spec.get('datum_utm_easting')}")
    for r in query("SELECT map_name, robot_name, kind, ended_at IS NULL, paused_at IS NOT NULL, "
                   "node_count, map_t_session FROM map_sessions ORDER BY map_name, started_at"):
        print(f"  session map={r[0]} robot={r[1]} kind={r[2]} open={r[3]} paused={r[4]} "
              f"nodes={r[5]} T={json.dumps(r[6], sort_keys=True)}")
    for name, online in query("SELECT name, status->>'online' FROM robotobjectv1 ORDER BY name"):
        print(f"  robot {name}: online={online}")
    for d in nodes(arango()):
        p, rp = d["pose"], d.get("robot_pose")
        print(f"  node {d['_key'][:12]} pose=({p['x']:.4f}, {p['y']:.4f}, {p['yaw']:.4f})"
              + (f" robot_pose=({rp['x']:.4f}, {rp['y']:.4f}) session={d.get('session_id')}"
                 if rp else ""))


def upgraded():
    (defn,) = query("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'fleet_events_source_check'")[0]
    check("graph_builder" in defn, f"fleet_events_source_check allows graph_builder: {defn}")


def _legacy_row():
    rows = query("SELECT session_id, datum, map_t_session, node_count FROM map_sessions "
                 "WHERE map_name = %s AND kind = 'legacy'", (MAP,))
    check(len(rows) == 1, "`map` has its legacy session")
    return rows[0]


def legacy():
    sid, datum, t, count = _legacy_row()
    spec = query("SELECT spec FROM mapobjectv1 WHERE name = %s", (MAP,))[0][0]
    g = spec["geo"]
    check(abs(math.degrees(t["yaw"]) + 1.445) < 0.002,
          f"legacy map_T_session yaw = {math.degrees(t['yaw']):.4f} deg (grid convergence)")
    docs = [d for d in nodes(arango()) if d.get("session_id") == str(sid)]
    check(len(docs) == count and count >= 1, f"legacy node_count {count} = its ArangoDB nodes")
    worst = 0.0
    for d in docs:
        rp, p = d["robot_pose"], d["pose"]
        # exact placement of the old pose: old datum frame -> lat/lon -> map zone - origin
        lat, lon = geo.local_to_gps(rp["x"], rp["y"], datum["latitude"], datum["longitude"],
                                    datum.get("bearing_deg") or 0.0, frame=datum.get("frame"))
        e, n = geo.latlon_to_utm(lat, lon, g["utm_zone"], g["utm_north"])
        err = math.hypot(p["x"] - (e - g["origin_e"]), p["y"] - (n - g["origin_n"]))
        worst = max(worst, err / max(1.0, math.hypot(rp["x"], rp["y"])))
    check(worst < 2e-4, f"legacy nodes in the map frame (worst {worst:.2e} of the distance: "
                        "the UTM scale factor, rigid map_T_session)")
    check(spec["datum_frame"] == "utm" and spec["datum_utm_easting"] == g["origin_e"]
          and spec["datum_utm_northing"] == g["origin_n"] and spec["datum_bearing_deg"] == 0.0,
          "`map` datum_* = its origin as a utm datum")
    status = query("SELECT status FROM mapobjectv1 WHERE name = %s", (MAP,))[0][0]
    check(status["node_count"] == len(nodes(arango())), "map status node_count = ArangoDB")


def reverted():
    sid, datum, t, _count = _legacy_row()
    check(t == map_geo.IDENTITY, "legacy session back to identity")
    docs = nodes(arango())
    check(all("robot_pose" not in d and "session_id" not in d for d in docs),
          "no node keeps robot_pose / session_id")
    got = sorted((round(d["pose"]["x"], 9), round(d["pose"]["y"], 9)) for d in docs)
    want = sorted((round(x, 9), round(y, 9)) for x, y, _ in LEGACY_POSES)
    if len(docs) == len(LEGACY_POSES):
        check(got == want, "the seeded legacy poses are back")
    spec = query("SELECT spec FROM mapobjectv1 WHERE name = %s", (MAP,))[0][0]
    check(spec["datum_frame"] == (datum.get("frame") or "enu")
          and abs(spec["datum_latitude"] - datum["latitude"]) < 1e-9,
          "`map` datum_* back to the legacy datum")


# --- ingest end to end ---------------------------------------------------------------------------

class Mqtt:
    def __init__(self):
        self.c = mqtt.Client(client_id=f"m2it-{uuid.uuid4().hex[:8]}")
        self.c.connect(os.environ["MQTT_HOST"], 1883, 30)
        self.c.loop_start()

    def pub(self, topic, payload):
        info = self.c.publish(topic, json.dumps(payload), qos=1)
        info.wait_for_publish()


def _events(reason=None):
    rows = query("SELECT payload FROM fleet_events WHERE code = 'MAP.INGEST_REJECTED' "
                 "AND source = 'graph_builder' AND robot_name = %s ORDER BY ts", (ROBOT,))
    return [r[0] for r in rows if reason is None or r[0]["reason"] == reason]


def _wait(pred, timeout=15.0, step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        value = pred()
        if value:
            return value
        time.sleep(step)
    return None


def _node_by_seq(seq, name=MAP):
    return [d for d in nodes(arango(), name)
            if d.get("robot_name") == ROBOT and d.get("session_node_id") == seq
            and "robot_pose" in d]


def _send(m, seq, x, y, yaw=0.0, image=True, **extra):
    if image:
        m.pub("robot/image_upload", {"session_node_id": seq, "robot_name": ROBOT,
                                     "camera_name": "left", "image_data": "aGVsbG8=",
                                     "timestamp": 1000 + seq, "yaw_offset": 0.0,
                                     "map_id": "default", **extra})
    m.pub("robot/node_update", {"session_node_id": seq, "robot_name": ROBOT, "x": x, "y": y,
                                "yaw": yaw, "camera_metadata": [{"camera_name": "left"}],
                                "metadata": {"source": "create_topomap"}, "map_id": "default",
                                **extra})


def _minio_objects(bucket):
    from minio import Minio
    c = Minio(f"{os.environ['MINIO_HOST']}:{os.environ.get('MINIO_PORT', '9000')}",
              access_key=os.environ["MINIO_ACCESS_KEY"],
              secret_key=os.environ["MINIO_SECRET_KEY"], secure=False)
    if not c.bucket_exists(bucket):
        return []
    return [o.object_name for o in c.list_objects(bucket, recursive=True)]


async def ingest():
    import httpx
    db = database()
    await db.async_init()
    r = httpx.get(os.environ["GB_URL"] + "/health", timeout=5)
    check(r.status_code == 200, f"graph-builder healthy: {r.json()}")
    query("UPDATE robotobjectv1 SET spec = spec || %s::jsonb, status = status || %s::jsonb "
          "WHERE name = %s", (json.dumps({"datum": ENU_DATUM}),
                              json.dumps({"online": True}), ROBOT))
    before_default = len(nodes(arango(), "default"))
    m = Mqtt()
    base = int(time.time()) % 100000 * 10

    # 1. no open session: dropped and reported
    open_now = query("SELECT count(*) FROM map_sessions WHERE robot_name = %s "
                     "AND ended_at IS NULL", (ROBOT,))[0][0]
    check(open_now == 0, "robot has no open session")
    _send(m, base + 1, 1.0, 1.0)
    ev = _wait(lambda: _events("no_session"))
    # The image goes first (as the robot sends it): the first drop is reported at once, the
    # node's drop is carried by the next report (at most one per robot and reason a minute).
    check(ev and ev[-1]["dropped_nodes"] + ev[-1]["dropped_images"] >= 1,
          f"no session -> MAP.INGEST_REJECTED {ev and ev[-1]}")
    time.sleep(1.0)
    check(not _node_by_seq(base + 1) and len(nodes(arango(), "default")) == before_default,
          "nothing stored, no 'default' map")

    # 2. a mapping session on `map`
    out = await maps.start_session(db, MAP, {"robot": ROBOT}, PUB, "m2it")
    s = out["session"]
    check(s and s["map_name"] == MAP and s["state"] == "mapping", "session open on `map`")
    t = s["map_T_session"]
    check(abs(math.degrees(t["yaw"]) + 1.445) < 0.002 and abs(t["tx"]) < 1e-6,
          f"session transform = the convergence rotation {t}")
    rows = query("SELECT status->>'state' FROM mapobjectv1 WHERE name = %s", (MAP,))
    check(rows[0] == ("mapping",), f"map state: {rows[0]}")
    time.sleep(1.2)  # the ingest cache (1 s)
    _send(m, base + 2, 10.0, 0.0, 0.5)
    doc = _wait(lambda: _node_by_seq(base + 2))
    check(bool(doc), "node stored in `map`")
    d = doc[0]
    x, y, yaw = map_geo.apply_pose(t, 10.0, 0.0, 0.5)
    check(abs(d["pose"]["x"] - x) < 1e-6 and abs(d["pose"]["y"] - y) < 1e-6
          and abs(d["pose"]["yaw"] - yaw) < 1e-9,
          f"pose in the map frame ({d['pose']['x']:.4f}, {d['pose']['y']:.4f})")
    check(d["robot_pose"] == {"x": 10.0, "y": 0.0, "yaw": 0.5} and d["session_id"] == s["session_id"],
          "robot_pose and session_id stored")
    objs = _wait(lambda: [o for o in _minio_objects("map-map") if o.startswith(d["_key"])])
    check(bool(objs), f"image in bucket map-map (not map-default): {objs}")
    count = _wait(lambda: query("SELECT node_count FROM map_sessions WHERE session_id = %s",
                                (uuid.UUID(s["session_id"]),))[0][0] == 1 or None)
    check(bool(count), "session node_count = 1")

    # 3. pause: nodes still go to the map (the open session decides)
    await maps.session_action(db, MAP, s["session_id"], "pause", PUB)
    time.sleep(1.2)
    _send(m, base + 3, 11.0, 0.0)
    check(bool(_wait(lambda: _node_by_seq(base + 3))), "paused session: node still stored")

    # 4. resume
    await maps.session_action(db, MAP, s["session_id"], "resume", PUB)
    time.sleep(1.2)
    _send(m, base + 4, 12.0, 0.0)
    check(bool(_wait(lambda: _node_by_seq(base + 4))), "resumed: node stored")

    # 5. a payload session_id that is not the open session
    _send(m, base + 5, 13.0, 0.0, session_id=str(uuid.uuid4()))
    check(bool(_wait(lambda: _node_by_seq(base + 5))), "foreign session_id accepted")

    # 6. finishing the session; data dropped again
    out = await maps.session_action(db, MAP, s["session_id"], "finish", PUB, "m2it")
    check(out["session"]["session_id"] == s["session_id"]
          and out["session"]["state"] == "finished", "session finished")
    state = query("SELECT status->>'state' FROM mapobjectv1 WHERE name = %s", (MAP,))[0][0]
    check(state == "ready", "`map` ready")
    time.sleep(1.2)
    n_before = len(_events("no_session"))
    _send(m, base + 6, 14.0, 0.0)
    time.sleep(2.0)
    check(not _node_by_seq(base + 6), "after finish: node not stored (reported once a minute: "
                                      f"{len(_events('no_session')) - n_before} new event(s))")

    # 7. a new geo map: origin = the robot's datum at its first session
    await maps.create_map(db, {"name": "m2it-new", "type": "geo"}, PUB, "m2it")
    out = await maps.start_session(db, "m2it-new", {"robot": ROBOT}, PUB, "m2it")
    check(out["session"]["map_name"] == "m2it-new", "new map: session open")
    spec = query("SELECT spec FROM mapobjectv1 WHERE name = 'm2it-new'")[0][0]
    check(spec["type"] == "geo" and spec["datum_frame"] == "utm",
          "new map: geo, legacy datum = its origin (utm)")
    time.sleep(1.2)
    _send(m, base + 7, 5.0, 5.0)
    doc = _wait(lambda: _node_by_seq(base + 7, "m2it-new"))
    check(bool(doc), "node stored in the new map")
    t2 = out["session"]["map_T_session"]
    check(abs(t2["tx"]) < 1e-6 and abs(t2["ty"]) < 1e-6, "first session of a new geo map: "
                                                         "no translation")
    await maps.start_session(db, MAP, {"robot": ROBOT, "replace": True}, PUB,
                             "m2it")  # back on `map`, as before
    rows = query("SELECT map_name FROM map_sessions WHERE robot_name = %s AND ended_at IS NULL",
                 (ROBOT,))
    check(rows == [(MAP,)], "switched back: one open session, on `map`")
    codes = [r[0] for r in query("SELECT code FROM fleet_events WHERE code LIKE 'MAP.%%' "
                                 "ORDER BY ts")]
    print(f"  MAP.* events: { {c: codes.count(c) for c in sorted(set(codes))} }")
    check(not any(r[0] for r in query("SELECT payload ? '_invalid' FROM fleet_events "
                                      "WHERE code LIKE 'MAP.%%'")), "event payloads valid")


def downgraded():
    (defn,) = query("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'fleet_events_source_check'")[0]
    check("graph_builder" not in defn, f"old CHECK back: {defn}")
    check(query("SELECT count(*) FROM fleet_events WHERE source = 'graph_builder'")[0][0] == 0,
          "graph_builder rows removed")


if __name__ == "__main__":
    step = sys.argv[1]
    if step == "ingest":
        asyncio.run(ingest())
    else:
        globals()[step]()
