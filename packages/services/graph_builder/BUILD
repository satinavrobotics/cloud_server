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

# Graph Builder Service (business logic)
mission_dispatch_py_library(
    name = "server",
    srcs = ["server.py"],
    deps = [
        requirement("paho-mqtt"),
        "//packages/topomap_dbs/image_db:client",
        "//packages/topomap_dbs/graph_db:client",
    ],
    visibility = ["//visibility:public"]
)

# Scene-graph ingest worker (SCENE_GRAPH_WORKER_ENABLED): leaf building, guard (Postgres lifecycle
# and advisory lock), runner, host, events, store adapter. Started by main.py; imports
# packages.scene_graph for the engine and store.
mission_dispatch_py_library(
    name = "scene",
    srcs = [
        "scene_adapter.py",
        "scene_clock.py",
        "scene_events.py",
        "scene_guard.py",
        "scene_host.py",
        "scene_leaf.py",
        "scene_runner.py",
        "scene_sources.py",
        "scene_worker.py",
    ],
    deps = [
        "//packages/scene_graph:scene_graph",
        "//packages/topomap_dbs/graph_db:client",
        "//packages/topomap_dbs/image_db:client",
        requirement("numpy"),
        requirement("scipy"),
        requirement("psycopg"),
    ],
    visibility = ["//visibility:public"],
)

# Graph Builder Service (FastAPI binary)
mission_dispatch_py_binary(
    name = "graph_builder",
    main = "main.py",
    srcs = ["main.py"],
    deps = [
        ":server",
        ":scene",
        requirement("fastapi"),
        requirement("uvicorn"),
        requirement("pydantic"),
    ],
    visibility = ["//visibility:public"],
)

