FROM python:3.10-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY packages/services/agent_orchestrator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create directory structure
RUN mkdir -p /app/packages/services/agent_orchestrator \
    /app/packages/utils

# Copy service code
COPY packages/services/agent_orchestrator/ /app/packages/services/agent_orchestrator/

# Copy utils package (mqtt client, service helpers, fastapi helpers)
COPY packages/utils/ /app/packages/utils/

# Copy config file
COPY packages/config.py /app/packages/config.py

# Set Python path and disable bytecode caching
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1

# Expose port
EXPOSE 8007

# Run the service
CMD ["python", "-m", "packages.services.agent_orchestrator.main", "--host", "0.0.0.0", "--port", "8007"]
