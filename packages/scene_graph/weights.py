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


def region_costs(length_m, rho_a, rho_b, scale):
    """The integer cost of the time a region statistic says the edges take beyond the ideal, added to `edge_costs`: an edge of
    length l between cells of excess rho_a and rho_b (ints, units of 1 / scale, the chart's excess time per metre x speed_max)
    costs `ceil(l / ROUTE_UNIT_M * (rho_a + rho_b) / (2 * scale) - 1e-6)` more, 0 for rho 0. Never negative, so every edge still
    costs at least its length and the search bounds stay valid."""
    units = np.asarray(length_m, float) / ROUTE_UNIT_M
    extra = np.ceil(units * (np.asarray(rho_a, float) + np.asarray(rho_b, float)) / (2.0 * scale) - 1e-6)
    return np.maximum(extra, 0.0)
