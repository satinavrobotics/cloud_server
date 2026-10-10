"""What a topomap node id looks like (graph-builder's `_generate_global_node_id` is a UUID4).

Anything else in a map bucket's top level (`reconstruction/`, ...) is not a node, and deleting
"its objects" would delete that data.
"""
import uuid

NON_NODE_PREFIXES = frozenset({"reconstruction"})


def is_node_id(value: object) -> bool:
    """True for the canonical lower-case UUID string graph-builder generates."""
    if not isinstance(value, str) or value in NON_NODE_PREFIXES:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False
