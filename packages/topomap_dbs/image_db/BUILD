load("@rules_python//python:defs.bzl", "py_binary", "py_library")
load("@com_nvidia_isaac_mission_dispatch//bazel:mission_dispatch.bzl", 
     "mission_dispatch_py_binary", 
     "mission_dispatch_py_library")
load("@pip_deps//:requirements.bzl", "requirement")

# Image Database Service (business logic)
mission_dispatch_py_library(
    name = "server",
    srcs = ["server.py"],
    deps = [
        requirement("minio"),
    ],
    visibility = ["//visibility:public"]
)

# Image Database Client
mission_dispatch_py_library(
    name = "client",
    srcs = ["client.py"],
    deps = [
        requirement("requests"),
    ],
    visibility = ["//visibility:public"]
)

# Image Database Service (FastAPI binary)
mission_dispatch_py_binary(
    name = "image_db_service",
    main = "main.py",
    srcs = ["main.py"],
    deps = [
        ":server",
        requirement("fastapi"),
        requirement("uvicorn"),
        requirement("pydantic"),
        requirement("python-multipart"),
    ],
    visibility = ["//visibility:public"],
)

