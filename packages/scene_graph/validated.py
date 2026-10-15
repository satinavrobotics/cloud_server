"""Validated-first planning (docs/scene_graph/close_evidence_and_turn_cost_design.md part G): the one place that builds the penalised
edge weights, shared by `engine.ChartGraph.route` and `lazy_plan.PlanGraph._search`.

Definitions. A costmap cell is VALIDATED when the costmap was reported free from a capture pose within `near_range_m` of it (the close-
evidence plane `Fn` >= 0) or the robot's body covered it (the driven image S). A lattice NODE (a 0.1 m block of 2 x 2 costmap cells) is
validated when any cell of its block is. An EDGE is unvalidated when either end node is unvalidated. The engine derives the node flag
(`ChartGraph._validated_nodes`), the store keeps it as the uint8 tile plane `val`, and both readers hand it to `penalise`.

Objective (lexicographic): among all routes minimise the number of unvalidated edges, and among those the cost J (the priced length
plus the turn cost, an integer in units of 1e-4 m). Realised as one ordinary shortest-path problem with the integer weights

    w'(e) = w(e) + P * [e unvalidated],        P = 1 + sum of the edge weights + (n - 2) * t_max

(n nodes of the graph, t_max the largest turn cost of one corner, 0 without a turn cost).

Exactness. (1) Removing a cycle from a path never increases its count c, nor its J: it deletes edges (weights >= 1) and, at the
junction of the cycle, the turn cost is a metric on headings (the triangle inequality of the wrapped angle), so the one direct turn
costs no more than the turns around the cycle. So the lexicographic optimum over all walks is attained by a simple path. (2) A simple
path uses each undirected edge at most once and has at most n - 2 corners, so its J <= sum w + (n - 2) * t_max < P. (3) For simple
paths A, B: if c_A < c_B then w'(A) = P c_A + J_A < P (c_A + 1) <= P c_B <= w'(B); if c_A = c_B then w'(A) < w'(B) iff J_A < J_B. So on
simple paths the order of w' IS the lexicographic order (c, J), and a walk with a cycle costs at least as much as its simple path
(w' >= 1, the cycle argument of (1)). Hence a path minimises w' iff it minimises (c, J); Dijkstra is exact for non-negative integer
weights, held exactly in float64 while every sum stays below 2**53 (checked in `penalise`); the canonical (y, x) tie rule of the plain
search applies unchanged to the set of w'-minimal paths, which is the set of (c, J)-minimal paths whatever P is, so readers that use a
different P (the lazy reader's subgraph, the engine's whole graph) return the same cells. The turn search (`turn.py`) takes w' as its
edge weights and adds the turn table: its optimum minimises w'-sum plus turns = P c + J, the same argument. Given a route of w'-cost
T, the count is c = [number of unvalidated edges on the path] and J = T - P c (always 0 <= J < P).

Float64 bound: any distance is at most P (n + 1) + sum w; `penalise` refuses (ValueError) a graph for which that is >= 2**53. On the
largest stored map (Hospital, about 17 000 cells) and on a 117 000-cell synthetic map the bound is about 1e12 and 3e14 (design G.4).
"""
from typing import Optional, Tuple

import numpy as np

EXACT = float(2 ** 53)           # integers up to here are exact in float64


def edge_flags(node_valid: np.ndarray, R: np.ndarray, C: np.ndarray) -> np.ndarray:
    """bool per edge (R[i], C[i]): True when an end node is unvalidated. `node_valid` is the bool flag per node."""
    return ~(node_valid[R] & node_valid[C])


def penalty(w_sum: float, n_nodes: int, t_max: float = 0.0) -> int:
    """P: larger than the J of any simple path (module docstring)"""
    return int(w_sum) + 1 + max(int(n_nodes) - 2, 0) * int(t_max)


def penalise(W: np.ndarray, unval: np.ndarray, n_nodes: int, t_max: float = 0.0) -> Tuple[np.ndarray, int]:
    """(w' = W + P * unval as float64, P). Raises ValueError when a distance could leave float64's exact integer range."""
    w_sum = float(W.sum())
    p = penalty(w_sum, n_nodes, t_max)
    bound = float(p) * (int(n_nodes) + 1) + w_sum
    if not bound < EXACT:
        raise ValueError("validated-first: the weights of a graph of %d nodes would reach %.3g, beyond the exact range of float64 (%.3g)"
                         % (n_nodes, bound, EXACT))
    return W + float(p) * unval, p


def path_summary(ok_path: np.ndarray, ys: np.ndarray, xs: np.ndarray, h: float) -> Tuple[int, float]:
    """(unvalidated edges, their length in m) of a path given the validity flag and the lattice (y, x) of its nodes, in order"""
    bad = ~(ok_path[:-1] & ok_path[1:])
    step = np.hypot(np.diff(np.asarray(ys, np.int64)), np.diff(np.asarray(xs, np.int64))) * h
    return int(bad.sum()), float(step[bad].sum())


def split(total: float, p: int, count: int) -> int:
    """J of a route whose w'-cost is `total` and which has `count` unvalidated edges (checked: 0 <= J < P)"""
    j = int(total) - p * int(count)
    if not 0 <= j < p:
        raise AssertionError("validated-first: cost %r with %d unvalidated edges and P %d is inconsistent" % (total, count, p))
    return j


def tmax(tab: Optional[np.ndarray]) -> float:
    """the largest corner cost of a turn table (0 without a turn cost)"""
    return 0.0 if tab is None else float(tab.max())
