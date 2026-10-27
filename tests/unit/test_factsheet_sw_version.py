"""The robot's build id reported on its factsheet reaches the fleet recorder."""
import pytest

import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission.fleet_recorder import sw_version_from_message

pytestmark = pytest.mark.unit

# What the orchestrator publishes (satibot_orchestrator app/build_id.py), trimmed to the
# fields under test. The optional sections are spelled out for Pydantic 2 (see
# test_factsheet_footprint.py).
_FACTSHEET = {
    "protocolLimits": None, "protocolFeatures": None, "agvGeometry": None,
    "loadSpecification": None, "localizationParameters": None,
    "information": [{"infoType": "swVersion", "infoLevel": "INFO",
                     "infoDescription": "sati/navstack:humble-20261027-9497ec0"}],
}


def _parse(data):
    parse = getattr(types.VDA5050Factsheet, "model_validate", None) or types.VDA5050Factsheet.parse_obj
    return parse(data)


def test_factsheet_build_id_is_kept_and_read():
    assert sw_version_from_message(_parse(_FACTSHEET)) == "sati/navstack:humble-20261027-9497ec0"


def test_factsheet_without_information_has_no_build_id():
    data = {k: v for k, v in _FACTSHEET.items() if k != "information"}
    assert sw_version_from_message(_parse(data)) is None
