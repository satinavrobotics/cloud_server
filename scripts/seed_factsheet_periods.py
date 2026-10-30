#!/usr/bin/env python3
"""Write known factsheet history into robot_factsheet_periods (source 'seed').

    python scripts/seed_factsheet_periods.py --robot formidable-peacock --from 2026-10-12T00:00:00Z \\
        --speed-max 0.24 --angular-speed-max 1.0 --length 0.8 --width 0.5 --agv-class CARRIER [--apply] [--replace]
    python scripts/seed_factsheet_periods.py --file periods.json [--apply] [--replace] [--compose-env docker_compose/.env]

`periods.json` is a list of objects with `robot` and `from` (ISO-8601; no zone = UTC) and any of `speed_max`,
`angular_speed_max`, `acceleration_max`, `length`, `width`, `agv_class`. A figure left out, or negative (the robot's -1 for
unknown), is stored NULL. A period lasts until the next one of the robot; the FleetRecorder also writes a period whenever a
received factsheet differs from the latest one (source 'factsheet'), so seed the history BEFORE the first change it knows.

The default is a DRY RUN: it prints what it would write (new / exists) and writes nothing (the transaction is READ ONLY).
`--apply` writes all entries in one transaction. An entry whose (robot, from) already exists is refused (exit 2, nothing
written) unless `--replace`, which overwrites it. Seeding changes no run label by itself: afterwards relabel the robot's runs with
    python scripts/backfill_run_cells.py --recompute --relabel --robot NAME [--apply]
(docs/region_stats_2026-10-27.md, "Factsheet periods"). Environment as scripts/backfill_run_cells.py.
"""
import argparse
import datetime
import json
import os
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from packages.utils import factsheet_periods as fp  # noqa: E402

KEYS = {"robot", "from", "agv_class"} | set(fp.NUMERIC)
EXISTS_SQL = f"SELECT 1 FROM {fp.TABLE} WHERE robot_name = %s AND valid_from = %s"
UPSERT_SQL = (fp.INSERT_SQL[:fp.INSERT_SQL.index(" ON CONFLICT")] +
              " ON CONFLICT (robot_name, valid_from) DO UPDATE SET " +
              ", ".join(f"{f} = EXCLUDED.{f}" for f in fp.FIELDS) + ", source = EXCLUDED.source, recorded_at = now()")


def parse_time(text: str) -> datetime.datetime:
    value = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    return value.replace(tzinfo=datetime.timezone.utc) if value.tzinfo is None else value.astimezone(datetime.timezone.utc)


def entry(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """One validated entry: {robot_name, valid_from, <FIELDS>}."""
    unknown = set(raw) - KEYS
    if unknown:
        raise ValueError(f"unknown key(s) {sorted(unknown)} in {dict(raw)}")
    if not raw.get("robot") or not raw.get("from"):
        raise ValueError(f"an entry needs 'robot' and 'from': {dict(raw)}")
    out: Dict[str, Any] = {"robot_name": str(raw["robot"]), "valid_from": parse_time(raw["from"]),
                           "agv_class": str(raw.get("agv_class") or "").strip() or None}
    for field in fp.NUMERIC:
        out[field] = fp.figure(raw.get(field))
    return out


def entries_of(args: argparse.Namespace) -> List[Dict[str, Any]]:
    if args.file:
        with open(args.file) as f:
            return [entry(r) for r in json.load(f)]
    if not (args.robot and args.from_):
        raise SystemExit("give --file, or --robot and --from")
    raw = {"robot": args.robot, "from": args.from_, "agv_class": args.agv_class}
    raw.update({f: getattr(args, f) for f in fp.NUMERIC})
    return [entry(raw)]


def plan(cur: Any, entries: Sequence[Dict[str, Any]]) -> List[str]:
    """'new' or 'exists' per entry."""
    out = []
    for e in entries:
        cur.execute(EXISTS_SQL, (e["robot_name"], e["valid_from"]))
        out.append("exists" if cur.fetchone() is not None else "new")
    return out


def write(cur: Any, entries: Sequence[Dict[str, Any]], replace: bool) -> None:
    for e in entries:
        cur.execute(UPSERT_SQL if replace else fp.INSERT_SQL,
                    (e["robot_name"], e["valid_from"], *(e[f] for f in fp.FIELDS), "seed"))


def run(conn: Any, entries: Sequence[Dict[str, Any]], apply: bool, replace: bool) -> int:
    """Print the plan; write when `apply`. 0 ok, 2 refused (an existing period without --replace; nothing written)."""
    with conn.cursor() as cur:
        if not apply:
            cur.execute("SET TRANSACTION READ ONLY")
        states = plan(cur, entries)
        for e, state in zip(entries, states):
            shown = {f: e[f] for f in fp.FIELDS}
            print(f"{state:7} {e['robot_name']} from {e['valid_from'].isoformat()} {json.dumps(shown)}")
        if "exists" in states and not replace:
            print("refused: a period with that (robot, from) exists; pass --replace to overwrite it")
            conn.rollback()
            return 2
        if not apply:
            conn.rollback()
            print("dry run: nothing written (pass --apply)")
            return 0
        write(cur, entries, replace)
    conn.commit()
    print(f"wrote {len(entries)} period(s)")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--file", help="JSON list of entries")
    p.add_argument("--robot")
    p.add_argument("--from", dest="from_", help="ISO-8601 time the figures took effect")
    p.add_argument("--agv-class", dest="agv_class")
    for f in fp.NUMERIC:
        p.add_argument("--" + f.replace("_", "-"), dest=f, type=float)
    p.add_argument("--apply", action="store_true", help="write (default: dry run)")
    p.add_argument("--replace", action="store_true", help="overwrite an existing (robot, from)")
    p.add_argument("--compose-env", help="read credentials from docker_compose/.env")
    args = p.parse_args(argv)
    entries = entries_of(args)
    env = dict(os.environ)
    if args.compose_env:
        from scripts import export_map
        env.update(export_map.compose_env(args.compose_env))
    import psycopg
    with psycopg.connect(
            f"host={env.get('POSTGRES_DATABASE_HOST', 'localhost')} port={env.get('POSTGRES_DATABASE_PORT', '5432')} "
            f"dbname={env.get('POSTGRES_DATABASE_NAME', 'mission')} user={env.get('POSTGRES_DATABASE_USERNAME', 'postgres')} "
            f"password={env.get('POSTGRES_DATABASE_PASSWORD', '')} connect_timeout=5") as conn:
        return run(conn, entries, args.apply, args.replace)


if __name__ == "__main__":
    sys.exit(main())
