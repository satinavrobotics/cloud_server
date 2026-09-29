"""Geo mapping session re-anchored after a robot restart (graph-builder ingest, maps M2 note)."""

import os
import uuid

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import asyncio  # noqa: E402
import datetime  # noqa: E402
import json  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import pytest  # noqa: E402

from packages.events.codes import EventCode  # noqa: E402
from packages.events.emit import build_row  # noqa: E402
from packages.services.graph_builder import ingest  # noqa: E402
from packages.services.graph_builder.server import GraphBuilderService  # noqa: E402
from packages.utils import map_geo  # noqa: E402

pytestmark = pytest.mark.unit

GEO = {"utm_zone": 34, "utm_north": True, "origin_e": 352000.0, "origin_n": 5262000.0}
SID = str(uuid.UUID(int=7))


def utm_datum(e, n, bearing=0.0, zone=34):
    return {"latitude": 47.4979, "longitude": 19.0402, "bearing_deg": bearing, "frame": "utm",
            "utm_zone": zone, "utm_north": True, "utm_easting": e, "utm_northing": n}


OLD = utm_datum(352010.0, 5262020.0, 0.0)
NEW = utm_datum(352050.0, 5262090.0, 0.5)


def session(sdatum=OLD, rdatum=NEW, geo=GEO, mtype="geo", t=None, **kw):
    base = dict(session_id=SID, map_name="yard", paused=False,
                map_t_session=t or map_geo.session_transform(GEO, OLD), map_lifecycle="ALIVE",
                map_state="mapping", session_datum=sdatum,
                robot_datum=map_geo.robot_datum(rdatum), map_geo=geo, map_type=mtype)
    base.update(kw)
    return ingest.OpenSession(**base)


class TestPlanRealign:
    def test_new_datum_in_zone(self):
        plan = ingest.plan_realign(session())
        assert plan is not None
        assert plan.map_t_session == pytest.approx(
            {"tx": 50.0, "ty": 90.0, "yaw": 0.5 * 3.141592653589793 / 180})
        assert plan.datum["utm_easting"] == 352050.0

    @pytest.mark.parametrize("kw", [
        {"geo": None},                         # no origin yet
        {"mtype": "local", "geo": None},       # local map: no absolute frame
        {"sdatum": None},                      # local session
        {"rdatum": {}},                        # robot lost its datum
        {"rdatum": utm_datum(352050.0, 5262090.0, zone=35)},   # other UTM zone
        {"geo": {**GEO, "utm_north": False}},  # other hemisphere
        {"geo": {"utm_zone": 34}},             # broken geo block
        {"rdatum": OLD},                       # unchanged
    ])
    def test_unusable(self, kw):
        assert ingest.plan_realign(session(**kw)) is None

    def test_decide_still_rejects_without_realigner(self):
        assert ingest.decide("r1", session()).reason == ingest.DATUM_CHANGED


def row_for(s: ingest.OpenSession):
    return (s.session_id, s.map_name, s.paused, s.map_t_session, s.map_lifecycle, s.map_state,
            s.session_datum, s.robot_datum, s.map_geo, s.map_type)


class FakeDb:
    """The map_sessions row and a CAS like REALIGN_SQL."""

    def __init__(self, s):
        self.s = s
        self.events = []
        self.cas = 0

    async def fetch(self, _robot):
        return row_for(self.s)

    async def realign(self, robot, sess, plan):
        await asyncio.sleep(0)
        if self.s.session_datum != sess.session_datum:  # somebody else won
            return False
        self.cas += 1
        self.s = ingest.dataclasses.replace(
            self.s, session_datum=plan.datum, map_t_session=plan.map_t_session)
        self.events.append(ingest.realign_event(robot, sess, plan,
                                                datetime.datetime.now(datetime.timezone.utc)))
        return True


