"""Person segmentation for dynamic-object removal (docs/reconstruction/person_removal.md).

One RGB frame in, one boolean mask out (True = a person). The backends are loaded LAZILY, on the first frame, and
UNLOADED on request or after an idle time, so the memory is held only while a map is being processed. Nothing heavy is
imported at module import: numpy and Pillow only; onnxruntime / ultralytics / torch / sam2 are imported by the backend
that needs them, and a missing one raises `SegmenterUnavailable` with the package to install.

Backends (`spec` = "<kind>:<path>"):

    onnx:<file.onnx>              YOLO-seg exported to ONNX (ultralytics `export(format="onnx")`, v8/11/26 heads), CPU
                                  onnxruntime. The robot path (arm64, no GPU): only onnxruntime + numpy + Pillow.
    ultralytics:<file.pt>         The same models through ultralytics/torch (GPU when PERSON_MASK_DEVICE=cuda).
    grounded-sam2:<dir>           Grounding DINO prompted with "person." for boxes, SAM 2 for the masks (the cloud box's
                                  Grounded-SAM-2 checkout). GPU. <dir> holds the checkpoints (see `GroundedSam2`).

Only one model is resident at a time (`ModelPool`): switching models unloads the previous one first.
"""
from __future__ import annotations

import ctypes
import gc
import io
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger("person_mask.segmenter")

PERSON_CLASS = 0                  # COCO "person"
DEFAULT_CONF = 0.35
DEFAULT_IOU = 0.5
MASK_THRESHOLD = 0.5


class SegmenterUnavailable(RuntimeError):
    """The backend cannot run here (a package or the weights are missing). The message says what to provide."""


@dataclass
class Segmentation:
    mask: np.ndarray                      # bool [H, W] at the input image's size; True = person
    scores: List[float] = field(default_factory=list)
    model: str = ""
    seconds: float = 0.0

    @property
    def persons(self) -> int:
        return len(self.scores)


# --- image helpers ------------------------------------------------------------------------------------------------

