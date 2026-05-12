"""
Unit tests for LiveKit Service.

Tests token generation with various permission combinations.
"""

import pytest
import sys
from unittest.mock import Mock, patch, MagicMock

# Mock the livekit module before importing LiveKitService
sys.modules['livekit'] = MagicMock()
sys.modules['livekit.api'] = MagicMock()

from packages.services.livekit.server import LiveKitService


@pytest.mark.unit
class TestLiveKitServiceInit:
    """Test LiveKitService initialization."""

    def test_init_with_defaults(self):
        """Test initialization with default parameters."""
        service = LiveKitService(
            api_key="test_api_key",
            api_secret="test_api_secret",
            server_url="wss://livekit.example.com"
        )

        assert service.api_key == "test_api_key"
        assert service.api_secret == "test_api_secret"
        assert service.server_url == "wss://livekit.example.com"
        assert service.default_ttl == 36000

    def test_init_with_custom_ttl(self):
        """Test initialization with custom TTL."""
        service = LiveKitService(
            api_key="test_api_key",
            api_secret="test_api_secret",
            server_url="wss://livekit.example.com",
            default_ttl=7200
        )

        assert service.default_ttl == 7200


@pytest.mark.unit
class TestLiveKitServiceCreateToken:
    """Test LiveKitService token creation."""

    @patch('packages.services.livekit.server.api.AccessToken')
    def test_create_token_with_defaults(self, mock_access_token):
        """Test token creation with default permissions."""
        mock_token_instance = Mock()
        mock_token_instance.to_jwt.return_value = "mock_jwt_token"
        mock_access_token.return_value = mock_token_instance

        service = LiveKitService(
            api_key="test_api_key",
            api_secret="test_api_secret",
            server_url="wss://livekit.example.com"
        )

        result = service.create_token(
            participant_name="user_1",
            room_name="room_1"
        )

        assert isinstance(result, dict)
        assert result["token"] == "mock_jwt_token"
        assert result["participant_name"] == "user_1"
        assert result["room_name"] == "room_1"
        assert result["server_url"] == "wss://livekit.example.com"
        mock_access_token.assert_called_once_with("test_api_key", "test_api_secret")
        mock_token_instance.to_jwt.assert_called_once()

    @patch('packages.services.livekit.server.api.AccessToken')
    def test_create_token_with_custom_ttl(self, mock_access_token):
        """Test token creation with custom TTL."""
        mock_token_instance = Mock()
        mock_token_instance.to_jwt.return_value = "mock_jwt_token"
        mock_access_token.return_value = mock_token_instance

        service = LiveKitService(
            api_key="test_api_key",
            api_secret="test_api_secret",
            server_url="wss://livekit.example.com"
        )

        result = service.create_token(
            participant_name="user_1",
            room_name="room_1",
            ttl=7200
        )

        assert result["token"] == "mock_jwt_token"
        assert result["ttl"] == 7200

    @patch('packages.services.livekit.server.api.AccessToken')
    def test_create_token_with_metadata(self, mock_access_token):
        """Test token creation with metadata."""
        mock_token_instance = Mock()
        mock_token_instance.to_jwt.return_value = "mock_jwt_token"
        mock_access_token.return_value = mock_token_instance

        service = LiveKitService(
            api_key="test_api_key",
            api_secret="test_api_secret",
            server_url="wss://livekit.example.com"
        )

        metadata = {"user_id": "123", "role": "moderator"}
        result = service.create_token(
            participant_name="user_1",
            room_name="room_1",
            metadata=metadata
        )

        assert result["token"] == "mock_jwt_token"
        mock_token_instance.with_metadata.assert_called_once()

    @patch('packages.services.livekit.server.api.AccessToken')
    def test_create_token_publish_only(self, mock_access_token):
        """Test token creation with publish-only permissions."""
        mock_token_instance = Mock()
        mock_token_instance.to_jwt.return_value = "mock_jwt_token"
        mock_access_token.return_value = mock_token_instance

        service = LiveKitService(
            api_key="test_api_key",
            api_secret="test_api_secret",
            server_url="wss://livekit.example.com"
        )

        result = service.create_token(
            participant_name="user_1",
            room_name="room_1",
            can_publish=True,
            can_subscribe=False
        )

        assert result["token"] == "mock_jwt_token"

    @patch('packages.services.livekit.server.api.AccessToken')
    def test_create_token_subscribe_only(self, mock_access_token):
        """Test token creation with subscribe-only permissions."""
        mock_token_instance = Mock()
        mock_token_instance.to_jwt.return_value = "mock_jwt_token"
        mock_access_token.return_value = mock_token_instance

        service = LiveKitService(
            api_key="test_api_key",
            api_secret="test_api_secret",
            server_url="wss://livekit.example.com"
        )

        result = service.create_token(
            participant_name="user_1",
            room_name="room_1",
            can_publish=False,
            can_subscribe=True
        )

        assert result["token"] == "mock_jwt_token"

    @patch('packages.services.livekit.server.api.AccessToken')
    def test_create_token_no_permissions(self, mock_access_token):
        """Test token creation with no publish/subscribe permissions."""
        mock_token_instance = Mock()
        mock_token_instance.to_jwt.return_value = "mock_jwt_token"
        mock_access_token.return_value = mock_token_instance

        service = LiveKitService(
            api_key="test_api_key",
            api_secret="test_api_secret",
            server_url="wss://livekit.example.com"
        )

        result = service.create_token(
            participant_name="user_1",
            room_name="room_1",
            can_publish=False,
            can_subscribe=False
        )

        assert result["token"] == "mock_jwt_token"

    @patch('packages.services.livekit.server.api.AccessToken')
    def test_create_token_with_data_publish(self, mock_access_token):
        """Test token creation with data publish permission."""
        mock_token_instance = Mock()
        mock_token_instance.to_jwt.return_value = "mock_jwt_token"
        mock_access_token.return_value = mock_token_instance

        service = LiveKitService(
            api_key="test_api_key",
            api_secret="test_api_secret",
            server_url="wss://livekit.example.com"
        )

        result = service.create_token(
            participant_name="user_1",
            room_name="room_1",
            can_publish_data=True
        )

        assert result["token"] == "mock_jwt_token"

    @patch('packages.services.livekit.server.api.AccessToken')
    def test_create_token_all_permissions(self, mock_access_token):
        """Test token creation with all permissions enabled."""
        mock_token_instance = Mock()
        mock_token_instance.to_jwt.return_value = "mock_jwt_token"
        mock_access_token.return_value = mock_token_instance

        service = LiveKitService(
            api_key="test_api_key",
            api_secret="test_api_secret",
            server_url="wss://livekit.example.com"
        )

        result = service.create_token(
            participant_name="user_1",
            room_name="room_1",
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True
        )

        assert result["token"] == "mock_jwt_token"

    @patch('packages.services.livekit.server.api.AccessToken')
    def test_create_token_multiple_rooms(self, mock_access_token):
        """Test creating tokens for different rooms."""
        mock_token_instance = Mock()
        mock_token_instance.to_jwt.return_value = "mock_jwt_token"
        mock_access_token.return_value = mock_token_instance

        service = LiveKitService(
            api_key="test_api_key",
            api_secret="test_api_secret",
            server_url="wss://livekit.example.com"
        )

        result1 = service.create_token("user_1", "room_1")
        result2 = service.create_token("user_2", "room_2")

        assert result1["token"] == "mock_jwt_token"
        assert result1["room_name"] == "room_1"
        assert result2["token"] == "mock_jwt_token"
        assert result2["room_name"] == "room_2"
        assert mock_access_token.call_count == 2


