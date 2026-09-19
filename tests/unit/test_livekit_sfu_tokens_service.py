"""
Unit tests for the LiveKit SFU Token Service.

Decodes real JWTs (no mocked SDK) so the tests pin the grants the self-hosted
LiveKit server will actually enforce for each role.
"""

import sys
from unittest.mock import MagicMock

# test_livekit_service.py / test_livekit_integration.py replace the livekit
# modules with MagicMocks at import time. This module needs the real SDK, so
# drop any such mock before importing it (their own references are unaffected).
for _name in ("livekit", "livekit.api"):
    if isinstance(sys.modules.get(_name), MagicMock):
        del sys.modules[_name]

import jwt
import pytest
from fastapi.testclient import TestClient

from packages.services.livekit_sfu_tokens import main as sfu_tokens_main
from packages.services.livekit_sfu_tokens.server import (
    DASHBOARD_IDENTITY_PREFIX, InvalidIdentityError, LiveKitSfuTokenService, ROLE_GRANTS, UnknownRoleError,
)

API_KEY = "APItestkey"
API_SECRET = "test-secret-at-least-32-bytes-long-000"
SERVER_URL = "ws://100.64.0.1:7880"
OPERATOR_URL = "wss://sfu.example.ts.net"


def _decode(token: str) -> dict:
    return jwt.decode(token, API_SECRET, algorithms=["HS256"], options={"verify_aud": False})


def _effective(video: dict) -> tuple:
    """(publish, subscribe, publish_data) as the LiveKit server resolves them:
    omitted canPublish/canSubscribe mean true, omitted canPublishData falls
    back to canPublish (GetCanPublishData in livekit/protocol auth/grants.go)."""
    publish = video.get("canPublish", True)
    return publish, video.get("canSubscribe", True), video.get("canPublishData", publish)


@pytest.fixture
def service():
    return LiveKitSfuTokenService(API_KEY, API_SECRET, SERVER_URL, ttl=3600)


@pytest.mark.unit
class TestRoleGrants:
    """The effective permissions each role gets."""

    def test_robot_is_bidirectional(self, service):
        """Robots publish video and subscribe to the operator's tracks and
        teleop commands."""
        video = _decode(service.create_token("jetson-golya", "fleet", "robot")["token"])["video"]
        assert _effective(video) == (True, True, True)

    def test_operator_is_bidirectional(self, service):
        """Operators watch video, send teleop commands / RPC over data, and
        can publish their own tracks."""
        video = _decode(service.create_token("cimbi", "fleet", "operator")["token"])["video"]
        assert _effective(video) == (True, True, True)

    def test_every_role_sets_publish_data_explicitly(self, service):
        """Neither the SDK default (true) nor LiveKit's fallback (canPublish)
        may decide data rights: every role's token carries canPublishData."""
        for role, grants in ROLE_GRANTS.items():
            video = _decode(service.create_token("p", "fleet", role)["token"])["video"]
            assert video["canPublishData"] is grants["can_publish_data"], role

    def test_unknown_role_is_rejected(self, service):
        with pytest.raises(UnknownRoleError, match="expected one of: robot, operator"):
            service.create_token("p", "fleet", "admin")


@pytest.mark.unit
class TestTokenContents:
    """Identity, room, lifetime and signature of the minted token."""

    def test_identity_and_room(self, service):
        claims = _decode(service.create_token("jetson-kolibri", "bench-x", "robot")["token"])
        assert claims["sub"] == "jetson-kolibri"
        assert claims["iss"] == API_KEY
        assert claims["video"]["room"] == "bench-x"
        assert claims["video"]["roomJoin"] is True

    def test_ttl(self, service):
        result = service.create_token("p", "fleet", "robot")
        claims = _decode(result["token"])
        assert result["ttl"] == 3600
        assert claims["exp"] - claims["nbf"] == 3600

    def test_signed_with_the_configured_secret(self, service):
        token = service.create_token("p", "fleet", "robot")["token"]
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(token, "a-different-secret-of-sufficient-len", algorithms=["HS256"])

    def test_response_shape_matches_node_token_server(self, service):
        """robot_client.py reads token and server_url; keep the exact keys."""
        result = service.create_token("p", "fleet", "robot")
        assert set(result) == {"token", "ttl", "server_url"}
        assert result["server_url"] == SERVER_URL


