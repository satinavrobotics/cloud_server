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
