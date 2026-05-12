"""
Unit tests for ModelDatabaseService (base models).

Tests mock the MinIO client to validate service logic without a real MinIO instance.
"""

import json
import pytest
from io import BytesIO
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock
from minio.error import S3Error

from packages.topomap_dbs.model_db.server import ModelDatabaseService, BUCKET


# ==================== Mock data ====================

BASE_MODEL_ID = "bm-11111111-1111-1111-1111-111111111111"
BASE_MODEL_META = {
    "model_id": BASE_MODEL_ID,
    "name": "obstacle-detector-v1",
    "description": "YOLO-based obstacle detector trained on warehouse data",
    "format": "onnx",
    "created_at": "2024-03-01T10:00:00",
}
BASE_MODEL_BINARY = b"\x00\x01\x02\x03ONNX_MODEL_BYTES"


def _make_service(mock_minio, **kwargs):
    """Return (service, mock_client) with MinIO mocked out."""
    mock_client = Mock()
    mock_client.bucket_exists.return_value = True
    mock_minio.return_value = mock_client
    return ModelDatabaseService(**kwargs), mock_client


def _mock_s3_error(code: str) -> S3Error:
    return S3Error(code=code, message="", resource="", request_id="", host_id="", response=Mock())


# ==================== Initialization ====================

@pytest.mark.unit
class TestModelDatabaseServiceInit:

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_default_values(self, mock_minio):
        service, _ = _make_service(mock_minio)
        assert service.minio_host == "localhost"
        assert service.minio_port == 9000
        assert service.presign_expiry_seconds == 3600

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_custom_connection_params(self, mock_minio):
        service, _ = _make_service(
            mock_minio,
            minio_host="10.0.0.5",
            minio_port=9001,
            presign_expiry_seconds=7200,
        )
        assert service.minio_host == "10.0.0.5"
        assert service.minio_port == 9001
        assert service.presign_expiry_seconds == 7200

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_ensures_base_models_bucket_on_init(self, mock_minio):
        mock_client = Mock()
        mock_client.bucket_exists.return_value = False
        mock_minio.return_value = mock_client
        ModelDatabaseService()
        mock_client.make_bucket.assert_called_once_with(BUCKET)

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_skips_bucket_creation_when_already_exists(self, mock_minio):
        mock_client = Mock()
        mock_client.bucket_exists.return_value = True
        mock_minio.return_value = mock_client
        ModelDatabaseService()
        mock_client.make_bucket.assert_not_called()


# ==================== Health Check ====================

