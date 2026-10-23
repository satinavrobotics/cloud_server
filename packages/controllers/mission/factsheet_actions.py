"""The actions a robot's factsheet lists (docs/node_policies_v2_plan.md, step 1.2).

VDA5050 lists them in `protocolFeatures.agvActions` (entries with `actionType`,
`actionScopes`, `actionParameters[{key, valueDataType, ...}]`, `blockingTypes`); the cloud
first read only a top-level `actions` list, which the robot's orchestrator also fills. Both
places are read and merged by `actionType`."""
from typing import Any, List, Optional, Tuple

from cloud_common.objects import robot as robot_object
from packages.controllers.mission import vda5050_types as types


def _agv_actions(message: types.VDA5050Factsheet) -> Optional[List[Any]]:
    features = message.protocolFeatures
    if features is None:
        return None
    listed = features.dict().get("agvActions")
    return listed if isinstance(listed, list) else None


def _from_agv_action(entry: Any) -> Optional[robot_object.CustomActionV1]:
    if not isinstance(entry, dict) or not isinstance(entry.get("actionType"), str) or \
            not entry["actionType"]:
        return None
    blocking = entry.get("blockingTypes")
    blocking_type = blocking[0] if isinstance(blocking, list) and blocking and \
        isinstance(blocking[0], str) else "HARD"
    params = [robot_object.CustomActionParameterV1(key=str(p["key"]), value="")
              for p in entry.get("actionParameters") or []
              if isinstance(p, dict) and p.get("key") is not None]
    return robot_object.CustomActionV1(
        action_type=entry["actionType"],
        action_description=str(entry.get("actionDescription") or ""),
        action_parameters=params, blocking_type=blocking_type)


def _from_top_level(action: types.VDA5050ActionDefinition) -> robot_object.CustomActionV1:
    return robot_object.CustomActionV1(
        action_type=action.actionType,
        action_description=action.actionDescription,
        action_parameters=[
            robot_object.CustomActionParameterV1(
                key=param.key, value="" if param.value is None else str(param.value))
            for param in action.actionParameters],
        blocking_type=action.blockingType.value,
        icon_hint=action.iconHint)


def listed_actions(message: types.VDA5050Factsheet) \
        -> Tuple[Optional[List[robot_object.CustomActionV1]], Optional[int], Optional[int]]:
    """(actions, top-level count, agvActions count). `actions` is None when the factsheet
    lists neither place (the stored list is then kept); otherwise the two lists merged by
    actionType, the top-level entry winning. A malformed agvActions entry is skipped."""
    top = message.actions
    agv = _agv_actions(message)
    if top is None and agv is None:
        return None, None, None
    merged = {a.actionType: _from_top_level(a) for a in top or []}
    for entry in agv or []:
        action = _from_agv_action(entry)
        if action is not None and action.action_type not in merged:
            merged[action.action_type] = action
    return list(merged.values()), None if top is None else len(top), \
        None if agv is None else len(agv)
