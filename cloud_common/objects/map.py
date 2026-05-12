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

from typing import Any, Dict, Optional
import pydantic

from cloud_common.objects import object


class MapSpecV1(pydantic.BaseModel):
    """Immutable properties of a map, including its GPS datum."""
    description: Optional[str] = pydantic.Field(
        None, description="Human-readable map name or label.")
    datum_latitude: Optional[float] = pydantic.Field(
        None, description="WGS84 latitude of the map's local-frame origin (degrees).")
    datum_longitude: Optional[float] = pydantic.Field(
        None, description="WGS84 longitude of the map's local-frame origin (degrees).")
    datum_bearing_deg: float = pydantic.Field(
        0.0,
        description=(
            "Angle in degrees (counter-clockwise positive, standard math convention) "
            "from the map's positive X-axis to true north. "
            "Example: 0° means map +X points east and +Y points north; "
            "90° means map +X points north and +Y points west. "
            "Used to rotate between local Cartesian and geographic frames."
        ))


class MapStatusV1(pydantic.BaseModel):
    """Live topology counts. Written once on map creation; fresh counts are "
    "always fetched from ArangoDB on GET /maps/{map_id}."""
    node_count: int = 0
    edge_count: int = 0


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
