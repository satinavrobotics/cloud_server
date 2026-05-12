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
load("@python_third_party//:requirements.bzl", "requirement")
load("//bzl:python.bzl", "mission_dispatch_py_binary", "mission_dispatch_py_library")

# R-tree spatial index library
mission_dispatch_py_library(
    name = "rtree_spatial_index",
    srcs = ["rtree_spatial_index.py"],
    deps = [
        requirement("rtree"),
    ],
    visibility = ["//visibility:public"]
)

# ArangoDB spatial index manager library
mission_dispatch_py_library(
    name = "spatial_index_manager",
    srcs = ["spatial_index_manager.py"],
    deps = [
        requirement("python-arango"),
    ],
    visibility = ["//visibility:public"]
)

# Graph Database Service (business logic)
mission_dispatch_py_library(
    name = "server",
    srcs = ["server.py"],
    deps = [
        ":rtree_spatial_index",
        ":spatial_index_manager",
        requirement("python-arango"),
    ],
    visibility = ["//visibility:public"]
)

# Graph Database Service (FastAPI binary)
mission_dispatch_py_binary(
    name = "graph_db_service",
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

