load("@rules_python//python:defs.bzl", "py_binary", "py_library")
load("@com_nvidia_isaac_mission_dispatch//bazel:mission_dispatch.bzl", 
     "mission_dispatch_py_binary", 
     "mission_dispatch_py_library")
load("@pip_deps//:requirements.bzl", "requirement")

# Package declaration
package(default_visibility = ["//visibility:public"])

# LiveKit Service __init__.py
mission_dispatch_py_library(
    name = "livekit",
    srcs = ["__init__.py"],
    deps = [
        ":server",
        ":client",
    ],
    visibility = ["//visibility:public"]
)

# LiveKit Service (business logic)
mission_dispatch_py_library(
    name = "server",
    srcs = ["server.py"],
    deps = [
        requirement("livekit"),
    ],
    visibility = ["//visibility:public"]
)

# LiveKit Service (FastAPI binary)
mission_dispatch_py_binary(
    name = "livekit_service",
    main = "main.py",
    srcs = ["main.py"],
    deps = [
        ":server",
        requirement("fastapi"),
        requirement("uvicorn"),
        requirement("pydantic"),
        requirement("livekit"),
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