@pytest.mark.unit
class TestServerUrlPerRole:
    """Operators (https dashboard) get the wss:// name, robots the plain IP."""

    def test_roles_get_their_own_url(self):
        svc = LiveKitSfuTokenService(API_KEY, API_SECRET, SERVER_URL, operator_server_url=OPERATOR_URL)
        assert svc.create_token("p", "fleet", "robot")["server_url"] == SERVER_URL
        assert svc.create_token("p", "fleet", "operator")["server_url"] == OPERATOR_URL

    def test_operator_url_defaults_to_server_url(self, service):
        assert service.create_token("p", "fleet", "operator")["server_url"] == SERVER_URL


@pytest.mark.unit
class TestOperatorTtl:
    """Operator tokens can be shorter-lived than robot tokens."""

    def test_operator_ttl_applies_to_operators_only(self):
        svc = LiveKitSfuTokenService(API_KEY, API_SECRET, SERVER_URL, ttl=3600, operator_ttl=600)
        robot, operator = svc.create_token("r", "fleet", "robot"), svc.create_token("o", "fleet", "operator")
        assert (robot["ttl"], operator["ttl"]) == (3600, 600)
        for result in (robot, operator):
            claims = _decode(result["token"])
            assert claims["exp"] - claims["nbf"] == result["ttl"]

    def test_operator_ttl_defaults_to_ttl(self, service):
        assert service.create_token("o", "fleet", "operator")["ttl"] == 3600


