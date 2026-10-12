"""Wiring of the scene-graph runner into graph-builder: scene_host.py and the lifespan in main.py."""
import asyncio
import sys
import threading

import pytest

from packages.services.graph_builder import scene_host as sh

try:
    import packages.services.graph_builder.main as gb_main
except Exception as exc:  # noqa: BLE001
    gb_main = None
    MAIN_ERR = exc


class FakeRunner:
    def __init__(self, crash_first=False):
        self.crash_first, self.runs, self.closed = crash_first, 0, 0
        self.started = threading.Event()

    def run(self, sleep, should_stop, tick_sleep_s=0.5):
        self.runs += 1
        self.started.set()
        if self.crash_first and self.runs == 1:
            raise RuntimeError("boom")
        while not should_stop():
            sleep(0.01)

    def close(self):
        self.closed += 1


class FakeGuard:
    closed = 0

    def close(self):
        self.closed += 1


def test_host_runs_and_stop_joins_and_closes():
    runner, guard = FakeRunner(), FakeGuard()
    host = sh.SceneWorkerHost(runner, guard)
    host.start()
    assert runner.started.wait(2) and host.running
    host.stop(timeout=5)
    assert not host.running
    assert runner.closed >= 1 and guard.closed == 1


def test_runner_crash_is_logged_and_restarted(caplog):
    runner = FakeRunner(crash_first=True)
    host = sh.SceneWorkerHost(runner, FakeGuard(), restart_pause_s=0.01)
    host.start()
    for _ in range(200):
        if runner.runs >= 2:
            break
        threading.Event().wait(0.01)
    assert runner.runs >= 2 and host.running
    host.stop(timeout=5)
    assert "crashed" in caplog.text


def test_start_returns_none_when_build_fails(caplog):
    def build(*a):
        raise RuntimeError("no store")
    assert sh.start_scene_worker(object(), None, build=build) is None
    assert "no store" in caplog.text


def test_import_failure_of_store_is_contained(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "packages.scene_graph.store", None)   # import raises ImportError
    assert sh.start_scene_worker(object(), None) is None
    assert "scene graph worker not started" in caplog.text


# ---------------------------------------------------------------- main.py lifespan

class FakeService:
    def __init__(self, *a, **k):
        self.database = type("D", (), {"async_init": staticmethod(self._noop)})()
        self.disconnected = False

    @staticmethod
    async def _noop():
        return None

    def connect_mqtt(self): pass
    def set_event_loop(self, loop): pass
    def disconnect_mqtt(self): self.disconnected = True
    def _cleanup_old_mappings(self): pass
    async def flush_rejects(self): pass


@pytest.fixture
def lifespan_env(monkeypatch):
    if gb_main is None:
        pytest.skip(f"graph_builder.main not importable: {MAIN_ERR}")
    monkeypatch.setattr(gb_main, "GraphBuilderService", FakeService)
    monkeypatch.setattr(gb_main, "require_secret", lambda name: "x")
    monkeypatch.setattr(gb_main, "postgres_database_password", lambda: "x")

    def run(enabled, start):
        monkeypatch.setattr(gb_main, "SCENE_GRAPH_WORKER_ENABLED", enabled)
        monkeypatch.setattr(sh, "start_scene_worker", start)

        async def go():
            async with gb_main.lifespan(gb_main.app):
                pass
        asyncio.run(go())
    return run


def test_disabled_never_touches_scene_host(lifespan_env):
    calls = []
    lifespan_env(False, lambda *a, **k: calls.append(a))
    assert calls == []


def test_enabled_starts_and_stops_on_shutdown(lifespan_env):
    class Host:
        stopped = 0
        def stop(self): Host.stopped += 1
    started = []
    lifespan_env(True, lambda svc, loop: started.append((svc, loop)) or Host())
    assert len(started) == 1 and Host.stopped == 1


def test_enabled_but_not_started_keeps_service_up(lifespan_env):
    lifespan_env(True, lambda svc, loop: None)    # start_scene_worker returned None: nothing to stop


def test_enabled_with_raising_start_does_not_crash(lifespan_env):
    def boom(*a):
        raise RuntimeError("x")
    lifespan_env(True, boom)


def test_build_host_wires_real_pieces_lazily():
    pytest.importorskip("packages.scene_graph.store")
    from types import SimpleNamespace
    svc = SimpleNamespace(topomap_db=object(), image_db=SimpleNamespace(list_maps=lambda: []),
                          graph_db=object(), update_publisher=object())
    host = sh.build_host(svc, None, conninfo="dbname=x")     # no connection is opened until used
    assert isinstance(host, sh.SceneWorkerHost) and host.runner is not None and host.guard is not None
