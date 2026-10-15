"""The settings a branch is played with. They mirror the deployed values of docker_compose/.env and the config defaults; they are
written here, not read from the environment, so that a run is the same for every agent and every ref.

close-evidence radius 1.3 m and away-from-walls 0.25 m (SCENE_GRAPH_NEAR_RANGE_M, SCENE_GRAPH_NEAR_MIN_CLEARANCE_M), turn cost lambda
0.7 m/rad (the fallback, capped at 0.7: the robot's factsheet is not sent to the tool), clearance pricing kappa 1.0
(the default), robot 0.5 m square. The exponential profile (`exponential=True`) is the design's: reach 0.8 m, k 6.0, kappa 0.75.
"""
ROBOT_HALF_SIDE_M = 0.25
NEAR_RANGE_M = 1.3
NEAR_MIN_CLEARANCE_M = 0.25
TURN_LAMBDA_M_PER_RAD = 0.7
KAPPA_LINEAR = 1.0
KAPPA_EXPONENTIAL = 0.75
EXP_REACH_M = 0.8
EXP_SCALING = 6.0
CHART_RADIUS_M = 2.0
SNAP_M = 0.5           # the planner's snap limit for the start and the goal
SEG_SAMPLES = 30       # points sampled along each route segment for the navigable check
RES_M = 0.05           # the costmap cell
GOAL_FRACTIONS = (0.2, 0.4, 0.6, 0.8, 1.0)
CLR_MAX_M = 2.0        # the clearance colour scale ends here