@pytest.mark.unit
class TestLiveKitServiceGetStats:
    """Test LiveKitService get_stats method."""

    def test_get_stats_masks_api_key(self):
        """Test that get_stats masks the API key for security."""
        service = LiveKitService(
            api_key="test_api_key_12345",
            api_secret="test_api_secret",
            server_url="wss://livekit.example.com"
        )

        stats = service.get_stats()

        assert "api_key" in stats
        assert stats["api_key"] != "test_api_key_12345"
        # API key should be masked with first 8 chars + "..."
        assert stats["api_key"] == "test_api..."

    def test_get_stats_includes_server_url(self):
        """Test that get_stats includes server URL."""
        service = LiveKitService(
            api_key="test_api_key",
            api_secret="test_api_secret",
            server_url="wss://livekit.example.com"
        )

        stats = service.get_stats()

        assert "server_url" in stats
        assert stats["server_url"] == "wss://livekit.example.com"

    def test_get_stats_includes_default_ttl(self):
        """Test that get_stats includes default TTL."""
        service = LiveKitService(
            api_key="test_api_key",
            api_secret="test_api_secret",
            server_url="wss://livekit.example.com",
            default_ttl=7200
        )

        stats = service.get_stats()

        assert "default_ttl" in stats
        assert stats["default_ttl"] == 7200

    def test_get_stats_structure(self):
        """Test that get_stats returns expected structure."""
        service = LiveKitService(
            api_key="test_api_key",
            api_secret="test_api_secret",
            server_url="wss://livekit.example.com"
        )

        stats = service.get_stats()

        assert isinstance(stats, dict)
        assert "service" in stats
        assert stats["service"] == "livekit"

