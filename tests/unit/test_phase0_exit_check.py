"""tools/phase0_exit_check.py: every pure check on fixtures (pass and fail cases), the output
(table + JSON), and the CLI's argument handling. The SQL side runs against TimescaleDB in
tests/integration/phase0_exit_check."""
import datetime
import json
import re
import os
import uuid

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import pytest  # noqa: E402

from tools import phase0_exit_check as xc  # noqa: E402

pytestmark = pytest.mark.unit

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 9, 27, 8, 0, tzinfo=UTC)


def t(minutes: float) -> datetime.datetime:
    return T0 + datetime.timedelta(minutes=minutes)


def run(i, state="COMPLETED", *, robot="bot", level="events_only", cause=None, site="s1",
        sw=None, start=None, end=None, mission=None):
    started = start or t(10 * i)
    ended = None if state == "RUNNING" else (end or started + datetime.timedelta(minutes=5))
    if cause is None and state not in ("COMPLETED", "RUNNING"):
        cause = "NAV.GOAL_UNREACHABLE"
    return xc.Run(uuid.UUID(int=i + 1), mission or f"m{i}", robot, site, sw, level, state,
                  cause, started, ended)


def ev(code, ts, robot="bot", run_id=None, **payload):
    return xc.Ev(ts, uuid.uuid4(), robot, run_id, code, payload)


def test_run_namespace_matches_dispatch():
    from packages.controllers.mission import fleet_recorder
    assert xc.RUN_NAMESPACE == fleet_recorder.RUN_NAMESPACE
    assert xc.expected_run_id("m1", "ab12cd34") == fleet_recorder.run_uuid("m1", "ab12cd34")


class TestRuns:
    def test_present(self):
        assert xc.check_runs_present([]).status == xc.FAIL
        r = xc.check_runs_present([run(0), run(1, "FAILED")])
        assert r.status == xc.PASS and r.metrics["by_state"] == {"COMPLETED": 1, "FAILED": 1}

    def test_one_row_per_run(self):
        runs = [run(0, mission="a"), run(1, mission="b")]
        rid = xc.expected_run_id("a", "tok")
        ok = xc.check_one_row_per_run([("a", "bot", "tok", "COMPLETED")], [rid], runs)
        assert ok.status == xc.PASS
        missing = xc.check_one_row_per_run([("a", "bot", "tok", "COMPLETED"),
                                            ("c", "bot", "zz", "FAILED")], [rid], runs)
        assert missing.status == xc.FAIL and missing.metrics["missing"] == 1
        dup = [run(0, mission="a"), run(1, mission="a", start=t(2))]   # overlaps run 0
        over = xc.check_one_row_per_run([], [], dup)
        assert over.status == xc.FAIL and over.metrics["overlapping"] == 1
        later = [run(0, mission="a"), run(1, mission="a", start=t(30))]  # re-created later
        assert xc.check_one_row_per_run([], [], later).status == xc.PASS

    def test_immutability(self):
        r = run(0, "FAILED", cause="UNKNOWN")
        good = {r.run_id: [ev(xc.RUN_FINISHED, t(5), outcome="FAILED", cause="UNKNOWN")]}
        trig = ("mission_runs_immutable_when_terminal", "O")
        assert xc.check_immutability(trig, [r], good).status == xc.PASS
        assert xc.check_immutability(None, [r], good).status == xc.FAIL
        assert xc.check_immutability((trig[0], "D"), [r], good).status == xc.FAIL
        changed = {r.run_id: [ev(xc.RUN_FINISHED, t(5), outcome="CANCELED",
                                 cause="OPERATOR.CANCELED")]}
        res = xc.check_immutability(trig, [r], changed)
        assert res.status == xc.FAIL and res.metrics["mismatched"] == 1

    def test_required_fields(self):
        runs = [run(0), run(1, "FAILED", cause="UNKNOWN")]
        res = xc.check_required_fields(runs, {})
        assert res.status == xc.PASS and res.metrics["sw_version_null"] == 2
        assert "robot-side step deferred" in res.summary
        no_site = run(2, site=None)
        res = xc.check_required_fields([no_site], {no_site.run_id: None})
        assert res.status == xc.FAIL and "assign the robot" in res.details[0]["hint"]
        no_cause = run(3, "CANCELED")
        no_cause.abort_cause = None
        res = xc.check_required_fields([no_cause], {})
        assert res.status == xc.FAIL and res.details[0]["missing"] == "abort_cause"
        res = xc.check_required_fields([run(4, "RUNNING")], {})
        assert res.status == xc.WARN and res.metrics["still_running"] == 1

    def test_run_events_once(self):
        a, b = run(0), run(1, "RUNNING")
        started = {a.run_id: [ev(xc.RUN_STARTED, t(0))], b.run_id: [ev(xc.RUN_STARTED, t(10))]}
        finished = {a.run_id: [ev(xc.RUN_FINISHED, t(5))]}
        assert xc.check_run_events([a, b], started, finished, {}).status == xc.PASS
        twice = {a.run_id: started[a.run_id] * 2}
        res = xc.check_run_events([a], twice, finished, {})
        assert res.status == xc.FAIL and res.details[0]["RUN_STARTED"] == 2
        res = xc.check_run_events([a], started, {}, {})
        assert res.status == xc.FAIL and res.details[0]["RUN_FINISHED"] == 0
        # level off: nothing expected
        off = run(2, level="off")
        assert xc.check_run_events([off], {}, {}, {off.run_id: "off"}).status == xc.PASS
        # off at start, full at the end: only RUN_FINISHED expected
        mixed = run(3, level="off")
        fin = {mixed.run_id: [ev(xc.RUN_FINISHED, t(35))]}
        assert xc.check_run_events([mixed], {}, fin, {mixed.run_id: "full"}).status == xc.PASS
        assert xc.check_run_events([mixed], {}, {}, {mixed.run_id: "full"}).status == xc.FAIL


