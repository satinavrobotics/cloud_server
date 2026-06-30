"""
Service-level routing tests for the Agent Orchestrator.

Drives the real `_on_state_message` handler with fake MQTT messages and a
degraded (no-key) agent — no broker, no network. Confirms messages route to the
right per-robot session, insights carry monotonic global ids, and the read/stats
APIs aggregate correctly.
"""

import json
import asyncio

import pytest

from packages.services.agent_orchestrator.server import AgentOrchestratorService

PREFIX = "uagv/v2/RobotCompany"


class FakeMsg:
    def __init__(self, topic: str, payload: dict):
        self.topic = topic
        self.payload = json.dumps(payload).encode("utf-8")


def _service() -> AgentOrchestratorService:
    return AgentOrchestratorService(
        mqtt_host="localhost", mqtt_port=1883, vda5050_prefix=PREFIX,
        api_key=None, model="claude-haiku-4-5", battery_low_threshold=20.0,
    )


def _state_msg(robot: str, payload: dict) -> FakeMsg:
    return FakeMsg(f"{PREFIX}/{robot}/state", payload)


async def _drain():
    # Let scheduled coroutines + their to_thread workers complete.
    await asyncio.sleep(0.25)


@pytest.mark.unit
class TestRouting:
    async def test_quiet_message_creates_session_but_no_insight(self):
        svc = _service()
        svc.set_event_loop(asyncio.get_running_loop())
        svc._on_state_message(None, None, _state_msg(
            "amr-07", {"operatingMode": "AUTOMATIC", "safetyState": {"eStop": "NONE"}, "errors": []}))
        await _drain()
        assert "amr-07" in svc.sessions          # session created lazily
        assert svc.get_insights(robot="amr-07") == []  # baseline only, no event

    async def test_event_produces_insight_in_correct_session(self):
        svc = _service()
        svc.set_event_loop(asyncio.get_running_loop())
        # baseline
        svc._on_state_message(None, None, _state_msg(
            "amr-07", {"operatingMode": "AUTOMATIC", "safetyState": {"eStop": "NONE"}, "errors": []}))
        # e-stop + fatal error
        svc._on_state_message(None, None, _state_msg("amr-07", {
            "operatingMode": "MANUAL",
            "safetyState": {"eStop": "MANUAL"},
            "errors": [{"errorType": "x", "errorLevel": "FATAL", "errorDescription": "drive stopped"}],
        }))
        await _drain()
        insights = svc.get_insights(robot="amr-07")
        assert len(insights) == 1
        assert insights[0]["robot"] == "amr-07"
        assert insights[0]["severity"] == "critical"
        assert {e["type"] for e in insights[0]["fired_events"]} == {
            "estop_engaged", "new_error", "operating_mode_change"}

    async def test_two_robots_isolated_with_monotonic_global_ids(self):
        svc = _service()
        svc.set_event_loop(asyncio.get_running_loop())
        # amr-07 standing fatal error on first sight
        svc._on_state_message(None, None, _state_msg("amr-07", {
            "errors": [{"errorType": "a", "errorLevel": "FATAL", "errorDescription": "d1"}]}))
        # amr-12 standing e-stop on first sight
        svc._on_state_message(None, None, _state_msg("amr-12", {
            "safetyState": {"eStop": "MANUAL"}}))
        await _drain()

        assert len(svc.get_insights(robot="amr-07")) == 1
        assert len(svc.get_insights(robot="amr-12")) == 1
        assert svc.get_insights(robot="amr-99") == []  # unknown robot

        merged = svc.get_insights()  # fleet feed, newest first
        ids = [i["id"] for i in merged]
        assert len(ids) == 2
        assert ids == sorted(ids, reverse=True)  # monotonic global ordering

    async def test_stats_aggregate_across_sessions(self):
        svc = _service()
        svc.set_event_loop(asyncio.get_running_loop())
        svc._on_state_message(None, None, _state_msg("amr-07", {
            "safetyState": {"eStop": "MANUAL"}}))
        svc._on_state_message(None, None, _state_msg("amr-12", {
            "safetyState": {"eStop": "MANUAL"}}))
        await _drain()
        stats = svc.get_stats()
        assert stats["robots_tracked"] == 2
        assert stats["insights_buffered"] == 2
        assert stats["insights_generated"] == 2
        assert stats["messages_seen"] == 2
        assert stats["degraded"] is True
