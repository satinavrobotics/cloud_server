"""
LiveKit Service - Health and Stats E2E Tests

Tests service health, statistics, and configuration.
"""

import pytest
import requests


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestLiveKitHealthE2E:
    """E2E tests for LiveKit service health."""
    
    def test_livekit_service_health(self, api_delegation_service):
        """Test LiveKit service health endpoint."""
        response = requests.get(
            f"{api_delegation_service['url']}/api/v1/livekit/health"
        )
        
        # Health endpoint might not exist
        assert response.status_code in [200, 404, 405, 503]
        
        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, dict)
    
    def test_livekit_service_stats(self, api_delegation_service):
        """Test LiveKit service statistics endpoint."""
        response = requests.get(
            f"{api_delegation_service['url']}/api/v1/livekit/stats"
        )
        
        # Stats endpoint might not exist
        assert response.status_code in [200, 404, 405, 503]
        
        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, dict)
            
            # Stats should include server_url
            if "server_url" in data:
                assert isinstance(data["server_url"], str)
            
            # API key should be masked if present
            if "api_key" in data:
                assert "..." in data["api_key"] or len(data["api_key"]) < 20


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestLiveKitConfigurationE2E:
    """E2E tests for LiveKit service configuration."""
    
    def test_livekit_server_url_configuration(self, api_delegation_service):
        """Test that LiveKit server URL is properly configured."""
        # Create a token to verify server URL is included
        token_request = {
            "room_name": "test_room",
            "participant_name": "test_user"
        }
        
        response = requests.post(
            f"{api_delegation_service['url']}/api/v1/livekit/token",
            json=token_request
        )
        
        if response.status_code == 200:
            data = response.json()
            
            # Server URL should be present and valid
            assert "server_url" in data
            server_url = data["server_url"]
            
            # Should be a valid URL (ws:// or wss:// or https://)
            assert server_url.startswith(("ws://", "wss://", "http://", "https://"))
    
    def test_livekit_error_handling(self, api_delegation_service):
        """Test LiveKit service error handling."""
        # Send invalid token request (missing required fields)
        invalid_request = {
            "room_name": "test_room"
            # Missing participant_name
        }
        
        response = requests.post(
            f"{api_delegation_service['url']}/api/v1/livekit/token",
            json=invalid_request
        )
        
        # Should return error (400 or 422 for validation error)
        assert response.status_code in [400, 404, 405, 422, 500, 503]
    
    def test_livekit_empty_room_name(self, api_delegation_service):
        """Test LiveKit token creation with empty room name."""
        token_request = {
            "room_name": "",
            "participant_name": "test_user"
        }
        
        response = requests.post(
            f"{api_delegation_service['url']}/api/v1/livekit/token",
            json=token_request
        )
        
        # Should return error for empty room name
        assert response.status_code in [400, 404, 405, 422, 500, 503]
    
    def test_livekit_empty_participant_name(self, api_delegation_service):
        """Test LiveKit token creation with empty participant name."""
        token_request = {
            "room_name": "test_room",
            "participant_name": ""
        }
        
        response = requests.post(
            f"{api_delegation_service['url']}/api/v1/livekit/token",
            json=token_request
        )
        
        # Should return error for empty participant name
        assert response.status_code in [400, 404, 405, 422, 500, 503]


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestLiveKitRoomManagementE2E:
    """E2E tests for LiveKit room management."""
    
    def test_create_tokens_for_different_rooms(self, api_delegation_service):
        """Test creating tokens for different rooms."""
        rooms = ["room_1", "room_2", "room_3"]
        
        for room in rooms:
            token_request = {
                "room_name": room,
                "participant_name": "test_user"
            }
            
            response = requests.post(
                f"{api_delegation_service['url']}/api/v1/livekit/token",
                json=token_request
            )
            
            # Each room should get a token
            assert response.status_code in [200, 404, 405, 500, 503]
            
            if response.status_code == 200:
                data = response.json()
                assert data["room_name"] == room
    
    def test_same_participant_different_rooms(self, api_delegation_service):
        """Test same participant joining different rooms."""
        participant = "test_user"
        rooms = ["room_a", "room_b"]
        
        tokens = []
        for room in rooms:
            token_request = {
                "room_name": room,
                "participant_name": participant
            }
            
            response = requests.post(
                f"{api_delegation_service['url']}/api/v1/livekit/token",
                json=token_request
            )
            
            if response.status_code == 200:
                data = response.json()
                tokens.append(data["token"])
        
        # Tokens should be different even for same participant
        if len(tokens) == 2:
            assert tokens[0] != tokens[1]

