"""Step 1.2 of docs/node_policies_v2_plan.md: the factsheet's actions are read from the top-level
`actions` AND protocolFeatures.agvActions, merged by actionType; where the factsheet came from is
stored; a registration without actions leaves the stored list alone."""
import pytest

import cloud_common.objects.robot as robot_object
import packages.controllers.mission.vda5050_types as types
from packages.api import main as api_main
from packages.controllers.mission import factsheet_actions
from tests.unit.test_factsheet_footprint import _factsheet, _robot as _factsheet_robot

AGV_NODE_POLICY = {"actionType": "nodePolicy", "actionScopes": ["NODE"],
                   "blockingTypes": ["NONE"],
                   "actionParameters": [{"key": "skippable", "valueDataType": "BOOL"},
                                        {"key": "maxWaitS", "valueDataType": "FLOAT"}]}


def _sheet(top=None, agv=None, **kw):
    fs = _factsheet()
    if top is not None:
        fs.actions = [types.VDA5050ActionDefinition(actionType=t) for t in top]
    if agv is not None:
        fs.protocolFeatures = types.VDA5050ProtocolFeatures(agvActions=agv)
    for k, v in kw.items():
        setattr(fs, k, v)
    return fs


@pytest.mark.unit
def test_the_standard_place_alone_is_read():
    actions, n_top, n_agv = factsheet_actions.listed_actions(_sheet(agv=[AGV_NODE_POLICY]))
    (a,) = actions
    assert (a.action_type, a.blocking_type, n_top, n_agv) == ("nodePolicy", "NONE", None, 1)
    assert [p.key for p in a.action_parameters] == ["skippable", "maxWaitS"]


@pytest.mark.unit
def test_the_legacy_place_alone_is_read():
    actions, n_top, n_agv = factsheet_actions.listed_actions(_sheet(top=["dock_robot"]))
    assert [a.action_type for a in actions] == ["dock_robot"] and (n_top, n_agv) == (1, None)


@pytest.mark.unit
def test_an_empty_top_level_list_does_not_hide_the_standard_one():
    """The 22 Oct factsheet: `actions: []` from the orchestrator next to agvActions."""
    actions, n_top, n_agv = factsheet_actions.listed_actions(
        _sheet(top=[], agv=[AGV_NODE_POLICY]))
    assert [a.action_type for a in actions] == ["nodePolicy"] and (n_top, n_agv) == (0, 1)


@pytest.mark.unit
def test_both_places_are_merged_by_action_type_and_bad_entries_skipped():
    actions, _, _ = factsheet_actions.listed_actions(
        _sheet(top=["nodePolicy", "dock_robot"], agv=[AGV_NODE_POLICY, {"actionType": "x"}, 7, {}]))
    assert sorted(a.action_type for a in actions) == ["dock_robot", "nodePolicy", "x"]


@pytest.mark.unit
def test_neither_place_means_no_listing():
    assert factsheet_actions.listed_actions(_sheet()) == (None, None, None)


@pytest.mark.unit
async def test_the_dispatcher_stores_the_actions_and_where_they_came_from():
    r, _ = _factsheet_robot()
    fs = _sheet(top=[], agv=[AGV_NODE_POLICY], headerId=7, timestamp="2026-10-23T10:00:00Z",
                manufacturer="Satinav Robotics", serialNumber="formidable-peacock",
                received_topic="uagv/v2/RobotCompany/formidable-peacock/factsheet",
                received_retained=True)
    await r._on_client_factsheet(fs)
    factsheet = r._robot_object.status.factsheet
    assert [a.action_type for a in factsheet.custom_actions] == ["nodePolicy"]
    src = factsheet.source
    assert (src.topic, src.retained, src.header_id, src.timestamp) == (
        "uagv/v2/RobotCompany/formidable-peacock/factsheet", True, 7, "2026-10-23T10:00:00Z")
    assert (src.manufacturer, src.serial_number) == ("Satinav Robotics", "formidable-peacock")
    assert (src.actions_top_level, src.actions_agv_actions) == (0, 1)
    assert src.received_at is not None


@pytest.mark.unit
async def test_a_factsheet_listing_no_actions_keeps_the_stored_ones_but_records_the_source():
    r, _ = _factsheet_robot()
    r._robot_object.status.factsheet.custom_actions = [
        robot_object.CustomActionV1(action_type="nodePolicy")]
    await r._on_client_factsheet(_sheet())
    assert len(r._robot_object.status.factsheet.custom_actions) == 1
    assert r._robot_object.status.factsheet.source is not None


@pytest.mark.unit
def test_a_registration_without_or_with_empty_actions_lists_none():
    assert api_main._custom_actions({"agv_class": "CARRIER"}) is None
    assert api_main._custom_actions({"actions": []}) is None
    (a,) = api_main._custom_actions({"actions": [{"action_type": "nodePolicy"}]})
    assert a.action_type == "nodePolicy"
