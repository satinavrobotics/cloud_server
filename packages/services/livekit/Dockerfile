# LiveKit Service Dockerfile
FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    pydantic \
    httpx \
    requests \
    livekit-api

# Copy service code
COPY packages/services/livekit/ /app/packages/services/livekit/

# Copy shared utilities
COPY packages/utils/ /app/packages/utils/
COPY packages/config.py /app/packages/

# Create __init__.py files for package structure
RUN touch /app/packages/__init__.py
RUN touch /app/packages/services/__init__.py

# Set Python path
ENV PYTHONPATH=/app

# Expose port
EXPOSE 8006

# Run the service
CMD ["python", "-m", "packages.services.livekit.main", "--host", "0.0.0.0", "--port", "8006"]

