# Server Diagnostics Service Dockerfile
# The docker CLI and compose plugin come along for operator deploys: the service starts a one-shot container from this same image
# that runs scripts/deploy_robot.sh (see operations.py). The service itself only talks to the engine's socket.
FROM docker:27-cli AS dockercli

FROM python:3.10-slim
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

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
