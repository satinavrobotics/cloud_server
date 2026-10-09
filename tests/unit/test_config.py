#!/usr/bin/env python3
"""
Unit tests for packages/config.py.

Validates that:
- Missing secrets do not break the import; require_secret() raises for them.
- MQTT_KEEPALIVE and other constants are accessible and correct type.
"""

import importlib
import os
import sys
import pytest


def _reload_config(env_overrides: dict):
    """
    Reload packages.config with a specific set of environment variables.
    Returns the reloaded module.
    """
    # Remove cached module so the import-time validation runs again
    for key in list(sys.modules.keys()):
        if "packages.config" in key:
            del sys.modules[key]

    env_backup = {k: os.environ.get(k) for k in env_overrides}
    os.environ.update({k: v for k, v in env_overrides.items() if v is not None})
    for k, v in env_overrides.items():
        if v is None and k in os.environ:
            del os.environ[k]

    try:
        import packages.config as config
        return config
    finally:
        # Restore environment
        for k, original in env_backup.items():
            if original is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = original
        # Remove cached module again so later tests start fresh
        for key in list(sys.modules.keys()):
            if "packages.config" in key:
                del sys.modules[key]


# ---- Required secrets must be set -------------------------------------------

REQUIRED_SECRETS = {
    "ARANGO_PASSWORD": ("MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"),
    "MINIO_ACCESS_KEY": ("ARANGO_PASSWORD", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"),
    "MINIO_SECRET_KEY": ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "POSTGRES_PASSWORD"),
    "POSTGRES_PASSWORD": ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY"),
}

FULL_ENV = {
    "ARANGO_PASSWORD": "test_arango",
    "MINIO_ACCESS_KEY": "test_minio_key",
    "MINIO_SECRET_KEY": "test_minio_secret",
    "POSTGRES_PASSWORD": "test_pg",
}


@pytest.mark.parametrize("missing_var", list(REQUIRED_SECRETS.keys()))
def test_missing_secret_does_not_break_import(missing_var):
    """Importing config never fails on a missing credential; it is None and require_secret raises."""
    env = {**FULL_ENV, missing_var: None}  # None -> removed from os.environ
    config = _reload_config(env)
    assert getattr(config, missing_var) is None
    with pytest.raises(EnvironmentError, match=missing_var):
        _with_env(env, lambda: config.require_secret(missing_var))


def _with_env(env, fn):
    backup = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return fn()
    finally:
        for k, v in backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_import_with_no_secrets_at_all():
    config = _reload_config({k: None for k in FULL_ENV})
    assert config.ARANGO_PASSWORD is None and config.POSTGRES_PASSWORD is None
    assert config.MQTT_VDA5050_PREFIX == "uagv/v2/RobotCompany"


def test_require_secret_returns_value():
    config = _reload_config(FULL_ENV)
    assert _with_env(FULL_ENV, lambda: config.require_secret("ARANGO_PASSWORD")) == "test_arango"


def test_postgres_database_password_falls_back_to_postgres_password():
    config = _reload_config(FULL_ENV)
    env = {"POSTGRES_DATABASE_PASSWORD": None, "POSTGRES_PASSWORD": "pg"}
    assert _with_env(env, config.postgres_database_password) == "pg"
    env = {"POSTGRES_DATABASE_PASSWORD": "dbpw", "POSTGRES_PASSWORD": None}
    assert _with_env(env, config.postgres_database_password) == "dbpw"
    env = {"POSTGRES_DATABASE_PASSWORD": None, "POSTGRES_PASSWORD": None}
    with pytest.raises(EnvironmentError, match="POSTGRES_PASSWORD"):
        _with_env(env, config.postgres_database_password)


# ---- Happy-path: all secrets present -----------------------------------------

def test_config_loads_with_all_secrets():
    """Config imports successfully when all required env vars are set."""
    config = _reload_config(FULL_ENV)
    assert config.ARANGO_PASSWORD == "test_arango"
    assert config.MINIO_ACCESS_KEY == "test_minio_key"
    assert config.MINIO_SECRET_KEY == "test_minio_secret"
    assert config.POSTGRES_PASSWORD == "test_pg"


