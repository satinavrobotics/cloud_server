#!/usr/bin/env python3
"""
LiveKit Service Module

Token generation service for LiveKit video conferencing.
"""

# Always export the client (used by other services)
from .client import LiveKitClient

__all__ = ['LiveKitClient']

# Only import server if livekit-api is available (for the service itself)
try:
    from .server import LiveKitService
    __all__.append('LiveKitService')
except ImportError:
    # Server not available - this is expected when used as a client library
    pass

