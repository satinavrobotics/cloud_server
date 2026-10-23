"""Person-mask service: RGB frame in, person mask PNG out (docs/reconstruction/person_removal.md).

A small optional service next to the API. It holds no state but the models, which it loads on the first frame and
unloads after `PERSON_MASK_IDLE_UNLOAD_S` without frames (or on POST /v1/unload), so between person-removal runs it
costs a Python process and nothing more. No database, no MinIO: the API sends the image bytes and stores the result.

    GET  /health                     no auth: {status, version, models: [...], default_model, backends, device, idle_unload_s}
    POST /v1/segment?model=<name>    body = JPEG/PNG bytes -> 200 image/png (L, 255 = person, the image's size),
                                     headers X-Person-Count, X-Person-Scores, X-Model, X-Infer-Seconds
    POST /v1/unload                  -> {"unloaded": [names]}

Errors are {"detail": {"code", "message"}}: 400 bad_image, 401, 404 unknown_model, 413 too_large,
503 model_unavailable (package or weights missing: the message says which).
Auth: `Authorization: Bearer <PERSON_MASK_SERVICE_KEY>` on the /v1 routes when the key is set.
"""
import asyncio
import contextlib
import hmac
import logging
import os
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request, Response

from packages.services.person_mask import segmenter as sg

logger = logging.getLogger("person_mask.server")

VERSION = "1.0.0"
MAX_BODY = 16 * 1024 * 1024


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


class Settings:
    def __init__(self, env=os.environ):
        self.models = sg.parse_models(env.get("PERSON_MASK_MODELS", "") or
                                      ("default=" + env["PERSON_MASK_MODEL"] if env.get("PERSON_MASK_MODEL") else ""))
        self.default_model = env.get("PERSON_MASK_DEFAULT_MODEL") or None
        self.key = env.get("PERSON_MASK_SERVICE_KEY") or ""
        self.device = env.get("PERSON_MASK_DEVICE", "cpu")
        self.conf = float(env.get("PERSON_MASK_CONF", sg.DEFAULT_CONF))
        threads = env.get("PERSON_MASK_THREADS", "")
        self.threads = int(threads) if threads else None
        self.idle_unload_s = float(env.get("PERSON_MASK_IDLE_UNLOAD_S", "120"))


def create_app(settings: Optional[Settings] = None, pool: Optional[sg.ModelPool] = None) -> FastAPI:
    settings = settings or Settings()
    pool = pool or sg.ModelPool(settings.models, settings.default_model, conf=settings.conf,
                                device=settings.device, threads=settings.threads)
    async def idle_watch() -> None:
        period = max(5.0, min(60.0, settings.idle_unload_s / 4)) if settings.idle_unload_s > 0 else 0
        while period:
            await asyncio.sleep(period)
            try:
                unloaded = await asyncio.to_thread(pool.unload_idle, settings.idle_unload_s)
                if unloaded:
                    logger.info("Idle for %.0f s: unloaded %s", settings.idle_unload_s, ", ".join(unloaded))
            except Exception:  # noqa: BLE001
                logger.exception("idle unload failed")

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        task = asyncio.get_running_loop().create_task(idle_watch())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.to_thread(pool.unload_all)

    app = FastAPI(title="person-mask", version=VERSION, lifespan=lifespan)
    app.state.pool, app.state.settings = pool, settings

    def check_auth(authorization: Optional[str]) -> None:
        if not settings.key:
            return
        given = (authorization or "")[len("Bearer "):].strip() if (authorization or "").startswith("Bearer ") else ""
        if not hmac.compare_digest(given.encode(), settings.key.encode()):
            raise HTTPException(401, {"code": "unauthorized", "message": "bad or missing bearer key"})

    @app.get("/health")
    async def health():
        return {"status": "healthy", "version": VERSION, "models": pool.describe(), "default_model": pool.default,
                "backends": sg.available_backends(), "device": settings.device,
                "idle_unload_s": settings.idle_unload_s}

    def _segment(model: Optional[str], data: bytes):
        try:
            seg = pool.get(model)
        except KeyError:
            raise _err(404, "unknown_model", f"no model '{model}' here (known: {', '.join(pool.specs)})")
        try:
            rgb = sg.decode_rgb(data)
        except ValueError as exc:
            raise _err(400, "bad_image", str(exc))
        try:
            return seg.segment(rgb)
        except sg.SegmenterUnavailable as exc:
            raise _err(503, "model_unavailable", str(exc))

    @app.post("/v1/segment")
    async def segment(request: Request, model: Optional[str] = None, authorization: Optional[str] = Header(None)):
        check_auth(authorization)
        data = await request.body()
        if not data:
            raise _err(400, "bad_image", "empty body")
        if len(data) > MAX_BODY:
            raise _err(413, "too_large", f"at most {MAX_BODY} bytes")
        result = await asyncio.to_thread(_segment, model, data)
        png = await asyncio.to_thread(sg.encode_mask_png, result.mask)
        return Response(png, media_type="image/png", headers={
            "X-Person-Count": str(result.persons),
            "X-Person-Scores": ",".join(f"{s:.3f}" for s in result.scores[:50]),
            "X-Model": result.model, "X-Infer-Seconds": f"{result.seconds:.4f}"})

    @app.post("/v1/unload")
    async def unload(authorization: Optional[str] = Header(None)):
        check_auth(authorization)
        return {"unloaded": await asyncio.to_thread(pool.unload_all)}

    return app
