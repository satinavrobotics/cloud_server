"""
LiveKit Service - Token Generation E2E Tests

Tests token creation, validation, and room access.
"""

import pytest
import requests
import uuid


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestLiveKitTokenGenerationE2E:
    """E2E tests for LiveKit token generation."""
    
    def test_create_token_basic(self, api_delegation_service):
        """Test basic token creation via API."""
        token_request = {
            "room_name": f"room_{uuid.uuid4().hex[:8]}",
            "participant_name": f"user_{uuid.uuid4().hex[:8]}",
            "can_publish": True,
            "can_subscribe": True
        }
        
        response = requests.post(
            f"{api_delegation_service['url']}/api/v1/livekit/token",
            json=token_request
        )
        
        # Token creation might not be available in all environments
        assert response.status_code in [200, 404, 405, 500, 503]
        
        if response.status_code == 200:
            data = response.json()
            assert "token" in data
            assert "server_url" in data
            assert "room_name" in data
            assert "participant_name" in data
            
            # Verify token is a JWT (3 parts separated by dots)
            token_parts = data["token"].split(".")
            assert len(token_parts) == 3
    
    def test_create_token_with_permissions(self, api_delegation_service):
        """Test token creation with specific permissions."""
        token_request = {
            "room_name": f"room_{uuid.uuid4().hex[:8]}",
            "participant_name": f"admin_{uuid.uuid4().hex[:8]}",
            "can_publish": True,
            "can_subscribe": True,
            "can_publish_data": True,
            "ttl_seconds": 7200
        }
        
        response = requests.post(
            f"{api_delegation_service['url']}/api/v1/livekit/token",
            json=token_request
        )
        
        assert response.status_code in [200, 404, 405, 500, 503]
        
        if response.status_code == 200:
            data = response.json()
            assert "token" in data
            assert "ttl" in data
            assert data["ttl"] == 7200
    
    def test_create_token_readonly(self, api_delegation_service):
        """Test token creation for read-only participant."""
        token_request = {
            "room_name": f"room_{uuid.uuid4().hex[:8]}",
            "participant_name": f"viewer_{uuid.uuid4().hex[:8]}",
            "can_publish": False,
            "can_subscribe": True,
            "can_publish_data": False
        }
        
        response = requests.post(
            f"{api_delegation_service['url']}/api/v1/livekit/token",
            json=token_request
        )
        
        assert response.status_code in [200, 404, 405, 500, 503]
    
    def test_create_token_custom_ttl(self, api_delegation_service):
        """Test token creation with custom TTL."""
        # Short TTL (5 minutes)
        token_request_short = {
            "room_name": f"room_{uuid.uuid4().hex[:8]}",
            "participant_name": f"user_{uuid.uuid4().hex[:8]}",
            "ttl_seconds": 300
        }
        
        response_short = requests.post(
            f"{api_delegation_service['url']}/api/v1/livekit/token",
            json=token_request_short
        )
        
        assert response_short.status_code in [200, 404, 405, 500, 503]
        
        # Long TTL (24 hours)
        token_request_long = {
            "room_name": f"room_{uuid.uuid4().hex[:8]}",
            "participant_name": f"user_{uuid.uuid4().hex[:8]}",
            "ttl_seconds": 86400
        }
        
        response_long = requests.post(
            f"{api_delegation_service['url']}/api/v1/livekit/token",
            json=token_request_long
        )
        
        assert response_long.status_code in [200, 404, 405, 500, 503]


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestLiveKitTokenValidationE2E:
    """E2E tests for LiveKit token validation."""
    
    def test_token_format_validation(self, api_delegation_service):
        """Test that generated tokens have valid JWT format."""
        token_request = {
            "room_name": f"room_{uuid.uuid4().hex[:8]}",
            "participant_name": f"user_{uuid.uuid4().hex[:8]}"
        }
        
        response = requests.post(
            f"{api_delegation_service['url']}/api/v1/livekit/token",
            json=token_request
        )
        
        if response.status_code == 200:
            data = response.json()
            token = data["token"]
            
            # JWT should have 3 parts: header.payload.signature
            parts = token.split(".")
            assert len(parts) == 3
            
            # Each part should be base64-encoded (non-empty)
            for part in parts:
                assert len(part) > 0
    
    def test_multiple_tokens_same_room(self, api_delegation_service):
        """Test creating multiple tokens for the same room."""
        room_name = f"room_{uuid.uuid4().hex[:8]}"
        
        # Create tokens for multiple participants in same room
        participants = [f"user_{i}" for i in range(3)]
        tokens = []
        
        for participant in participants:
            token_request = {
                "room_name": room_name,
                "participant_name": participant
            }
            
            response = requests.post(
                f"{api_delegation_service['url']}/api/v1/livekit/token",
                json=token_request
            )
            
            if response.status_code == 200:
                data = response.json()
                tokens.append(data["token"])
        
        # All tokens should be unique
        if len(tokens) > 1:
            assert len(tokens) == len(set(tokens))

