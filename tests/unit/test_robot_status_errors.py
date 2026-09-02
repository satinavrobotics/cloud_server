"""Unit tests for vda5050_errors_to_status_dict, the writer for
RobotStatusV1.errors (previously a dead field with no writer, see
sati-client's docs/ROBOT_STATE_ONTOLOGY.md §5).
"""
import pytest

import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission.server import vda5050_errors_to_status_dict


def _error(error_type=None, description="something is wrong",
           level=types.VDA5050ErrorLevel.WARNING):
    return types.VDA5050Error(
        errorType=error_type, errorReferences=[],
        errorDescription=description, errorLevel=level)


@pytest.mark.unit
def test_empty_errors_list_yields_empty_dict():
    assert vda5050_errors_to_status_dict([]) == {}


@pytest.mark.unit
def test_errors_keyed_by_error_type():
    errors = [
        _error(error_type="edgeBlocked", description="Waypoint unreachable"),
        _error(error_type="lowBattery", description="Battery critical",
               level=types.VDA5050ErrorLevel.FATAL),
    ]
    assert vda5050_errors_to_status_dict(errors) == {
        "edgeBlocked": "Waypoint unreachable",
        "lowBattery": "Battery critical",
    }


@pytest.mark.unit
def test_untyped_error_falls_back_to_a_positional_key():
    errors = [_error(error_type=None, description="unspecified fault")]
    assert vda5050_errors_to_status_dict(errors) == {
        "error_0": "unspecified fault",
    }


@pytest.mark.unit
def test_mixed_typed_and_untyped_errors_do_not_collide():
    errors = [
        _error(error_type=None, description="first unspecified"),
        _error(error_type="edgeBlocked", description="blocked"),
        _error(error_type=None, description="second unspecified"),
    ]
    assert vda5050_errors_to_status_dict(errors) == {
        "error_0": "first unspecified",
        "edgeBlocked": "blocked",
        "error_2": "second unspecified",
    }


@pytest.mark.unit
def test_a_repeated_error_type_lets_the_later_entry_win():
    # Mirrors how info_by_type in _on_client_message handles duplicate
    # infoType entries: last one in the message wins.
    errors = [
        _error(error_type="edgeBlocked", description="first"),
        _error(error_type="edgeBlocked", description="second"),
    ]
    assert vda5050_errors_to_status_dict(errors) == {"edgeBlocked": "second"}