class TestEvents:
    def test_duplicate_ids(self):
        assert xc.check_duplicate_event_ids([]).status == xc.PASS
        res = xc.check_duplicate_event_ids([(uuid.UUID(int=5), 2)])
        assert res.status == xc.FAIL and res.details[0]["count"] == 2

    def test_logical_duplicates_pass(self):
        evs = [ev(xc.HB_LOST, t(1)), ev(xc.HB_RESTORED, t(2)), ev(xc.HB_LOST, t(3)),
               ev("ROBOT.ERROR_RAISED", t(1), error_type="a"),
               ev("ROBOT.ERROR_RAISED", t(1), error_type="b"),       # different key
               ev("ROBOT.ERROR_CLEARED", t(2), error_type="a"),
               ev(xc.STATE_CHANGED, t(1), old="IDLE", new="ON_TASK"),
               ev(xc.STATE_CHANGED, t(2), old="ON_TASK", new="IDLE"),
               ev(xc.STATE_CHANGED, t(3), old="IDLE", new="ON_TASK"),
               ev(xc.HB_LOST, t(1), robot="other")]
        assert xc.check_logical_duplicates(evs).status == xc.PASS

    @pytest.mark.parametrize("evs", [
        [ev(xc.HB_LOST, t(1)), ev(xc.HB_LOST, t(2))],
        [ev("ROBOT.ONLINE", t(1)), ev("ROBOT.ONLINE", t(2))],
        [ev("SYSTEM.NODE_DOWN", t(1), node="/a"), ev("SYSTEM.NODE_DOWN", t(2), node="/a")],
        [ev(xc.ALERT_RAISED, t(1), robot=None, alert="spill_pending", process="api"),
         ev(xc.ALERT_RAISED, t(2), robot=None, alert="spill_pending", process="api")],
        [ev(xc.STATE_CHANGED, t(1), old="IDLE", new="ON_TASK"),
         ev(xc.STATE_CHANGED, t(2), old="IDLE", new="ON_TASK")],
        [ev(xc.NODE_FAILED, t(1), run_id=uuid.UUID(int=9), node_id="n1"),
         ev(xc.NODE_FAILED, t(2), run_id=uuid.UUID(int=9), node_id="n1")],
    ], ids=["heartbeat", "online", "node", "recorder-alert", "state", "node-failed"])
    def test_logical_duplicates_fail(self, evs):
        res = xc.check_logical_duplicates(list(reversed(evs)))   # order-independent
        assert res.status == xc.FAIL and len(res.details) == 1

    def test_heartbeat_pairs(self):
        pair = [ev(xc.HB_LOST, t(1), run_id=uuid.UUID(int=1)),
                ev(xc.HB_RESTORED, t(2), gap_s=61.0)]
        res = xc.check_heartbeat_pairs(pair, [])
        assert res.status == xc.PASS and res.metrics["pairs"] == 1
        assert res.metrics["pairs_list"][0]["gap_s"] == 61.0
        assert xc.check_heartbeat_pairs([], []).status == xc.FAIL
        assert xc.check_heartbeat_pairs([], [], expect_disconnect=False).status == xc.INFO
        # RESTORED whose LOST is before the window: ignored
        assert xc.check_heartbeat_pairs([ev(xc.HB_RESTORED, t(0))] + pair, []).status == xc.PASS
        open_ = pair + [ev(xc.HB_LOST, t(4))]
        assert xc.check_heartbeat_pairs(open_, []).status == xc.FAIL
        assert xc.check_heartbeat_pairs(open_, ["bot"]).status == xc.WARN
        twice = [ev(xc.HB_LOST, t(1)), ev(xc.HB_LOST, t(2)), ev(xc.HB_RESTORED, t(3))]
        assert xc.check_heartbeat_pairs(twice, []).status == xc.FAIL

    def test_unknown_share(self):
        runs = [run(0), run(1, "FAILED", cause="UNKNOWN"), run(2, "TIMEOUT",
                                                              cause="DISPATCH.TIMEOUT"),
                run(3, "CANCELED", cause="OPERATOR.CANCELED"), run(4, "RUNNING")]
        res = xc.unknown_cause_share(runs)
        assert res.status == xc.INFO
        assert res.metrics["unknown"] == 1 and res.metrics["non_completed"] == 3
        assert res.metrics["share_of_non_completed"] == pytest.approx(1 / 3)
        assert res.metrics["share_of_terminal"] == pytest.approx(1 / 4)
        assert xc.unknown_cause_share([run(0)]).metrics["share_of_non_completed"] is None


