#!/usr/bin/env python3
"""
LiveKit SFU Token Service - Core Logic

Mints role-scoped access tokens for the self-hosted, Tailscale-only LiveKit
SFU (livekit-sfu in docker_compose/mission_dispatch_services.yaml). Unlike
packages/services/livekit, callers never choose raw permissions: they name a
role, and the role decides what the token can do. The JWT itself is built by
packages/utils/livekit_tokens.py, shared with packages/services/livekit. See
docs/livekit_sfu/README.md.
"""

import logging
import os
from typing import Any, Dict, Optional

from packages.utils.livekit_tokens import mint_room_token

# role -> grants. Both roles are fully bidirectional: robots publish video and
# subscribe to the operator's tracks and teleop commands; operators publish
# teleop commands (data) and any tracks, and subscribe to the robots'. The roles
# differ only in server URL, token TTL and identity rules, not in grants.
#
# can_publish_data is always set explicitly: the Python SDK's VideoGrants
# writes canPublishData=true unless told otherwise, and LiveKit resolves an
# omitted canPublishData to the canPublish value (GetCanPublishData in
# livekit/protocol auth/grants.go), so relying on either default is fragile.
ROLE_GRANTS: Dict[str, Dict[str, bool]] = {
    "robot": {"can_publish": True, "can_subscribe": True, "can_publish_data": True},
    "operator": {"can_publish": True, "can_subscribe": True, "can_publish_data": True},
}

# LiveKit disconnects an existing participant when another joins its room under
# the same identity. The dashboard route is reachable without any authentication
# beyond the tailnet, so it must not be able to claim a robot's identity: the
# dashboard's identities all start with this prefix (sati-client
# PARTICIPANT_ID_PREFIX) and robot tokens may not use it.
DASHBOARD_IDENTITY_PREFIX = "WEB-"
MAX_NAME_LENGTH = 128
# Connected participants are not dropped when their token expires (verified on
# the SFU); the TTL only bounds how long a leaked token can be used to join.
DEFAULT_OPERATOR_TTL = 3600


# The API records the teleop session (packages/api/teleop.py); this service has no database.
TELEOP_NOTIFY_URL = os.getenv("TELEOP_NOTIFY_URL",
                              "http://127.0.0.1:8000/internal/teleop/token-issued")


def notify_token_issued(role: str, participant_name: str, room_name: str,
                        url: Optional[str] = None) -> bool:
    """Tell the API that a robot token was issued (sync; runs in a worker thread after the
    response). Only robot tokens name a robot; operator tokens are not sent. Never raises and
    never carries the token. An empty TELEOP_NOTIFY_URL turns it off."""
    url = TELEOP_NOTIFY_URL if url is None else url
    if role != "robot" or not url:
        return False
    try:
        import requests
        response = requests.post(url, json={"role": role, "participantName": participant_name,
                                            "roomName": room_name}, timeout=2)
        return response.ok
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("LiveKitSfuTokenService").debug("teleop notify failed: %s", exc)
        return False


class UnknownRoleError(ValueError):
    """Raised when a token is requested for a role not in ROLE_GRANTS."""


class InvalidIdentityError(ValueError):
    """Raised when a participant or room name is not acceptable for the role."""


class LiveKitSfuTokenService:
    """Creates role-scoped JWTs for the self-hosted LiveKit server."""

    def __init__(self, api_key: str, api_secret: str, server_url: str, ttl: int = 36000,
                 operator_server_url: Optional[str] = None, operator_ttl: Optional[int] = None):
        """
        Args:
            api_key: API key of the self-hosted LiveKit server
            api_secret: API secret of the self-hosted LiveKit server
            server_url: URL handed to robots (e.g. ws://100.85.3.47:7880)
            ttl: Token time-to-live in seconds (robots)
            operator_server_url: URL handed to operators, e.g. the
                `tailscale serve` name wss://admin-satinav-pc.tail055f44.ts.net:
                the dashboard is served over https, so it needs wss://. Robots
                keep the plain Tailscale IP so their video path doesn't depend
                on MagicDNS or cert renewal. Defaults to server_url.
            operator_ttl: Token time-to-live for operators. Defaults to ttl.
                Operator tokens are minted on the public dashboard route and
                carry data-publish (teleop) rights, so they can be shorter-lived;
                the client refetches on expiry.
        """
        self.logger = logging.getLogger("LiveKitSfuTokenService")
        self.api_key = api_key
        self.api_secret = api_secret
        self.server_url = server_url
        self.operator_server_url = operator_server_url or server_url
        self.ttl = ttl
        self.operator_ttl = operator_ttl or ttl
        self.logger.info(
            f"LiveKit SFU token service: robots -> {self.server_url} (ttl {self.ttl}s), "
            f"operators -> {self.operator_server_url} (ttl {self.operator_ttl}s)"
        )

    def create_token(self, participant_name: str, room_name: str, role: str) -> Dict[str, Any]:
        """
        Create a token for one participant in one room.

        Returns:
            Dictionary with token, ttl and server_url

        Raises:
            UnknownRoleError: If role is not a key of ROLE_GRANTS
            InvalidIdentityError: If a name is empty or too long, or a robot
                asks for a dashboard identity
        """
        grants = ROLE_GRANTS.get(role)
        if grants is None:
            raise UnknownRoleError(
                f"unknown role '{role}', expected one of: {', '.join(ROLE_GRANTS)}"
            )
        for label, name in (("participantName", participant_name), ("roomName", room_name)):
            if not 1 <= len(name) <= MAX_NAME_LENGTH:
                raise InvalidIdentityError(f"{label} must be 1-{MAX_NAME_LENGTH} characters")
        if role == "robot" and participant_name.startswith(DASHBOARD_IDENTITY_PREFIX):
            raise InvalidIdentityError(
                f"participantName may not start with '{DASHBOARD_IDENTITY_PREFIX}' (reserved for dashboard clients)"
            )
        ttl = self.operator_ttl if role == "operator" else self.ttl

        self.logger.info(f"Creating {role} token for {participant_name} at {room_name}")
        token = mint_room_token(
            self.api_key, self.api_secret,
            identity=participant_name,
            room_name=room_name,
            ttl=ttl,
            **grants,
        )
        server_url = self.operator_server_url if role == "operator" else self.server_url
        return {"token": token, "ttl": ttl, "server_url": server_url}
