"""DependencyHealthChecker: a hung check times out instead of blocking the caller (I4.1)."""
import threading
import time

import pytest

from packages.utils.service_utils import DependencyHealthChecker

pytestmark = pytest.mark.unit


def test_hung_check_times_out_without_waiting_for_it():
    release = threading.Event()
    checker = DependencyHealthChecker(timeout=0.2, max_retries=1)
    checker.add_dependency("db", lambda: release.wait(10) or True)
    started = time.monotonic()
    try:
        assert checker.check_all() == {"db": False}
        assert time.monotonic() - started < 2.0   # 2 attempts x 0.2 s, not the 10 s hang
    finally:
        release.set()


def test_healthy_and_failing_checks():
    checker = DependencyHealthChecker(timeout=1.0, max_retries=0)
    checker.add_dependency("ok", lambda: True)

    def boom():
        raise RuntimeError("down")
    checker.add_dependency("bad", boom)
    assert checker.check_all() == {"ok": True, "bad": False}
