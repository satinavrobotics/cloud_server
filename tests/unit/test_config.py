#!/usr/bin/env python3
"""
Unit tests for packages/config.py.

Validates that:
- Required secret env vars trigger EnvironmentError when missing.
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
def test_missing_secret_raises_environment_error(missing_var):
    """If a required credential env var is absent, config import must raise EnvironmentError."""
    env = {**FULL_ENV, missing_var: None}  # None -> removed from os.environ

    with pytest.raises(EnvironmentError, match=missing_var.split("_")[0]):
        _reload_config(env)


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
    for attr in ("URL_MISSION_DISPATCH",
                 "URL_MISSION_PLANNER", "URL_LIVEKIT"):
        val = getattr(config, attr)
        assert isinstance(val, str), f"{attr} should be a string"
        assert val.startswith("http"), f"{attr} should start with http"
