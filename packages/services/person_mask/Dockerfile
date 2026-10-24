# Person-mask service (optional; docs/reconstruction/person_removal.md). Context: the repo root.
#
# CPU image (robot Pi 5, arm64; also amd64), the ONNX path only:
#   docker buildx build --platform linux/arm64,linux/amd64 -f packages/services/person_mask/Dockerfile -t <registry>/sati/person_mask:<tag> .
# GPU image (cloud/dev box): a PyTorch CUDA base (Blackwell / RTX 5090 needs CUDA 12.8 and torch >= 2.7) plus
# ultralytics and the Grounded-SAM-2 dependencies:
#   docker build -f packages/services/person_mask/Dockerfile \
#     --build-arg BASE=pytorch/pytorch:2.7.0-cuda12.8-cudnn9-runtime --build-arg REQUIREMENTS=requirements-gpu.txt \
#     -t person-mask:gpu .
# No weights are baked in: mount them read-only at /models (PERSON_MASK_MODELS names the files).
ARG BASE=python:3.10-slim
FROM ${BASE}
ARG REQUIREMENTS=requirements.txt

WORKDIR /app
COPY packages/services/person_mask/requirements*.txt /tmp/
RUN cd /tmp && pip install --no-cache-dir -r ${REQUIREMENTS}

COPY packages/services/person_mask/ /app/packages/services/person_mask/
RUN touch /app/packages/__init__.py /app/packages/services/__init__.py

ENV PYTHONPATH=/app \
    PERSON_MASK_MODELS="nano=onnx:/models/yolo11n-seg.onnx" \
    PERSON_MASK_IDLE_UNLOAD_S=120
EXPOSE 8013
CMD ["python", "-m", "packages.services.person_mask.main", "--host", "127.0.0.1", "--port", "8013"]
