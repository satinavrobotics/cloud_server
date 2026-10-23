"""Person removal (packages/api/person_removal.py): the masking rules on synthetic frames, the manifests of the
reconstruction and mesh gateways, the job with a fake segmentation model, the settings, the client and the routes.

The segmentation model is a fake: it marks the pure-red pixels of the synthetic RGB as a person, so every expected
mask is known exactly. The real model wrapper is tested in test_person_mask_service.py.
"""
import asyncio
import datetime
import io
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from PIL import Image  # noqa: E402

from packages.api import mesh  # noqa: E402
from packages.api import person_removal as pr  # noqa: E402
from packages.api import reconstruction as rc  # noqa: E402

pytestmark = pytest.mark.unit

DEPTH_H, DEPTH_W = 120, 160          # the depth image
RGB_H, RGB_W = 240, 320              # the RGB is twice the size (same field of view)
CAMERA = {"frame_id": "camera", "width": DEPTH_W, "height": DEPTH_H, "fx": 100.0, "fy": 100.0, "cx": 80.0,
          "cy": 60.0, "distortion_model": "plumb_bob", "d": [0, 0, 0, 0, 0], "depth_type": "z",
          "valid_range_m": [0.2, 15.0], "rgb_width": RGB_W, "rgb_height": RGB_H,
          "T_base_cam": {"x": 0.1, "y": 0.0, "z": 0.45, "qx": -0.5, "qy": 0.5, "qz": -0.5, "qw": 0.5}}


# --- synthetic frames ---------------------------------------------------------------------------------------------

def rgb_jpeg(person=None):
    """Grey scene; `person` = (top, left, bottom, right) in RGB pixels painted pure red. PNG (lossless) so the fake
    model's red test is exact."""
    arr = np.full((RGB_H, RGB_W, 3), 128, np.uint8)
    if person:
        t, l, b, r = person
        arr[t:b, l:r] = (255, 0, 0)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def depth_png(value_mm=2000):
    arr = np.full((DEPTH_H, DEPTH_W), value_mm, np.uint16)
    arr[:, :10] = 0                                   # some pixels without data already
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def decode(png):
    with Image.open(io.BytesIO(png)) as im:
        return np.array(im)


def fake_model_mask(image_bytes):
    """The fake segmentation model: red pixels are a person. Returns (mask PNG at RGB size, persons)."""
    with Image.open(io.BytesIO(image_bytes)) as im:
        arr = np.asarray(im.convert("RGB"))
    mask = (arr[:, :, 0] == 255) & (arr[:, :, 1] == 0)
    buf = io.BytesIO()
    Image.fromarray(np.where(mask, 255, 0).astype(np.uint8), mode="L").save(buf, format="PNG")
    return buf.getvalue(), int(mask.any())


# --- pure rules ---------------------------------------------------------------------------------------------------

def test_dilate_grows_a_pixel_into_a_square_and_keeps_the_input():
    m = np.zeros((9, 9), bool)
    m[4, 4] = True
    d = pr.dilate(m, 2)
    assert d.sum() == 25 and d[2:7, 2:7].all() and m.sum() == 1
    assert (pr.dilate(m, 0) == m).all()
    edge = np.zeros((5, 5), bool)
    edge[0, 0] = True
    assert pr.dilate(edge, 1).sum() == 4                       # clipped at the border, no wrap-around
    assert not pr.dilate(np.zeros((3, 3), bool), 3).any()


def test_fit_mask_is_conservative_when_downsampling():
    m = np.zeros((RGB_H, RGB_W), bool)
    m[100, 50:60] = True                                       # a one-pixel line in the RGB
    small = pr.fit_mask(m, DEPTH_H, DEPTH_W)
    assert small.shape == (DEPTH_H, DEPTH_W) and small[50, 25:30].all()
    assert (pr.fit_mask(small, DEPTH_H, DEPTH_W) == small).all()


