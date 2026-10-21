#!/usr/bin/env bash
# Render this machine's compose env file from docker_compose/profiles + secrets.env. See scripts/render_env.py.
exec python3 "$(dirname "$0")/render_env.py" "$@"
