# Test Runner Dockerfile
# Provides isolated environment for running tests

FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install system dependencies (including libspatialindex for rtree)
RUN apt-get update && apt-get install -y \
    git \
    curl \
    libspatialindex-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (for Docker layer caching)
COPY tests/requirements-test.txt /app/tests/requirements-test.txt

# Install Python test dependencies
RUN pip install --no-cache-dir -r tests/requirements-test.txt

# Install production dependencies (needed for imports)
RUN pip install --no-cache-dir \
    fastapi==0.109.1 \
    paho-mqtt==1.6.1 \
    minio==7.2.0 \
    pydantic==1.9.0 \
    uvicorn==0.17.6 \
    python-arango==8.2.2 \
    psycopg[binary,pool]==3.2.4 \
    Pillow==10.0.0 \
    requests==2.31.0 \
    rtree==1.1.0 \
    websockets==12.0

# Copy the entire project
COPY . /app/

# Set Python path
ENV PYTHONPATH=/app

# Default command: run all tests
CMD ["pytest", "tests/", "-v", "--cov=packages", "--cov-report=html", "--cov-report=term-missing"]

