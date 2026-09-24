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

from typing import Any, Dict, List, Optional
import pydantic

from cloud_common.objects import common, object

# There is exactly one settings row, always addressed by this fixed name — a
# singleton simulated by convention on top of the normal name-keyed object
# storage (see packages/api/main.py's /api/v1/settings routes), since the
# database layer has no separate keyless/singleton storage mode.
GLOBAL_SETTINGS_NAME = "global"


class SettingsSpecV1(pydantic.BaseModel):
    """Fleet-wide, operator-editable configuration not tied to any one robot/mission/map."""
    fault_error_types: List[str] = pydantic.Field(
        default_factory=list,
        description=(
            "VDA5050 errorType strings severe enough that a robot reporting one should be "
            "badged FAULT by clients. Any other errorType a robot reports is treated as a "
            "non-fault warning. Empty by default: nothing is FAULT until an operator opts "
            "specific error types in here."
        ))
    telemetry_recording: Optional[common.TelemetryRecordingV1] = \
        common.telemetry_recording_field("fleet (global default)")


class SettingsStatusV1(pydantic.BaseModel):
    """No live/derived state yet — settings are pure operator input."""
    pass


class SettingsQueryParamsV1(pydantic.BaseModel):
    """Query parameters for listing settings. Extended as needed."""
    pass


class SettingsObjectV1(SettingsSpecV1, object.ApiObject):
    """The single fleet-wide settings object, always stored under GLOBAL_SETTINGS_NAME."""

    status: SettingsStatusV1 = SettingsStatusV1()

    @classmethod
    def get_alias(cls) -> str:
        return "settings"

    @classmethod
    def get_spec_class(cls) -> Any:
        return SettingsSpecV1

    @classmethod
    def get_status_class(cls) -> Any:
        return SettingsStatusV1

    @classmethod
    def get_query_params(cls) -> Any:
        return SettingsQueryParamsV1

    @classmethod
    def default_spec(cls) -> Dict:
        return SettingsSpecV1().dict()

    @classmethod
    def supports_spec_update(cls) -> bool:
        return True

    @staticmethod
    def get_query_map() -> Dict:
        return {}
