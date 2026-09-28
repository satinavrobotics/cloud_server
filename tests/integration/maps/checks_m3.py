"""Maps M3 step of the M2 integration rehearsal (run_m2.sh, after `ingest`): the robot mapping
switch over the test mosquitto, with the real Postgres. docs/satinav-maps-redesign.md §8, §13.3.

    checks_m3.py switch    the API's MappingControl on a real MQTTClient (as the API wires it):
                           on connect every robot's retained mapping/set is re-published (the
                           robot's open session from `ingest` -> enabled); a subscriber that
                           connects later (a restarting topomap) gets the retained message;
                           pause -> enabled false; the robot's mapping/state is cached and shows
                           in the map's session summary; resume; finish -> disabled, nulls; a new
                           session while the robot's topomap is offline -> mapping_service
                           not_running, still started; broker unreachable -> robot_notified false.

Environment: as checks_m2.py (MQTT_HOST, Postgres).
"""
import asyncio
import json
import os
import sys
import time
import uuid

import paho.mqtt.client as mqtt

from packages.api import maps
from packages.api.mapping_control import MappingControl, set_topic
from packages.config import MQTT_VDA5050_PREFIX
from packages.utils.mqtt_client import MQTTClient
from tests.integration.maps.checks import check, database, query

ROBOT = "masked-frigatebird"
MAP = "map"
PUB = uuid.uuid4()
SET = set_topic(MQTT_VDA5050_PREFIX, ROBOT)
STATE = f"{MQTT_VDA5050_PREFIX}/{ROBOT}/mapping/state"


