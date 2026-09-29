"""HTTP client for the external reconstruction service (docs/reconstruction/handover.md §2).

    POST /jobs                 submit(manifest)  -> (status, body)
    GET  /jobs/{id}            get_job(job_id)   -> (status, body)
    POST /jobs/{id}/cancel     cancel(job_id)    -> (status, body)

Every call carries `Authorization: Bearer RECONSTRUCTION_SERVICE_KEY`. The gateway
(packages/api/reconstruction.py) interprets the status codes; this module only turns
connection errors, timeouts and unparsable bodies into `ServiceUnreachable`.
"""

import logging
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger("ApiDelegationService.reconstruction_client")

SUBMIT_TIMEOUT_S = 30.0  # a manifest can be ~20 MB at the node limit
CALL_TIMEOUT_S = 10.0


class ServiceUnreachable(Exception):
    """The service did not answer (connection refused, DNS, timeout, broken response)."""


class ReconstructionClient:
    def __init__(self, base_url: str, key: str,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {key}"}
        self._transport = transport  # tests: httpx.MockTransport
        self._client: Optional[httpx.AsyncClient] = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=self.base_url, headers=self._headers,
                                             transport=self._transport,
                                             timeout=CALL_TIMEOUT_S)
        return self._client

    async def _call(self, method: str, path: str, *, json: Any = None,
                    timeout: float = CALL_TIMEOUT_S) -> Tuple[int, Dict[str, Any]]:
        try:
            response = await self._http().request(method, path, json=json, timeout=timeout)
        except httpx.HTTPError as exc:
            raise ServiceUnreachable(f"{method} {path}: {type(exc).__name__}: {exc}") from exc
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {"raw": response.text[:500]}
        if not isinstance(body, dict):
            body = {"body": body}
        return response.status_code, body

    async def submit(self, manifest: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        return await self._call("POST", "/jobs", json=manifest, timeout=SUBMIT_TIMEOUT_S)

    async def get_job(self, job_id: str) -> Tuple[int, Dict[str, Any]]:
        return await self._call("GET", f"/jobs/{job_id}")

    async def cancel(self, job_id: str) -> Tuple[int, Dict[str, Any]]:
        return await self._call("POST", f"/jobs/{job_id}/cancel")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
