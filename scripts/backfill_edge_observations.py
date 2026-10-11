#!/usr/bin/env python3
"""Fill edge_observations (per-edge traversal statistics) from data that already exists.

    python scripts/backfill_edge_observations.py [--apply] [--compose-env docker_compose/.env]
    docker compose ... exec api-delegation-service python scripts/backfill_edge_observations.py

Sources, both READ-ONLY: `run_legs` of finished runs (not RUNNING ones: dispatch writes those at
run end) and the finished MAPPING sessions of `map_sessions` with their nodes in ArangoDB. An
observation counts only on a pair that is an edge of its map in ArangoDB now (the rules of
packages/utils/edge_stats.py: no topomap ids, duration <= 0 or too long, speed too high, or no
such edge: dropped). Per (map, edge, robot) only the newest EDGE_STATS_WINDOW observations are
kept, as the live writers do.

The default is a DRY RUN: it counts and prints, writes nothing (the Postgres session is READ ONLY).
`--apply` writes `edge_observations`; it is idempotent (the primary key is the source's own id:
run_id:seq, session_id:step), so running it again, or after the live writers ran, adds nothing.
Environment as scripts/export_map.py (the services' names; `--compose-env FILE` reads
docker_compose/.env for a run on the cloud host against localhost).
"""
import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from packages import config  # noqa: E402
from packages.api import session_trace as api_trace  # noqa: E402
from packages.utils import edge_stats as es  # noqa: E402
from packages.utils import edge_stats_db as db  # noqa: E402
from packages.utils import map_sessions as ms  # noqa: E402

logger = logging.getLogger("backfill_edge_observations")

LEG_FIELDS = ("run_id", "seq", "robot_name", "map_id", "from_topomap_node", "to_topomap_node",
              "ended_at", "duration_s", "straight_m", "recoveries", "blocks")
LEGS_SQL = (f"SELECT {', '.join('l.' + f for f in LEG_FIELDS)} FROM run_legs l "
            "JOIN mission_runs r USING (run_id) WHERE r.state <> 'RUNNING' "
            "AND l.from_topomap_node IS NOT NULL AND l.to_topomap_node IS NOT NULL "
            "AND l.map_id IS NOT NULL ORDER BY l.run_id, l.seq")
SESSIONS_SQL = ("SELECT session_id, map_name, robot_name, purpose FROM map_sessions "
                "WHERE ended_at IS NOT NULL AND purpose = 'mapping' ORDER BY started_at, session_id")


def collect(sources: Any) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """(the observations to store, counts). `sources`: legs() -> run_legs row dicts (LEG_FIELDS),
    sessions() -> {session_id, map_name, robot_name, purpose} dicts, session_rows(map, sid) ->
    GraphDatabaseService.session_trace_nodes rows, edges(map) -> get_edges rows. Pure apart from
    those reads."""
    counts = {"legs": 0, "leg_unusable": 0, "sessions": 0, "mapping_candidates": 0,
              "non_edge": 0, "stored_after_window": 0}
    edge_cache: Dict[str, Any] = {}

    def edges_of(map_id: str):
        if map_id not in edge_cache:
            edge_cache[map_id] = es.edge_set(sources.edges(map_id))
        return edge_cache[map_id]

    found: List[Dict[str, Any]] = []
    for leg in sources.legs():
        counts["legs"] += 1
        obs = es.leg_observation(leg)
        if obs is None:
            counts["leg_unusable"] += 1
            continue
        if (obs["from_node"], obs["to_node"]) not in edges_of(obs["map_id"]):
            counts["non_edge"] += 1
            continue
        found.append(obs)
    for session in sources.sessions():
        if ms.purpose_of(session) != ms.MAPPING:
            continue
        counts["sessions"] += 1
        nodes = api_trace.ordered_nodes(
            sources.session_rows(session["map_name"], str(session["session_id"])))
        candidates = es.session_observations(session["map_name"], str(session["session_id"]),
                                             session["robot_name"], nodes)
        counts["mapping_candidates"] += len(candidates)
        kept = es.on_edges(candidates, edges_of(session["map_name"]))
        counts["non_edge"] += len(candidates) - len(kept)
        found.extend(kept)
    groups: Dict[Tuple[str, str, str, str], List[Dict[str, Any]]] = {}
    for obs in found:
        groups.setdefault((obs["map_id"], obs["from_node"], obs["to_node"], obs["robot_name"]),
                          []).append(obs)
    out = [dict(o) for g in groups.values() for o in es.newest(g)]
    counts["stored_after_window"] = len(out)
    return out, counts


