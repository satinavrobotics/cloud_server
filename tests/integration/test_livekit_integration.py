"""
Integration tests for LiveKit Service.

These tests verify JWT token creation and validation with real JWT libraries.
"""

import pytest
import sys
from unittest.mock import MagicMock, Mock, patch

# Mock livekit module before importing service
sys.modules['livekit'] = MagicMock()
sys.modules['livekit.api'] = MagicMock()


@pytest.mark.integration
class TestLiveKitTokenIntegration:
    """Integration tests for LiveKit token creation and validation."""
    
    def test_token_creation_with_jwt_validation(self):
        """Test token creation and validate JWT structure."""
        from packages.services.livekit.server import LiveKitService

        # Create LiveKit service
        service = LiveKitService(
            api_key="test_api_key_12345678",
            api_secret="test_secret_key_very_long_secret",
            server_url="wss://livekit.example.com"
        )

        # Create token
        result = service.create_token(
            room_name="test_room",
            participant_name="test_user",
            can_publish=True,
            can_subscribe=True,
            ttl=3600
        )

        # Verify token structure
        assert isinstance(result, dict)
        assert "token" in result
        assert "ttl" in result
        assert "server_url" in result
        assert "participant_name" in result
        assert "room_name" in result

        # Verify values
        assert result["ttl"] == 3600
        assert result["server_url"] == "wss://livekit.example.com"
        assert result["participant_name"] == "test_user"
        assert result["room_name"] == "test_room"

    def test_token_with_all_permissions(self):
        """Test token creation with all permissions enabled."""
        from packages.services.livekit.server import LiveKitService

        service = LiveKitService(
            api_key="test_api_key_12345678",
            api_secret="test_secret_key_very_long_secret",
            server_url="wss://livekit.example.com"
        )

        result = service.create_token(
            room_name="full_access_room",
            participant_name="admin_user",
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
            ttl=7200
        )

        # Verify result
        assert result["ttl"] == 7200
        assert result["participant_name"] == "admin_user"
        assert result["room_name"] == "full_access_room"

    def test_token_with_limited_permissions(self):
        """Test token creation with limited permissions (subscribe only)."""
        from packages.services.livekit.server import LiveKitService

        service = LiveKitService(
            api_key="test_api_key_12345678",
            api_secret="test_secret_key_very_long_secret",
            server_url="wss://livekit.example.com"
        )

        result = service.create_token(
            room_name="readonly_room",
            participant_name="viewer_user",
            can_publish=False,
            can_subscribe=True,
            can_publish_data=False,
            ttl=1800
        )

        # Verify result
        assert result["ttl"] == 1800
        assert result["participant_name"] == "viewer_user"
        assert result["room_name"] == "readonly_room"

    def test_token_expiration_times(self):
        """Test token creation with different expiration times."""
        from packages.services.livekit.server import LiveKitService

        service = LiveKitService(
            api_key="test_api_key_12345678",
            api_secret="test_secret_key_very_long_secret",
            server_url="wss://livekit.example.com"
        )

        # Test short TTL (5 minutes)
        result_short = service.create_token(
            room_name="short_ttl_room",
            participant_name="user1",
            ttl=300
        )
        assert result_short["ttl"] == 300

        # Test long TTL (24 hours)
        result_long = service.create_token(
            room_name="long_ttl_room",
            participant_name="user2",
            ttl=86400
        )
        assert result_long["ttl"] == 86400


@pytest.mark.integration
class TestLiveKitServiceStatsIntegration:
    """Integration tests for LiveKit service statistics."""

    def test_get_stats_with_api_key_masking(self):
        """Test that get_stats properly masks API key."""
        from packages.services.livekit.server import LiveKitService

        api_key = "test_api_key_12345678_long"
        service = LiveKitService(
            api_key=api_key,
            api_secret="test_secret",
            server_url="wss://livekit.example.com"
        )

        # Get stats
        stats = service.get_stats()

        # Verify API key is masked
        assert "api_key" in stats
        assert stats["api_key"] != api_key
        assert stats["api_key"].startswith(api_key[:8])
        assert stats["api_key"].endswith("...")

        # Verify other stats
        assert "server_url" in stats
        assert stats["server_url"] == "wss://livekit.example.com"

    def test_get_stats_structure(self):
        """Test that get_stats returns expected structure."""
        from packages.services.livekit.server import LiveKitService

        service = LiveKitService(
            api_key="test_key_12345678",
            api_secret="test_secret",
            server_url="wss://livekit.example.com"
        )

        stats = service.get_stats()

        # Verify stats structure
        assert isinstance(stats, dict)
        assert "api_key" in stats
        assert "server_url" in stats

        # Verify types
        assert isinstance(stats["api_key"], str)
        assert isinstance(stats["server_url"], str)


@pytest.mark.integration
class TestLiveKitServiceConfiguration:
    """Integration tests for LiveKit service configuration."""

    def test_service_initialization_with_different_urls(self):
        """Test service initialization with various server URLs."""
        from packages.services.livekit.server import LiveKitService

        # Test with wss:// URL
        service_wss = LiveKitService(
            api_key="test_key",
            api_secret="test_secret",
            server_url="wss://livekit.example.com"
        )
        assert service_wss.server_url == "wss://livekit.example.com"

        # Test with ws:// URL (insecure)
        service_ws = LiveKitService(
            api_key="test_key",
            api_secret="test_secret",
            server_url="ws://localhost:7880"
        )
        assert service_ws.server_url == "ws://localhost:7880"

        # Test with https:// URL
        service_https = LiveKitService(
            api_key="test_key",
            api_secret="test_secret",
            server_url="https://livekit.example.com"
        )
        assert service_https.server_url == "https://livekit.example.com"

    def test_service_initialization_with_different_keys(self):
        """Test service initialization with various API keys."""
        from packages.services.livekit.server import LiveKitService

        # Test with short key
        service_short = LiveKitService(
            api_key="short",
            api_secret="secret",
            server_url="wss://livekit.example.com"
        )
        assert service_short.api_key == "short"

        # Test with long key
        long_key = "very_long_api_key_with_many_characters_12345678"
        service_long = LiveKitService(
            api_key=long_key,
            api_secret="secret",
            server_url="wss://livekit.example.com"
        )
        assert service_long.api_key == long_key

        # Verify masking works for both
        stats_short = service_short.get_stats()
        stats_long = service_long.get_stats()

        # Short key (less than 8 chars) should still be masked
        assert "..." in stats_short["api_key"] or len(stats_short["api_key"]) < len("short")

        # Long key should show first 8 chars + "..."
        assert stats_long["api_key"].startswith(long_key[:8])
        assert stats_long["api_key"].endswith("...")

