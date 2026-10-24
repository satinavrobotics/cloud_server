"""A bag's robot_name is one object-key segment (I3.13), and a DB error is not "not found" (I3.7)."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from packages.api.main import CreateBagEntryRequest
from packages.api.server import ApiDelegationService

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", ["", "..", ".", "other/x", "a/"])
def test_robot_name_with_a_slash_is_refused(name):
    with pytest.raises(ValidationError):
        CreateBagEntryRequest(robot_name=name)


def test_plain_robot_name_is_accepted():
    assert CreateBagEntryRequest(robot_name="robot-1").robot_name == "robot-1"


def _svc(get_object):
    svc = ApiDelegationService.__new__(ApiDelegationService)
    svc.logger = MagicMock()
    svc.database = MagicMock()
    svc.database.get_object = get_object
    return svc


async def test_get_map_missing_is_not_found_but_a_db_error_is_raised():
    svc = _svc(AsyncMock(side_effect=HTTPException(404, "x")))
    assert (await svc.get_map("m"))["success"] is False
    svc = _svc(AsyncMock(side_effect=RuntimeError("db down")))
    with pytest.raises(RuntimeError):
        await svc.get_map("m")


async def test_navigate_check_keeps_404_and_does_not_hide_db_errors():
    svc = _svc(AsyncMock(side_effect=HTTPException(404, "x")))
    with pytest.raises(HTTPException) as exc:
        await svc.navigate("r", 1.0, 2.0)
    assert exc.value.status_code == 404
    svc = _svc(AsyncMock(side_effect=RuntimeError("db down")))
    with pytest.raises(RuntimeError):
        await svc.navigate("r", 1.0, 2.0)
