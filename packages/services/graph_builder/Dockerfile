FROM python:3.10-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY packages/services/graph_builder/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create directory structure
RUN mkdir -p /app/packages/services/graph_builder \
    /app/packages/database \
    /app/packages/utils

# Copy service code
COPY packages/services/graph_builder/ /app/packages/services/graph_builder/

# Copy utils package
COPY packages/utils/ /app/packages/utils/

# Copy database package
COPY packages/database/ /app/packages/database/

# Copy cloud_common package
COPY cloud_common/ /app/cloud_common/

# Copy config file
COPY packages/config.py /app/packages/config.py

# Copy all topomap_dbs packages
COPY packages/topomap_dbs/ /app/packages/topomap_dbs/

# Set Python path and disable bytecode caching
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1

# Expose port
EXPOSE 8004

# Run the service
CMD ["python", "-m", "packages.services.graph_builder.main", "--host", "0.0.0.0", "--port", "8004"]

