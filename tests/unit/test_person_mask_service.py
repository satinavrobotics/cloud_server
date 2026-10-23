"""The optional person-mask service (packages/services/person_mask): the YOLO-seg decoding on synthetic network
outputs, the lazy load / unload of the models, and the HTTP routes with a fake model. No model weights and no
onnxruntime / torch are needed: the real ONNX path was checked by hand against ultralytics (docs/reconstruction/
person_removal.md, 'Verification')."""
import io

import httpx
import numpy as np
import pytest
from PIL import Image

from packages.services.person_mask import segmenter as sg
from packages.services.person_mask import server

pytestmark = pytest.mark.unit

NM, PH, PW, SIZE = 4, 16, 16, 64


def proto_with_blobs():
    """Mask prototypes: channel 0 is +8 inside a block of rows 4-12, cols 2-6 (proto px), -8 elsewhere."""
    proto = np.full((1, NM, PH, PW), -8.0, np.float32)
    proto[0, 0, 4:12, 2:6] = 8.0
    proto[0, 1, :, :] = 8.0                                     # an "everything" prototype, never selected
    return proto


def classic_output(rows):
    """[1, 4 + nc + nm, N] from rows (cx, cy, w, h, scores[nc], coefs[nm])."""
    return np.array(rows, np.float32).T[None]


def test_classic_head_keeps_persons_only_crops_to_the_box_and_suppresses_duplicates():
    nc = 3
    person = [16, 32, 16, 32] + [0.9, 0.05, 0.0] + [1, 0, 0, 0]       # box x 8-24, y 16-48 (input px)
    dup = [17, 32, 16, 32] + [0.8, 0.0, 0.0] + [1, 0, 0, 0]          # overlaps: removed by NMS
    chair = [48, 32, 16, 16] + [0.1, 0.95, 0.0] + [0, 1, 0, 0]       # another class: ignored
    weak = [48, 48, 8, 8] + [0.2, 0.0, 0.0] + [0, 1, 0, 0]           # below conf
    out0 = classic_output([person, dup, chair, weak])
    assert out0.shape == (1, 4 + nc + NM, 4)
    mask, scores = sg.yolo_seg_postprocess(out0, proto_with_blobs(), input_size=SIZE, orig_hw=(SIZE, SIZE),
                                           scale=1.0, pad=(0, 0))
    assert scores == [pytest.approx(0.9)]
    # prototype block rows 4-12, cols 2-6 (x4 = input rows 16-48, cols 8-24), inside the box: kept
    assert mask[20:44, 10:22].all()
    assert not mask[:, 30:].any() and not mask[:12, :].any()


def test_box_crop_removes_mask_outside_the_box():
    row = [8, 32, 8, 32] + [0.9] + [1, 0, 0, 0]                      # box x 4-12 only (input px), nc = 1
    mask, _ = sg.yolo_seg_postprocess(classic_output([row]), proto_with_blobs(), input_size=SIZE,
                                      orig_hw=(SIZE, SIZE), scale=1.0, pad=(0, 0))
    assert mask[20:44, 8:11].all() and not mask[:, 14:].any()


def test_end_to_end_head_and_letterbox_padding():
    # a 32 x 64 (h x w) image letterboxed into 64: scale 1, padding 16 rows on top
    e2e = np.zeros((1, 3, 6 + NM), np.float32)
    e2e[0, 0] = [8, 16, 24, 48, 0.9, 0] + [1, 0, 0, 0]                 # person, xyxy in input px
    e2e[0, 1] = [0, 0, 64, 64, 0.95, 2] + [0, 1, 0, 0]                # not a person
    mask, scores = sg.yolo_seg_postprocess(e2e, proto_with_blobs(), input_size=SIZE, orig_hw=(32, 64), scale=1.0,
                                           pad=(0, 16))
    assert mask.shape == (32, 64) and scores == [pytest.approx(0.9)]
    # input rows 16-48 -> image rows 0-32
    assert mask[2:30, 10:22].all() and not mask[:, 30:].any()


