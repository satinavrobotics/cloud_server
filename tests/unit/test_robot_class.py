"""packages/utils/robot_class.py: the class string of a factsheet."""
import pytest

from cloud_common.objects.robot import RobotTypeIdentifierV1
from packages.utils import robot_class as rc

pytestmark = pytest.mark.unit


def test_full_factsheet_rounds_to_two_decimals():
    fs = RobotTypeIdentifierV1(agv_class="AMR", length=0.8049, width=0.5, speed_max=1.2)
    assert rc.robot_class(fs) == "AMR|0.80x0.50|1.20"
    assert rc.speed_max_of(fs) == 1.2


def test_unknown_fields_are_question_marks():
    fs = RobotTypeIdentifierV1(agv_class="", length=0.8, width=-1, speed_max=-1)
    assert rc.robot_class(fs) == "-|0.80x?|?"
    assert rc.speed_max_of(fs) is None


def test_nothing_known_is_the_empty_class():
    assert rc.robot_class(RobotTypeIdentifierV1()) == ""
    assert rc.robot_class(None) == ""
    assert rc.robot_class({}) == ""


def test_dict_and_nan_and_negative():
    assert rc.robot_class({"agv_class": "x", "length": float("nan"), "width": -0.5, "speed_max": "1.5"}) == "x|?x?|1.50"


def test_same_factsheet_same_class_and_zero_is_known():
    a = RobotTypeIdentifierV1(agv_class="AMR", length=0.8, width=0.5, speed_max=0.0)
    assert rc.robot_class(a) == "AMR|0.80x0.50|0.00"