# ---- MQTT_KEEPALIVE ----------------------------------------------------------

def test_mqtt_keepalive_default():
    """MQTT_KEEPALIVE defaults to 60 when env var is not set."""
    env = {**FULL_ENV, "MQTT_KEEPALIVE": None}
    config = _reload_config(env)
    assert config.MQTT_KEEPALIVE == 60
    assert isinstance(config.MQTT_KEEPALIVE, int)


def test_mqtt_keepalive_from_env():
    """MQTT_KEEPALIVE is read from environment variable."""
    env = {**FULL_ENV, "MQTT_KEEPALIVE": "120"}
    config = _reload_config(env)
    assert config.MQTT_KEEPALIVE == 120


# ---- Sanity checks on URL constants -----------------------------------------

def test_url_constants_are_strings():
    """Service URL constants must be non-empty strings."""
    config = _reload_config(FULL_ENV)
    for attr in ("URL_GRAPH_BUILDER", "URL_MISSION_PLANNER", "URL_LIVEKIT",
                 "URL_AGENT_ORCHESTRATOR", "URL_API_DELEGATION"):
        val = getattr(config, attr)
        assert isinstance(val, str), f"{attr} should be a string"
        assert val.startswith("http"), f"{attr} should start with http"


def test_url_defaults_and_env_overrides():
    config = _reload_config({**FULL_ENV, "LIVEKIT_URL": None, "GRAPH_BUILDER_URL": None})
    assert config.URL_LIVEKIT == "http://localhost:8006"
    assert config.URL_GRAPH_BUILDER == "http://localhost:8004"
    assert not hasattr(config, "URL_MISSION_DISPATCH")
    config = _reload_config({**FULL_ENV, "LIVEKIT_URL": "http://lk:1", "GRAPH_BUILDER_URL": "http://gb:2"})
    assert config.URL_LIVEKIT == "http://lk:1"
    assert config.URL_GRAPH_BUILDER == "http://gb:2"


def test_dispatch_and_api_tunable_defaults_and_overrides():
    keys = ["BATTERY_LOW_PCT", "AGENT_BATTERY_LOW_THRESHOLD", "DISPATCH_MAX_ORDER_MISMATCHES",
            "POSTGRES_POOL_MAX_SIZE", "DISPATCH_NOTIFY_RETRY_BACKOFF_S"]
    config = _reload_config({**FULL_ENV, **{k: None for k in keys}})
    assert config.BATTERY_LOW_PCT == 20.0 and config.BATTERY_OK_PCT == 25.0
    assert config.AGENT_BATTERY_LOW_THRESHOLD == 20.0
    assert config.DISPATCH_MAX_ORDER_MISMATCHES == 40
    assert (config.POSTGRES_POOL_MIN_SIZE, config.POSTGRES_POOL_MAX_SIZE) == (2, 10)
    assert config.DISPATCH_NOTIFY_RETRY_BACKOFF_S == (1.0, 2.0, 4.0)
    assert config.RECORDER_OP_RETRY_DELAYS_S == (1.0, 2.0, 5.0, 10.0, 30.0)
    assert config.OPEN_SESSION_CACHE_TTL_S == 1.0 and config.ORCHESTRATOR_PROXY_TIMEOUT_S == 60.0
    config = _reload_config({**FULL_ENV, "BATTERY_LOW_PCT": "15", "POSTGRES_POOL_MAX_SIZE": "4",
                             "DISPATCH_NOTIFY_RETRY_BACKOFF_S": "0.5,1"})
    assert config.BATTERY_LOW_PCT == 15.0 and config.AGENT_BATTERY_LOW_THRESHOLD == 15.0
    assert config.POSTGRES_POOL_MAX_SIZE == 4
    assert config.DISPATCH_NOTIFY_RETRY_BACKOFF_S == (0.5, 1.0)
    config = _reload_config({**FULL_ENV, "BATTERY_LOW_PCT": "15", "AGENT_BATTERY_LOW_THRESHOLD": "30"})
    assert config.AGENT_BATTERY_LOW_THRESHOLD == 30.0