def test_derive_masks_the_person_and_leaves_the_raw_depth_alone():
    raw = depth_png()
    rgb_mask, _ = fake_model_mask(rgb_jpeg(person=(80, 100, 200, 160)))   # RGB rows 80-200, cols 100-160
    out = pr.derive(rgb_mask, raw, dilate_px=3)
    mask, masked = decode(out.mask_png) > 0, decode(out.depth_png)
    # a 16-bit greyscale PNG (IHDR bit depth 16, colour type 0) as the services require; Pillow reads it as int32
    assert (out.depth_png[24], out.depth_png[25]) == (16, 0) and masked.shape == (DEPTH_H, DEPTH_W)
    assert (out.mask_png[24], out.mask_png[25]) == (8, 0)
    # the person in depth pixels: rows 40-100, cols 50-80, grown by 3 (and by the conservative resize, at most 1)
    assert mask[40:100, 50:80].all()
    assert not mask[:36, :].any() and not mask[105:, :].any() and not mask[:, :46].any() and not mask[:, 85:].any()
    assert (masked[mask] == 0).all()
    untouched = ~mask
    assert (masked[untouched] == decode(raw)[untouched]).all()   # everything else exactly as recorded
    assert decode(raw)[50, 60] == 2000                           # the input bytes were not modified
    assert out.pixels == int(mask.sum()) and 0 < out.ratio < 1


def test_derive_without_a_person_writes_nothing():
    out = pr.derive(fake_model_mask(rgb_jpeg())[0], depth_png(), 6)
    assert (out.pixels, out.mask_png, out.depth_png, out.width, out.height) == (0, None, None, DEPTH_W, DEPTH_H)


def test_derive_rejects_unreadable_inputs():
    with pytest.raises(ValueError):
        pr.derive(b"not a png", depth_png(), 3)
    with pytest.raises(ValueError):
        pr.derive(fake_model_mask(rgb_jpeg((0, 0, 10, 10)))[0], b"nope", 3)


def test_frame_state_and_is_done():
    assert pr.frame_state(None) == pr.UNCHECKED
    assert pr.frame_state({"version": 99, "pixels": 5}) == pr.UNCHECKED
    assert pr.frame_state({"version": pr.VERSION, "pixels": 0}) == pr.CLEAR
    assert pr.frame_state({"version": pr.VERSION, "pixels": 12}) == pr.MASKED
    rec = {"version": pr.VERSION, "pixels": 0, "model": "nano", "dilate_px": 6}
    assert pr.is_done(rec, "nano", 6) and not pr.is_done(rec, "large", 6) and not pr.is_done(rec, "nano", 4)
    assert not pr.is_done(None, "nano", 6)


def test_effective_settings():
    d = pr.Defaults(enabled=False, model="", dilate_px=6)
    assert pr.effective(None, d) == {"enabled": False, "source": "server", "model": None, "dilate_px": 6}
    assert pr.effective({"enabled": True, "model": "large", "dilate_px": 2}, d) == {
        "enabled": True, "source": "map", "model": "large", "dilate_px": 2}
    on = pr.Defaults(enabled=True, model="nano", dilate_px=4)
    assert pr.effective({"enabled": None, "model": None, "dilate_px": None}, on) == {
        "enabled": True, "source": "server", "model": "nano", "dilate_px": 4}
    assert pr.effective({"enabled": False}, on)["enabled"] is False


def test_bodies_are_validated():
    with pytest.raises(HTTPException) as e:
        pr.parse(pr.SettingsBody, {"enabled": True, "nope": 1})
    assert e.value.status_code == 422
    with pytest.raises(HTTPException):
        pr.parse(pr.SettingsBody, {"model": "bad name!"})
    with pytest.raises(HTTPException):
        pr.parse(pr.RunBody, {"dilate_px": 65})
    with pytest.raises(HTTPException):
        pr.parse(pr.RunBody, [1])
    assert pr.parse(pr.RunBody, None).force is False
    assert pr.parse(pr.RunBody, {"model": "yolo11n-seg.v2", "force": True}).model == "yolo11n-seg.v2"


