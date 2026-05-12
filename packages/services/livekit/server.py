#!/usr/bin/env python3
"""
LiveKit Service - Core Logic

This service handles LiveKit token generation for video conferencing.
It creates JWT tokens that allow clients to connect to LiveKit rooms.
"""

import logging
from typing import Optional, Dict, Any
from datetime import timedelta

try:
    from livekit import api
except ImportError:
    raise ImportError(
        "livekit-api package is required. Install with: pip install livekit-api"
    )


class LiveKitService:
    """
    LiveKit Service for generating access tokens.
    
    This service creates JWT tokens that allow clients to join LiveKit rooms
    for real-time video/audio communication.
    """
    
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        server_url: str,
        default_ttl: int = 36000  # 10 hours in seconds
    ):
        """
        Initialize LiveKit Service.
        
        Args:
            api_key: LiveKit API key
            api_secret: LiveKit API secret
            server_url: LiveKit server WebSocket URL (e.g., wss://your-server.livekit.cloud)
            default_ttl: Default token time-to-live in seconds (default: 36000 = 10 hours)
        """
        self.logger = logging.getLogger("LiveKitService")
        
        self.api_key = api_key
        self.api_secret = api_secret
        self.server_url = server_url
        self.default_ttl = default_ttl
        
        self.logger.info("✅ LiveKit Service initialized")
        self.logger.info(f"   Server URL: {server_url}")
        self.logger.info(f"   Default TTL: {default_ttl} seconds")
    
    def create_token(
        self,
        participant_name: str,
        room_name: str = "quickstart-room",
        ttl: Optional[int] = None,
        metadata: Optional[str] = None,
        can_publish: bool = True,
        can_subscribe: bool = True,
        can_publish_data: bool = True
    ) -> Dict[str, Any]:
        """
        Create a LiveKit access token for a participant.
        
        Args:
            participant_name: Unique identifier for the participant
            room_name: Name of the room to join (default: "quickstart-room")
            ttl: Token time-to-live in seconds (uses default_ttl if not provided)
            metadata: Optional metadata to attach to the participant
            can_publish: Whether participant can publish tracks (default: True)
            can_subscribe: Whether participant can subscribe to tracks (default: True)
            can_publish_data: Whether participant can publish data messages (default: True)
            
        Returns:
            Dictionary containing:
                - token: JWT token string
                - ttl: Token time-to-live in seconds
                - server_url: LiveKit server URL
                - participant_name: Participant identifier
                - room_name: Room name
                
        Raises:
            Exception: If token generation fails
        """
        try:
            # Use default TTL if not provided
            if ttl is None:
                ttl = self.default_ttl
            
            self.logger.info(f"Creating token for participant '{participant_name}' in room '{room_name}'")
            
            # Create access token
            token = api.AccessToken(self.api_key, self.api_secret)
            token.with_identity(participant_name)
            token.with_name(participant_name)
            
            # Set token lifetime
            token.with_ttl(timedelta(seconds=ttl))
            
            # Add metadata if provided
            if metadata:
                token.with_metadata(metadata)
            
            # Add video grants (permissions)
            video_grants = api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=can_publish,
                can_subscribe=can_subscribe,
                can_publish_data=can_publish_data
            )
            token.with_grants(video_grants)
            
            # Generate JWT
            jwt_token = token.to_jwt()
            
            self.logger.info(f"✅ Token created successfully for '{participant_name}'")
            
            return {
                "token": jwt_token,
                "ttl": ttl,
                "server_url": self.server_url,
                "participant_name": participant_name,
                "room_name": room_name
            }
            
        except Exception as e:
            self.logger.error(f"Failed to create token: {e}")
            raise Exception(f"Token generation failed: {str(e)}")
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get service statistics and configuration.
        
        Returns:
            Dictionary with service information
        """
        return {
            "service": "livekit",
            "server_url": self.server_url,
            "default_ttl": self.default_ttl,
            "api_key": self.api_key[:8] + "..." if len(self.api_key) > 8 else "***"  # Masked for security
        }

