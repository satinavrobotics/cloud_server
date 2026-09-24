"""Table-driven golden tests for packages/events/detectors.py."""

import datetime

import pytest

from packages.events.detectors import (
    Change, Direction, Hysteresis, Lost, Restored, SetChange, SetDiff, StateDiff,
    Timeout, Transition,
)

pytestmark = pytest.mark.unit

T0 = datetime.datetime(2026, 9, 24, 12, 0, 0, tzinfo=datetime.timezone.utc)


def at(seconds: float) -> datetime.datetime:
    return T0 + datetime.timedelta(seconds=seconds)


ENTERED, EXITED = Transition.ENTERED, Transition.EXITED

# ---- StateDiff ------------------------------------------------------------

STATE_DIFF_CASES = [
    ("unseeded first sample is baseline", {}, ["IDLE"], [None]),
    ("unseeded then change", {}, ["IDLE", "ON_TASK"], [None, Change("IDLE", "ON_TASK")]),
    ("repeat is silent", {}, ["IDLE", "IDLE", "IDLE"], [None, None, None]),
    ("seeded change fires on first sample", {"initial": "IDLE"}, ["ON_TASK"],
     [Change("IDLE", "ON_TASK")]),
    ("seeded same value is silent", {"initial": "IDLE"}, ["IDLE"], [None]),
    ("seeded None is a real previous value", {"initial": None}, ["v1"], [Change(None, "v1")]),
    ("flap", {}, ["A", "B", "A"], [None, Change("A", "B"), Change("B", "A")]),
]


@pytest.mark.parametrize("name,kwargs,samples,expected", STATE_DIFF_CASES,
                         ids=[c[0] for c in STATE_DIFF_CASES])
def test_state_diff(name, kwargs, samples, expected):
    det = StateDiff(**kwargs)
    assert [det.update(s) for s in samples] == expected
    assert det.value == samples[-1]


# ---- SetDiff --------------------------------------------------------------

def sc(added=(), removed=()):
    return SetChange(frozenset(added), frozenset(removed))


SET_DIFF_CASES = [
    ("unseeded first sample is baseline", None, [{"a"}], [None]),
    ("seeded empty raises all", set(), [{"a", "b"}], [sc(added={"a", "b"})]),
    ("add then clear", set(), [{"a"}, {"a", "b"}, set()],
     [sc(added={"a"}), sc(added={"b"}), sc(removed={"a", "b"})]),
    ("swap", {"a"}, [{"b"}], [sc(added={"b"}, removed={"a"})]),
    ("unchanged is silent", {"a"}, [["a", "a"]], [None]),
    ("order does not matter", {"a", "b"}, [["b", "a"]], [None]),
]


@pytest.mark.parametrize("name,initial,samples,expected", SET_DIFF_CASES,
                         ids=[c[0] for c in SET_DIFF_CASES])
def test_set_diff(name, initial, samples, expected):
    det = SetDiff(initial)
    assert [det.update(s) for s in samples] == expected


# ---- Hysteresis -----------------------------------------------------------

# Battery: LOW at <= 20 %, OK again at >= 25 %.
BATTERY_CASES = [
    ("above band stays ok", False, [80, 50, 21], [None, None, None]),
    ("exactly 20 enters", False, [20.0], [ENTERED]),
    ("just above 20 does not enter", False, [20.01], [None]),
    ("dead band does not exit", True, [21, 24.99], [None, None]),
    ("exactly 25 exits", True, [25.0], [EXITED]),
    ("no chatter around low threshold", False, [19, 21, 19, 21], [ENTERED, None, None, None]),
    ("full cycle", False, [30, 19, 22, 26, 19], [None, ENTERED, None, EXITED, ENTERED]),
    ("None samples ignored", False, [None, 10, None], [None, ENTERED, None]),
    ("unknown start low is silent", None, [10, 30], [None, EXITED]),
    ("unknown start in band counts as ok", None, [22, 20], [None, ENTERED]),
]