class TestResolver:
    async def test_realigned_then_accepted(self):
        db = FakeDb(session())
        res = ingest.SessionResolver(db.fetch, ttl=0, realign=db.realign)
        r = await res.resolve("r1")
        assert r.accepted
        assert r.session.map_t_session["tx"] == pytest.approx(50.0)
        assert r.session.session_datum == db.s.session_datum
        assert len(db.events) == 1
        # the next node sees the stored datum: no second realignment
        assert (await res.resolve("r1")).accepted and db.cas == 1

    async def test_concurrent_ingests_agree(self):
        db = FakeDb(session())
        res = ingest.SessionResolver(db.fetch, ttl=0, realign=db.realign)
        results = await asyncio.gather(*(res.resolve("r1") for _ in range(5)))
        assert all(r.accepted for r in results)
        assert db.cas == 1 and len(db.events) == 1
        assert all(r.session.map_t_session["tx"] == pytest.approx(50.0) for r in results)

    async def test_unusable_datum_still_rejected_and_no_write(self):
        db = FakeDb(session(rdatum=utm_datum(352050.0, 5262090.0, zone=35)))
        res = ingest.SessionResolver(db.fetch, ttl=0, realign=db.realign)
        assert (await res.resolve("r1")).reason == ingest.DATUM_CHANGED
        assert db.cas == 0

    async def test_paused_session_is_not_realigned(self):
        db = FakeDb(session(paused=True))
        res = ingest.SessionResolver(db.fetch, ttl=0, realign=db.realign)
        assert (await res.resolve("r1")).reason == ingest.SESSION_PAUSED
        assert db.cas == 0

    async def test_realign_failure_keeps_rejection(self):
        db = FakeDb(session())
        res = ingest.SessionResolver(db.fetch, ttl=0, realign=AsyncMock(side_effect=RuntimeError))
        assert (await res.resolve("r1")).reason == ingest.DATUM_CHANGED


class TestEventAndSql:
    def test_event_row_is_valid(self):
        s = session()
        plan = ingest.plan_realign(s)
        ev = ingest.realign_event("r1", s, plan, datetime.datetime(2026, 9, 29,
                                                                   tzinfo=datetime.timezone.utc))
        row = build_row(ev, strict=True)
        assert row["code"] == "MAP.SESSION_REALIGNED" and row["source"] == "graph_builder"
        assert row["robot_name"] == "r1"

    def test_sql_is_compare_and_set_and_params(self):
        assert "datum = %s::jsonb" in ingest.REALIGN_SQL.split("WHERE")[1]
        assert "ended_at IS NULL" in ingest.REALIGN_SQL
        s = session()
        plan = ingest.plan_realign(s)
        d, t, sid, old = ingest.realign_params(s, plan)
        assert json.loads(d) == plan.datum and json.loads(old) == OLD and str(sid) == SID

    def test_open_session_sql_reads_map_geo(self):
        assert "m.spec->'geo'" in ingest.OPEN_SESSION_SQL

    def test_from_row_accepts_old_and_new_shapes(self):
        s = session()
        assert ingest.OpenSession.from_row(row_for(s)).map_geo == GEO
        assert ingest.OpenSession.from_row(row_for(s)[:8]).map_geo is None


def _service(rowcount):
    svc = GraphBuilderService.__new__(GraphBuilderService)
    svc.stats = {}
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=MagicMock(rowcount=rowcount))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    svc.database = MagicMock(connection=MagicMock(return_value=cm))
    return svc, conn


class TestServerRealign:
    async def test_cas_lost_writes_no_event(self, monkeypatch):
        from packages.services.graph_builder import server as gb
        emitted = AsyncMock()
        monkeypatch.setattr(gb, "emit", emitted)
        svc, conn = _service(0)
        s = session()
        assert await svc._realign_session("r1", s, ingest.plan_realign(s)) is False
        assert conn.execute.await_count == 1 and emitted.await_count == 0

    async def test_cas_won_emits_in_same_connection(self, monkeypatch):
        from packages.services.graph_builder import server as gb
        emitted = AsyncMock()
        monkeypatch.setattr(gb, "emit", emitted)
        svc, conn = _service(1)
        s = session()
        assert await svc._realign_session("r1", s, ingest.plan_realign(s)) is True
        assert emitted.await_args.args[0] is conn
        assert emitted.await_args.args[1].code == EventCode.MAP_SESSION_REALIGNED
