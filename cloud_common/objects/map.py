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
from typing import Any, Dict, Literal, Optional
import pydantic

from cloud_common.objects import common, object

# Map type and lifecycle state (docs/satinav-maps-redesign.md §2, M1). Both are optional on the
# stored object: a row written before M1 (or by an old image) has neither, and readers use
# effective_type() / effective_state() below. The M1 migration classifies every existing map.
MAP_TYPES = ("local", "geo")
MAP_STATES = ("draft", "mapping", "paused", "ready", "archived")
MapTypeV1 = Literal["local", "geo"]
MapStateV1 = Literal["draft", "mapping", "paused", "ready", "archived"]


class MapGeoV1(pydantic.BaseModel):
    """Where a `geo` map sits on the Earth: map x/y are UTM metres in this one zone, relative to
    (origin_e, origin_n). Set from the first mapping session's datum (doc Q1), never changed."""
    utm_zone: int = pydantic.Field(..., ge=1, le=60, description="UTM zone of the map frame.")
    utm_north: bool = pydantic.Field(..., description="UTM hemisphere (True = north).")
    origin_e: float = pydantic.Field(..., description="UTM easting of the map origin (m).")
    origin_n: float = pydantic.Field(..., description="UTM northing of the map origin (m).")


def has_real_datum(latitude: Optional[float], longitude: Optional[float]) -> bool:
    """A datum that anchors a map: both coordinates set and not the (0, 0) placeholder."""
    return (latitude is not None and longitude is not None
            and not (latitude == 0.0 and longitude == 0.0))


class MapSpecV1(pydantic.BaseModel):
    """Immutable properties of a map, including its GPS datum."""
    description: Optional[str] = pydantic.Field(
        None, description="Human-readable map name or label.")
    type: Optional[MapTypeV1] = pydantic.Field(
        None, description="'local' (own metric frame) or 'geo' (UTM, anchored to the Earth). "
                          "None only on rows written before the M1 migration or by an old "
                          "image: see effective_type().")
    geo: Optional[MapGeoV1] = pydantic.Field(
        None, description="UTM zone and origin of a 'geo' map; None until its first session.")
    datum_latitude: Optional[float] = pydantic.Field(
        None, description="WGS84 latitude of the map's local-frame origin (degrees).")
    datum_longitude: Optional[float] = pydantic.Field(
        None, description="WGS84 longitude of the map's local-frame origin (degrees).")
    datum_bearing_deg: float = pydantic.Field(
        0.0,
        description=(
            "Angle in degrees of the map's +X axis from east (grid east for a 'utm' datum), "
            "counter-clockwise positive. 0 means +X east and +Y north; 90 means +X north and "
            "+Y west. Rotates between the local Cartesian and the geographic frame "
            "(packages/utils/geo.py)."
        ))
    datum_frame: common.DatumFrameV1 = pydantic.Field(
        "enu",
        description=(
            "Frame of the map's local x/y: 'utm' = UTM grid offsets from the datum (real robot "
            "with GNSS), 'enu' = east/north in the tangent plane at the datum (sim). Maps "
            "stored before this field existed are 'enu'."
        ))
    datum_utm_zone: Optional[int] = pydantic.Field(
        None, ge=1, le=60,
        description="UTM zone of a 'utm' datum (default: the datum longitude's zone).")
    datum_utm_north: Optional[bool] = pydantic.Field(
        None, description="UTM hemisphere of a 'utm' datum (default: the datum's own).")
    datum_utm_easting: Optional[float] = pydantic.Field(
        None, description="Exact UTM easting of a 'utm' datum, when the robot reported it.")
    datum_utm_northing: Optional[float] = pydantic.Field(
        None, description="Exact UTM northing of a 'utm' datum, when the robot reported it.")

    _normalize_frame = pydantic.validator("datum_frame", pre=True, allow_reuse=True)(
        common.normalize_datum_frame)


class MapStatusV1(pydantic.BaseModel):
    """Live topology counts. Written once on map creation; fresh counts are "
    "always fetched from ArangoDB on GET /maps/{map_id}."""
    node_count: int = 0
    edge_count: int = 0
    # Set while lifecycle is DELETING (packages/api/map_delete.py): when the delete was
    # requested, failed cleanup attempts so far, and the last attempt's error.
    delete_requested_at: Optional[datetime.datetime] = None
    delete_attempts: int = 0
    delete_error: Optional[str] = None
    # Lifecycle (docs/satinav-maps-redesign.md §2). Separate from the object `lifecycle`
    # (ALIVE/DELETING), which stays the delete bookkeeping. None: see effective_state().
    state: Optional[MapStateV1] = None
    # The map's open mapping session (map_sessions.session_id), if any.
    open_session_id: Optional[str] = None
    # Latest grid layer version (M7); None while the map has no grid.
    grid_version: Optional[int] = None


def effective_type(spec: Any) -> str:
    """The map's type; a map stored without one is 'geo' if it has a real datum, else 'local'
    (the M1 migration rule)."""
    if getattr(spec, "type", None):
        return spec.type
    return "geo" if has_real_datum(getattr(spec, "datum_latitude", None),
                                   getattr(spec, "datum_longitude", None)) else "local"


def effective_state(status: Any) -> str:
    """The map's lifecycle state; a map stored without one is 'ready' (the M1 migration rule:
    every map that existed before M1 is usable and takes no data without a session)."""
    return getattr(status, "state", None) or "ready"


class MapQueryParamsV1(pydantic.BaseModel):
    """Query parameters for listing maps. Extended as needed."""
    pass


class MapObjectV1(MapSpecV1, object.ApiObject):
    """Represents a topological map registered in the fleet management system."""

    status: MapStatusV1 = MapStatusV1()

    @classmethod
    def get_alias(cls) -> str:
        return "map"

    @classmethod
    def get_spec_class(cls) -> Any:
        return MapSpecV1

    @classmethod
    def get_status_class(cls) -> Any:
        return MapStatusV1

    @classmethod
    def get_query_params(cls) -> Any:
        return MapQueryParamsV1

    @classmethod
    def default_spec(cls) -> Dict:
        return MapSpecV1().dict()

    @classmethod
    def supports_spec_update(cls) -> bool:
        return True

    @staticmethod
    def get_query_map() -> Dict:
        return {}
