"""
What the dispatcher decides in advance for each node of an order, so the robot can carry a
route out on its own when it loses its connection mid-route (robot team, 2026-10-08):
how close counts as reached (allowedDeviationXY/Theta), and the nodePolicy action (how
long to wait at a blocked node, whether it may be skipped).

Settings are read from the environment once, at import. mission-dispatch's image has no
packages/config.py (it needs credentials dispatch does not have), so the keys live here;
config.py points at this module.
"""
import logging
import os
from typing import Optional

import pydantic


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _optional_float(name: str, default: Optional[float]) -> Optional[float]:
    """A float, or None for "" / "none" / "off"."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return None if raw.strip().lower() in ("", "none", "off") else float(raw)


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class NodePolicyMode:
    OFF = "off"              # never send nodePolicy
    FACTSHEET = "factsheet"  # only to a robot whose factsheet lists the nodePolicy action
    ON = "on"                # always
    ALL = (OFF, FACTSHEET, ON)


class OrderPolicy(pydantic.BaseModel):
    # allowedDeviationXY (m): a node the robot drives through, the last node of an order
    # and a node with actions, and the start node (the robot's own pose).
    deviation_xy_pass_m: float = 0.35
    deviation_xy_final_m: float = 0.1
    deviation_xy_start_m: float = 0.35
    # allowedDeviationTheta (rad): heading is free on a pass-through node.
    deviation_theta_pass_rad: float = 3.1416
    deviation_theta_final_rad: float = 0.785
    # A waypoint value of 0 means "not set" (sati-client used to send 0 explicitly);
    # only a positive value overrides the policy.
    deviation_zero_is_unset: bool = True
    # A waypoint allowedDeviationXY equal to this is the old Pose2D default, which routes
    # stored before 2026-10-08 carry explicitly: read as not set too. None: off.
    deviation_xy_legacy_default_m: Optional[float] = 0.1

    node_policy_mode: str = NodePolicyMode.FACTSHEET
    node_policy_max_wait_s: float = 10.0

    # A mission's timeout does not run while its robot is offline: a route the robot
    # carries out on its own is not a stalled one.
    timeout_pause_offline: bool = True

    # A graph node the robot reported blocked is kept out of new routes this long.
    blocked_node_exclusion_min: float = 10.0
    # A REPLACE cancel (reroute) waits this long for the robot to adopt the order just
    # sent before cancelling it, so a second reroute right after the first does not
    # cancel an order version the robot has not even seen.
    cancel_min_dwell_s: float = 2.0

    @pydantic.validator("node_policy_mode", pre=True)
    def _known_node_policy_mode(cls, value):  # pylint: disable=no-self-argument
        mode = str(value).strip().lower()
        if mode not in NodePolicyMode.ALL:
            logging.getLogger(__name__).warning(
                "Unknown VDA5050_NODE_POLICY_MODE %r (one of %s); using %r", value,
                ", ".join(NodePolicyMode.ALL), NodePolicyMode.FACTSHEET)
            return NodePolicyMode.FACTSHEET
        return mode

    def deviation_xy(self, value: Optional[float], final: bool) -> float:
        legacy = self.deviation_xy_legacy_default_m
        if value is not None and legacy is not None and abs(value - legacy) < 1e-9:
            value = None
        if value is not None and (value > 0 or (value == 0 and not
                                                self.deviation_zero_is_unset)):
            return value
        return self.deviation_xy_final_m if final else self.deviation_xy_pass_m

    def deviation_theta(self, value: Optional[float], final: bool) -> float:
        if value is not None and (value > 0 or (value == 0 and not
                                                self.deviation_zero_is_unset)):
            return value
        return self.deviation_theta_final_rad if final else self.deviation_theta_pass_rad


def from_env() -> OrderPolicy:
    defaults = OrderPolicy()
    return OrderPolicy(
        deviation_xy_pass_m=_float("ROUTE_DEVIATION_XY_PASS_M", defaults.deviation_xy_pass_m),
        deviation_xy_final_m=_float("ROUTE_DEVIATION_XY_FINAL_M",
                                    defaults.deviation_xy_final_m),
        deviation_xy_start_m=_float("ROUTE_START_DEVIATION_XY_M",
                                    defaults.deviation_xy_start_m),
        deviation_theta_pass_rad=_float("ROUTE_DEVIATION_THETA_PASS_RAD",
                                        defaults.deviation_theta_pass_rad),
        deviation_theta_final_rad=_float("ROUTE_DEVIATION_THETA_FINAL_RAD",
                                         defaults.deviation_theta_final_rad),
        deviation_zero_is_unset=_bool("ROUTE_DEVIATION_ZERO_IS_UNSET",
                                      defaults.deviation_zero_is_unset),
        deviation_xy_legacy_default_m=_optional_float(
            "ROUTE_DEVIATION_XY_LEGACY_DEFAULT_M", defaults.deviation_xy_legacy_default_m),
        node_policy_mode=os.getenv("VDA5050_NODE_POLICY_MODE", defaults.node_policy_mode),

        node_policy_max_wait_s=_float("NODE_POLICY_MAX_WAIT_S",
                                      defaults.node_policy_max_wait_s),
        timeout_pause_offline=_bool("MISSION_TIMEOUT_PAUSE_OFFLINE",
                                    defaults.timeout_pause_offline),
        blocked_node_exclusion_min=_float("BLOCKED_NODE_EXCLUSION_MIN",
                                          defaults.blocked_node_exclusion_min),
        cancel_min_dwell_s=_float("ORDER_CANCEL_MIN_DWELL_S", defaults.cancel_min_dwell_s))


_current = from_env()


def current() -> OrderPolicy:
    return _current
