"""Steps of the 3D reconstruction integration test (run.sh drives them, each in a throwaway
container on the test's private network). docs/reconstruction/design.md §5, §6, §8, §11.

    checks.py seed      a robot, a local map `reconit` and a placed mapping session through the
                        API; 4 nodes with depth (2 with robot_pose3d, 2 with null) and 1 without
                        over MQTT through graph-builder (depth sent BEFORE its node, as the robot
                        does): PNG in MinIO, depth.left + pose3d_map on the ArangoDB node
    checks.py build     POST .../reconstruction -> the stub fetches every presigned input URL
                        from its own container (host = RECONSTRUCTION_MINIO_ENDPOINT), PUTs to
                        staging, calls back; the gateway verifies, copies, commits; the files
                        stream; STARTED/FINISHED events; the staging bucket and its expiry rule
    checks.py rebuild   a second job supersedes the first (old prefix removed); a new node with
                        depth makes the result stale (new_nodes 1)
    checks.py cancel    a held job, cancelled: the stub gets the cancel, fails `cancelled`; the
                        previous result stays
    checks.py down      (the stub is stopped) the job waits queued (waiting_for_service), then
                        fails service_unavailable after RECONSTRUCTION_QUEUE_TIMEOUT_S
    checks.py lost      the stub forgets the job: after 60 s of silence the poll gets 404 and
                        resubmits (attempt 2), which succeeds
    checks.py delete    the map is deleted mid-job: the job is cancelled (map_deleting event),
                        the late PUT lands in staging, the late finish gets 410, the bucket is
                        not re-created, the rows are gone

Environment: PGHOST/PGPASSWORD, ARANGO_HOST/ARANGO_PASSWORD, MINIO_HOST/keys, MQTT_HOST,
API_URL, STUB_URL, MINIO_PUBLIC (the endpoint the presigned URLs must carry).
"""
import base64
import hashlib
import json
import os
import sys
import time
import uuid

import httpx
import paho.mqtt.client as mqtt

from tests.integration.maps.checks import check, query

ROBOT = "recon-bot"
MAP = "reconit"
BUCKET = "map-reconit"
API = os.environ.get("API_URL", "http://localhost:8000")
STUB = os.environ.get("STUB_URL", "http://localhost:8009")
PUBLIC = os.environ.get("MINIO_PUBLIC", "minio-public:9000")
CAMERA = {"frame_id": "camera", "width": 4, "height": 3, "fx": 3.0, "fy": 3.0, "cx": 2.0,
          "cy": 1.5, "distortion_model": "plumb_bob", "d": [0, 0, 0, 0, 0], "depth_type": "z",
          "valid_range_m": [0.2, 15.0], "rgb_width": 4, "rgb_height": 3,
          "T_base_cam": {"x": 0.1, "y": 0.0, "z": 0.45, "qx": -0.5, "qy": 0.5, "qz": -0.5,
                         "qw": 0.5}}


def png(seq):
    return b"\x89PNG\r\n\x1a\n" + f"depth-{seq}".encode() * 50


def jpeg(seq):
    return b"\xff\xd8\xff" + f"rgb-{seq}".encode() * 50


def api(method, path, **kw):
    return httpx.request(method, API + path, timeout=30, **kw)


def stub(method, path, **kw):
    return httpx.request(method, STUB + path, timeout=10, **kw)