class Subscriber:
    """A fake topomap: records the set messages it receives (retained flag included)."""

    def __init__(self, topic=SET):
        self.msgs = []
        self.c = mqtt.Client(client_id=f"m3it-{uuid.uuid4().hex[:8]}")
        self.c.on_message = lambda _c, _u, m: m.payload and self.msgs.append(
            (bool(m.retain), json.loads(m.payload)))
        self.c.connect(os.environ["MQTT_HOST"], 1883, 30)
        self.c.subscribe(topic, 1)
        self.c.loop_start()

    def wait(self, pred, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            for retained, msg in reversed(self.msgs):
                if pred(msg):
                    return retained, msg
            time.sleep(0.05)
        return None

    def publish(self, topic, payload):
        self.c.publish(topic, json.dumps(payload), qos=1, retain=True).wait_for_publish()

    def close(self):
        self.c.loop_stop()
        self.c.disconnect()


def _open_session():
    rows = query("SELECT session_id::text, map_name FROM map_sessions WHERE robot_name = %s "
                 "AND ended_at IS NULL", (ROBOT,))
    return rows[0] if rows else None


async def switch():
    db = database()
    await db.async_init()
    loop = asyncio.get_running_loop()
    control = MappingControl(MQTT_VDA5050_PREFIX)
    synced = asyncio.Event()

    async def on_connect():
        results = await maps.sync_all_robots(control, db)
        print(f"  re-published on connect: {results}")
        synced.set()

    control.on_connect = on_connect
    client = MQTTClient(client_id=f"m3it-api-{uuid.uuid4().hex[:6]}",
                        broker=os.environ["MQTT_HOST"], port=1883)
    control.attach(client, loop)
    client.connect()
    await asyncio.wait_for(synced.wait(), 15)

    # 1. the open session from the `ingest` step -> retained enabled; a late subscriber sees it
    sid, map_name = _open_session()
    check(map_name == MAP, f"robot has an open session on `map` ({sid})")
    robot = Subscriber()
    got = robot.wait(lambda m: m["enabled"] and m["session_id"] == sid)
    check(got is not None and got[0], f"on connect: retained set enabled for {sid}: {got}")
    check(got[1]["map"] == MAP and "issued_at" in got[1], f"payload {got[1]}")
    robot.close()

    # 2. pause -> enabled false, same session (seen live and by a late subscriber)
    robot = Subscriber()
    robot.wait(lambda m: m["enabled"])
    out = await maps.session_action(db, MAP, sid, "pause", PUB, control=control)
    check(out["robot_notified"] is True and out["map_state"] == "paused",
          f"pause: robot_notified {out['robot_notified']}")
    got = robot.wait(lambda m: m["enabled"] is False and m["session_id"] == sid)
    check(got is not None and not got[0], f"pause: live set enabled=false {got}")
    late = Subscriber()
    got = late.wait(lambda m: True)
    check(got is not None and got[0] and got[1]["enabled"] is False
          and got[1]["session_id"] == sid, f"pause: retained set for a restarting topomap {got}")
    late.close()

    # 3. the robot reports its state -> cached, in the session summary and the robot's view
    robot.publish(STATE, {"online": True, "enabled": False, "session_id": sid, "map": MAP,
                          "nodes_sent": 3, "since": "2026-09-29T10:00:00+00:00",
                          "stamp": "2026-09-29T10:00:01+00:00", "source": "mqtt"})
    end = time.time() + 5
    while control.state(ROBOT) is None and time.time() < end:
        await asyncio.sleep(0.05)
    st = control.state(ROBOT)
    check(st is not None and st["status"] == "off" and st["nodes_sent"] == 3
          and "received_at" in st, f"state cached: {st}")
    summary = await maps.session_summary(db, MAP, control)
    check(summary["mapping_state"]["session_id"] == sid
          and summary["mapping_service"] == "running",
          f"GET /maps/map sessions.mapping_state {summary['mapping_state']['status']}, "
          f"mapping_service {summary['mapping_service']}")

    # 4. resume -> enabled
    out = await maps.session_action(db, MAP, sid, "resume", PUB, control=control)
    got = robot.wait(lambda m: m["enabled"] is True and m["session_id"] == sid)
    check(out["robot_notified"] and got is not None, "resume: set enabled=true")

    # 5. finish -> disabled, nulls
    out = await maps.session_action(db, MAP, sid, "finish", PUB, control=control)
    got = robot.wait(lambda m: m["enabled"] is False and m["session_id"] is None)
    check(out["robot_notified"] and got is not None and got[1]["map"] is None,
          "finish: set enabled=false, session/map null")

    # 6. topomap offline (its last will) -> a new session still starts, not_running
    robot.publish(STATE, {"online": False, "enabled": False, "session_id": None, "map": None,
                          "nodes_sent": 0, "since": None})
    end = time.time() + 5
    while (control.state(ROBOT) or {}).get("status") != "unreachable" and time.time() < end:
        await asyncio.sleep(0.05)
    out = await maps.start_session(db, MAP, {"robot": ROBOT}, PUB, "m3it", control=control)
    new_sid = out["session"]["session_id"]
    check(out["mapping_service"] == "not_running" and out["robot_notified"] is True
          and out["mapping_state"]["status"] == "unreachable",
          f"start with topomap offline: mapping_service {out['mapping_service']}, "
          "robot_notified true")
    got = robot.wait(lambda m: m["enabled"] is True and m["session_id"] == new_sid)
    check(got is not None and got[1]["map"] == MAP, "start: set enabled with the new session")

    # 7. broker unreachable -> the call succeeds, robot_notified false
    client.disconnect()
    await asyncio.sleep(0.5)
    out = await maps.session_action(db, MAP, new_sid, "pause", PUB, control=control)
    check(out["changed"] and out["map_state"] == "paused" and out["robot_notified"] is False,
          "MQTT down: pause committed, robot_notified false")
    state = query("SELECT paused_at IS NOT NULL FROM map_sessions WHERE session_id = %s",
                  (uuid.UUID(new_sid),))[0][0]
    check(state, "the pause is in the database")

    # 8. the API reconnects -> the current state (paused) is re-published
    synced.clear()
    client2 = MQTTClient(client_id=f"m3it-api2-{uuid.uuid4().hex[:6]}",
                         broker=os.environ["MQTT_HOST"], port=1883)
    control.attach(client2, loop)
    client2.connect()
    await asyncio.wait_for(synced.wait(), 15)
    got = robot.wait(lambda m: m["enabled"] is False and m["session_id"] == new_sid)
    check(got is not None, "reconnect: the missed pause is re-published")

    # back to what `ingest` left: the robot mapping `map` (open, unpaused)
    await maps.session_action(db, MAP, new_sid, "resume", PUB, control=control)
    robot.close()
    client2.disconnect()


if __name__ == "__main__":
    step = sys.argv[1]
    if step == "switch":
        asyncio.run(switch())
    else:
        globals()[step]()
