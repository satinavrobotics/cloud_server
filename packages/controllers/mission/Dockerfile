FROM python:3.10-slim
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y curl procps && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY packages/controllers/mission/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy cloud_common
COPY cloud_common/ /app/cloud_common/

# Copy database client (required by mission dispatch)
COPY packages/database/postgres.py /app/packages/database/
COPY packages/database/__init__.py /app/packages/database/

# Copy controllers package
COPY packages/controllers/ /app/packages/controllers/

# Copy utils package
COPY packages/utils/ /app/packages/utils/

# Create __init__.py files for packages
RUN touch /app/packages/__init__.py

# Set Python path
ENV PYTHONPATH=/app

# Run the mission dispatch service
# Args are provided via docker-compose command:
#   --mqtt_host <host> --mqtt_port <port> --mqtt_transport tcp
#   --database_url http://<mission-db-host>:<controller_port>
ENTRYPOINT ["python", "packages/controllers/mission/main.py"]