@pytest.mark.unit
class TestModelDatabaseIsHealthy:

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_healthy_when_bucket_exists(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.bucket_exists.return_value = True
        assert service.is_healthy() is True

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_unhealthy_when_bucket_missing(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.bucket_exists.return_value = False
        assert service.is_healthy() is False

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_unhealthy_when_exception(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.bucket_exists.side_effect = Exception("Timeout")
        assert service.is_healthy() is False


# ==================== Object Path Helpers ====================

@pytest.mark.unit
class TestModelObjectPaths:

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_model_object_path(self, mock_minio):
        service, _ = _make_service(mock_minio)
        assert service._model_object(BASE_MODEL_ID) == f"models/{BASE_MODEL_ID}"

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_meta_object_path(self, mock_minio):
        service, _ = _make_service(mock_minio)
        assert service._meta_object(BASE_MODEL_ID) == f"meta/{BASE_MODEL_ID}.json"


# ==================== Create Model Entry ====================

@pytest.mark.unit
class TestCreateModelEntry:

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_returns_upload_url_and_model_id(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.presigned_put_object.return_value = "http://minio:9000/base-models/models/bm-1?sig=abc"

        result = service.create_model_entry(
            model_id=BASE_MODEL_ID,
            name="obstacle-detector-v1",
            description="YOLO detector",
            format="onnx",
        )

        assert result is not None
        assert result["model_id"] == BASE_MODEL_ID
        assert result["upload_url"].startswith("http")
        assert result["expires_in"] == 3600

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_writes_sidecar_metadata(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.presigned_put_object.return_value = "http://presigned"

        service.create_model_entry(
            model_id=BASE_MODEL_ID,
            name="detector",
            description="desc",
            format="pt",
        )

        # Verify sidecar was written to meta/{model_id}.json
        call_args = mock_client.put_object.call_args
        assert call_args[0][0] == BUCKET
        assert call_args[0][1] == f"meta/{BASE_MODEL_ID}.json"
        written = json.loads(call_args[0][2].read())
        assert written["model_id"] == BASE_MODEL_ID
        assert written["name"] == "detector"
        assert written["format"] == "pt"

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_stores_null_description_when_omitted(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.presigned_put_object.return_value = "http://presigned"

        service.create_model_entry(model_id=BASE_MODEL_ID, name="model")

        call_args = mock_client.put_object.call_args
        written = json.loads(call_args[0][2].read())
        assert written["description"] is None

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_returns_none_when_presign_fails(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.presigned_put_object.side_effect = Exception("MinIO down")

        result = service.create_model_entry(model_id=BASE_MODEL_ID, name="model")
        assert result is None


# ==================== Get Model Metadata ====================

@pytest.mark.unit
class TestGetModelMetadata:

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_returns_metadata_with_upload_stats(self, mock_minio):
        service, mock_client = _make_service(mock_minio)

        mock_client.get_object.return_value = BytesIO(json.dumps(BASE_MODEL_META).encode())
        mock_stat = Mock()
        mock_stat.size = 1024 * 512
        mock_stat.content_type = "application/octet-stream"
        mock_stat.last_modified = datetime(2024, 3, 1, 12, 0, 0)
        mock_client.stat_object.return_value = mock_stat

        result = service.get_model_metadata(BASE_MODEL_ID)

        assert result is not None
        assert result["model_id"] == BASE_MODEL_ID
        assert result["name"] == "obstacle-detector-v1"
        assert result["format"] == "onnx"
        assert result["size"] == 1024 * 512
        assert result["uploaded"] is True

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_uploaded_false_when_binary_not_yet_present(self, mock_minio):
        service, mock_client = _make_service(mock_minio)

        mock_client.get_object.return_value = BytesIO(json.dumps(BASE_MODEL_META).encode())
        mock_client.stat_object.side_effect = _mock_s3_error("NoSuchKey")

        result = service.get_model_metadata(BASE_MODEL_ID)

        assert result["uploaded"] is False
        assert result["size"] is None

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_returns_none_for_unknown_model(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.get_object.side_effect = _mock_s3_error("NoSuchKey")

        result = service.get_model_metadata("nonexistent-id")
        assert result is None


# ==================== Get Download URL ====================

@pytest.mark.unit
class TestGetModelDownloadUrl:

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_returns_presigned_get_url(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.stat_object.return_value = Mock()
        mock_client.presigned_get_object.return_value = "http://minio/presigned-get"

        url = service.get_download_url(BASE_MODEL_ID)
        assert url == "http://minio/presigned-get"

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_returns_none_if_binary_not_uploaded(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.stat_object.side_effect = _mock_s3_error("NoSuchKey")

        url = service.get_download_url(BASE_MODEL_ID)
        assert url is None

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_calls_presigned_get_with_correct_object(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.stat_object.return_value = Mock()
        mock_client.presigned_get_object.return_value = "http://presigned"

        service.get_download_url(BASE_MODEL_ID)

        args, _ = mock_client.presigned_get_object.call_args
        assert args[0] == BUCKET
        assert args[1] == f"models/{BASE_MODEL_ID}"


# ==================== List Models ====================

@pytest.mark.unit
class TestListModels:

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_returns_all_models(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.bucket_exists.return_value = True

        obj1 = Mock(); obj1.object_name = f"meta/{BASE_MODEL_ID}.json"
        obj2 = Mock(); obj2.object_name = "meta/bm-22222222.json"
        mock_client.list_objects.return_value = [obj1, obj2]

        meta1 = {**BASE_MODEL_META, "uploaded": True, "size": 1024}
        meta2 = {**BASE_MODEL_META, "model_id": "bm-22222222", "uploaded": False, "size": None}

        mock_client.get_object.side_effect = [
            BytesIO(json.dumps(BASE_MODEL_META).encode()),
            BytesIO(json.dumps({**BASE_MODEL_META, "model_id": "bm-22222222"}).encode()),
        ]
        mock_client.stat_object.side_effect = [
            Mock(size=1024, content_type="application/octet-stream", last_modified=datetime(2024, 3, 1)),
            _mock_s3_error("NoSuchKey"),
        ]

        results = service.list_models()
        assert len(results) == 2
        assert results[0]["model_id"] == BASE_MODEL_ID
        assert results[1]["model_id"] == "bm-22222222"

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_returns_empty_list_when_bucket_missing(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.bucket_exists.return_value = False

        assert service.list_models() == []

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_skips_non_json_objects(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.bucket_exists.return_value = True

        bad_obj = Mock(); bad_obj.object_name = "meta/not-a-json-file"
        good_obj = Mock(); good_obj.object_name = f"meta/{BASE_MODEL_ID}.json"
        mock_client.list_objects.return_value = [bad_obj, good_obj]

        mock_client.get_object.return_value = BytesIO(json.dumps(BASE_MODEL_META).encode())
        mock_client.stat_object.return_value = Mock(size=512, content_type="", last_modified=None)

        results = service.list_models()
        assert len(results) == 1
        assert mock_client.get_object.call_count == 1


# ==================== Get Model Binary ====================

@pytest.mark.unit
class TestGetModelBinary:

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_returns_binary_bytes(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.get_object.return_value = BytesIO(BASE_MODEL_BINARY)

        result = service.get_model_binary(BASE_MODEL_ID)
        assert result == BASE_MODEL_BINARY

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_returns_none_when_binary_not_uploaded(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.get_object.side_effect = _mock_s3_error("NoSuchKey")

        result = service.get_model_binary(BASE_MODEL_ID)
        assert result is None


# ==================== Delete Model ====================

@pytest.mark.unit
class TestDeleteModel:

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_deletes_both_binary_and_sidecar(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.remove_object.return_value = None

        result = service.delete_model(BASE_MODEL_ID)

        assert result is True
        assert mock_client.remove_object.call_count == 2
        removed = {call[0][1] for call in mock_client.remove_object.call_args_list}
        assert f"meta/{BASE_MODEL_ID}.json" in removed
        assert f"models/{BASE_MODEL_ID}" in removed

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_returns_true_when_binary_never_uploaded(self, mock_minio):
        service, mock_client = _make_service(mock_minio)

        def remove_side_effect(bucket, obj):
            if obj.startswith("models/"):
                raise _mock_s3_error("NoSuchKey")

        mock_client.remove_object.side_effect = remove_side_effect

        result = service.delete_model(BASE_MODEL_ID)
        assert result is True

    @patch("packages.topomap_dbs.model_db.server.Minio")
    def test_returns_false_when_sidecar_deletion_fails(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.remove_object.side_effect = Exception("Permission denied")

        result = service.delete_model(BASE_MODEL_ID)
        assert result is False