# --- manifests ----------------------------------------------------------------------------------------------------

MASKED_REC = {"version": pr.VERSION, "pixels": 400, "model": "nano", "dilate_px": 6}
CLEAR_REC = {"version": pr.VERSION, "pixels": 0, "model": "nano", "dilate_px": 6}


def node_doc(key, masks=None):
    doc = {"_key": key, "node_id": key, "pose": {"x": 1.0, "y": 2.0, "yaw": 0.0}, "created_at": f"2026-10-23T{key}",
           "session_id": "s1", "depth": {"left": {"camera": CAMERA, "depth_scale": 0.001}}}
    if masks is not None:
        doc["person_mask"] = masks
    return doc


def presign(bucket, key):
    return f"http://minio/{bucket}/{key}?sig"


def manifest_kwargs(frames, person_removal):
    return dict(job_id="j1", attempt=1, map_name="lab", map_type="local", crs=None,
                frames=frames, map_bucket="map-lab", staging_bucket="st", presign_get=presign,
                presign_put=presign, callback_base_url="http://api", token="t",
                now=datetime.datetime(2026, 10, 23, tzinfo=datetime.timezone.utc), expiry_s=60,
                person_removal=person_removal)


def frames():
    return rc.frames_from_nodes([node_doc("10", {"left": MASKED_REC}), node_doc("11", {"left": CLEAR_REC}),
                                 node_doc("12"), node_doc("13", {"right": MASKED_REC})])


def test_frames_carry_the_mask_records_of_their_cameras_only():
    fs = {f.node_id: f for f in frames()}
    assert fs["10"].person_masks == {"left": MASKED_REC} and fs["12"].person_masks == {}
    assert fs["13"].person_masks == {}                          # a record of a camera without depth is ignored
    # the stale digest does not depend on the masks
    assert rc.input_digest(frames()) == rc.input_digest(rc.frames_from_nodes(
        [node_doc(k) for k in ("10", "11", "12", "13")]))


@pytest.mark.parametrize("builder", [rc.build_manifest, mesh.build_manifest])
def test_manifest_uses_masked_depth_only_when_on_and_only_for_frames_with_people(builder):
    on = builder(params={"voxel_m": 0.05, "person_removal": True}, **manifest_kwargs(frames(), True))
    cams = {n["node_id"]: n["cameras"][0] for n in on["nodes"]}
    assert cams["10"]["depth_url"] == "http://minio/map-lab/10/depth_masked/left.png?sig"
    assert cams["10"]["mask_url"] == "http://minio/map-lab/10/person_mask/left.png?sig"
    assert cams["10"]["rgb_url"] == "http://minio/map-lab/10/images/left?sig"     # the photo is never replaced
    for k in ("11", "12", "13"):
        assert cams[k]["depth_url"] == f"http://minio/map-lab/{k}/depth/left.png?sig" and "mask_url" not in cams[k]
    assert "person_removal" not in on["params"]                                      # gateway-only
    off = builder(params={"voxel_m": 0.05, "person_removal": False}, **manifest_kwargs(frames(), False))
    assert all(n["cameras"][0]["depth_url"].endswith("/depth/left.png?sig") and "mask_url" not in n["cameras"][0]
               for n in off["nodes"])


def test_summarize():
    assert pr.summarize(frames(), True) == {"enabled": True, "masked": 1, "clear": 1, "unchecked": 2}
    assert pr.summarize(frames(), False) == {"enabled": False, "masked": 0, "clear": 0, "unchecked": 0}


def test_start_bodies_accept_person_removal():
    cfg = rc.ReconConfig.from_env()
    assert rc.job_params(cfg, {"person_removal": True})["person_removal"] is True
    assert "person_removal" not in rc.job_params(cfg, {})
    assert mesh.job_params(mesh.MeshConfig.from_env(), {"person_removal": False})["person_removal"] is False
    with pytest.raises(HTTPException):
        rc.job_params(cfg, {"person_removal": "maybe"})


