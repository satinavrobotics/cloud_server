"""
Naming of the VDA5050 ids the mission dispatcher generates for a mission.

A VDA5050 ``orderId`` identifies one order: a robot may treat a repeated id as the
same order (a replay or a continuation), so the dispatcher must never send two
different orders under one id -- neither across runs of a mission that happens to
reuse a name (delete + re-create), nor within a run when a cancelled node is resent
with new content (operator route update, edge-blocked reroute).

Every id therefore starts with a *prefix* built from the mission name plus two
dispatcher-owned status fields (``MissionStatusV1.run_id`` / ``order_rev``):

    order id   "{prefix}-n{node_idx}"
    node id    "{prefix}-n{node_idx}-s{sequence}"
    prefix     "{name}"                       legacy: mission already running before
                                              run_id existed
               "{name}-r{run_id}"             run_id set, order_rev 0
               "{name}-r{run_id}v{order_rev}" run_id set, order_rev > 0

The "-n{idx}" / "-s{seq}" suffixes are unchanged. Everything that builds a prefix or
reads one of these ids back -- order/node matching, node indexes, sequence ids --
goes through this module, so the grammar lives in one place.
"""
import re
from typing import Optional

# The one grammar for the suffixes we generate; everything that reads an id back
# (orders, nodes, and node references in robot errors) goes through these.
_ORDER_SUFFIX = re.compile(r"-n(\d+)$")
_NODE_SUFFIX = re.compile(r"-n(\d+)(?:-s(\d+))?$")


def run_prefix(name: str, run_id: Optional[str], order_rev: int = 0) -> str:
    """Prefix shared by every order and node id of one revision of one run."""
    if not run_id:
        # A mission that was already running when run_id was introduced keeps the
        # ids it has already sent, or the robot would see its order change id
        # mid-mission.
        return name
    prefix = f"{name}-r{run_id}"
    return f"{prefix}v{order_rev}" if order_rev > 0 else prefix


def order_prefix(order_id: str) -> Optional[str]:
    """The prefix of an order id we generated ("{prefix}-n{idx}"), else None."""
    match = _ORDER_SUFFIX.search(order_id)
    return order_id[:match.start()] if match else None


def order_node_index(order_id: str) -> int:
    """The mission_tree index of an order id we generated. Raises ValueError if
    ``order_id`` is not one of ours."""
    match = _ORDER_SUFFIX.search(order_id)
    if match is None:
        raise ValueError(f"not an order id we generated: {order_id!r}")
    return int(match.group(1))


def is_order_of(prefix: str, order_id: str) -> bool:
    """True if ``order_id`` is an order generated for exactly this prefix."""
    return order_prefix(order_id) == prefix


def node_index(node_id: str) -> Optional[int]:
    """The mission_tree index encoded in a node id we generated, else None."""
    match = _NODE_SUFFIX.search(node_id)
    return int(match.group(1)) if match else None


def node_sequence(node_id: str) -> Optional[int]:
    """The sequence id encoded in a node id we generated ("...-s{seq}"), else None."""
    match = _NODE_SUFFIX.search(node_id)
    return int(match.group(2)) if match and match.group(2) is not None else None


def is_node_of(prefix: str, node_id: str) -> bool:
    """True if ``node_id`` is a node generated for exactly this prefix. Anything else
    -- another mission's node, another run's or revision's, or the empty id a robot
    reports before its first order -- is not progress in the current order.

    Exact, not ``startswith``: a legacy prefix ("m1") must not claim the nodes of a
    run-qualified one ("m1-r1234abcd-...") or of a mission named "m1-other".
    """
    match = _NODE_SUFFIX.search(node_id)
    return match is not None and node_id[:match.start()] == prefix
