#!/usr/bin/env python3
"""
API Delegation Module

Single entry point for clients to interact with the robot fleet system.
Provides REST and WebSocket interfaces for:
- Map loading and updates
- Image retrieval
- Navigation requests
- Mission status monitoring
"""

from .server import ApiDelegationService

__all__ = [
    'ApiDelegationService',
]

