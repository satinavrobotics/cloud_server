#!/usr/bin/env python3
"""
LiveKit SFU Token Service Module

Role-scoped token service for the self-hosted, Tailscale-only LiveKit server.
"""

from .server import (
    DASHBOARD_IDENTITY_PREFIX, InvalidIdentityError, LiveKitSfuTokenService, ROLE_GRANTS, UnknownRoleError,
)

__all__ = ['DASHBOARD_IDENTITY_PREFIX', 'InvalidIdentityError', 'LiveKitSfuTokenService',
           'ROLE_GRANTS', 'UnknownRoleError']
