"""Helpers shared by the code that talks to a robot's orchestrator services (mapping switch,
relocalization, stored-map reads)."""

from typing import Optional, Sequence


def pick_service(listed: Sequence[str], candidates: Sequence[str]) -> Optional[str]:
    """The first candidate the orchestrator lists (config order), or None."""
    for name in candidates:
        if name in listed:
            return name
    return None
