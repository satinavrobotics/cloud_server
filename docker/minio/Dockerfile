# MinIO built from source at an exact upstream tag, multi-arch (amd64 + arm64).
#   docker buildx build --platform linux/amd64,linux/arm64 -f docker/minio/Dockerfile docker/minio
# The tag's go.mod requires go 1.24.0 (toolchain go1.24.2).
ARG MINIO_TAG=RELEASE.2025-09-07T16-13-09Z

FROM --platform=$BUILDPLATFORM golang:1.24.2-bookworm AS builder
ARG MINIO_TAG
ARG TARGETOS
ARG TARGETARCH
ENV GOTOOLCHAIN=local
WORKDIR /src
RUN git clone --depth 1 --branch "${MINIO_TAG}" https://github.com/minio/minio.git .
# Official ldflags (gen-ldflags.go), with the version taken from the tag
# instead of the commit time: RELEASE.2025-09-07T16-13-09Z.
RUN LDFLAGS="$(MINIO_RELEASE=RELEASE go run buildscripts/gen-ldflags.go "${MINIO_TAG#RELEASE.}")" \
 && CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} \
    go build -tags kqueue -trimpath -ldflags "${LDFLAGS}" -o /minio .

FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
COPY --from=builder /minio /usr/bin/minio
ENV MINIO_UPDATE=off
EXPOSE 9000
VOLUME ["/data"]
ENTRYPOINT ["/usr/bin/minio"]
CMD ["server", "/data"]