def wait(pred, timeout=30.0, step=0.5, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        value = pred()
        if value:
            return value
        time.sleep(step)
    raise AssertionError(f"timed out waiting for {what}")


def arango():
    from arango import ArangoClient
    client = ArangoClient(hosts=f"http://{os.environ['ARANGO_HOST']}:8529")
    return client.db("topomap_db", username="root", password=os.environ["ARANGO_PASSWORD"])


def nodes():
    db = arango()
    if not db.has_collection(f"nodes_{MAP}"):
        return []
    return list(db.collection(f"nodes_{MAP}").all())


def minio():
    from minio import Minio
    return Minio(f"{os.environ['MINIO_HOST']}:9000", access_key=os.environ["MINIO_ACCESS_KEY"],
                 secret_key=os.environ["MINIO_SECRET_KEY"], secure=False)


def objects(bucket, prefix=""):
    c = minio()
    if not c.bucket_exists(bucket):
        return {}
    return {o.object_name: o.size for o in c.list_objects(bucket, prefix=prefix, recursive=True)}


def status():
    r = api("GET", f"/api/v1/maps/{MAP}/reconstruction")
    assert r.status_code == 200, r.text
    return r.json()


def job_row(job_id):
    rows = query("SELECT state, attempts, error, stage FROM map_reconstructions "
                 "WHERE job_id = %s", (uuid.UUID(job_id),))
    return rows[0] if rows else None


def events(code, job_id):
    return [r[0] for r in query("SELECT payload FROM fleet_events WHERE code = %s AND "
                                "source = 'reconstruction' AND payload->>'job_id' = %s",
                                (code, job_id))]


def start(expect=202):
    r = api("POST", f"/api/v1/maps/{MAP}/reconstruction")
    check(r.status_code == expect, f"POST reconstruction -> {r.status_code} {r.text[:200]}")
    return r.json()


def wait_state(job_id, states, timeout=60.0):
    return wait(lambda: (job_row(job_id) or (None,))[0] in states and job_row(job_id),
                timeout, what=f"job {job_id} in {states}")


class Mqtt:
    def __init__(self):
        self.c = mqtt.Client(client_id=f"reconit-{uuid.uuid4().hex[:8]}")
        self.c.connect(os.environ["MQTT_HOST"], 1883, 30)
        self.c.loop_start()

    def pub(self, topic, payload):
        self.c.publish(topic, json.dumps(payload), qos=1).wait_for_publish()


def send_node(m, seq, x, y, yaw, depth=True, pose3d=True):
    if depth:  # before its node, as the robot sends it (R1)
        m.pub("robot/depth_upload", {
            "session_node_id": seq, "robot_name": ROBOT, "camera_name": "left",
            "depth_data": base64.b64encode(png(seq)).decode(), "content_type": "image/png",
            "depth_encoding": "u16_mm", "depth_scale": 0.001,
            "depth_stamp_ms": 1_000_000 + seq, "rgb_stamp_ms": 1_000_000 + seq - 39,
            "robot_pose3d": ({"x": x, "y": y, "z": 0.02, "qx": 0.0, "qy": 0.01, "qz": 0.0,
                              "qw": 0.99995} if pose3d else None),
            "camera": CAMERA})
    m.pub("robot/image_upload", {"session_node_id": seq, "robot_name": ROBOT,
                                 "camera_name": "left",
                                 "image_data": base64.b64encode(jpeg(seq)).decode(),
                                 "timestamp": 1_000_000 + seq, "yaw_offset": 0.0})
    m.pub("robot/node_update", {"session_node_id": seq, "robot_name": ROBOT, "x": x, "y": y,
                                "yaw": yaw, "camera_metadata": [{"camera_name": "left"}],
                                "metadata": {"source": "create_topomap"}})
    # Paced like a robot (keyframes ~1 s apart): graph-builder handles messages concurrently,
    # and a burst lets a later node overtake an earlier one, which its "session reset"
    # heuristic (a lower session_node_id) answers by clearing the buffers.
    time.sleep(0.5)


def by_seq(seq):
    return [d for d in nodes() if d.get("session_node_id") == seq]


# --- steps -------------------------------------------------------------------------------------

def seed():
    query("INSERT INTO robotobjectv1 (name, lifecycle, spec, status) VALUES (%s, 'ALIVE', "
          "'{}', %s) ON CONFLICT (name) DO NOTHING", (ROBOT, json.dumps({"online": True})))
    r = api("POST", "/api/v1/maps", json={"name": MAP, "type": "local"})
    check(r.status_code == 201, f"map {MAP} created ({r.status_code})")
    r = api("POST", f"/api/v1/maps/{MAP}/sessions", json={"robot": ROBOT})
    check(r.status_code == 201 and r.json()["session"]["aligned"],
          f"placed mapping session ({r.status_code})")
    time.sleep(1.5)  # graph-builder's session cache
    m = Mqtt()
    poses = {1: (0.0, 1.0, 0.0), 2: (1.0, 0.0, 1.5708), 3: (2.0, 0.0, 0.0), 4: (3.0, 0.0, 0.0)}
    for seq, (x, y, yaw) in poses.items():
        send_node(m, seq, x, y, yaw, pose3d=seq <= 2)
    send_node(m, 5, 4.0, 0.0, 0.0, depth=False)
    wait(lambda: len(nodes()) == 5 and sum(1 for d in nodes() if d.get("depth")) == 4,
         what="5 nodes, 4 with depth")
    check(True, "graph-builder stored 5 nodes, 4 with depth (depth arrived before its node)")
    for seq in (1, 3):
        d = by_seq(seq)[0]
        rec = d["depth"]["left"]
        check(rec["camera"] == CAMERA and rec["depth_scale"] == 0.001
              and rec["session_id"] == d["session_id"], f"node {seq}: depth.left parameters")
        if seq == 1:
            p = rec["pose3d_map"]
            check(abs(p["x"] - 0.0) < 1e-9 and abs(p["y"] - 1.0) < 1e-9 and p["z"] == 0.02,
                  f"node 1: pose3d_map (identity session) {p}")
        else:
            check("pose3d_map" not in rec, "node 3: robot_pose3d null -> no pose3d_map")
        key = f"{d['node_id']}/depth/left.png"
        data = minio().get_object(BUCKET, key).read()
        check(data == png(seq), f"node {seq}: {key} stored byte for byte")
    stats = api("GET", f"/api/v1/maps/{MAP}")
    check(stats.status_code == 200, "map readable")


def build():
    stub("POST", "/control", json={"mode": "normal"})
    job = start()
    check(job["state"] == "queued", "job queued")
    jid = job["job_id"]
    wait(lambda: (status()["reconstruction"] or {}).get("job_id") == jid, 60,
         what="the result")
    row = job_row(jid)
    check(row[0] == "succeeded" and row[1] == 1, f"job succeeded at attempt 1 ({row})")
    rec = stub("GET", "/record").json()
    manifest = [m for m in rec["manifests"] if m["job_id"] == jid][-1]
    by_id = {d["node_id"]: d for d in nodes()}
    check(len(manifest["nodes"]) == 4, "manifest: the 4 nodes with depth")
    for n in manifest["nodes"]:
        doc = by_id[n["node_id"]]
        check(n["pose"] == {k: doc["pose"][k] for k in ("x", "y", "yaw")},
              f"manifest pose = ArangoDB map-frame pose ({doc['session_node_id']})")
        cam = n["cameras"][0]
        check(("pose3d" in cam) == (doc["session_node_id"] <= 2), "pose3d only when sent")
    fetches = [f for f in rec["fetches"] if f["job_id"] == jid]
    check(len(fetches) == 8 and all(f["status"] == 200 for f in fetches),
          "the stub fetched all 8 presigned input URLs from its own container: 200")
    check({f["host"] for f in fetches} == {PUBLIC},
          f"presigned for RECONSTRUCTION_MINIO_ENDPOINT {PUBLIC}")
    seq_of = {d["node_id"]: d["session_node_id"] for d in nodes()}
    ok = all(f["sha256"] == hashlib.sha256(
        (png if f["kind"] == "depth_url" else jpeg)(seq_of[f["node_id"]])).hexdigest()
        for f in fetches)
    check(ok, "the fetched bytes are the robot's depth PNGs and RGB images")
    check(all(p["status"] == 200 for p in rec["puts"] if p["job_id"] == jid),
          "the stub's presigned PUTs into staging: 200")
    files = objects(BUCKET, f"reconstruction/{jid}/")
    check(sorted(files) == sorted(f"reconstruction/{jid}/{f}" for f in
                                  ("cloud.ply", "ortho.png", "height.png", "meta.json")),
          f"copied to {BUCKET}/reconstruction/{jid}/")
    check(objects("recon-staging", f"{jid}/") == {}, "staging prefix removed")
    lc = minio().get_bucket_lifecycle("recon-staging")
    check(lc is not None and lc.rules[0].expiration.days == 1,
          "recon-staging has the 1-day expiry rule")
    r = api("GET", f"/api/v1/maps/{MAP}/reconstruction/files/meta.json?v={jid}")
    check(r.status_code == 200 and r.json()["job_id"] == jid
          and "immutable" in r.headers["cache-control"], "meta.json streamed, immutable")
    r = api("GET", f"/api/v1/maps/{MAP}/reconstruction/files/cloud.ply")
    check(r.status_code == 200 and r.content.startswith(b"ply"), "cloud.ply streamed")
    s = status()
    check(s["reconstruction"]["stale"] is False and s["job"] is None,
          "status: current, not stale, no job")
    check(len(events("MAP.RECONSTRUCTION_STARTED", jid)) == 1
          and len(events("MAP.RECONSTRUCTION_FINISHED", jid)) == 1,
          "STARTED and FINISHED events")


def rebuild():
    old = status()["reconstruction"]["job_id"]
    stub("POST", "/control", json={"mode": "normal"})
    jid = start()["job_id"]
    wait(lambda: (status()["reconstruction"] or {}).get("job_id") == jid, 60,
         what="the rebuilt result")
    check(job_row(old)[0] == "superseded", "the old job is superseded")
    check(objects(BUCKET, f"reconstruction/{old}/") == {}, "the old prefix is removed")
    send_node(Mqtt(), 6, 5.0, 0.0, 0.0)
    wait(lambda: by_seq(6) and by_seq(6)[0].get("depth"), what="node 6 with depth")
    time.sleep(11)  # the stale cache
    s = status()["reconstruction"]
    check(s["stale"] is True and s["stale_reason"] == {"new_nodes": 1, "removed_nodes": 0,
                                                       "moved_nodes": 0},
          f"stale after a new node: {s['stale_reason']}")


def cancel():
    current = status()["reconstruction"]["job_id"]
    stub("POST", "/control", json={"mode": "hold"})
    jid = start()["job_id"]
    wait(lambda: (job_row(jid) or ("", 0, None, None))[3] == "integrating",
         what="the job running")
    r = api("POST", f"/api/v1/maps/{MAP}/reconstruction/cancel")
    check(r.status_code == 200 and r.json()["cancel_requested"], "cancel requested")
    row = wait_state(jid, ("cancelled",), 30)
    check(row[2]["reason"] == "cancelled", "job cancelled by the stub's fail(cancelled)")
    rec = stub("GET", "/record").json()
    check(any(c.get("kind") == "cancel-received" and c["job_id"] == jid
              for c in rec["callbacks"]), "the service got POST /jobs/{id}/cancel")
    check(status()["reconstruction"]["job_id"] == current, "the previous result stays")
    check(len(events("MAP.RECONSTRUCTION_FAILED", jid)) == 1, "FAILED event (cancelled)")
    stub("POST", "/control", json={"mode": "normal"})


def down():
    current = status()["reconstruction"]["job_id"]
    jid = start()["job_id"]
    wait(lambda: status()["job"] and status()["job"]["waiting_for_service"], 20,
         what="waiting_for_service")
    check(True, "service down: the job waits queued (waiting_for_service)")
    row = wait_state(jid, ("failed",), 90)
    check(row[2]["reason"] == "service_unavailable", f"then failed service_unavailable: {row[2]}")
    check(status()["reconstruction"]["job_id"] == current, "the previous result stays")


def lost():
    wait(lambda: httpx.get(STUB + "/health", timeout=3).status_code == 200, 30,
         what="the stub back")
    stub("POST", "/control", json={"mode": "forget"})
    jid = start()["job_id"]
    wait_state(jid, ("running",), 30)
    check(True, "the stub accepted and forgot the job")
    row = wait_state(jid, ("succeeded",), 150)
    check(row[1] == 2, f"poll 404 -> resubmitted, succeeded at attempt {row[1]}")
    rec = stub("GET", "/record").json()
    check(sorted(m["attempt"] for m in rec["manifests"] if m["job_id"] == jid) == [1, 2],
          "the service saw attempts 1 and 2")


def delete():
    sid = query("SELECT session_id FROM map_sessions WHERE map_name = %s AND ended_at IS NULL",
                (MAP,))[0][0]
    r = api("POST", f"/api/v1/maps/{MAP}/sessions/{sid}/finish")
    check(r.status_code == 200, "session finished (a map with an open session can't be deleted)")
    stub("POST", "/control", json={"mode": "deaf"})
    jid = start()["job_id"]
    wait(lambda: (job_row(jid) or ("", 0, None, None))[3] == "integrating",
         what="the job running")
    r = api("DELETE", f"/api/v1/maps/{MAP}")
    check(r.status_code == 202, "DELETE map -> 202")
    ev = wait(lambda: events("MAP.RECONSTRUCTION_FAILED", jid), 20, what="the FAILED event")
    check(ev[0]["reason"] == "map_deleting", "job cancelled by the map delete (map_deleting)")
    wait(lambda: not query("SELECT 1 FROM mapobjectv1 WHERE name = %s", (MAP,)), 60,
         what="the map deleted")
    check(not minio().bucket_exists(BUCKET), "the map bucket is gone")
    check(query("SELECT count(*) FROM map_reconstructions WHERE map_name = %s",
                (MAP,))[0][0] == 0, "the map's reconstruction rows are gone")
    stub("POST", "/control", json={"release": jid})
    wait(lambda: any(c["job_id"] == jid and c["kind"] == "finish"
                     for c in stub("GET", "/record").json()["callbacks"]), 30,
         what="the late finish")
    rec = stub("GET", "/record").json()
    finish = [c for c in rec["callbacks"] if c["job_id"] == jid and c["kind"] == "finish"][0]
    check(finish["answer"][0] == 410, f"late finish -> 410 {finish['answer']}")
    check(all(p["status"] == 200 for p in rec["puts"] if p["job_id"] == jid)
          and objects("recon-staging", f"{jid}/"), "the late PUTs landed in staging (harmless)")
    time.sleep(2)
    check(not minio().bucket_exists(BUCKET), "the map bucket was not re-created")


STEPS = {"seed": seed, "build": build, "rebuild": rebuild, "cancel": cancel, "down": down,
         "lost": lost, "delete": delete}

if __name__ == "__main__":
    STEPS[sys.argv[1]]()
    print(f"{sys.argv[1]}: passed")