async def test_gateway_resolves_person_removal_from_the_run_then_the_policy():
    gw = rc.ReconstructionGateway.__new__(rc.ReconstructionGateway)
    gw.person_removal_policy = None
    params = {}
    await gw._resolve_person_removal("lab", params)
    assert params == {"person_removal": False}                   # no policy: off

    asked = []

    async def policy(map_name):
        asked.append(map_name)
        return True
    gw.person_removal_policy = policy
    params = {}
    await gw._resolve_person_removal("lab", params)
    assert params == {"person_removal": True} and asked == ["lab"]
    params = {"person_removal": False}                           # the run wins
    await gw._resolve_person_removal("lab", params)
    assert params == {"person_removal": False} and asked == ["lab"]

    async def broken(map_name):
        raise RuntimeError("postgres down")
    gw.person_removal_policy = broken
    params = {}
    await gw._resolve_person_removal("lab", params)
    assert params == {"person_removal": False}                   # never blocks a job


# --- the job ------------------------------------------------------------------------------------------------------

def job_node(key, session="s1", cameras=("left",), masks=None):
    doc = {"_key": key, "node_id": key, "session_id": session,
           "depth": {c: {"camera": CAMERA, "depth_scale": 0.001, "session_id": session} for c in cameras}}
    if masks:
        doc["person_mask"] = masks
    return doc


class FakeSources:
    def __init__(self, nodes, sessions=(), row=("ALIVE", None)):
        self.nodes = {n["_key"]: n for n in nodes}
        self.rgb = {}
        self.depth = {}
        for n in nodes:
            for c in n["depth"]:
                self.rgb[(n["_key"], c)] = rgb_jpeg()
                self.depth[(n["_key"], c)] = depth_png()
        self.sess, self.row = list(sessions), row
        self.objects, self.calls = {}, []
        self.fail_store = False

    def list_nodes(self, map_name):
        return [json.loads(json.dumps(n)) for n in self.nodes.values()]

    def coverage(self, map_name):
        frames = checked = masked = 0
        for n in self.nodes.values():
            for c in n["depth"]:
                frames += 1
                st = pr.frame_state((n.get("person_mask") or {}).get(c))
                checked += st != pr.UNCHECKED
                masked += st == pr.MASKED
        return {"frames": frames, "checked": checked, "masked": masked}

    def get_rgb(self, map_name, node_id, camera):
        return self.rgb.get((node_id, camera))

    def get_depth(self, map_name, node_id, camera):
        return self.depth.get((node_id, camera))

    def store(self, map_name, node_id, key, png):
        self.calls.append(("png", key))
        if self.fail_store:
            return False
        self.objects[key] = png
        return True

    def set_records(self, map_name, key, records):
        self.calls.append(("record", key))
        self.nodes[key].setdefault("person_mask", {}).update(records)
        return True

    async def map_row(self, map_name):
        return self.row if map_name == "lab" else None

    async def sessions(self, map_name, session_id):
        rows = [r for r in self.sess if session_id is None or r["session_id"] == session_id]
        return rows if session_id is not None else [r for r in rows if r.get("ended_at") is None]


class FakeClient:
    """The person-mask service with the fake red-pixel model."""

    def __init__(self, models=("nano", "large"), default="nano", fatal_after=None, bad=()):
        self.models, self.default = list(models), default
        self.segmented, self.unloads = [], 0
        self.fatal_after, self.bad = fatal_after, set(bad)

    def health(self):
        return {"default_model": self.default,
                "models": [{"name": m, "present": True, "path": f"/models/{m}"} for m in self.models]}

    def segment(self, image, model):
        if self.fatal_after is not None and len(self.segmented) >= self.fatal_after:
            raise pr.ServiceError("service_unavailable", "gone", True)
        self.segmented.append(model)
        if image in self.bad:
            raise pr.ServiceError("bad_image", "cannot decode", False)
        png, persons = fake_model_mask(image)
        return png, persons, model

    def unload(self):
        self.unloads += 1


