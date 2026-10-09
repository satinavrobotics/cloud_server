"""The robot view's `localization` block (packages/api/localization_view.py)."""
import os

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import pytest  # noqa: E402

from cloud_common.objects.robot import RobotStatusV1  # noqa: E402
from packages.api import localization_view as lv  # noqa: E402

pytestmark = pytest.mark.unit

RELOC_SESSION = {"placement_source": "reloc", "map": "shed"}


def status(online=True, initialized=True, map_id="map", errors=None, score=None):
    st = RobotStatusV1(online=online, position_initialized=initialized,
                       localization_score=score, errors=errors or {})
    st.pose.map_id = map_id
    return st


def loc(mode="relocalization", map_name="cloud-shed", **extra):
    intent = {"mode": mode, "map": map_name, "set_at": "2026-10-09T10:00:00+00:00"}
    return {"mode": mode, "map": map_name, "intent": intent, **extra}


class TestDevice:
    @pytest.mark.parametrize("st,state", [
        (status(online=False, map_id="cloud-shed"), lv.UNKNOWN),
        (status(initialized=None, map_id="cloud-shed"), lv.UNKNOWN),   # no position: map_id old
        (status(map_id=""), lv.UNKNOWN),
        (status(map_id="map"), lv.NOT_ON_MAP),
        (status(map_id="cloud-shed"), lv.LOCALIZED),
        (status(initialized=False, errors={"relocalizationNotReadyError": "on 'cloud-shed'"}),
         lv.RELOCALIZING),
        # rejected wins over relocalizing
        (status(initialized=False, errors={"relocalizationNotReadyError": "a",
                                           "relocalizationMapRejectedError": "b"}),
         lv.MAP_REJECTED),
    ])
    def test_state(self, st, state):
        assert lv.device_view(st)["state"] == state

    def test_localized_names_its_map_and_cloud_map(self):
        d = lv.device_view(status(map_id="cloud-shed"))
        assert (d["map"], d["cloud_map"], d["detail"]) == ("cloud-shed", "shed", None)
        assert lv.device_view(status(map_id="by_hand"))["cloud_map"] is None

    def test_errors_give_the_detail(self):
        d = lv.device_view(status(errors={"relocalizationMapRejectedError": "map 'x' refused"}))
        assert (d["map"], d["detail"]) == (None, "map 'x' refused")


class TestIntent:
    def test_new_orchestrator(self):
        i = lv.intent_view({**loc(), "topomap": False})
        assert i == {"mode": "relocalization", "map": "cloud-shed", "cloud_map": "shed",
                     "set_at": "2026-10-09T10:00:00+00:00", "topomap": False}

    def test_older_orchestrator_has_no_intent_object(self):
        i = lv.intent_view({"mode": "slam", "map": None})
        assert (i["mode"], i["map"], i["set_at"], i["topomap"]) == ("slam", None, None, None)

    @pytest.mark.parametrize("body", [None, {}, {"mode": None, "map": None, "intent": None}])
    def test_none_when_never_set_or_not_read(self, body):
        assert lv.intent_view(body) is None

    def test_switch_job(self):
        body = loc(jobs={"switch": {"kind": "switch", "status": "failed",
                                    "request": {"mode": "relocalization", "map": "cloud-x"},
                                    "error": {"status_code": 409, "detail": "order active"},
                                    "started_at": "s", "finished_at": "f"}, "save": None},
                   busy={"switching": False, "saving": False})
        b = lv.build(status(), body)
        assert b["switch"] == {"status": "failed", "mode": "relocalization", "map": "cloud-x",
                               "started_at": "s", "finished_at": "f", "error": "order active"}
        assert b["switching"] is False

    @pytest.mark.parametrize("busy,job_status", [(True, "done"), (False, "running")])
    def test_switching(self, busy, job_status):
        body = loc(jobs={"switch": {"status": job_status, "request": {}}},
                   busy={"switching": busy})
        assert lv.build(status(), body)["switching"] is True


class TestBuild:
    def test_on_intended_map(self):
        assert lv.build(status(map_id="cloud-shed"), loc())["on_intended_map"] is True
        assert lv.build(status(map_id="cloud-other"), loc())["on_intended_map"] is False
        relocalizing = status(initialized=False, errors={"relocalizationNotReadyError": "x"})
        assert lv.build(relocalizing, loc())["on_intended_map"] is False
        # unknown device, or an intent other than relocalization: no answer
        assert lv.build(status(online=False), loc())["on_intended_map"] is None
        assert lv.build(status(), loc("odometry", None))["on_intended_map"] is None
        assert lv.build(status(map_id="cloud-shed"), None)["on_intended_map"] is None

    def test_usable_stale_and_reason(self):
        b = lv.build(status(initialized=False, errors={
            "poseHealthNotReadyError": "pose", "relocalizationNotReadyError": "on 'cloud-shed'"}))
        assert b["usable"] is False and b["stale"] is False
        assert b["reason"] == "relocalizationNotReadyError: on 'cloud-shed'"
        off = lv.build(status(online=False, errors={"poseHealthNotReadyError": "old"}))
        assert (off["usable"], off["stale"], off["reason"]) == (None, True, None)

    def test_read_metadata(self):
        import datetime
        at = datetime.datetime(2026, 10, 9, 10, 0, tzinfo=datetime.timezone.utc)
        b = lv.build(status(), None, at, "unreachable")
        assert (b["intent"], b["intent_read_at"], b["intent_error"]) == \
            (None, at.isoformat(), "unreachable")


class TestWarning:
    def _w(self, st, body, session=RELOC_SESSION):
        return lv.warning(session, st, lv.build(st, body))

    def test_healthy(self):
        assert self._w(status(map_id="cloud-shed"), loc()) is None

    def test_flag_and_score_still_come_first(self):
        assert "not initialized" in self._w(status(initialized=False), loc())

    def test_wrong_mode(self):
        assert "odometry mode" in self._w(status(), loc("odometry", None))

    def test_intent_on_another_cloud_map(self):
        assert "'other'" in self._w(status(map_id="cloud-other"), loc(map_name="cloud-other"))

    def test_localized_on_another_map_than_told(self):
        w = self._w(status(map_id="by_hand"), loc(map_name="cloud-shed"))
        assert "'by_hand'" in w and "'cloud-shed'" in w

    def test_hand_named_intent_map_is_not_judged_against_the_session(self):
        assert self._w(status(map_id="by_hand"), loc(map_name="by_hand")) is None

    @pytest.mark.parametrize("session", [None, {"placement_source": "user", "map": "shed"}])
    def test_only_placed_reloc_sessions(self, session):
        assert self._w(status(), loc("odometry", None), session) is None

    def test_no_block_or_unknown_device_says_nothing_more(self):
        assert lv.warning(RELOC_SESSION, status()) is None
        assert self._w(status(online=False), loc("odometry", None)) is None
