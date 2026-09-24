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
import re
from typing import Any, Dict, Optional

import pydantic

from cloud_common.objects import common, object

# A site's id is its object name. It appears in NOTIFY payloads that are split on spaces
# (packages/database/postgres.py PostgresWatcher) and in URLs, so it is kept to a safe set.
SITE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$"
_SITE_ID_RE = re.compile(SITE_ID_PATTERN)

# RFC 7946 object types accepted as a site geofence.
GEOJSON_TYPES = frozenset({
    "Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon",
    "GeometryCollection", "Feature", "FeatureCollection"})


def valid_site_id(site_id: Any) -> bool:
    return isinstance(site_id, str) and bool(_SITE_ID_RE.match(site_id))


def _known_timezone(name: str) -> bool:
    try:
        import zoneinfo  # Python 3.9+
    except ImportError:  # pragma: no cover - Python 3.10 everywhere
        return True
    try:
        zoneinfo.ZoneInfo(name)
        return True
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        # No tz database in this image at all: cannot check, accept.
        return not zoneinfo.available_timezones()


class SiteSpecV1(pydantic.BaseModel):
    """Operator-edited description of a customer site (docs/satinav-fleet-agent-phase0-v2.md
    §3.6). Robots are assigned to a site through robot_site_assignments (history), not here."""
    customer: Optional[str] = pydantic.Field(None, description="Customer the site belongs to.")
    display_name: Optional[str] = pydantic.Field(None, description="Human-readable site name.")
    sector: Optional[str] = pydantic.Field(
        None, description="Business sector of the site, e.g. 'agriculture', 'solar'.")
    geofence: Optional[Dict[str, Any]] = pydantic.Field(
        None, description="Site boundary as a GeoJSON object (WGS84 lon/lat, RFC 7946), "
                          "typically a Polygon or MultiPolygon.")
    gps_datum: Optional[str] = pydantic.Field(
        None, description="Geodetic datum/reference frame of the site's survey, e.g. 'WGS84', "
                          "'ETRS89'.")
    rtk_base: Optional[Dict[str, Any]] = pydantic.Field(
        None, description="RTK correction source, free-form, e.g. {\"caster\": \"...\", "
                          "\"mountpoint\": \"...\", \"latitude\": .., \"longitude\": .., "
                          "\"height_m\": ..}.")
    timezone: Optional[str] = pydantic.Field(
        None, description="IANA time zone of the site, e.g. 'Europe/Budapest'.")
    telemetry_recording: Optional[common.TelemetryRecordingV1] = \
        common.telemetry_recording_field("site")

    @pydantic.validator("geofence")
    def _geofence_is_geojson(cls, value):  # pylint: disable=no-self-argument
        if value is None:
            return value
        if value.get("type") not in GEOJSON_TYPES:
            raise ValueError(f"not a GeoJSON object: 'type' must be one of "
                             f"{sorted(GEOJSON_TYPES)}")
        if value["type"] == "FeatureCollection":
            if not isinstance(value.get("features"), list):
                raise ValueError("a GeoJSON FeatureCollection needs a 'features' list")
        elif value["type"] == "GeometryCollection":
            if not isinstance(value.get("geometries"), list):
                raise ValueError("a GeoJSON GeometryCollection needs a 'geometries' list")
        elif value["type"] == "Feature":
            if "geometry" not in value:
                raise ValueError("a GeoJSON Feature needs a 'geometry'")
        elif not isinstance(value.get("coordinates"), list):
            raise ValueError(f"a GeoJSON {value['type']} needs a 'coordinates' list")
        return value

    @pydantic.validator("timezone")
    def _timezone_is_iana(cls, value):  # pylint: disable=no-self-argument
        if value is not None and not _known_timezone(value):
            raise ValueError(f"unknown IANA time zone {value!r}")
        return value


class SiteStatusV1(pydantic.BaseModel):
    """No derived state yet: sites are pure operator input."""
    pass


class SiteQueryParamsV1(pydantic.BaseModel):
    """Query parameters for listing sites. Extended as needed."""
    pass


class SiteObjectV1(SiteSpecV1, object.ApiObject):
    """A site; its name is the site_id stored on runs and events."""

    status: SiteStatusV1 = SiteStatusV1()

    @classmethod
    def get_alias(cls) -> str:
        return "site"

    @classmethod
    def get_spec_class(cls) -> Any:
        return SiteSpecV1

    @classmethod
    def get_status_class(cls) -> Any:
        return SiteStatusV1

    @classmethod
    def get_query_params(cls) -> Any:
        return SiteQueryParamsV1

    @classmethod
    def default_spec(cls) -> Dict:
        return SiteSpecV1().dict()

    @classmethod
    def supports_spec_update(cls) -> bool:
        return True

    @staticmethod
    def get_query_map() -> Dict:
        return {}
