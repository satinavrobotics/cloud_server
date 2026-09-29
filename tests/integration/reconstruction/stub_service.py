"""A stub reconstruction service for tests/integration/reconstruction (docs/reconstruction/
handover.md §2-§3): it speaks the contract but computes nothing. For each job it GETs every
input URL (recording status, host and sha256 of what came back), PUTs small fixed outputs to
the presigned PUT URLs and calls back.

    POST /jobs, GET /jobs/{id}, POST /jobs/{id}/cancel, GET /health  (the contract)
    POST /control {"mode": ...}   test control (no auth):
        normal   run the job at once
        hold     send progress every 0.5 s until released; a cancel (/cancel or a progress
                 answer `cancel`) ends it with fail(cancelled); a 410 stops it silently
        deaf     like hold, but ignores cancels and 410 (to test late PUTs and callbacks)
        forget   accept the job, then forget it (GET -> 404); the next POST runs normally
    POST /control {"release": job_id}  a held job PUTs its outputs and calls finish
    GET  /record                  what happened: jobs, fetches, callback answers

    python tests/integration/reconstruction/stub_service.py   (port 8009, key STUB_KEY)
"""
import asyncio
import hashlib
import json
import os
import urllib.parse

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

KEY = os.environ.get("STUB_KEY", "stub-key")
app = FastAPI()
MODE = {"mode": "normal"}
JOBS = {}       # job_id -> status dict
RELEASE = {}    # job_id -> asyncio.Event
CANCEL = {}     # job_id -> asyncio.Event
RECORD = {"manifests": [], "fetches": [], "puts": [], "callbacks": []}
META = {"version": 1, "map_name": None, "job_id": None, "frame": "map", "resolution_m": 0.05,
        "origin": {"x": 0.0, "y": 0.0}, "width": 2, "height": 2, "points": 3}


def _auth(authorization):
    if authorization != f"Bearer {KEY}":
        raise HTTPException(401, "bad key")


def _outputs(manifest):
    meta = dict(META, map_name=manifest["map"]["name"], job_id=manifest["job_id"])
    return {"cloud": b"ply\nformat binary_little_endian 1.0\nelement vertex 0\nend_header\n",
            "ortho": b"\x89PNG-ortho-stub", "height": b"\x89PNG-height-stub",
            "meta": json.dumps(meta).encode()}


async def _callback(client, manifest, kind, body):
    cb = manifest["callback"]
    try:
        r = await client.post(f"{cb['base_url']}/{kind}", json=body, timeout=10,
                              headers={"Authorization": f"Bearer {cb['token']}"})
        answer = (r.status_code, r.json() if r.content else None)
    except Exception as exc:  # noqa: BLE001
        answer = (None, str(exc))
    RECORD["callbacks"].append({"job_id": manifest["job_id"], "attempt": manifest["attempt"],
                                "kind": kind, "answer": answer})
    return answer


async def _run(manifest, mode):
    job_id, attempt = manifest["job_id"], manifest["attempt"]
    status = JOBS[job_id]
    status.update(state="running", stage="integrating", started_at="now")
    async with httpx.AsyncClient() as client:
        answer = await _callback(client, manifest, "progress", {
            "attempt": attempt, "stage": "integrating", "progress": 0.1, "frames_done": 0,
            "frames_total": sum(len(n["cameras"]) for n in manifest["nodes"])})
        for node in manifest["nodes"]:
            for cam in node["cameras"]:
                for kind in ("rgb_url", "depth_url"):
                    r = await client.get(cam[kind], timeout=30)
                    RECORD["fetches"].append({
                        "job_id": job_id, "node_id": node["node_id"], "kind": kind,
                        "host": urllib.parse.urlsplit(cam[kind]).netloc,
                        "status": r.status_code, "sha256": hashlib.sha256(r.content).hexdigest()})
        if mode in ("hold", "deaf"):
            while not RELEASE[job_id].is_set():
                if mode == "hold" and (CANCEL[job_id].is_set() or answer[0] == 410
                                       or (answer[1] or {}).get("action") == "cancel"):
                    if answer[0] != 410:
                        await _callback(client, manifest, "fail", {
                            "attempt": attempt, "reason": "cancelled", "stage": "integrating",
                            "message": "cancelled"})
                    status.update(state="cancelled")
                    return
                await asyncio.sleep(0.5)
                answer = await _callback(client, manifest, "progress", {
                    "attempt": attempt, "stage": "integrating", "progress": 0.5})
        outputs = {}
        for name, data in _outputs(manifest).items():
            out = manifest["outputs"][name]
            r = await client.put(out["url"], content=data, timeout=30,
                                 headers={"Content-Type": out["content_type"]})
            RECORD["puts"].append({"job_id": job_id, "name": name, "status": r.status_code,
                                   "host": urllib.parse.urlsplit(out["url"]).netloc})
            outputs[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        result = {"points": 3, "frames_total": 1, "frames_used": 1,
                  "frames_skipped": {"no_valid_depth": 0}, "voxel_m": 0.05,
                  "bounds3d": {"min": [0, 0, 0], "max": [1, 1, 1]}, "duration_s": 0.1}
        status.update(state="succeeded", stage="uploading", progress=1.0, result=result,
                      outputs=outputs)
        await _callback(client, manifest, "finish", {"attempt": attempt, "result": result,
                                                     "outputs": outputs})


@app.post("/jobs")
async def post_job(request: Request, authorization: str = Header(None)):
    _auth(authorization)
    manifest = await request.json()
    RECORD["manifests"].append(manifest)
    job_id, attempt = manifest["job_id"], manifest["attempt"]
    known = JOBS.get(job_id)
    if known and known["attempt"] == attempt:
        return JSONResponse(known, status_code=200)
    if known and known["attempt"] > attempt:
        return JSONResponse({"detail": {"code": "stale_attempt"}}, status_code=409)
    mode = MODE["mode"]
    JOBS[job_id] = {"job_id": job_id, "attempt": attempt, "state": "queued", "stage": "queued",
                    "progress": 0.0, "result": None, "outputs": None, "error": None}
    RELEASE[job_id], CANCEL[job_id] = asyncio.Event(), asyncio.Event()
    if mode == "forget":
        MODE["mode"] = "normal"
        body = dict(JOBS.pop(job_id))
        return JSONResponse(body, status_code=202)
    asyncio.get_running_loop().create_task(_run(manifest, mode))
    return JSONResponse(JOBS[job_id], status_code=202)


@app.get("/jobs/{job_id}")
async def get_job(job_id: str, authorization: str = Header(None)):
    _auth(authorization)
    if job_id not in JOBS:
        raise HTTPException(404, "unknown job")
    return JOBS[job_id]


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, authorization: str = Header(None)):
    _auth(authorization)
    if job_id not in JOBS:
        raise HTTPException(404, "unknown job")
    RECORD["callbacks"].append({"job_id": job_id, "kind": "cancel-received"})
    CANCEL[job_id].set()
    return JOBS[job_id]


@app.get("/health")
async def health():
    return {"status": "healthy", "version": "stub", "queue": {"running": 0, "queued": 0,
                                                               "max": 4}, "gpu": False}


@app.post("/control")
async def control(body: dict):
    if "mode" in body:
        MODE["mode"] = body["mode"]
    if "release" in body:
        RELEASE[body["release"]].set()
    return MODE


@app.get("/record")
async def record():
    return RECORD


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8009, log_level="warning")