def test_no_detection_gives_an_empty_mask():
    out0 = classic_output([[8, 8, 4, 4, 0.01, 0, 0, 0, 0]])
    mask, scores = sg.yolo_seg_postprocess(out0, proto_with_blobs(), input_size=SIZE, orig_hw=(10, 20), scale=0.3,
                                           pad=(0, 0))
    assert mask.shape == (10, 20) and not mask.any() and scores == []
    with pytest.raises(ValueError):
        sg.yolo_seg_postprocess(np.zeros((1, 5, 3), np.float32), proto_with_blobs(), input_size=SIZE,
                                orig_hw=(10, 10), scale=1, pad=(0, 0))


def test_letterbox_and_nms():
    rgb = np.zeros((30, 60, 3), np.uint8)
    blob, scale, pad = sg.letterbox(rgb, 64)
    assert blob.shape == (1, 3, 64, 64) and blob.dtype == np.float32
    assert scale == pytest.approx(64 / 60) and pad == (0, 16)
    assert blob[0, 0, 0, 0] == pytest.approx(114 / 255) and blob[0, 0, 32, 32] == 0
    boxes = np.array([[0, 0, 10, 10], [1, 1, 10, 10], [20, 20, 30, 30]], np.float32)
    assert sorted(sg.nms(boxes, np.array([0.9, 0.8, 0.7]), 0.5)) == [0, 2]


def test_specs():
    assert sg.parse_spec("onnx:/m/a.onnx") == ("onnx", "/m/a.onnx")
    for bad in ("", "onnx", "torch:/a", "onnx:"):
        with pytest.raises(ValueError):
            sg.parse_spec(bad)
    assert sg.parse_models("fast=onnx:/a.onnx; quality=ultralytics:/b.pt\n") == {
        "fast": "onnx:/a.onnx", "quality": "ultralytics:/b.pt"}
    with pytest.raises(ValueError):
        sg.parse_models("onnx:/a.onnx")


def test_a_missing_package_or_weights_is_a_clear_unavailable(tmp_path):
    with pytest.raises(sg.SegmenterUnavailable, match="not found"):
        sg.OnnxYoloSeg(str(tmp_path / "none.onnx")).load()
    f = tmp_path / "m.onnx"
    f.write_bytes(b"x")
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        with pytest.raises(sg.SegmenterUnavailable, match="onnxruntime"):
            sg.OnnxYoloSeg(str(f)).load()


class FakeBackend(sg.Backend):
    kind = "fake"
    loaded = 0
    closed = 0

    def load(self):
        FakeBackend.loaded += 1

    def predict(self, rgb):
        mask = (rgb[:, :, 0] == 255) & (rgb[:, :, 1] == 0)
        return mask, [0.9] if mask.any() else []

    def close(self):
        FakeBackend.closed += 1


def red_image(fmt="PNG"):
    arr = np.full((40, 50, 3), 128, np.uint8)
    arr[10:30, 20:40] = (255, 0, 0)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG" if fmt == "PNG" else fmt)
    return buf.getvalue()


def test_lazy_segmenter_loads_on_first_use_and_unloads_when_idle():
    FakeBackend.loaded = FakeBackend.closed = 0
    now = {"t": 0.0}
    seg = sg.LazySegmenter("fake", lambda: FakeBackend("x"), clock=lambda: now["t"])
    assert not seg.loaded and FakeBackend.loaded == 0
    rgb = sg.decode_rgb(red_image("PNG"))
    out = seg.segment(rgb)
    assert seg.loaded and out.persons == 1 and out.mask[15, 25] and not out.mask[0, 0]
    seg.segment(rgb)
    assert FakeBackend.loaded == 1 and seg.frames == 2
    now["t"] = 30
    assert seg.unload_if_idle(60) is False and seg.loaded
    now["t"] = 100
    assert seg.unload_if_idle(60) is True and not seg.loaded and FakeBackend.closed == 1
    assert seg.unload() is False
    seg.segment(rgb)
    assert FakeBackend.loaded == 2 and seg.loads == 2


