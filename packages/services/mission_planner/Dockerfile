FROM python:3.10-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY packages/services/mission_planner/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create directory structure
RUN mkdir -p /app/packages/services/mission_planner \
    /app/packages/database \
    /app/packages/utils \
    /app/cloud_common/objects

# Create __init__.py files for packages
RUN touch /app/packages/__init__.py \
    /app/packages/database/__init__.py

# Copy service code
COPY packages/services/mission_planner/ /app/packages/services/mission_planner/

# Copy topomap_dbs
COPY packages/topomap_dbs/ /app/packages/topomap_dbs/

# Copy database client
COPY packages/database/postgres.py /app/packages/database/
COPY packages/database/__init__.py /app/packages/database/

# Copy utils package
COPY packages/utils/ /app/packages/utils/

# Copy config file
COPY packages/config.py /app/packages/config.py

# Copy cloud_common objects
COPY cloud_common/ /app/cloud_common/

# Set Python path
ENV PYTHONPATH=/app

# Expose port
EXPOSE 8005

# Run the service
CMD ["python", "-m", "packages.services.mission_planner.main", "--host", "0.0.0.0", "--port", "8005"]