def decode_rgb(data: bytes) -> np.ndarray:
    """uint8 [H, W, 3] RGB of a JPEG/PNG, or ValueError."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as im:
            arr = np.asarray(im.convert("RGB"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"cannot decode the image: {exc}") from exc
    if arr.ndim != 3 or arr.shape[0] == 0 or arr.shape[1] == 0:
        raise ValueError(f"not an RGB image: shape {arr.shape}")
    return arr


def encode_mask_png(mask: np.ndarray) -> bytes:
    """Single-channel PNG, 255 = person, 0 = not."""
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(np.where(mask, 255, 0).astype(np.uint8), mode="L").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def letterbox(rgb: np.ndarray, size: int) -> Tuple[np.ndarray, float, Tuple[int, int]]:
    """The network input (float32 [1, 3, size, size], 0-1, grey 114 padding as ultralytics does), the scale applied
    and the (left, top) padding in pixels."""
    from PIL import Image
    h, w = rgb.shape[:2]
    scale = min(size / h, size / w)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    resized = np.asarray(Image.fromarray(rgb).resize((nw, nh), Image.BILINEAR))
    left, top = (size - nw) // 2, (size - nh) // 2
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    canvas[top:top + nh, left:left + nw] = resized
    blob = canvas.astype(np.float32).transpose(2, 0, 1)[None] / 255.0
    return np.ascontiguousarray(blob), scale, (left, top)


def nms(boxes: np.ndarray, scores: np.ndarray, iou: float) -> List[int]:
    """Indices kept by greedy non-maximum suppression (boxes xyxy)."""
    order = np.argsort(-scores)
    keep: List[int] = []
    areas = np.maximum(0, boxes[:, 2] - boxes[:, 0]) * np.maximum(0, boxes[:, 3] - boxes[:, 1])
    while order.size:
        i = int(order[0])
        keep.append(i)
        rest = order[1:]
        xx1 = np.maximum(boxes[i, 0], boxes[rest, 0])
        yy1 = np.maximum(boxes[i, 1], boxes[rest, 1])
        xx2 = np.minimum(boxes[i, 2], boxes[rest, 2])
        yy2 = np.minimum(boxes[i, 3], boxes[rest, 3])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        union = areas[i] + areas[rest] - inter
        order = rest[inter / np.maximum(union, 1e-9) <= iou]
    return keep


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def yolo_seg_postprocess(out0: np.ndarray, proto: np.ndarray, *, input_size: int, orig_hw: Tuple[int, int],
                         scale: float, pad: Tuple[int, int], conf: float = DEFAULT_CONF, iou: float = DEFAULT_IOU,
                         cls: int = PERSON_CLASS) -> Tuple[np.ndarray, List[float]]:
    """(person mask bool [H, W], scores) from a YOLO-seg ONNX output.

    `out0` is either the classic head [1, 4 + nc + nm, N] (xywh, class scores, mask coefficients; YOLOv8/11/26 with
    end2end off) or the end-to-end head [1, N, 6 + nm] (xyxy, score, class, coefficients; NMS already done).
    `proto` is [1, nm, ph, pw]. Boxes are in letterboxed input pixels."""
    proto = proto[0]
    nm, ph, pw = proto.shape
    out = out0[0]
    if out.ndim != 2:
        raise ValueError(f"unexpected YOLO-seg output shape {out0.shape}")
    if out.shape[1] == 6 + nm:
        # end-to-end: [N, 6 + nm]
        sel = (out[:, 5].round().astype(int) == cls) & (out[:, 4] >= conf)
        boxes, scores, coefs = out[sel, :4], out[sel, 4], out[sel, 6:]
    else:
        pred = out.T                                            # [N, 4 + nc + nm]
        nc = pred.shape[1] - 4 - nm
        if nc <= cls:
            raise ValueError(f"unexpected YOLO-seg output shape {out0.shape} with {nm} mask coefficients")
        cls_scores = pred[:, 4:4 + nc]
        best = cls_scores.argmax(axis=1)
        score = cls_scores[:, cls]
        sel = (best == cls) & (score >= conf)
        xywh, scores, coefs = pred[sel, :4], score[sel], pred[sel, 4 + nc:]
        boxes = np.stack([xywh[:, 0] - xywh[:, 2] / 2, xywh[:, 1] - xywh[:, 3] / 2,
                          xywh[:, 0] + xywh[:, 2] / 2, xywh[:, 1] + xywh[:, 3] / 2], axis=1) if len(xywh) else \
            np.zeros((0, 4), dtype=np.float32)
        if len(boxes):
            keep = nms(boxes, scores, iou)
            boxes, scores, coefs = boxes[keep], scores[keep], coefs[keep]
    h, w = orig_hw
    if not len(boxes):
        return np.zeros((h, w), dtype=bool), []
    soft = _sigmoid(coefs.astype(np.float32) @ proto.reshape(nm, -1).astype(np.float32)).reshape(-1, ph, pw)
    # crop every instance to its own box (in proto pixels), then the union
    sx, sy = pw / input_size, ph / input_size
    ys, xs = np.arange(ph)[:, None] + 0.5, np.arange(pw)[None, :] + 0.5
    union = np.zeros((ph, pw), dtype=np.float32)
    for (x1, y1, x2, y2), m in zip(boxes, soft):
        inside = (xs >= x1 * sx) & (xs <= x2 * sx) & (ys >= y1 * sy) & (ys <= y2 * sy)
        np.maximum(union, np.where(inside, m, 0.0), out=union)
    # remove the letterbox padding, back to the original image size
    left, top = pad
    x0, y0 = int(round(left * sx)), int(round(top * sy))
    x1_, y1_ = int(round((left + w * scale) * sx)), int(round((top + h * scale) * sy))
    crop = union[y0:max(y0 + 1, y1_), x0:max(x0 + 1, x1_)]
    from PIL import Image
    full = np.asarray(Image.fromarray(crop.astype(np.float32), mode="F").resize((w, h), Image.BILINEAR))
    return full > MASK_THRESHOLD, [float(s) for s in scores]


# --- backends -----------------------------------------------------------------------------------------------------

def _require(module: str, install: str):
    import importlib
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise SegmenterUnavailable(f"the '{module}' package is not installed in this image ({install})") from exc


def _require_file(path: str, what: str) -> None:
    if not path or not os.path.exists(path):
        raise SegmenterUnavailable(f"{what} not found at '{path}' (mount the weights; docs/reconstruction/"
                                   "person_removal.md, 'Weights')")


class Backend:
    """load() -> None, predict(rgb) -> (mask, scores), close() -> None."""
    kind = "base"

    def __init__(self, path: str, *, conf: float = DEFAULT_CONF, iou: float = DEFAULT_IOU, device: str = "cpu",
                 threads: Optional[int] = None):
        self.path, self.conf, self.iou, self.device, self.threads = path, conf, iou, device, threads

    def load(self) -> None:
        raise NotImplementedError

    def predict(self, rgb: np.ndarray) -> Tuple[np.ndarray, List[float]]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class OnnxYoloSeg(Backend):
    """YOLO-seg on onnxruntime (CPU by default; CUDAExecutionProvider when device=cuda and available)."""
    kind = "onnx"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.session = None
        self.input_name = ""
        self.size = 640

    def load(self) -> None:
        _require_file(self.path, "ONNX model")
        ort = _require("onnxruntime", "pip install onnxruntime")
        opts = ort.SessionOptions()
        if self.threads:
            opts.intra_op_num_threads = int(self.threads)
            opts.inter_op_num_threads = 1
        providers = ["CPUExecutionProvider"]
        if self.device.startswith("cuda") and "CUDAExecutionProvider" in ort.get_available_providers():
            providers.insert(0, "CUDAExecutionProvider")
        self.session = ort.InferenceSession(self.path, sess_options=opts, providers=providers)
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        dims = [d for d in inp.shape[2:] if isinstance(d, int)]
        self.size = int(dims[0]) if dims else 640
        outs = self.session.get_outputs()
        if len(outs) < 2:
            raise SegmenterUnavailable(f"{self.path} is not a segmentation model (one output; use a *-seg model)")

    def predict(self, rgb: np.ndarray) -> Tuple[np.ndarray, List[float]]:
        blob, scale, pad = letterbox(rgb, self.size)
        out0, proto = self.session.run(None, {self.input_name: blob})[:2]
        if proto.ndim != 4:          # some exports list the outputs the other way round
            out0, proto = proto, out0
        return yolo_seg_postprocess(out0, proto, input_size=self.size, orig_hw=rgb.shape[:2], scale=scale, pad=pad,
                                    conf=self.conf, iou=self.iou)

    def close(self) -> None:
        self.session = None


class UltralyticsYoloSeg(Backend):
    """YOLO-seg through ultralytics (torch). For the GPU box: larger models (yolo11x-seg, yolo26x-seg ...)."""
    kind = "ultralytics"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.model = None

    def load(self) -> None:
        _require_file(self.path, "YOLO weights")
        ul = _require("ultralytics", "pip install ultralytics (and torch)")
        self.model = ul.YOLO(self.path)

    def predict(self, rgb: np.ndarray) -> Tuple[np.ndarray, List[float]]:
        h, w = rgb.shape[:2]
        res = self.model.predict(np.ascontiguousarray(rgb[:, :, ::-1]), classes=[PERSON_CLASS], conf=self.conf,
                                 iou=self.iou, device=self.device, retina_masks=True, verbose=False)[0]
        if res.masks is None or not len(res.masks):
            return np.zeros((h, w), dtype=bool), []
        masks = res.masks.data.cpu().numpy() > MASK_THRESHOLD
        mask = masks.any(axis=0)
        if mask.shape != (h, w):
            from PIL import Image
            mask = np.asarray(Image.fromarray(mask.astype(np.uint8) * 255).resize((w, h), Image.NEAREST)) > 127
        return mask, [float(s) for s in res.boxes.conf.cpu().numpy()]

    def close(self) -> None:
        self.model = None
        _free_torch()


class GroundedSam2(Backend):
    """Grounding DINO ("person.") boxes refined by SAM 2 masks, as Grounded-SAM-2's grounded_sam2_local_demo.py.

    `path` is a directory with: `sam2.1_hiera_large.pt` (or PERSON_MASK_SAM2_CKPT), `groundingdino_swinb_cogcoor.pth`
    (or PERSON_MASK_GDINO_CKPT); the Grounded-SAM-2 checkout must be importable (`sam2`, `grounding_dino` packages;
    PERSON_MASK_GSAM2_ROOT is added to sys.path). Thresholds: box 0.35, text 0.25 (the demo's)."""
    kind = "grounded-sam2"
    PROMPT = "person."

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.predictor = None
        self.gdino = None

    def load(self) -> None:
        import sys
        root = os.getenv("PERSON_MASK_GSAM2_ROOT", "")
        if root and root not in sys.path:
            sys.path.insert(0, root)
        sam_ckpt = os.getenv("PERSON_MASK_SAM2_CKPT", os.path.join(self.path, "sam2.1_hiera_large.pt"))
        sam_cfg = os.getenv("PERSON_MASK_SAM2_CFG", "configs/sam2.1/sam2.1_hiera_l.yaml")
        gd_ckpt = os.getenv("PERSON_MASK_GDINO_CKPT", os.path.join(self.path, "groundingdino_swinb_cogcoor.pth"))
        gd_cfg = os.getenv("PERSON_MASK_GDINO_CFG", os.path.join(
            root, "grounding_dino/groundingdino/config/GroundingDINO_SwinB_cfg.py") if root else "")
        _require_file(sam_ckpt, "SAM 2 checkpoint")
        _require_file(gd_ckpt, "Grounding DINO checkpoint")
        _require_file(gd_cfg, "Grounding DINO config")
        _require("torch", "the GPU image (torch)")
        build = _require("sam2.build_sam", "the Grounded-SAM-2 checkout on PYTHONPATH (PERSON_MASK_GSAM2_ROOT)")
        pred = _require("sam2.sam2_image_predictor", "the Grounded-SAM-2 checkout")
        gd = _require("grounding_dino.groundingdino.util.inference", "the Grounded-SAM-2 checkout")
        self.predictor = pred.SAM2ImagePredictor(build.build_sam2(sam_cfg, sam_ckpt, device=self.device))
        self.gdino = (gd, gd.load_model(model_config_path=gd_cfg, model_checkpoint_path=gd_ckpt, device=self.device))

    def predict(self, rgb: np.ndarray) -> Tuple[np.ndarray, List[float]]:
        import torch
        gd, model = self.gdino
        h, w = rgb.shape[:2]
        tf = _gdino_transform()
        from PIL import Image
        image, _ = tf(Image.fromarray(rgb), None)
        with torch.no_grad():
            boxes, logits, _phrases = gd.predict(model=model, image=image, caption=self.PROMPT,
                                                 box_threshold=self.conf, text_threshold=0.25, device=self.device)
        if boxes is None or not len(boxes):
            return np.zeros((h, w), dtype=bool), []
        cxcywh = boxes.cpu().numpy() * np.array([w, h, w, h], dtype=np.float32)
        xyxy = np.stack([cxcywh[:, 0] - cxcywh[:, 2] / 2, cxcywh[:, 1] - cxcywh[:, 3] / 2,
                         cxcywh[:, 0] + cxcywh[:, 2] / 2, cxcywh[:, 1] + cxcywh[:, 3] / 2], axis=1)
        self.predictor.set_image(rgb)
        with torch.no_grad(), torch.autocast(self.device.split(":")[0], dtype=torch.bfloat16,
                                             enabled=self.device.startswith("cuda")):
            masks, _scores, _ = self.predictor.predict(point_coords=None, point_labels=None, box=xyxy,
                                                       multimask_output=False)
        masks = np.asarray(masks)
        if masks.ndim == 4:
            masks = masks[:, 0]
        return masks.astype(bool).any(axis=0), [float(s) for s in logits.cpu().numpy()]

    def close(self) -> None:
        self.predictor = None
        self.gdino = None
        _free_torch()


def _gdino_transform():
    import grounding_dino.groundingdino.datasets.transforms as T  # noqa: N812
    return T.Compose([T.RandomResize([800], max_size=1333), T.ToTensor(),
                      T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])])