def test_a_failed_load_leaves_nothing_resident():
    class Broken(FakeBackend):
        def load(self):
            raise sg.SegmenterUnavailable("no weights")
    seg = sg.LazySegmenter("b", lambda: Broken("x"))
    with pytest.raises(sg.SegmenterUnavailable):
        seg.segment(np.zeros((4, 4, 3), np.uint8))
    assert not seg.loaded


def test_pool_keeps_one_model_resident():
    pool = sg.ModelPool({"a": "onnx:/a", "b": "onnx:/b"}, "b", backend_factory=lambda n, s: FakeBackend(s))
    rgb = np.zeros((4, 4, 3), np.uint8)
    pool.get(None).segment(rgb)
    assert pool.segmenters["b"].loaded and not pool.segmenters["a"].loaded
    pool.get("a").segment(rgb)
    assert pool.segmenters["a"].loaded and not pool.segmenters["b"].loaded
    with pytest.raises(KeyError):
        pool.get("c")
    assert pool.unload_all() == ["a"]
    names = [m["name"] for m in pool.describe()]
    assert names == ["a", "b"] and [m["default"] for m in pool.describe()] == [False, True]
    with pytest.raises(ValueError):
        sg.ModelPool({})


def make_app(key="", unavailable=False):
    class Settings:
        pass
    s = Settings()
    s.key, s.device, s.idle_unload_s = key, "cpu", 0

    def factory(name, spec):
        if unavailable:
            class Missing(FakeBackend):
                def load(self):
                    raise sg.SegmenterUnavailable("the 'onnxruntime' package is not installed")
            return Missing(spec)
        return FakeBackend(spec)
    pool = sg.ModelPool({"nano": "onnx:/models/n.onnx"}, None, backend_factory=factory)
    return server.create_app(s, pool)


async def test_routes():
    app = make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/health")
        assert r.status_code == 200 and r.json()["default_model"] == "nano"
        assert r.json()["models"][0]["present"] is False                  # no weights in tests
        r = await c.post("/v1/segment", content=red_image())
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert r.headers["X-Person-Count"] == "1" and r.headers["X-Model"] == "nano"
        with Image.open(io.BytesIO(r.content)) as im:
            m = np.array(im)
        assert m.shape == (40, 50) and m[20, 30] == 255 and m[0, 0] == 0
        r = await c.post("/v1/segment?model=big", content=red_image())
        assert r.status_code == 404 and r.json()["detail"]["code"] == "unknown_model"
        r = await c.post("/v1/segment", content=b"garbage")
        assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_image"
        r = await c.post("/v1/segment", content=b"")
        assert r.status_code == 400
        r = await c.post("/v1/unload")
        assert r.json() == {"unloaded": ["nano"]}


async def test_auth_and_unavailable_model():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(key="k")), base_url="http://t") as c:
        assert (await c.get("/health")).status_code == 200
        assert (await c.post("/v1/segment", content=red_image())).status_code == 401
        r = await c.post("/v1/segment", content=red_image(), headers={"Authorization": "Bearer k"})
        assert r.status_code == 200
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(unavailable=True)),
                                 base_url="http://t") as c:
        r = await c.post("/v1/segment", content=red_image())
        assert r.status_code == 503 and r.json()["detail"]["code"] == "model_unavailable"
        assert "onnxruntime" in r.json()["detail"]["message"]


def test_settings_from_env():
    s = server.Settings({"PERSON_MASK_MODEL": "onnx:/m/a.onnx", "PERSON_MASK_THREADS": "2"})
    assert s.models == {"default": "onnx:/m/a.onnx"} and s.threads == 2 and s.idle_unload_s == 120
    s = server.Settings({"PERSON_MASK_MODELS": "n=onnx:/a;l=ultralytics:/b", "PERSON_MASK_DEFAULT_MODEL": "l"})
    assert list(s.models) == ["n", "l"] and s.default_model == "l"