class FakeSettings:
    def __init__(self, stored=None, defaults=None):
        self.stored = stored
        self.defaults = defaults or pr.Defaults()

    async def get(self, map_name):
        return self.stored

    async def put(self, map_name, body):
        doc = body.dict()
        self.stored = None if all(v is None for v in doc.values()) else doc
        return self.stored or {}

    async def effective(self, map_name):
        return pr.effective(self.stored, self.defaults)

    async def enabled_for(self, map_name):
        return (await self.effective(map_name))["enabled"]


async def inline(fn, *a):
    return fn(*a)


def make(src, client=None, settings=None):
    return pr.PersonRemoval(src, FakeClient() if client is None else client, settings or FakeSettings(),
                            to_thread=inline)


async def wait(ctl, map_name="lab"):
    await asyncio.wait_for(ctl.tasks[map_name], 10)
    return (await ctl.status(map_name))["job"]


def finished(sid="s1"):
    return {"session_id": sid, "purpose": "mapping", "paused_at": None, "ended_at": "2026-10-22T10:00:00"}


def recording(sid="s1"):
    return {"session_id": sid, "purpose": "mapping", "paused_at": None, "ended_at": None}


async def test_a_run_masks_frames_with_people_and_records_every_frame():
    src = FakeSources([job_node("a", cameras=("left", "right")), job_node("b")])
    src.rgb[("a", "left")] = rgb_jpeg(person=(80, 100, 200, 160))
    ctl = make(src)
    status, view = await ctl.start("lab", {})
    assert status == 202 and view["state"] == pr.QUEUED
    job = await wait(ctl)
    assert job["state"] == pr.SUCCEEDED and job["model"] == "nano"
    assert (job["frames_total"], job["masked"], job["clear"], job["persons"], job["failed"]) == (3, 1, 2, 1, 0)
    assert set(src.objects) == {"a/person_mask/left.png", "a/depth_masked/left.png"}   # nothing for clear frames
    # the PNGs before the record that makes a manifest use them
    assert src.calls[:3] == [("png", "a/person_mask/left.png"), ("png", "a/depth_masked/left.png"), ("record", "a")]
    rec = src.nodes["a"]["person_mask"]["left"]
    assert rec["persons"] == 1 and rec["pixels"] > 0 and rec["model"] == "nano" and rec["dilate_px"] == 6
    assert src.nodes["b"]["person_mask"]["left"]["pixels"] == 0
    assert decode(src.depth[("a", "left")])[60, 60] == 2000        # raw depth untouched
    assert decode(src.objects["a/depth_masked/left.png"])[60, 60] == 0
    assert ctl.client.unloads == 1                                 # the model is freed after the run
    st = await ctl.status("lab")
    assert st["coverage"] == {"frames": 3, "checked": 3, "masked": 1} and st["available"] is True
    assert any("off for this map" in w for w in job["warnings"])   # masks written, but the map does not use them


async def test_a_second_run_skips_what_is_done_unless_forced_or_the_model_changed():
    src = FakeSources([job_node("a"), job_node("b")])
    ctl = make(src)
    await ctl.start("lab", None)
    await wait(ctl)
    await ctl.start("lab", {})
    job = await wait(ctl)
    assert (job["frames_total"], job["skipped"]) == (0, {pr.SKIP_DONE: 2}) and job["state"] == pr.SUCCEEDED
    await ctl.start("lab", {"force": True})
    assert (await wait(ctl))["frames_total"] == 2
    await ctl.start("lab", {"model": "large"})
    job = await wait(ctl)
    assert job["frames_total"] == 2 and job["model"] == "large" and ctl.client.segmented[-1] == "large"
    await ctl.start("lab", {"dilate_px": 2})
    assert (await wait(ctl))["frames_total"] == 2


