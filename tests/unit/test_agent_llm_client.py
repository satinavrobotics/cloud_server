"""The agent's Anthropic client is bounded: timeout and max_retries are set (I4.3)."""
import sys
import types

import pytest

from packages import config
from packages.services.agent_orchestrator.agent import FleetAgent

pytestmark = pytest.mark.unit


def test_client_gets_timeout_and_max_retries(monkeypatch):
    seen = {}
    fake = types.ModuleType("anthropic")
    fake.Anthropic = lambda **kw: seen.update(kw) or object()
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    agent = FleetAgent(api_key="k", model="m", base_url="http://proxy")
    assert not agent.degraded
    assert seen == {"api_key": "k", "base_url": "http://proxy",
                    "timeout": config.AGENT_LLM_TIMEOUT_S,
                    "max_retries": config.AGENT_LLM_MAX_RETRIES}
