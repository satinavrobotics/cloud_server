# LiveKit SFU Token Service Dockerfile
FROM python:3.10-slim

WORKDIR /app

# Pinned to the same FastAPI/pydantic v1 versions as the other services
COPY packages/services/livekit_sfu_tokens/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Service code plus shared utilities. packages/config.py is intentionally not
# copied -- see the module docstring in main.py.
COPY packages/services/livekit_sfu_tokens/ /app/packages/services/livekit_sfu_tokens/
COPY packages/utils/ /app/packages/utils/

RUN touch /app/packages/__init__.py /app/packages/services/__init__.py

ENV PYTHONPATH=/app

EXPOSE 8008

CMD ["python", "-m", "packages.services.livekit_sfu_tokens.main", "--host", "0.0.0.0", "--port", "8008"]
