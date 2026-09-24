FROM python:3.10-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY packages/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create directory structure
RUN mkdir -p /app/packages/api \
    /app/packages/services/mission_planner \
    /app/packages/services/livekit \
    /app/packages/database \
    /app/packages/utils

# Copy service code
COPY packages/api/ /app/packages/api/

# Copy all topomap_dbs packages
COPY packages/topomap_dbs/ /app/packages/topomap_dbs/

# Copy mission_planner client
COPY packages/services/mission_planner/client.py /app/packages/services/mission_planner/
COPY packages/services/mission_planner/__init__.py /app/packages/services/mission_planner/

# Copy livekit client library (not the full service)
COPY packages/services/livekit/client.py /app/packages/services/livekit/
COPY packages/services/livekit/__init__.py /app/packages/services/livekit/

# Copy database client
COPY packages/database/postgres.py /app/packages/database/
COPY packages/database/__init__.py /app/packages/database/

# Copy utils
COPY packages/utils/ /app/packages/utils/

# Phase 0 libraries (fleet events + telemetry ingest, used by packages/api/telemetry.py)
COPY packages/events/ /app/packages/events/
COPY packages/telemetry_ingest/ /app/packages/telemetry_ingest/

# Copy config
COPY packages/config.py /app/packages/config.py

# Copy cloud_common
COPY cloud_common/ /app/cloud_common/

# Set Python path
ENV PYTHONPATH=/app

# Expose port
EXPOSE 8000

# The entrypoint runs `alembic upgrade head` under pg_advisory_lock('migrations'), then execs
# the command (compose `command:` or the CMD below). See packages/api/entrypoint.py.
ENTRYPOINT ["python", "-m", "packages.api.entrypoint"]
CMD ["python", "-m", "packages.api.main", "--host", "0.0.0.0", "--port", "8000"]

