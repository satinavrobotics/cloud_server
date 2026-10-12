# scene_graph_sim

Offline simulator for `packages/scene_graph`. Reads only local files and the throw-away test ArangoDB
(`~/.sg_test_arango.env`, port 18529); refuses the live port.

```bash
# 1. read-only export from the live stack (once per map), then copy it into the throw-away instance
docker exec -i docker_compose-graph-builder-service-1 python - office8 /tmp/sg_export < export_live.py
docker cp docker_compose-graph-builder-service-1:/tmp/sg_export/. data/office8/
python seed_test_arango.py office8

# 2. replay the leaves, write PNG frames (map by owner, anchors, links), pipeline.png and metrics.csv
python run_sim.py office8 --every 10 --tau 1.5 --radius 0.25        # --shuffle SEED to change the order
python run_worlds.py [name ...]                                       # synthetic reference worlds
python store_test_arango.py office8                                   # sg_* collections + SHORTEST_PATH
```
`out/` and `data/` are not committed.