def _free_torch() -> None:
    import sys
    torch = sys.modules.get("torch")
    if torch is not None and getattr(torch, "cuda", None) is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()


BACKENDS: Dict[str, Callable[..., Backend]] = {
    "onnx": OnnxYoloSeg, "ultralytics": UltralyticsYoloSeg, "grounded-sam2": GroundedSam2}


def parse_spec(spec: str) -> Tuple[str, str]:
    """("onnx", "/models/yolo11n-seg.onnx") from "onnx:/models/yolo11n-seg.onnx"; ValueError when malformed."""
    kind, sep, path = (spec or "").strip().partition(":")
    if not sep or kind not in BACKENDS or not path:
        raise ValueError(f"bad model spec {spec!r}: use <kind>:<path> with kind one of {', '.join(BACKENDS)}")
    return kind, path


def parse_models(text: str) -> Dict[str, str]:
    """{"fast": "onnx:/models/a.onnx", ...} from "fast=onnx:/models/a.onnx;quality=ultralytics:/models/b.pt"."""
    models: Dict[str, str] = {}
    for part in (text or "").replace("\n", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        name, sep, spec = part.partition("=")
        name = name.strip()
        if not sep or not name:
            raise ValueError(f"bad PERSON_MASK_MODELS entry {part!r}: use name=<kind>:<path>")
        parse_spec(spec)
        models[name] = spec.strip()
    return models


def release_memory() -> None:
    """Give freed heap back to the OS (glibc keeps it otherwise), after a model was dropped."""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


# --- lazy load / unload -------------------------------------------------------------------------------------------

class LazySegmenter:
    """One backend, created on the first `segment` and dropped by `unload` (or `unload_if_idle`). Thread-safe: one
    inference at a time (the machine's cores are shared with the rest of the stack)."""

    def __init__(self, name: str, factory: Callable[[], Backend], clock: Callable[[], float] = time.monotonic):
        self.name, self._factory, self._clock = name, factory, clock
        self._backend: Optional[Backend] = None
        self._lock = threading.RLock()
        self.loads = 0
        self.frames = 0
        self.last_used: Optional[float] = None
        self.load_seconds: Optional[float] = None

    @property
    def loaded(self) -> bool:
        return self._backend is not None

    def load(self) -> Backend:
        with self._lock:
            if self._backend is None:
                t0 = self._clock()
                backend = self._factory()
                try:
                    backend.load()
                except BaseException:
                    backend.close()
                    release_memory()
                    raise
                self._backend = backend
                self.loads += 1
                self.load_seconds = round(self._clock() - t0, 3)
                logger.info("Model %s loaded in %.2f s", self.name, self.load_seconds)
            return self._backend

    def segment(self, rgb: np.ndarray) -> Segmentation:
        with self._lock:
            backend = self.load()
            t0 = self._clock()
            mask, scores = backend.predict(rgb)
            self.frames += 1
            self.last_used = self._clock()
            if mask.shape != rgb.shape[:2]:
                raise ValueError(f"backend returned a {mask.shape} mask for a {rgb.shape[:2]} image")
            return Segmentation(mask.astype(bool), scores, self.name, round(self.last_used - t0, 4))

    def unload(self) -> bool:
        with self._lock:
            if self._backend is None:
                return False
            try:
                self._backend.close()
            finally:
                self._backend = None
                release_memory()
            logger.info("Model %s unloaded", self.name)
            return True

    def unload_if_idle(self, idle_s: float) -> bool:
        if idle_s <= 0 or self._backend is None or self.last_used is None:
            return False
        if not self._lock.acquire(blocking=False):       # busy: not idle
            return False
        try:
            if self._clock() - self.last_used >= idle_s:
                return self.unload()
            return False
        finally:
            self._lock.release()


class ModelPool:
    """The configured models by name; at most one resident (getting another unloads the others first)."""

    def __init__(self, models: Dict[str, str], default: Optional[str] = None, *, conf: float = DEFAULT_CONF,
                 device: str = "cpu", threads: Optional[int] = None,
                 backend_factory: Optional[Callable[[str, str], Backend]] = None):
        if not models:
            raise ValueError("no person-mask model configured (PERSON_MASK_MODELS)")
        self.specs = dict(models)
        self.default = default if default in models else next(iter(models))
        self.conf, self.device, self.threads = conf, device, threads
        self._make = backend_factory or self._backend
        self._lock = threading.Lock()
        self.segmenters: Dict[str, LazySegmenter] = {
            name: LazySegmenter(name, (lambda n=name, s=spec: self._make(n, s))) for name, spec in models.items()}

    def _backend(self, name: str, spec: str) -> Backend:
        kind, path = parse_spec(spec)
        return BACKENDS[kind](path, conf=self.conf, device=self.device, threads=self.threads)

    def get(self, name: Optional[str]) -> LazySegmenter:
        name = name or self.default
        if name not in self.segmenters:
            raise KeyError(name)
        with self._lock:
            for other, seg in self.segmenters.items():
                if other != name and seg.loaded:
                    seg.unload()
        return self.segmenters[name]

    def unload_all(self) -> List[str]:
        return [name for name, seg in self.segmenters.items() if seg.unload()]

    def unload_idle(self, idle_s: float) -> List[str]:
        return [name for name, seg in self.segmenters.items() if seg.unload_if_idle(idle_s)]

    def describe(self) -> List[Dict[str, Any]]:
        out = []
        for name, spec in self.specs.items():
            kind, path = parse_spec(spec)
            seg = self.segmenters[name]
            out.append({"name": name, "backend": kind, "path": path, "present": os.path.exists(path),
                        "loaded": seg.loaded, "default": name == self.default, "loads": seg.loads,
                        "frames": seg.frames, "load_seconds": seg.load_seconds})
        return out


def available_backends(kinds: Sequence[str] = tuple(BACKENDS)) -> Dict[str, bool]:
    """Which backends' packages are importable here (without importing them)."""
    import importlib.util
    need = {"onnx": "onnxruntime", "ultralytics": "ultralytics", "grounded-sam2": "sam2"}
    return {k: importlib.util.find_spec(need[k]) is not None for k in kinds}
