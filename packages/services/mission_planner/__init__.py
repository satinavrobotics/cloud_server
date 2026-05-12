"""
Mission Planner Service

Plans navigation missions by querying graph database and submitting to Mission Dispatcher.
"""

try:
    from .server import MissionPlannerService
    __all__ = ["MissionPlannerService"]
except ImportError:
    # Server module not available (e.g., when only client is copied)
    __all__ = []

