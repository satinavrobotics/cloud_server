"""
Unit tests for navReasoning extraction in the agent orchestrator.

The robot narrates what it is doing via the VDA5050 state information[] array
(infoType="navReasoning"). The agent surfaces the latest such line as advisory
context for the LLM so it can explain *why* a robot stalled. These tests pin the
extraction (_latest_nav_reasoning) and its inclusion in the compacted state sent
to the model (_compact_state).
"""

import pytest

from packages.services.agent_orchestrator.agent import (
    _compact_state,
    _latest_nav_reasoning,
)


def _state_with_info(info):
    return {"orderId": "m1-n0", "information": info}


@pytest.mark.unit
class TestLatestNavReasoning:
    def test_returns_navreasoning_line(self):
        state = _state_with_info([
            {"infoType": "missionStatus", "infoDescription": "running"},
            {"infoType": "navReasoning",
             "infoDescription": "Heading to waypoint 3"},
        ])
        assert _latest_nav_reasoning(state) == "Heading to waypoint 3"

    def test_last_navreasoning_wins(self):
        # If multiple appear (shouldn't normally), the latest entry is used.
        state = _state_with_info([
            {"infoType": "navReasoning", "infoDescription": "Heading to waypoint 1"},
            {"infoType": "navReasoning",
             "infoDescription": "Waypoint 1 aborted - local retry 1/2"},
        ])
        assert (_latest_nav_reasoning(state)
                == "Waypoint 1 aborted - local retry 1/2")

    def test_absent_returns_none(self):
        state = _state_with_info([
            {"infoType": "recordingState", "infoDescription": "idle"},
        ])
        assert _latest_nav_reasoning(state) is None

    def test_no_information_array_returns_none(self):
        assert _latest_nav_reasoning({"orderId": "m1-n0"}) is None

    def test_malformed_entries_are_tolerated(self):
        state = _state_with_info([
            "not-a-dict",
            {"infoType": "navReasoning"},  # missing description
            {"infoType": "navReasoning", "infoDescription": ""},  # empty
        ])
        assert _latest_nav_reasoning(state) is None

    def test_information_not_a_list(self):
        assert _latest_nav_reasoning({"information": "oops"}) is None


@pytest.mark.unit
class TestCompactStateNavReasoning:
    def test_navreasoning_added_when_present(self):
        state = _state_with_info([
            {"infoType": "navReasoning",
             "infoDescription": "Reached waypoint 2"},
        ])
        compact = _compact_state(state)
        assert compact["navReasoning"] == "Reached waypoint 2"
        # The standard relevant field is still carried through.
        assert compact["orderId"] == "m1-n0"

    def test_navreasoning_absent_key_omitted(self):
        compact = _compact_state({"orderId": "m1-n0", "information": []})
        assert "navReasoning" not in compact
