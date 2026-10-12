from packages.services.graph_builder import scene_runner as sr
from packages.services.graph_builder import scene_worker as sw


class FakeWorker:
    def __init__(self, map_id, script):
        self.map_id, self.script, self.closed, self.steps = map_id, list(script), False, 0

    def step(self):
        self.steps += 1
        state = self.script.pop(0) if self.script else sw.IDLE
        if isinstance(state, Exception):
            raise state
        return sw.StepResult(state)

    def close(self):
        self.closed = True


class Rig:
    def __init__(self, maps, scripts=None, cfg=None):
        self.t, self.maps, self.scripts = 0.0, list(maps), scripts or {}
        self.made, self.status, self.fail_make = [], [], set()
        self.runner = sr.SceneRunner(lambda: list(self.maps), self.make, cfg or sr.RunnerConfig(),
                                     now=lambda: self.t, on_status=lambda m, s: self.status.append((m, s)))

    def make(self, map_id):
        if map_id in self.fail_make:
            raise RuntimeError("no engine")
        w = FakeWorker(map_id, self.scripts.get(map_id, []))
        self.made.append(w)
        return w


def test_idle_map_is_revisited_after_idle_s():
    r = Rig(["a"])
    assert r.runner.tick() == 1
    assert r.runner.tick() == 0
    r.t = 2.0
    assert r.runner.tick() == 1 and r.made[0].steps == 2


def test_worked_is_stepped_again_at_once():
    r = Rig(["a"], {"a": [sw.WORKED, sw.WORKED]})
    r.runner.tick(); r.runner.tick(); r.runner.tick()
    assert r.made[0].steps == 3


def test_terminal_closes_worker_and_retries_later_with_new_one():
    r = Rig(["a"], {"a": [sw.DELETING]})
    r.runner.tick()
    assert r.made[0].closed and "a" not in r.runner.workers
    r.t = 30
    assert r.runner.tick() == 0
    r.t = 61
    assert r.runner.tick() == 1 and len(r.made) == 2


def test_needs_rebuild_reported_once_and_retried_slowly():
    r = Rig(["a"], {"a": [sw.NEEDS_REBUILD, sw.NEEDS_REBUILD]})
    r.runner.tick()
    r.t = 301
    r.runner.tick()
    assert len(r.made) == 2                       # retried with a fresh worker
    assert r.status == [("a", sw.NEEDS_REBUILD)]  # reported once


def test_step_exception_drops_worker_and_backs_off():
    r = Rig(["a"], {"a": [RuntimeError("boom")]})
    r.runner.tick()
    assert r.made[0].closed
    r.t = 5
    assert r.runner.tick() == 0
    r.t = 11
    assert r.runner.tick() == 1


def test_build_failure_backs_off():
    r = Rig(["a"])
    r.fail_make.add("a")
    r.runner.tick()
    assert r.made == []
    r.t = 11
    r.fail_make.clear()
    assert r.runner.tick() == 1


def test_map_removed_from_list_closes_worker():
    r = Rig(["a", "b"])
    r.runner.tick()
    r.maps = ["b"]
    r.t = 61
    r.runner.tick()
    assert not [w for w in r.made if w.map_id == "a"][0].closed   # one empty listing drops nothing
    r.t = 122
    r.runner.tick()
    assert [w.closed for w in r.made if w.map_id == "a"] == [True]


def test_max_maps_bounds_memory():
    r = Rig(["a", "b", "c"], cfg=sr.RunnerConfig(max_maps=2))
    r.runner.tick()
    assert len(r.runner.workers) == 2


def test_run_closes_everything_on_stop():
    r = Rig(["a"])
    import itertools
    stops = itertools.chain([False, False, True], itertools.repeat(True))
    r.runner.run(sleep=lambda s: None, should_stop=lambda: next(stops))
    assert r.made[0].closed


def test_round_robin_one_step_per_map_per_pass_and_rotating_start():
    r = Rig(["a", "b", "c"], {k: [sw.WORKED] * 6 for k in "abc"})
    order = []
    orig = r.make

    def make(map_id):
        w = orig(map_id)
        step = w.step

        def logged():
            order.append(map_id)
            return step()
        w.step = logged
        return w
    r.runner.make_worker = make
    for _ in range(3):
        r.runner.tick()
    assert [sorted(order[i:i + 3]) for i in (0, 3, 6)] == [["a", "b", "c"]] * 3   # each map once per pass
    assert [order[0], order[3], order[6]] == ["a", "b", "c"]                     # the start rotates


def test_slow_map_does_not_starve_the_others():
    r = Rig(["slow", "b"], {"slow": [sw.WORKED] * 5, "b": [sw.WORKED] * 5})
    for _ in range(4):
        r.runner.tick()
    steps = {w.map_id: w.steps for w in r.made}
    assert steps["b"] == steps["slow"] == 4


def test_should_stop_is_checked_between_maps():
    r = Rig(["a", "b", "c"])
    calls = iter([False, True])
    assert r.runner.tick(lambda: next(calls, True)) == 1


def test_skipped_map_is_reported_once_per_interval_and_not_stepped():
    events = []
    r = Rig(["a", "off"])
    r.runner.skip_reason = lambda m: "map_not_enabled" if m == "off" else None
    r.runner.on_event = lambda m, k, p: events.append((m, k, p))
    r.runner.tick()
    assert [w.map_id for w in r.made] == ["a"]
    assert events == [("off", "skipped", {"reason": "map_not_enabled"})]
    r.t = 61
    r.runner.tick()
    assert len(events) == 1                     # rate limited
    r.t = 3700
    r.runner.tick()
    assert len(events) == 2


def test_non_string_and_empty_map_ids_are_ignored():
    r = Rig([])
    r.runner.list_maps = lambda: ["a", "", None, 7]
    r.runner.tick()
    assert [w.map_id for w in r.made] == ["a"]