@pytest.mark.unit
class TestIdentityRules:
    """LiveKit kicks the existing participant when a second one joins under the
    same identity, so a dashboard client must not be able to take a robot's."""

    def test_robot_cannot_use_the_dashboard_prefix(self, service):
        with pytest.raises(InvalidIdentityError, match="reserved for dashboard"):
            service.create_token(f"{DASHBOARD_IDENTITY_PREFIX}abc", "fleet", "robot")

    @pytest.mark.parametrize("participant,room", [("", "fleet"), ("p", ""), ("x" * 129, "fleet"), ("p", "x" * 129)])
    def test_empty_or_oversized_names_are_rejected(self, service, participant, room):
        with pytest.raises(InvalidIdentityError, match="characters"):
            service.create_token(participant, room, "operator")

    def test_limit_length_is_accepted(self, service):
        assert service.create_token("x" * 128, "y" * 128, "operator")["token"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("LIVEKIT_SFU_API_KEY", API_KEY)
    monkeypatch.setenv("LIVEKIT_SFU_API_SECRET", API_SECRET)
    monkeypatch.setenv("LIVEKIT_SFU_SERVER_URL", SERVER_URL)
    monkeypatch.setenv("LIVEKIT_SFU_OPERATOR_URL", OPERATOR_URL)
    monkeypatch.setenv("LIVEKIT_SFU_TTL", "600")
    monkeypatch.setenv("LIVEKIT_SFU_OPERATOR_TTL", "120")
    with TestClient(sfu_tokens_main.app) as c:
        yield c


@pytest.mark.unit
class TestEndpoints:
    """HTTP behaviour of the FastAPI app."""

    def test_create_token(self, client):
        resp = client.post("/api/createToken",
                           json={"participantName": "cimbi", "roomName": "fleet", "role": "operator"})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"token", "ttl", "server_url"}
        assert body["ttl"] == 120  # operator role -> LIVEKIT_SFU_OPERATOR_TTL
        assert _effective(_decode(body["token"])["video"]) == (True, True, True)

    def test_robot_ttl_over_http(self, client):
        resp = client.post("/api/createToken",
                           json={"participantName": "jetson", "roomName": "fleet", "role": "robot"})
        assert resp.json()["ttl"] == 600

    def test_urls_by_role_over_http(self, client):
        for role, url in (("robot", SERVER_URL), ("operator", OPERATOR_URL)):
            resp = client.post("/api/createToken",
                               json={"participantName": "p", "roomName": "fleet", "role": role})
            assert resp.json()["server_url"] == url

    def test_operator_route_issues_operator_tokens(self, client):
        resp = client.post("/api/operator/createToken",
                           json={"participantName": "WEB-cimbi", "roomName": "a@b.c"})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"token", "ttl", "server_url"}
        assert body["server_url"] == OPERATOR_URL
        assert _effective(_decode(body["token"])["video"]) == (True, True, True)

    def test_operator_route_cannot_mint_a_robot_token(self, client):
        """The route exposed on the public gateway ignores any role in the body."""
        resp = client.post("/api/operator/createToken",
                           json={"participantName": "WEB-p", "roomName": "fleet", "role": "robot"})
        assert resp.status_code == 200
        # grants are identical for both roles now, so the operator URL and TTL
        # are what show the body's `role: robot` was ignored
        assert resp.json()["server_url"] == OPERATOR_URL
        assert resp.json()["ttl"] == 120
        assert _effective(_decode(resp.json()["token"])["video"]) == (True, True, True)

    @pytest.mark.parametrize("name", ["jetson-golya", "web-lower", "", "XWEB-abc"])
    def test_operator_route_rejects_identities_without_the_dashboard_prefix(self, client, name):
        """The public route must not be able to take over a robot's identity."""
        resp = client.post("/api/operator/createToken", json={"participantName": name, "roomName": "fleet"})
        assert resp.status_code in (400, 422)

    def test_operator_route_rejects_oversized_names(self, client):
        assert client.post("/api/operator/createToken",
                           json={"participantName": "WEB-" + "x" * 128, "roomName": "fleet"}).status_code == 422
        assert client.post("/api/operator/createToken",
                           json={"participantName": "WEB-a", "roomName": "r" * 129}).status_code == 422

    def test_robot_route_rejects_the_dashboard_prefix(self, client):
        resp = client.post("/api/createToken",
                           json={"participantName": "WEB-abc", "roomName": "fleet", "role": "robot"})
        assert resp.status_code == 400

    def test_robot_route_rejects_empty_names(self, client):
        resp = client.post("/api/createToken",
                           json={"participantName": "", "roomName": "fleet", "role": "robot"})
        assert resp.status_code == 422

    @pytest.mark.parametrize("missing", ["participantName", "roomName"])
    def test_operator_route_missing_field_is_rejected(self, client, missing):
        body = {"participantName": "WEB-p", "roomName": "fleet"}
        del body[missing]
        assert client.post("/api/operator/createToken", json=body).status_code == 422

    def test_unknown_role_returns_400(self, client):
        resp = client.post("/api/createToken",
                           json={"participantName": "p", "roomName": "fleet", "role": "admin"})
        assert resp.status_code == 400

    @pytest.mark.parametrize("missing", ["participantName", "roomName", "role"])
    def test_missing_field_is_rejected(self, client, missing):
        body = {"participantName": "p", "roomName": "fleet", "role": "robot"}
        del body[missing]
        assert client.post("/api/createToken", json=body).status_code == 422

    def test_health(self, client):
        assert client.get("/health").json()["service"] == "livekit-sfu-tokens"

    def test_refuses_to_start_without_credentials(self, monkeypatch):
        monkeypatch.delenv("LIVEKIT_SFU_API_SECRET", raising=False)
        monkeypatch.setenv("LIVEKIT_SFU_API_KEY", API_KEY)
        monkeypatch.setenv("LIVEKIT_SFU_SERVER_URL", SERVER_URL)
        with pytest.raises(RuntimeError, match="LIVEKIT_SFU_API_SECRET"):
            with TestClient(sfu_tokens_main.app):
                pass
