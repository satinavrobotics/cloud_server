"""Parameters of the scene graph. Units are metres and cells; tau is dimensionless."""
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Params:
    res: float = 0.05           # cell size of the costmaps (m)
    tau: float = 1.5            # chart bound: every cell of a region is within tau x straight distance of its anchor
    hysteresis: float = 0.15    # relative band on the bottleneck and chart tests around the previous partition
    min_place: float = 1.5      # a basin with peak clearance below this x robot radius is transit, not a place
    free_detour: float = 4.0    # detour around an obstacle that costs nothing in the chart test, in robot radii
    gamma: float = 0.5          # bottleneck: regions merge only through a passage >= gamma x the narrower peak clearance
    robot_radius: float = 0.25  # cells closer than this to a blocked cell are not navigable (m)
    budget: int = 40_000        # most cells one mcmap may hold (resource bound, = one leaf window)
    slack: float = 0.05         # an anchor moves only if the new place is better by this fraction of mean cost
    soft_weight: float = 1.0    # cost of a soft costmap value v: 1 + soft_weight * v / 100
    clear_weight: float = 2.0   # cost near blocked cells: clear_weight * exp(-clear_decay * (clearance - robot_radius))
    clear_decay: float = 3.0    # 1 / m
    v_block: int = 100          # costmap values >= v_block are lethal
    margin_m: float = 1.5       # context around the touched region, so clearance is not cut at the crop edge
    drive_gap_m: float = 2.0    # consecutive leaves of a session closer than this are joined by the robot's path
    knn_attach_m: float = 1.0   # blocked known cells join the nearest region within this distance

    @property
    def free_detour_m(self) -> float:
        return self.free_detour * self.robot_radius

    @property
    def block(self) -> int:
        """Cells per side of a coarse block: the largest for which a step between neighbouring blocks, diagonals
        included, cannot reach across a wall (opposite sides of a wall are at least 2 x robot_radius apart)."""
        return max(1, int((2.0 * self.robot_radius / (math.sqrt(2.0) * self.res) + 1.0) / 2.0))