async def test_the_map_setting_supplies_model_and_dilation():
    src = FakeSources([job_node("a")])
    ctl = make(src, settings=FakeSettings({"enabled": True, "model": "large", "dilate_px": 1}))
    await ctl.start("lab", {})
    job = await wait(ctl)
    assert (job["model"], job["dilate_px"], job["warnings"]) == ("large", 1, [])


async def test_frame_failures_are_counted_and_a_fatal_service_error_fails_the_job():
    src = FakeSources([job_node("a"), job_node("b"), job_node("c")])
    del src.rgb[("a", "left")]
    del src.depth[("b", "left")]
    ctl = make(src)
    await ctl.start("lab", {})
    job = await wait(ctl)
    assert job["state"] == pr.SUCCEEDED and job["failed_by_reason"] == {pr.FAIL_RGB: 1, pr.FAIL_DEPTH: 1}
    assert job["clear"] == 1

    src = FakeSources([job_node("a"), job_node("b")])
    ctl = make(src, client=FakeClient(fatal_after=1))
    await ctl.start("lab", {})
    job = await wait(ctl)
    assert job["state"] == pr.FAILED and job["error"]["reason"] == "service_unavailable"
    assert ctl.client.unloads == 1

    src = FakeSources([job_node("a")])
    bad = rgb_jpeg(person=(0, 0, 5, 5))
    src.rgb[("a", "left")] = bad
    ctl = make(src, client=FakeClient(bad=[bad]))
    await ctl.start("lab", {})
    job = await wait(ctl)
    assert job["state"] == pr.FAILED and job["error"]["reason"] == "nothing_written"
    assert job["failed_by_reason"] == {pr.FAIL_SEGMENT: 1}

    src = FakeSources([job_node("a")])
    src.rgb[("a", "left")] = rgb_jpeg(person=(0, 0, 50, 50))
    src.fail_store = True
    ctl = make(src)
    await ctl.start("lab", {})
    job = await wait(ctl)
    assert job["failed_by_reason"] == {pr.FAIL_STORE: 1} and "person_mask" not in src.nodes["a"]


async def test_unknown_or_missing_model_fails_before_any_frame():
    src = FakeSources([job_node("a")])
    client = FakeClient()
    ctl = make(src, client=client)
    await ctl.start("lab", {"model": "huge"})
    job = await wait(ctl)
    assert job["state"] == pr.FAILED and job["error"]["reason"] == "unknown_model" and client.segmented == []
    with pytest.raises(pr.ServiceError) as e:
        pr.resolve_model({"default_model": "nano", "models": [{"name": "nano", "present": False, "path": "/m"}]}, None)
    assert e.value.code == "model_unavailable" and e.value.fatal


async def test_guards():
    with pytest.raises(HTTPException) as e:
        await pr.PersonRemoval(FakeSources([job_node("a")]), None, FakeSettings()).start("lab", {})
    assert (e.value.status_code, e.value.detail["code"]) == (503, "not_configured")
    with pytest.raises(HTTPException) as e:
        await make(FakeSources([job_node("a")])).start("nope", {})
    assert e.value.detail["code"] == "map_not_found"
    with pytest.raises(HTTPException) as e:
        await make(FakeSources([job_node("a")], row=("DELETING", None))).start("lab", {})
    assert e.value.detail["code"] == "map_deleting"
    with pytest.raises(HTTPException) as e:
        await make(FakeSources([job_node("a")], sessions=[recording()])).start("lab", {})
    assert e.value.detail["code"] == "session_active"
    with pytest.raises(HTTPException) as e:
        await make(FakeSources([job_node("a")], sessions=[finished()])).start("lab", {"session_id": "s9"})
    assert e.value.detail["code"] == "session_not_found"
    with pytest.raises(HTTPException) as e:
        await make(FakeSources([])).start("lab", {})
    assert e.value.detail["code"] == "no_depth"
    with pytest.raises(HTTPException) as e:
        make(FakeSources([])).cancel("lab")
    assert e.value.detail["code"] == "no_active_job"


