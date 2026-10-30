"""scripts/seed_factsheet_periods.py: dry run by default, --apply writes, an existing (robot, from) needs --replace."""
import datetime
import json

import pytest

from packages.utils import factsheet_periods as fp
from scripts import seed_factsheet_periods as seed

pytestmark = pytest.mark.unit

UTC = datetime.timezone.utc


class Cur:
    def __init__(self, db):
        self.db = db
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False

    def execute(self, sql, params=None):
        self.db.statements.append(sql)
        if sql == seed.EXISTS_SQL:
            self.row = (1,) if (params[0], params[1]) in self.db.rows else None
        elif sql in (fp.INSERT_SQL, seed.UPSERT_SQL):
            key = (params[0], params[1])
            if key not in self.db.rows or sql == seed.UPSERT_SQL:
                self.db.rows[key] = dict(zip(fp.COLUMNS, params))

    def fetchone(self):
        return self.row


class Conn:
    def __init__(self):
        self.rows, self.statements, self.commits = {}, [], 0

    def cursor(self):
        return Cur(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def peacock(**kw):
    raw = {"robot": "formidable-peacock", "from": "2026-10-12T08:00:00Z", "speed_max": 0.24, "length": 0.8, "width": -1}
    raw.update(kw)
    return seed.entry(raw)


def test_entry_parses_the_time_and_stores_unknown_as_null():
    e = peacock()
    assert e["valid_from"] == datetime.datetime(2026, 10, 12, 8, tzinfo=UTC)
    assert (e["speed_max"], e["width"], e["acceleration_max"], e["agv_class"]) == (0.24, None, None, None)
    assert seed.entry({"robot": "r", "from": "2026-10-12T08:00:00"})["valid_from"].tzinfo is UTC
    with pytest.raises(ValueError):
        seed.entry({"robot": "r", "from": "2026-10-12", "speed": 1})


def test_dry_run_writes_nothing(capsys):
    conn = Conn()
    assert seed.run(conn, [peacock()], apply=False, replace=False) == 0
    assert conn.rows == {} and conn.commits == 0
    out = capsys.readouterr().out
    assert "new" in out and "dry run" in out
    assert conn.statements[0] == "SET TRANSACTION READ ONLY"


def test_apply_writes_with_source_seed():
    conn = Conn()
    assert seed.run(conn, [peacock(), peacock(**{"from": "2026-10-20T00:00:00Z", "speed_max": 0.36})], True, False) == 0
    assert conn.commits == 1 and len(conn.rows) == 2
    assert {r["source"] for r in conn.rows.values()} == {"seed"}


def test_an_existing_period_is_refused_unless_replace(capsys):
    conn = Conn()
    seed.run(conn, [peacock()], True, False)
    assert seed.run(conn, [peacock(speed_max=0.5)], True, False) == 2
    assert next(iter(conn.rows.values()))["speed_max"] == 0.24 and conn.commits == 1
    assert seed.run(conn, [peacock(speed_max=0.5)], True, True) == 0
    assert next(iter(conn.rows.values()))["speed_max"] == 0.5


def test_file_input(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps([{"robot": "a", "from": "2026-10-01T00:00:00Z", "speed_max": 0.2}]))
    args = seed.argparse.Namespace(file=str(path))
    assert [e["robot_name"] for e in seed.entries_of(args)] == ["a"]
