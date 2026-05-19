#!/usr/bin/env python3
"""
Model Database Service

Manages storage of standalone base ML models (ONNX and other formats) using MinIO.
Models are not tied to any map or robot — they live in a single shared bucket.

Storage layout (bucket: base-models):
    models/{model_id}       — model binary (uploaded by the client via presigned URL)
    meta/{model_id}.json    — metadata JSON (written by the API at upload-URL creation)
"""

import io
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

try:
    from minio import Minio
    from minio.error import S3Error
except ImportError:
    raise ImportError("minio required. Install: pip install minio")

from packages.topomap_dbs.minio_base import MinIOService

BUCKET = "base-models"
PRESIGN_EXPIRY_DEFAULT = 3600


class ModelDatabaseService(MinIOService):
    """
    Storage service for standalone base ML models.

    Uses a single fixed bucket (base-models). Binaries are uploaded by the
    client directly to MinIO via presigned PUT URLs. Metadata is stored as a
    JSON sidecar written at upload-URL creation time.
    """

    def __init__(
        self,
        minio_host: str = "localhost",
        minio_port: int = 9000,
        minio_access_key: str = "minioadmin",
        minio_secret_key: str = "minioadmin",
        minio_secure: bool = False,
        presign_expiry_seconds: int = PRESIGN_EXPIRY_DEFAULT,
    ):
        # bucket_prefix is unused; the service uses a single fixed bucket
        super().__init__(
            minio_host=minio_host,
            minio_port=minio_port,
            minio_access_key=minio_access_key,
            minio_secret_key=minio_secret_key,
            minio_secure=minio_secure,
            bucket_prefix="",
        )
        self.presign_expiry_seconds = presign_expiry_seconds
        self.logger.info("Model Database Service initialized")
        self.logger.info(f"   MinIO: {minio_host}:{minio_port}")

    def _connect(self, access_key: str, secret_key: str, secure: bool) -> None:
        """Create client and ensure the shared bucket exists. Does not raise on error."""
        try:
            self.client = Minio(
                f"{self.minio_host}:{self.minio_port}",
                access_key=access_key,
                secret_key=secret_key,
                secure=secure,
            )
            self._ensure_bucket(BUCKET)
        except Exception as e:
            self.logger.error(f"Failed to connect to MinIO: {e}")
            self.client = None

    def is_healthy(self) -> bool:
        try:
            return self.client is not None and self.client.bucket_exists(BUCKET)
        except Exception:
            return False

    # -------------------- Internal helpers --------------------

    def _model_object(self, model_id: str) -> str:
        return f"models/{model_id}"

    def _meta_object(self, model_id: str) -> str:
        return f"meta/{model_id}.json"

    def _write_meta(self, model_id: str, meta: Dict[str, Any]) -> bool:
        try:
            data = json.dumps(meta).encode()
            self.client.put_object(
                BUCKET,
                self._meta_object(model_id),
                io.BytesIO(data),
                length=len(data),
                content_type="application/json",
            )
            return True
        except Exception as e:
            self.logger.error(f"Failed to write metadata for model {model_id}: {e}")
            return False

    def _read_meta(self, model_id: str) -> Optional[Dict[str, Any]]:
        try:
            response = self.client.get_object(BUCKET, self._meta_object(model_id))
            return json.loads(response.read())
        except S3Error as e:
            if e.code == "NoSuchKey":
                return None
            raise
        except Exception as e:
            self.logger.error(f"Failed to read metadata for model {model_id}: {e}")
            return None

    # -------------------- Public API --------------------

    def create_model_entry(
        self,
        model_id: str,
        name: str,
        description: Optional[str] = None,
        format: Optional[str] = "onnx",
    ) -> Optional[Dict[str, Any]]:
        """
        Write a metadata sidecar and return a presigned PUT URL for the model binary.

        Returns dict with model_id, upload_url, expires_in — or None on error.
        """
        if not self._ensure_bucket(BUCKET):
            return None

        meta = {
            "model_id": model_id,
            "name": name,
            "description": description,
            "format": format,
            "created_at": datetime.utcnow().isoformat(),
        }
        if not self._write_meta(model_id, meta):
            return None

        try:
            url = self._rewrite_presigned_url(self.client.presigned_put_object(
                BUCKET,
                self._model_object(model_id),
                expires=timedelta(seconds=self.presign_expiry_seconds),
            ))
            return {
                "model_id": model_id,
                "upload_url": url,
                "expires_in": self.presign_expiry_seconds,
            }
        except Exception as e:
            self.logger.error(f"Failed to create upload URL for model {model_id}: {e}")
            return None

    def get_model_metadata(self, model_id: str) -> Optional[Dict[str, Any]]:
        """Return metadata for a model, or None if no sidecar exists."""
        meta = self._read_meta(model_id)
        if meta is None:
            return None

        try:
            stat = self.client.stat_object(BUCKET, self._model_object(model_id))
            meta["size"] = stat.size
            meta["content_type"] = stat.content_type
            meta["last_modified"] = stat.last_modified.isoformat() if stat.last_modified else None
            meta["uploaded"] = True
        except S3Error as e:
            if e.code == "NoSuchKey":
                meta["size"] = None
                meta["content_type"] = None
                meta["last_modified"] = None
                meta["uploaded"] = False
            else:
                raise

        return meta

    def get_download_url(self, model_id: str) -> Optional[str]:
        """Return a presigned GET URL. Returns None if binary not yet uploaded."""
        try:
            self.client.stat_object(BUCKET, self._model_object(model_id))
            return self._rewrite_presigned_url(self.client.presigned_get_object(
                BUCKET,
                self._model_object(model_id),
                expires=timedelta(seconds=self.presign_expiry_seconds),
            ))
        except S3Error as e:
            if e.code == "NoSuchKey":
                return None
            raise
        except Exception as e:
            self.logger.error(f"Failed to create download URL for model {model_id}: {e}")
            return None

    def list_models(self) -> List[Dict[str, Any]]:
        """List all models by enumerating metadata sidecars."""
        try:
            if not self.client.bucket_exists(BUCKET):
                return []
            objects = list(self.client.list_objects(BUCKET, prefix="meta/", recursive=True))
            results = []
            for obj in objects:
                name = obj.object_name
                if not name.endswith(".json"):
                    continue
                model_id = name[len("meta/"):-len(".json")]
                meta = self.get_model_metadata(model_id)
                if meta is not None:
                    results.append(meta)
            return results
        except Exception as e:
            self.logger.error(f"Failed to list models: {e}")
            return []

    def get_model_binary(self, model_id: str) -> Optional[bytes]:
        """Download the model binary. Returns None if binary does not exist."""
        try:
            response = self.client.get_object(BUCKET, self._model_object(model_id))
            return response.read()
        except S3Error as e:
            if e.code == "NoSuchKey":
                self.logger.warning(f"Binary for base model {model_id} not found")
                return None
            raise
        except Exception as e:
            self.logger.error(f"Failed to download binary for model {model_id}: {e}")
            return None

    def delete_model(self, model_id: str) -> bool:
        """Delete the model binary and its metadata sidecar. Returns False only if sidecar deletion fails."""
        meta_ok = True
        binary_ok = True

        try:
            self.client.remove_object(BUCKET, self._meta_object(model_id))
        except S3Error as e:
            if e.code != "NoSuchKey":
                self.logger.error(f"Failed to delete metadata for model {model_id}: {e}")
                meta_ok = False
        except Exception as e:
            self.logger.error(f"Failed to delete metadata for model {model_id}: {e}")
            meta_ok = False

        try:
            self.client.remove_object(BUCKET, self._model_object(model_id))
        except S3Error as e:
            if e.code != "NoSuchKey":
                self.logger.error(f"Failed to delete binary for model {model_id}: {e}")
                binary_ok = False
        except Exception as e:
            self.logger.error(f"Failed to delete binary for model {model_id}: {e}")
            binary_ok = False

        return meta_ok and binary_ok
