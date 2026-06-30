"""
Unit tests for agent._parse_json_object — the defensive extraction of the triage
JSON from model output. Non-Claude models (behind the LiteLLM proxy) wrap the
object in code fences or add prose, so the parser must be robust to that.
"""

import pytest

from packages.services.agent_orchestrator.agent import _parse_json_object

OBJ = '{"severity": "warning", "summary": "Pincer01 battery depleted", "suggested_action": "Recharge"}'


@pytest.mark.unit
class TestParseJsonObject:
    def test_bare_json(self):
        assert _parse_json_object(OBJ)["summary"] == "Pincer01 battery depleted"

    def test_markdown_fenced(self):
        text = f"```json\n{OBJ}\n```"
        assert _parse_json_object(text)["severity"] == "warning"

    def test_prose_before(self):
        text = f"Here is the triage result:\n{OBJ}"
        assert _parse_json_object(text)["suggested_action"] == "Recharge"

    def test_trailing_prose_with_braces(self):
        # The exact failure mode: a stray brace in trailing prose poisoned the
        # old first-{ ... last-} slice. The balanced scan must ignore it.
        text = f"{OBJ}\n\nNote: thresholds like {{20%}} are configurable."
        result = _parse_json_object(text)
        assert result is not None
        assert result["summary"] == "Pincer01 battery depleted"

    def test_nested_braces_in_string(self):
        obj = '{"summary": "battery at {0%} now", "severity": "warning"}'
        assert _parse_json_object(obj)["summary"] == "battery at {0%} now"

    def test_no_json_returns_none(self):
        assert _parse_json_object("the robot is fine, nothing to report") is None

    def test_empty_returns_none(self):
        assert _parse_json_object("") is None

    def test_malformed_returns_none(self):
        assert _parse_json_object("{severity: warning, no quotes}") is None