async def test_session_scope_idempotent_start_and_cancel():
    src = FakeSources([job_node("a", "s1"), job_node("b", "s2")], sessions=[finished("s1"), finished("s2")])
    ctl = make(src)
    s1, v1 = await ctl.start("lab", {"session_id": "s1"})
    s2, v2 = await ctl.start("lab", {"session_id": "s1"})
    assert (s1, s2, v1["job_id"]) == (202, 200, v2["job_id"])
    with pytest.raises(HTTPException) as e:
        await ctl.start("lab", {"session_id": "s2"})
    assert e.value.detail["code"] == "job_active"
    assert ctl.cancel("lab")["cancel_requested"] is True
    job = await wait(ctl)
    assert job["state"] == pr.CANCELLED and "person_mask" not in src.nodes["b"]

    await ctl.start("lab", {"session_id": "s1"})
    job = await wait(ctl)
    assert job["frames_total"] == 1 and "person_mask" in src.nodes["a"] and "person_mask" not in src.nodes["b"]


async def test_settings_roundtrip():
    st = FakeSettings(defaults=pr.Defaults(enabled=False, model="nano", dilate_px=6))
    ctl = make(FakeSources([job_node("a")]), settings=st)
    out = await ctl.put_settings("lab", {"enabled": True})
    assert out["settings"] == {"enabled": True, "source": "map", "model": "nano", "dilate_px": 6}
    assert await st.enabled_for("lab") is True
    out = await ctl.put_settings("lab", {"enabled": None})
    assert out["settings"]["source"] == "server" and out["stored_settings"] is None
    with pytest.raises(HTTPException) as e:
        await ctl.put_settings("nope", {"enabled": True})
    assert e.value.detail["code"] == "map_not_found"


class FakeConn:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, sql, params):
        self.store.sql.append(sql)
        if sql == pr.SettingsStore.GET_SQL:
            val = self.store.rows.get(params[0])
            return SimpleNamespace(fetchone=_async((val,) if val is not None else None))
        if sql == pr.SettingsStore.UPSERT_SQL:
            self.store.rows[params[0]] = json.loads(params[1])
            return SimpleNamespace(fetchone=_async((self.store.rows[params[0]],)))
        if sql == pr.SettingsStore.DELETE_SQL:
            self.store.rows.pop(params[0], None)
            return SimpleNamespace()
        raise AssertionError(sql)


def _async(value):
    async def f():
        return value
    return f


class FakeDb:
    def __init__(self):
        self.rows, self.sql = {}, []

    def connection(self):
        return FakeConn(self)


async def test_settings_store_against_postgres_shapes():
    db = FakeDb()
    store = pr.SettingsStore(db, pr.Defaults(enabled=True, model="", dilate_px=6))
    assert await store.effective("lab") == {"enabled": True, "source": "server", "model": None, "dilate_px": 6}
    await store.put("lab", pr.SettingsBody(enabled=False, dilate_px=3))
    assert db.rows["lab"] == {"enabled": False, "model": None, "dilate_px": 3}
    assert await store.enabled_for("lab") is False
    await store.put("lab", pr.SettingsBody())
    assert "lab" not in db.rows

    class Broken:
        def connection(self):
            raise RuntimeError("relation map_person_removal does not exist")
    assert (await pr.SettingsStore(Broken(), pr.Defaults()).effective("lab"))["enabled"] is False


# --- the client of the person-mask service -------------------------------------------------------------------------

