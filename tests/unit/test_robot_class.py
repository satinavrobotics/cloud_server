"""packages/utils/robot_class.py: the class string and the speed basis of a factsheet."""
import pytest

from cloud_common.objects.robot import RobotTypeIdentifierV1
from packages import config
from packages.utils import robot_class as rc

pytestmark = pytest.mark.unit


def test_full_factsheet_rounds_to_two_decimals():
    fs = RobotTypeIdentifierV1(agv_class="AMR", length=0.8049, width=0.5, speed_max=1.2)
    assert rc.robot_class(fs) == "AMR|0.80x0.50"
    assert rc.speed_max_of(fs) == 1.2
    assert rc.speed_basis(fs) == rc.SPEED_FACTSHEET


def test_unknown_fields_are_question_marks():
    fs = RobotTypeIdentifierV1(agv_class="", length=0.8, width=-1, speed_max=-1)
    assert rc.robot_class(fs) == "-|0.80x?"
    assert rc.speed_max_of(fs) is None
    assert rc.speed_basis(fs) == rc.SPEED_UNKNOWN


def test_nothing_known_is_the_empty_class():
    assert rc.robot_class(RobotTypeIdentifierV1()) == ""
    assert rc.robot_class(None) == ""
    assert rc.robot_class({}) == ""
    assert rc.speed_basis(None) == rc.SPEED_UNKNOWN


def test_dict_and_nan_and_negative():
    assert rc.robot_class({"agv_class": "x", "length": float("nan"), "width": -0.5, "speed_max": "1.5"}) == "x|?x?"


def test_speed_max_is_not_part_of_the_class():
    """A speed change must not split the history (decision 2)."""
    a = RobotTypeIdentifierV1(agv_class="CARRIER", length=0.5, width=0.5, speed_max=0.24)
    b = RobotTypeIdentifierV1(agv_class="CARRIER", length=0.5, width=0.5, speed_max=0.6)
    assert rc.robot_class(a) == rc.robot_class(b) == "CARRIER|0.50x0.50"


def test_speed_max_alone_does_not_make_a_class():
    assert rc.robot_class({"speed_max": 0.3}) == ""
    assert rc.speed_basis({"speed_max": 0.3}) == rc.SPEED_FACTSHEET


def test_placeholder_is_one_point_zero_without_a_footprint():
    """The live factsheets of subtle-pelican and masked-frigatebird: CARRIER, length = width = -1, speed_max 1.0."""
    fs = RobotTypeIdentifierV1(agv_class="CARRIER", length=-1, width=-1, speed_max=1.0)
    assert rc.robot_class(fs) == "CARRIER|?x?"
    assert rc.speed_basis(fs) == rc.SPEED_PLACEHOLDER
    assert rc.speed_max_of(fs) == 1.0                   # the number is kept, the basis says not to use it


def test_one_point_zero_with_a_footprint_is_a_real_speed():
    assert rc.speed_basis({"agv_class": "AMR", "length": 0.8, "width": 0.5, "speed_max": 1.0}) == rc.SPEED_FACTSHEET
    assert rc.speed_basis({"agv_class": "AMR", "length": 0.8, "width": -1, "speed_max": 1.0}) == rc.SPEED_FACTSHEET


def test_a_real_speed_without_a_footprint_is_not_a_placeholder():
    assert rc.speed_basis({"agv_class": "AMR", "length": -1, "width": -1, "speed_max": 0.24}) == rc.SPEED_FACTSHEET


def test_placeholder_value_is_a_config_knob(monkeypatch):
    monkeypatch.setattr(config, "ROBOT_PLACEHOLDER_SPEED_MAX", 2.0)
    assert rc.speed_basis({"length": -1, "width": -1, "speed_max": 2.0}) == rc.SPEED_PLACEHOLDER
    assert rc.speed_basis({"length": -1, "width": -1, "speed_max": 1.0}) == rc.SPEED_FACTSHEET


def test_zero_speed_is_unknown():
    assert rc.speed_basis({"agv_class": "AMR", "length": 0.8, "width": 0.5, "speed_max": 0.0}) == rc.SPEED_UNKNOWN
