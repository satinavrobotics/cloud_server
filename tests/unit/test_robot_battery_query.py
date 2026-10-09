import pytest

from cloud_common.objects.robot import RobotObjectV1


@pytest.mark.unit
@pytest.mark.parametrize("param,op", [("min_battery", ">="), ("max_battery", "<=")])
def test_battery_range_filters_exclude_unknown_battery(param, op):
    clause = RobotObjectV1.get_query_map()[param]
    assert f"(status->'battery_level')::float {op} %s" in clause
    assert "COALESCE((status->>'battery_unknown')::boolean, false) = false" in clause