# Thermal: HIGH at >= 85 °C, OK again at <= 78 °C.
THERMAL_CASES = [
    ("exactly 85 enters", False, [84.99, 85.0], [None, ENTERED]),
    ("dead band does not exit", True, [84, 78.01], [None, None]),
    ("exactly 78 exits", True, [78.0], [EXITED]),
    ("full cycle", False, [60, 90, 80, 70, 86], [None, ENTERED, None, EXITED, ENTERED]),
    ("unknown start hot is silent", None, [90, 70], [None, EXITED]),
]


@pytest.mark.parametrize("name,active,samples,expected", BATTERY_CASES,
                         ids=[c[0] for c in BATTERY_CASES])
def test_battery_hysteresis(name, active, samples, expected):
    det = Hysteresis(enter=20, exit=25, direction=Direction.BELOW, active=active)
    assert [det.update(s) for s in samples] == expected


@pytest.mark.parametrize("name,active,samples,expected", THERMAL_CASES,
                         ids=[c[0] for c in THERMAL_CASES])
def test_thermal_hysteresis(name, active, samples, expected):
    det = Hysteresis(enter=85, exit=78, direction="above", active=active)
    assert [det.update(s) for s in samples] == expected


@pytest.mark.parametrize("enter,exit,direction", [
    (20, 25, Direction.ABOVE), (85, 78, Direction.BELOW), (20, 20, Direction.BELOW),
])
def test_hysteresis_rejects_inverted_thresholds(enter, exit, direction):
    with pytest.raises(ValueError):
        Hysteresis(enter, exit, direction)


# ---- Timeout (heartbeat) ---------------------------------------------------

# Each step is ("seen", t) or ("check", t); timeout is 10 s.
TIMEOUT_CASES = [
    ("never seen never times out", {}, [("check", 1000)], [None]),
    ("within timeout", {}, [("seen", 0), ("check", 9.999)], [None, None]),
    ("exactly at timeout is not lost", {}, [("seen", 0), ("check", 10)], [None, None]),
    ("just past timeout is lost", {}, [("seen", 0), ("check", 10.001)], [None, Lost(at(0))]),
    ("lost fires once", {}, [("seen", 0), ("check", 11), ("check", 12), ("check", 60)],
     [None, Lost(at(0)), None, None]),
    ("restored with gap", {}, [("seen", 0), ("check", 11), ("seen", 15)],
     [None, Lost(at(0)), Restored(at(0), 15.0)]),
    ("restored once", {}, [("seen", 0), ("check", 11), ("seen", 15), ("seen", 16)],
     [None, Lost(at(0)), Restored(at(0), 15.0), None]),
    ("seen keeps it alive", {}, [("seen", 0), ("seen", 8), ("check", 17), ("check", 18.5)],
     [None, None, None, Lost(at(8))]),
    ("late message does not move last_seen", {},
     [("seen", 5), ("seen", 3), ("check", 15.5)], [None, None, Lost(at(5))]),
    ("rehydrated last_seen", {"last_seen": at(0)}, [("check", 11)], [Lost(at(0))]),
    ("rehydrated lost restores", {"last_seen": at(0), "lost": True}, [("check", 30), ("seen", 40)],
     [None, Restored(at(0), 40.0)]),
    ("lost again after restore", {}, [("seen", 0), ("check", 11), ("seen", 12), ("check", 22.5)],
     [None, Lost(at(0)), Restored(at(0), 12.0), Lost(at(12))]),
]


@pytest.mark.parametrize("name,kwargs,steps,expected", TIMEOUT_CASES,
                         ids=[c[0] for c in TIMEOUT_CASES])
def test_heartbeat_timeout(name, kwargs, steps, expected):
    det = Timeout(10, **kwargs)
    out = [det.seen(at(t)) if op == "seen" else det.check(at(t)) for op, t in steps]
    assert out == expected


def test_timeout_rejects_non_positive():
    with pytest.raises(ValueError):
        Timeout(0)


def test_detectors_do_not_read_the_clock():
    import ast
    import inspect
    from packages.events import detectors
    clock = {"now", "utcnow", "today", "time", "monotonic", "perf_counter"}
    calls = [n.func for n in ast.walk(ast.parse(inspect.getsource(detectors)))
             if isinstance(n, ast.Call)]
    assert not [f.attr for f in calls if isinstance(f, ast.Attribute) and f.attr in clock]
    assert not [f.id for f in calls if isinstance(f, ast.Name) and f.id in clock]