def write(cursor: Any, observations: Iterable[Mapping[str, Any]],
          window: Optional[int] = None) -> int:
    """The synchronous twin of edge_stats_db.store (same SQL). Returns the rows inserted."""
    observations = list(observations)
    inserted = 0
    for obs in observations:
        cursor.execute(db.INSERT_SQL, tuple(obs[c] for c in db.COLUMNS))
        inserted += max(0, cursor.rowcount)
    for params in db.prune_params(observations, window):
        cursor.execute(db.PRUNE_SQL, params)
    return inserted


def run(sources: Any, connect_write: Any = None, apply: bool = False) -> Dict[str, Any]:
    """Collect, and with `apply` write through `connect_write()` (a context manager yielding a
    cursor in a committing transaction). Returns the counts."""
    observations, counts = collect(sources)
    counts["apply"] = apply
    if apply:
        with connect_write() as cursor:
            counts["inserted"] = write(cursor, observations)
    return counts


class Sources:
    """The real read-only sources: Postgres (READ ONLY session) and ArangoDB."""

    def __init__(self, env: Mapping[str, str]):
        import psycopg
        from arango import ArangoClient
        self.env = env
        self.psycopg = psycopg
        client = ArangoClient(hosts=f"http://{env.get('ARANGO_HOST', 'localhost')}:"
                                    f"{env.get('ARANGO_PORT', '8529')}")
        self.arango = client.db(env.get("DATABASE_NAME", "topomap_db"),
                                username=env.get("ARANGO_USERNAME", "root"),
                                password=env.get("ARANGO_PASSWORD", ""))
        self.read = self._connect(read_only=True)

    def _connect(self, read_only: bool):
        e = self.env
        return self.psycopg.connect(
            host=e.get("POSTGRES_DATABASE_HOST", "localhost"),
            port=int(e.get("POSTGRES_DATABASE_PORT", "5432")),
            dbname=e.get("POSTGRES_DATABASE_NAME", "mission"),
            user=e.get("POSTGRES_DATABASE_USERNAME", "postgres"),
            password=e.get("POSTGRES_DATABASE_PASSWORD", ""),
            options="-c default_transaction_read_only=on" if read_only else None,
            connect_timeout=5)

    def connect_write(self):
        return _Writer(self._connect(read_only=False))

    def legs(self):
        with self.read.cursor() as cur:
            cur.execute(LEGS_SQL)
            for row in cur:
                yield dict(zip(LEG_FIELDS, row))

    def sessions(self):
        with self.read.cursor() as cur:
            cur.execute(SESSIONS_SQL)
            return [dict(zip(("session_id", "map_name", "robot_name", "purpose"), row))
                    for row in cur.fetchall()]

    def session_rows(self, map_id: str, session_id: str):
        from packages.topomap_dbs.graph_db.server import GraphDatabaseService
        name = f"nodes_{map_id}"
        if not self.arango.has_collection(name):
            return []
        return list(self.arango.aql.execute(GraphDatabaseService.SESSION_TRACE_AQL,
                                            bind_vars={"@col": name, "sid": session_id}))

    def edges(self, map_id: str):
        col = f"edges_{map_id}"
        if not self.arango.has_collection(col):
            return []
        return [{"from_node_id": e["_from"].split("/")[-1], "to_node_id": e["_to"].split("/")[-1]}
                for e in self.arango.collection(col).all()]


class _Writer:
    """A cursor in one committing transaction (commit on a clean exit, else rollback)."""

    def __init__(self, conn: Any):
        self.conn = conn

    def __enter__(self):
        return self.conn.cursor()

    def __exit__(self, exc_type, exc, tb):
        try:
            self.conn.rollback() if exc_type else self.conn.commit()
        finally:
            self.conn.close()
        return False


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--apply", action="store_true", help="write (default: dry run)")
    p.add_argument("--compose-env", help="read credentials from docker_compose/.env")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    env = dict(os.environ)
    if args.compose_env:
        from scripts import export_map
        env.update(export_map.compose_env(args.compose_env))
    sources = Sources(env)
    summary = run(sources, sources.connect_write, apply=args.apply)
    print(json.dumps(summary, indent=1))
    if not args.apply:
        print("dry run: nothing written (pass --apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