class TestHealthAndScenario:
    def test_recorder_health(self):
        assert xc.check_recorder_health(None, []).status == xc.SKIP
        ok = [{"process": "dispatch", "report_age_s": 4, "alerts": []},
              {"process": "api", "report_age_s": 1, "alerts": []}]
        evs = [ev(xc.ALERT_RAISED, t(1), robot=None, alert="report_stale", process="dispatch"),
               ev(xc.ALERT_CLEARED, t(2), robot=None, alert="report_stale", process="dispatch")]
        res = xc.check_recorder_health(ok, evs)
        assert res.status == xc.PASS and res.metrics["alerts_raised_in_window"] == 1
        stale = [dict(ok[0], report_age_s=90), ok[1]]
        assert xc.check_recorder_health(stale, []).status == xc.FAIL
        assert xc.check_recorder_health(ok[1:], []).status == xc.FAIL
        alerting = [ok[0], dict(ok[1], alerts=[{"alert": "spill_pending"}])]
        assert xc.check_recorder_health(alerting, []).status == xc.FAIL

    def test_scenario(self):
        full = run(4, level="full")
        runs = [run(0), run(1, "FAILED"), run(2, "CANCELED", cause="OPERATOR.CANCELED"),
                run(3, "TIMEOUT", cause="DISPATCH.TIMEOUT"), full]
        pairs = [{"robot": "bot", "lost": xc.iso(t(1)), "restored": xc.iso(t(2))}]
        res = xc.check_scenario(runs, pairs, [full.run_id])
        assert res.status == xc.PASS
        res = xc.check_scenario(runs, [], [])
        assert res.status == xc.FAIL
        assert set(res.details) == {"disconnect mid-run", "full + level change mid-run"}
        res = xc.check_scenario(runs[:3], pairs, [full.run_id])
        assert ">=5 runs" in res.details

    def test_timeseries_levels(self):
        assert xc.check_timeseries_levels([], 2).status == xc.PASS
        res = xc.check_timeseries_levels([{"robot": "bot", "table": "robot_state_ts",
                                           "rows": 7}], 2)
        assert res.status == xc.FAIL and "7 row(s)" in res.summary


def timeline(r, segments, not_recorded, events=(), points=()):
    return {"window": {"from": xc.iso(r.started_at), "to": xc.iso(r.ended_at)},
            "events": list(events),
            "tracks": {"robot_state": {"source": "raw" if points else "none",
                                       "points": list(points)},
                       "diagnostics": {"source": "none", "points": []}},
            "recording": {"segments": segments, "not_recorded": not_recorded}}


def seg(a, b, level, **extra):
    return {"from": xc.iso(a), "to": xc.iso(b), "level": level, "source": "robot", **extra}


