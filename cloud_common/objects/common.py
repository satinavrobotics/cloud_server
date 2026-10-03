"""
SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
Copyright (c) 2021-2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

SPDX-License-Identifier: Apache-2.0
"""
import datetime
import enum
from typing import Any, Literal

import pydantic

# Tell pylint to ignore the invalid names. We must use fields that are specified
# by VDA5050.
# pylint: disable=invalid-name


# Phase 0 recording level (docs/satinav-fleet-agent-phase0-v2.md §4.1). The same values as
# packages/events/schemas.py RecordingLevel, spelled out here because cloud_common is also
# shipped to services without packages/events (graph-builder, mission-planner).
TELEMETRY_RECORDING_LEVELS = ("full", "events_only", "off")
TelemetryRecordingV1 = Literal["full", "events_only", "off"]


# Frame of a GPS datum's local x/y (packages/utils/geo.py): "utm" = UTM grid offsets from the
# datum (robot with GNSS, sati_vda5050_client), "enu" = east/north in the tangent plane at the
# datum (sim, orchestrator gps_anchor). A datum without a frame is "enu" (all of them were,
# before the frame was sent).
DATUM_FRAMES = ("enu", "utm")
DatumFrameV1 = Literal["enu", "utm"]


def normalize_datum_frame(value: Any) -> Any:
    """Pre-validator body for a datum frame field: None/"" -> "enu", case-insensitive."""
    if value is None or value == "":
        return "enu"
    return value.strip().lower() if isinstance(value, str) else value


# WGS84 coordinate types shared by every model that carries a position (map approx location,
# its PUT body, the robot's approx_position topic and the status copy of it).
Latitude = pydantic.confloat(ge=-90.0, le=90.0)
Longitude = pydantic.confloat(ge=-180.0, le=180.0)
AccuracyM = pydantic.confloat(ge=0.0)


def is_null_island(latitude: Any, longitude: Any) -> bool:
    """(0, 0): the "no location / no fix" placeholder, never a real position."""
    return latitude is not None and longitude is not None \
        and latitude == 0.0 and longitude == 0.0


def lenient_utc_stamp(value: Any) -> Any:
    """Pre-validator body for a publisher's timestamp (ISO 8601 or unix seconds): unparseable
    -> None (never a rejected message); naive -> taken as UTC; aware -> converted to UTC."""
    if value is None:
        return None
    try:
        stamp = pydantic.parse_obj_as(datetime.datetime, value)
    except (ValueError, TypeError, pydantic.ValidationError):
        return None
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp.astimezone(datetime.timezone.utc)


def telemetry_recording_field(scope: str) -> Any:
    """The optional `telemetry_recording` spec field; None means "not set here"."""
    return pydantic.Field(
        None, description=(
            f"Phase 0 live-data recording level for this {scope}: 'full', 'events_only' or "
            "'off'. None (the default) means not set here: the level is inherited, resolved "
            "robot -> site -> global settings, and 'events_only' when none is set."))


class ICSError(Exception):
    """
    Base class for exceptions in this module.
    If unexpected Error occurs user will be shown this error.
    """
    error_code: str = "ICS_ERROR"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def __repr__(self):
        return f"{self.__class__.__name__}: {self.message}"

    def __str__(self):
        return self.message


class ICSUsageError(ICSError):
    """ Exception raised for errors to notify users with appropriate message. """
    error_code: str = "USAGE"


class ICSServerError(ICSError):
    """ Exception raised for errors in the server. """
    error_code: str = "SERVER"


class TaskType(enum.Enum):
    MISSION = "MISSION"
    MAP_UPDATE = "MAP_UPDATE"

class Pose2D(pydantic.BaseModel):
    """Specifies a pose to be traveled to by the robot"""
    x: float = pydantic.Field(
        description="Local-frame x coordinate in metres.",
        default=0.0)
    y: float = pydantic.Field(
        description="Local-frame y coordinate in metres.",
        default=0.0)
    theta: float = pydantic.Field(
        description="The rotation of the pose in radians", default=0.0)
    map_id: str = pydantic.Field(
        description="The ID of the map this pose is associated with", default="")
    allowedDeviationXY: float = pydantic.Field(
        description="Allowed coordinate deviation radius",
        default=0.1)
    allowedDeviationTheta: float = pydantic.Field(
        description="Allowed theta deviation radians",
        default=0.0)


def handle_response(response):
    if response.status_code >= 400 and response.status_code < 500:
        raise ICSUsageError(response.text)
    if response.status_code >= 500:
        raise ICSServerError(response.text)
