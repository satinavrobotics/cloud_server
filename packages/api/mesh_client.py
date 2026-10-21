"""HTTP client for the external mesh service (cloud_mesh/CONTRACT.md).

The protocol is the reconstruction service's (POST /jobs, GET /jobs/{id}, POST /jobs/{id}/cancel,
bearer key), so this is the same client under its own name.
"""

from packages.api.reconstruction_client import ReconstructionClient, ServiceUnreachable

__all__ = ["MeshClient", "ServiceUnreachable"]


class MeshClient(ReconstructionClient):
    pass
