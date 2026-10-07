"""Battery-unknown handling for VDA5050 state messages.

A robot with no working battery sensor reports ``batteryCharge = 0.0`` together
with a WARNING ``BATTERY_UNKNOWN`` in ``errors[]`` (re-sent in every state
message). A real empty battery and "unknown" look identical in the charge
field, so consumers must check this flag before acting on the value.
"""
from typing import Iterable

BATTERY_UNKNOWN_ERROR_TYPE = "BATTERY_UNKNOWN"


def battery_unknown(errors: Iterable) -> bool:
    """True if errors[] carries the BATTERY_UNKNOWN marker."""
    return any(e.errorType == BATTERY_UNKNOWN_ERROR_TYPE for e in errors or ())
