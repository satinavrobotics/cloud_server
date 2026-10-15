# scene_graph_sim

Offline simulator for `packages/scene_graph`. Reads only local files and the throw-away test ArangoDB
(`~/.sg_test_arango.env`, port 18529); refuses the live port.

```bash
# 1. read-only export from the live stack (once per map), then copy it into the throw-away instance
docker exec -i docker_compose-graph-builder-service-1 python - office8 /tmp/sg_export < export_live.py
docker cp docker_compose-graph-builder-service-1:/tmp/sg_export/. data/office8/
python seed_test_arango.py office8

# 2. fold the leaves through the engine and print what it makes (charts, links, time; --verify runs the verifier)
python run_engine.py office8 --every 6 --verify
```
`out/` and `data/` are not committed.

## Scripts

| Script | Purpose | Documented in |
|---|---|---|
| `branch_viewer/` | play git refs over a map's captures: per capture the robot pose, the map, clearance, price and charts, the planner route to five goals and five checks; writes one HTML page. `run.py` is the command line, `worker.py` plays one tree, `driver.py` exports the refs and builds the page, `encode.py` and `checks.py` are pure, `settings.py` holds the run settings, `page/` is the HTML, CSS and JS. Writes `out/branch_viewer.html` and `out/branch_viewer.json`. | this file, section below
| `export_live.py`, `seed_test_arango.py`, `run_engine.py` | export a map's leaves, load the throw-away ArangoDB, fold through the engine | this file |
| `accept_route_safety.py` | acceptance gates of the clearance-priced route (`--profile hinge\|exp`) through the production lazy path | `docs/scene_graph/route_safety.md`, `inflation_profile_validation.md` |
| `bench_route_safety.py`, `bench_*.py` | timing and size benchmarks of the engine, store and pricing | `docs/scene_graph/route_safety.md` |
| `inflation_campaign.py`, `inflation_campaign_report.py`, `inflation_efficiency.py`, `inflation_operational.py` | the validation campaign of the exponential tier (grid, perturbation, synthetic worlds, operational pairs) | `docs/scene_graph/inflation_profile_validation.md` |
| `research/inflation/` | the research scripts behind `inflation_profile_design.md` (budget sweep, two-pass floor, waypoint rule) | `docs/scene_graph/inflation_profile_design.md` |
| `close_evidence_replay.py` | independent replay of the close-evidence rule on a leaf export, with the component table | `docs/scene_graph/close_evidence_and_turn_cost_design.md` A.3, A.4 |
| `close_evidence_engine_check.py` | the same numbers through the engine (`near_range_m`, `--min-clearance` for the away-from-walls filter, `--old-rule` to hold by the latest lethal from any distance for comparison) | same, A.6b, A.6c |
| `close_evidence_route_changes.py` | what the rule and the filter do to routes: navigable cells, held components, 150 pairs same / changed / lost, through the production store and lazy reader | same, A.6c |
| `outdoor_world.py`, `outdoor_world_export.py` | conceptual forest + meadow world through the production path (fold, charts at two radii, plans with the live pricing, turn cost and waypoint budget); the export writes one JSON for a viewer | `docs/scene_graph/close_evidence_and_turn_cost_design.md` E |
| `turn_cost_prototype.py` | turn-cost search on the research lattice: forms and lambda sweep | same, B.2 |
| `turn_cost_restricted.py` | the exact ellipse-restricted turn-cost search: exactness and cost on the real and synthetic maps | same, B.5 |

The `turn_cost_*` scripts are research tools for a feature that is not built; they are kept so the numbers of the design can be
reproduced. `depth_occupancy_backfill.py` and `write_occupancy_depth.py` belong to the occupancy view
(`docs/scene_graph/occupancy_view_proposal.md`).

## Branch viewer (for checking a change to the scene graph)

```bash
# the maps must be exported first (export_live.py, as above) into data/<map>/
python tools/scene_graph_sim/branch_viewer/run.py --map office8 --map office7 --ref main --ref <branch-or-sha> --data tools/scene_graph_sim/data
# open tools/scene_graph_sim/out/branch_viewer.html; the table at the top lists failing captures per branch and reason
# unit tests of the encodings and checks: pytest tests/unit/scene_graph/test_branch_viewer_encode.py
```

Each ref is exported with `git archive` and runs its own code, so a branch's behaviour is shown as it is committed, not as the
working tree is. A failing check is a finding to read, not a pass: `goal_not_navigable` in the first captures is expected while the
goal is still unobserved.
