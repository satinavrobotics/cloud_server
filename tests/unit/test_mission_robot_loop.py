"""Regression guard for the dispatcher's per-robot message loop (Robot.run).

It used to wrap `await self._messages.get()` in the same broad `except Exception` as the
handlers. A queue that fails on every call (in the unit tests: an asyncio.Queue bound to a
previous test's event loop, from a Robot built in a sync test) then spun forever, logging a
warning per iteration -- millions per second, all kept by pytest's log capture, until the
host ran out of memory. A failing queue must end the loop; a failing handler must not.
"""
import asyncio

import pytest

pytest.importorskip("py_trees")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import cloud_common.objects as api_objects  # noqa: E402
from packages.controllers.mission.server import Robot  # noqa: E402
from packages.database.postgres import PostgresDatabase  # noqa: E402

pytestmark = pytest.mark.unit

MAX_WARNINGS = 20


def _robot():
    server = MagicMock()
    server.push_telemetry = False
    with patch.object(Robot, "run", new=AsyncMock()):   # no background loop from __init__
        robot = Robot("r1", AsyncMock(spec=PostgresDatabase), MagicMock(), "p", server)
    warnings = []

    def warning(message):
        warnings.append(message)
        if len(warnings) >= MAX_WARNINGS:                # a hot loop: stop it, bounded
            robot._alive = False
    robot.warning = warning
    return robot, warnings


async def test_a_failing_queue_ends_the_loop_instead_of_spinning():
    robot, warnings = _robot()
    robot._messages = MagicMock()
    robot._messages.get = AsyncMock(side_effect=RuntimeError("bound to a different event loop"))
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(Robot.run(robot), timeout=2)
    assert robot._messages.get.await_count == 1
    assert warnings == []


async def test_a_failing_handler_is_logged_and_the_loop_goes_on():
    robot, warnings = _robot()
    handled = []

    async def on_robot_change(message):
        handled.append(message.name)
        if len(handled) == 1:
            raise ValueError("handler bug")
        robot._alive = False
    robot._on_robot_change = on_robot_change
    await robot._messages.put(api_objects.RobotObjectV1(name="a", status={}))
    await robot._messages.put(api_objects.RobotObjectV1(name="b", status={}))
    await asyncio.wait_for(Robot.run(robot), timeout=2)
    assert handled == ["a", "b"]
    assert len(warnings) == 1 and "handler bug" in warnings[0]
