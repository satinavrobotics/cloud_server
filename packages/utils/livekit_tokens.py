#!/usr/bin/env python3
"""
LiveKit access-token minting shared by the two token services.

packages/services/livekit (LiveKit Cloud, caller-chosen grants) and
packages/services/livekit_sfu_tokens (self-hosted SFU, role-scoped grants) differ
in who decides the permissions, the identity rules and the URLs they hand out,
but build the JWT identically. Only that step lives here; policy stays in each
service.

Requires the livekit-api package, so import this module directly rather than
through a package that other services load without it.
"""

from datetime import timedelta
from typing import Optional

from livekit import api


def mint_room_token(
    api_key: str,
    api_secret: str,
    identity: str,
    room_name: str,
    ttl: int,
    *,
    can_publish: bool,
    can_subscribe: bool,
    can_publish_data: bool,
    display_name: Optional[str] = None,
    metadata: Optional[str] = None,
) -> str:
    """
    Sign a JWT that lets `identity` join `room_name` for `ttl` seconds.

    All three grants are required keyword arguments: the SDK defaults are
    fragile (VideoGrants writes canPublishData=true unless told otherwise, and
    LiveKit resolves an omitted canPublishData to canPublish), so every caller
    states them.
    """
    token = api.AccessToken(api_key, api_secret)
    token.with_identity(identity)
    if display_name is not None:
        token.with_name(display_name)
    if metadata:
        token.with_metadata(metadata)
    token.with_ttl(timedelta(seconds=ttl))
    token.with_grants(api.VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=can_publish,
        can_subscribe=can_subscribe,
        can_publish_data=can_publish_data,
    ))
    return token.to_jwt()