class TestTimeline:
    def test_consistent_mixed_levels(self):
        r = run(0, level="full", start=t(0), end=t(10))
        tl = timeline(r, [seg(t(0), t(4), "full"), seg(t(4), t(10), "events_only")],
                      [{"from": xc.iso(t(4)), "to": xc.iso(t(10)), "level": "events_only",
                        "missing": ["time_series"]}],
                      events=[{"ts": xc.iso(t(6)), "code": "ROBOT.ONLINE", "robot_name": "bot"}],
                      points=[{"ts": xc.iso(t(1))}, {"ts": xc.iso(t(4) + datetime.timedelta(
                          seconds=2))}])  # inside the grace after the change
        assert xc.check_timeline(r, tl) == []

    def test_problems(self):
        r = run(0, level="full", start=t(0), end=t(10))
        gap = [{"from": xc.iso(t(4)), "to": xc.iso(t(10)), "level": "off",
                "missing": ["events", "time_series"]}]
        segments = [seg(t(0), t(4), "full"), seg(t(4), t(10), "off")]
        leaky = timeline(r, segments, gap,
                         events=[{"ts": xc.iso(t(6)), "code": "BATTERY.LOW", "robot_name": "bot"},
                                 {"ts": xc.iso(t(7)), "code": xc.RECORDING_CHANGED,
                                  "robot_name": "bot"}],
                         points=[{"ts": xc.iso(t(8))}])
        problems = xc.check_timeline(r, leaky)
        assert {p["problem"].split(" ")[0] for p in problems} == {"events", "robot_state"}
        wrong = timeline(r, segments, [])
        assert any("does not match" in p["problem"] for p in xc.check_timeline(r, wrong))
        holes = timeline(r, [seg(t(0), t(3), "full"), seg(t(4), t(10), "full")], [])
        assert any("gap" in p["problem"] for p in xc.check_timeline(r, holes))
        rec = timeline(r, [seg(t(0), t(10), "full", reconstructed_level="events_only")], [])
        res = xc.check_timelines([(r, xc.check_timeline(r, rec))], 0)
        assert res.status == xc.WARN
        res = xc.check_timelines([(r, problems)], 3)
        assert res.status == xc.FAIL and "3 run(s) not checked" in res.summary


class TestOutput:
    def results(self):
        return [xc.Result("runs_in_window", xc.PASS, "5 run(s)"),
                xc.Result("heartbeat_pairs", xc.FAIL, "no pair", [{"robot": "bot"}]),
                xc.Result("unknown_cause_share", xc.INFO, "UNKNOWN on 1/3", [],
                          {"share_of_non_completed": 1 / 3, "share_of_terminal": 0.25,
                           "unknown": 1, "non_completed": 3})]

    def test_table_and_json(self):
        results = self.results()
        table = xc.render_table(results, t(0), t(60), None)
        assert "OVERALL: FAIL" in table and re.search(r"heartbeat_pairs +FAIL +no pair", table)
        assert '- {"robot": "bot"}' in table
        doc = xc.to_json(results, t(0), t(60), "bot")
        json.dumps(doc)
        assert doc["overall"] == "FAIL" and doc["robot"] == "bot"
        assert doc["baseline"]["unknown_cause_share_of_non_completed"] == pytest.approx(1 / 3)
        assert [c["name"] for c in doc["checks"]] == [r.name for r in results]
        assert xc.overall(results[:1] + results[2:]) == xc.PASS

    def test_cli_arguments(self, capsys):
        with pytest.raises(SystemExit):
            xc.main(["--from", "2026-09-27T08:00:00"])          # no time zone
        with pytest.raises(SystemExit):
            xc.main(["--from", "2026-09-27T08:00:00Z", "--to", "2026-09-27T07:00:00Z"])
        assert xc.main(["--from", "2026-09-20T08:00:00Z", "--dsn",
                        "host=127.0.0.1 port=1 dbname=x user=x connect_timeout=1"]) == 2
        assert "error" in capsys.readouterr().err

    def test_default_dsn(self, monkeypatch):
        from packages import config
        monkeypatch.setattr(config, "POSTGRES_DATABASE_HOST", "dbhost")
        monkeypatch.setattr(config, "POSTGRES_DATABASE_NAME", "mission")
        dsn = xc.default_dsn()
        assert "host=dbhost" in dsn and "dbname=mission" in dsn
