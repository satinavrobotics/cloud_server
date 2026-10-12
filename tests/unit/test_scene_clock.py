import random

from packages.services.graph_builder import scene_clock as sc
from packages.services.graph_builder import scene_leaf as sl

T0 = 1_790_000_000_000   # a 2026 epoch ms
S = 1000


def _fed(clock, session, pairs):
    return [clock.correct(session, stamp, receipt) for stamp, receipt in pairs]


def test_too_few_observations_fall_back_to_receipt():
    clock = sc.SessionClock(min_obs=3)
    out = _fed(clock, "a", [(T0 - 50 * S, T0), (T0 + 10 * S, T0 + 20 * S)])
    assert out == [(T0, False), (T0 + 20 * S, False)]
    assert clock.offset_ms("a") is None and not clock.suspect("a")


def test_ahead_clock_no_longer_beats_newer_correct_observation():
    ahead, good = sc.SessionClock(), sc.SessionClock()
    # robot A runs 120 s ahead, uploads live with 1 s latency
    pairs = [(T0 + i * 10 * S + 120 * S, T0 + i * 10 * S + S) for i in range(5)]
    corrected = _fed(ahead, "A", pairs)
    last_a_receipt = pairs[-1][1]
    assert corrected[-1] == (last_a_receipt, True)
    # robot B (correct clock) observes the same cell later than A's last receipt
    b_stamp = last_a_receipt + 5 * S
    b = _fed(good, "B", [(b_stamp - 2 * S + i, b_stamp - 2 * S + i + S) for i in range(3)])[-1]
    assert b[0] > corrected[-1][0]
    raw_a = pairs[-1][0]
    assert raw_a > b_stamp          # the old rule: the ahead clock won


def test_behind_clock_is_corrected():
    clock = sc.SessionClock()
    pairs = [(T0 + i * S - 90 * S, T0 + i * S + S // 2) for i in range(4)]
    out = _fed(clock, "A", pairs)
    assert clock.offset_ms("A") == 90 * S + S // 2
    assert out[-1] == (pairs[-1][1], True)


def test_offline_batch_with_one_live_leaf():
    clock = sc.SessionClock(min_obs=3)
    live_receipt = T0 + 3600 * S
    # 4 leaves captured an hour earlier (true time == stamp), uploaded together; plus one live
    old = [(T0 + i * 10 * S, live_receipt) for i in range(4)]
    live = (live_receipt - 100, live_receipt)
    out = _fed(clock, "A", old + [live])
    assert clock.offset_ms("A") == 100
    # the old leaves, once re-folded with the estimate, keep their (old) true time
    refold = clock.correct("A", old[0][0], old[0][1])
    assert refold == (old[0][0] + 100, True)
    assert out[-1][0] == live_receipt


def test_offline_only_session_is_clamped_to_receipt():
    clock = sc.SessionClock()
    out = _fed(clock, "A", [(T0 + i * S, T0 + 600 * S) for i in range(4)])
    assert all(stamp <= T0 + 600 * S for stamp, _ in out)


def test_estimate_only_improves_and_never_exceeds_receipt():
    rng = random.Random(7)
    clock = sc.SessionClock(min_obs=1)
    skew, prev = rng.randint(-200, 200) * S, None
    for i in range(200):
        receipt = T0 + i * 5 * S
        stamp = receipt - skew - rng.randint(0, 30) * S
        out, _ = clock.correct("A", stamp, receipt)
        assert out <= receipt
        offset = clock.offset_ms("A")
        assert prev is None or offset <= prev
        prev = offset


def test_suspect_flag():
    clock = sc.SessionClock(suspect_s=300)
    _fed(clock, "ahead", [(T0 + i + 3600 * S, T0 + i) for i in range(3)])
    _fed(clock, "ok", [(T0 + i - S, T0 + i) for i in range(3)])
    assert clock.suspect("ahead") and not clock.suspect("ok")
    assert clock.offsets()["ahead"] == -3600 * S


def test_repeated_pair_counts_once():
    clock = sc.SessionClock(min_obs=3)
    for _ in range(10):
        clock.observe("A", T0, T0 + S)
    assert clock.offset_ms("A") is None


def test_memory_is_bounded():
    clock = sc.SessionClock(max_sessions=10, ttl_s=3600)
    for i in range(1000):
        clock.observe("s%d" % i, T0 + i, T0 + i + S)
    assert len(clock) <= 10
    clock.observe("late", T0 + 7200 * S, T0 + 7200 * S)
    assert len(clock) == 1      # ttl dropped the older sessions


def test_leaf_stamp_integration():
    import datetime
    clock = sc.SessionClock()
    base = datetime.datetime(2026, 10, 12, 12, 0, 0, tzinfo=datetime.timezone.utc)
    rms = int(base.timestamp() * 1000)

    def node(i, ahead_s):
        r = base + datetime.timedelta(seconds=10 * i)
        return {"session_id": "s", "timestamp": r.replace(tzinfo=None).isoformat(),
                "costmap": {"occupancy": {"costmap_stamp_ms": int(r.timestamp() * 1000) + ahead_s * 1000}}}

    first = [sl.leaf_stamp(node(i, 120), clock=clock) for i in range(4)]
    assert [s for _, s in first[:2]] == [sl.STAMP_PROVISIONAL] * 2
    assert first[3] == (rms + 30 * 1000, sl.STAMP_COSTMAP_CORRECTED)
    # without a session id the old behaviour applies
    n = node(0, 120)
    del n["session_id"]
    assert sl.leaf_stamp(n, clock=clock) == sl.leaf_stamp(n)
    # DB-clock receipt is preferred
    n = node(5, 0)
    n["costmap_stored_ms"] = rms + 49 * 1000
    assert sl.leaf_stamp(n, clock=clock)[0] <= rms + 49 * 1000
