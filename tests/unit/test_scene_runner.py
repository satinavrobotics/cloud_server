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
    assert [w.closed for w in r.made if w.map_id == "a"] == [True]


def test_max_maps_bounds_memory():
    r = Rig(["a", "b", "c"], cfg=sr.RunnerConfig(max_maps=2))
    r.runner.tick()
    assert len(r.runner.workers) == 2


def test_run_closes_everything_on_stop():
    r = Rig(["a"])
    stops = iter([False, True])
    r.runner.run(sleep=lambda s: None, should_stop=lambda: next(stops))
    assert r.made[0].closed