def test_mask_client_answers_and_errors():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, dict(request.url.params), request.headers.get("authorization")))
        if request.url.path == "/health":
            return httpx.Response(200, json={"default_model": "nano", "models": [{"name": "nano", "present": True}]})
        if request.url.path == "/v1/unload":
            return httpx.Response(200, json={"unloaded": ["nano"]})
        model = request.url.params.get("model")
        if model == "bad":
            return httpx.Response(400, json={"detail": {"code": "bad_image", "message": "nope"}})
        if model == "gone":
            return httpx.Response(503, json={"detail": {"code": "model_unavailable", "message": "no onnxruntime"}})
        return httpx.Response(200, content=b"PNG", headers={"X-Person-Count": "2", "X-Model": model or "nano"})

    c = pr.MaskClient("http://pm:8013/", "k", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert c.health()["default_model"] == "nano"
    assert c.segment(b"img", "large") == (b"PNG", 2, "large")
    assert c.segment(b"img", None) == (b"PNG", 2, "nano")
    with pytest.raises(pr.ServiceError) as e:
        c.segment(b"img", "bad")
    assert (e.value.code, e.value.fatal) == ("bad_image", False)
    with pytest.raises(pr.ServiceError) as e:
        c.segment(b"img", "gone")
    assert (e.value.code, e.value.fatal, e.value.message) == ("model_unavailable", True, "no onnxruntime")
    c.unload()
    assert all(s[3] == "Bearer k" for s in seen)


def test_mask_client_retries_then_gives_up():
    tries, sleeps = [], []

    def handler(request):
        tries.append(1)
        if len(tries) < 3:
            raise httpx.ConnectError("refused")
        if len(tries) == 3:
            return httpx.Response(500)
        return httpx.Response(200, content=b"PNG", headers={"X-Person-Count": "0", "X-Model": "nano"})

    c = pr.MaskClient("http://pm", None, transport=httpx.MockTransport(handler), sleep=sleeps.append)
    assert c.segment(b"x", None)[1] == 0 and sleeps == [1.0, 3.0, 10.0]

    def down(request):
        raise httpx.ConnectError("refused")
    c = pr.MaskClient("http://pm", None, transport=httpx.MockTransport(down), sleep=lambda s: None)
    with pytest.raises(pr.ServiceError) as e:
        c.health()
    assert e.value.fatal and e.value.code == "service_unavailable"
    c.unload()                                      # best effort: never raises


# --- routes -------------------------------------------------------------------------------------------------------

async def test_routes():
    import packages.api.main as main
    src = FakeSources([job_node("a")])
    src.rgb[("a", "left")] = rgb_jpeg(person=(10, 10, 60, 60))
    ctl = make(src)
    with patch.object(main, "service", SimpleNamespace(person_removal=ctl)):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t") as client:
            base = "/api/v1/maps/lab/person-removal"
            r = await client.get(base)
            assert r.status_code == 200 and r.json()["job"] is None and r.json()["coverage"]["checked"] == 0
            r = await client.post(base, json={"dilate_px": 2})
            assert r.status_code == 202 and r.json()["dilate_px"] == 2
            r2 = await client.post(base)
            assert r2.status_code == 200 and r2.json()["job_id"] == r.json()["job_id"]
            await asyncio.wait_for(ctl.tasks["lab"], 10)
            r = await client.get(base)
            assert r.json()["job"]["state"] == "succeeded" and r.json()["coverage"]["masked"] == 1
            r = await client.post(base + "/cancel")
            assert r.status_code == 404 and r.json()["detail"]["code"] == "no_active_job"
            r = await client.put(base + "/settings", json={"enabled": True})
            assert r.status_code == 200 and r.json()["settings"]["enabled"] is True
            r = await client.put(base + "/settings", json={"enabled": "yes please"})
            assert r.status_code == 422
            r = await client.post("/api/v1/maps/nope/person-removal", json={})
            assert r.status_code == 404 and r.json()["detail"]["code"] == "map_not_found"
    with patch.object(main, "service", SimpleNamespace()):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t") as client:
            r = await client.get("/api/v1/maps/lab/person-removal")
            assert r.status_code == 503
