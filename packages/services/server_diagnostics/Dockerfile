# Server Diagnostics Service Dockerfile
FROM python:3.10-slim

WORKDIR /app

COPY packages/services/server_diagnostics/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Service code plus shared utilities. packages/config.py is intentionally not copied -- see main.py.
COPY packages/services/server_diagnostics/ /app/packages/services/server_diagnostics/
COPY packages/utils/ /app/packages/utils/

RUN touch /app/packages/__init__.py /app/packages/services/__init__.py

ENV PYTHONPATH=/app

EXPOSE 8011

CMD ["python", "-m", "packages.services.server_diagnostics.main", "--host", "127.0.0.1", "--port", "8011"]
