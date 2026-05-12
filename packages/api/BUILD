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

load("@rules_python//python:defs.bzl", "py_binary", "py_library")
load("@mission_dispatch_pip_deps//:requirements.bzl", "requirement")

def mission_dispatch_py_library(**kwargs):
    py_library(**kwargs)

def mission_dispatch_py_binary(**kwargs):
    py_binary(**kwargs)

# API Delegation Service (business logic)
mission_dispatch_py_library(
    name = "server",
    srcs = ["server.py"],
    deps = [
        "//packages/topomap_dbs/graph_db:client",
        "//packages/topomap_dbs/image_db:client",
        "//packages/services/mission_planner:client",
        "//packages/database:client",
        requirement("fastapi"),
        requirement("paho-mqtt"),
    ],
    visibility = ["//visibility:public"]
)

# API Delegation Service (FastAPI binary)
mission_dispatch_py_binary(
    name = "api_delegation",
    main = "main.py",
    srcs = ["main.py"],
    deps = [
        ":server",
        requirement("fastapi"),
        requirement("uvicorn"),
        requirement("pydantic"),
    ],
    visibility = ["//visibility:public"],
)

# Client library for other services
mission_dispatch_py_library(
    name = "client",
    srcs = ["client.py"],
    deps = [
        requirement("requests"),
    ],
    visibility = ["//visibility:public"]
)

