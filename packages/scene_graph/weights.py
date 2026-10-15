"""Integer edge costs of the scene-graph route search: the one place that chooses them, shared by the engine (`ChartGraph.route`)
and the lazy reader (`lazy_plan.PlanGraph._search`), so the two cannot disagree.

An edge of length l (m) costs `ceil(l / ROUTE_UNIT_M * (1 + rate * deficit / 2) - 1e-6)`, at least 1, where `deficit` is the sum of
the clearance prices of its two end cells (api.edge_weight). rate 0 is the unpriced length.
"""
import numpy as np

from packages.scene_graph.api import edge_weight

ROUTE_UNIT_M = 1e-4     # the route cost unit: a path cost is an integer count of these


def edge_costs(length_m, deficit_sum, rate):
    """The integer edge costs of a route search: length_m the edges' lengths (m, array); deficit_sum the sum of the two end cells'
    prices per edge, used only when rate > 0."""
    units = np.asarray(length_m, float) / ROUTE_UNIT_M
    if rate:
        return edge_weight(units, deficit_sum, rate)
    return np.maximum(np.ceil(units - 1e-6), 1.0)
