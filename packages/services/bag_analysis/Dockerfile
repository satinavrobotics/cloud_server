# Bag analysis worker (docs/bag_analysis_server_2026-10-28.md).
#
# BASE_IMAGE decides what the runner can do: the default (plain Python) can run the server code and its tests, but not
# the real analysis. The production worker is built FROM the analysis image (ROS humble + sati_benchmark + tools +
# run_analysis.py, one worker per analysis image tag), which must provide python3 (>= 3.10), pip and useradd:
#   docker build -f packages/services/bag_analysis/Dockerfile --build-arg BASE_IMAGE=sati/navstack-analysis:<tag> -t bag_analysis_service:<tag> .
# This image never gets the Docker socket and runs as an unprivileged user.
ARG BASE_IMAGE=python:3.10-slim
FROM ${BASE_IMAGE}

WORKDIR /app

# The analysis image ends as USER analyst: install into the system site as root (a user install would land in
# analyst's home, which the unprivileged runtime user cannot read).
USER root
COPY packages/services/bag_analysis/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /app/packages/services/bag_analysis /app/packages/utils
COPY packages/config.py /app/packages/config.py
COPY packages/services/bag_analysis/ /app/packages/services/bag_analysis/

# The scratch directory is a volume mounted at /scratch; created here so a fresh named volume takes this owner.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin baganalysis \
    && mkdir -p /scratch && chown 10001:10001 /scratch
USER 10001

ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1

CMD ["python", "-m", "packages.services.bag_analysis.main"]
