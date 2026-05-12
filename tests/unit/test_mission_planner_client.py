#!/usr/bin/env python3
"""
Unit tests for Mission Planner Client.

Tests the MissionPlannerClient class that provides async methods to interact
with the Mission Planner Service via httpx.AsyncClient.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

from packages.services.mission_planner.client import MissionPlannerClient


@pytest.mark.unit
class TestMissionPlannerClientInit:
    """Test MissionPlannerClient initialization."""

    def test_client_init_default_values(self):
        """Test client initialization with default values."""
        client = MissionPlannerClient()

        assert client.url == "http://localhost:8005"
        assert client.timeout == 30
        assert client.logger is not None

    def test_client_init_custom_values(self):
        """Test client initialization with custom values."""
        client = MissionPlannerClient(
            url="http://custom-host:9000",
            timeout=60
        )

        assert client.url == "http://custom-host:9000"
        assert client.timeout == 60

    def test_client_init_strips_trailing_slash(self):
        """Test that trailing slash is removed from URL."""
        client = MissionPlannerClient(url="http://localhost:8005/")

        assert client.url == "http://localhost:8005"


def _mock_response(status_code=200, json_data=None, raise_for_status=None):
    """Build a MagicMock that looks like an httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    if raise_for_status is not None:
        resp.raise_for_status.side_effect = raise_for_status
    else:
        resp.raise_for_status.return_value = None
    return resp


@pytest.mark.unit
class TestMissionPlannerClientNavigate:
    """Test MissionPlannerClient.navigate() method."""

    async def test_navigate_success(self):
        """Test successful navigation request."""
        client = MissionPlannerClient()
        mock_resp = _mock_response(json_data={
            "success": True,
            "mission_name": "mission_001",
            "path": [{"x": 0, "y": 0}, {"x": 10, "y": 20}]
        })

        with patch.object(client._client, 'post', new=AsyncMock(return_value=mock_resp)):
            result = await client.navigate(
                robot_name="robot_1",
                target_x=10.0,
                target_y=20.0
            )

        assert result["success"] is True
        assert result["mission_name"] == "mission_001"
        assert "path" in result

    async def test_navigate_payload_fields(self):
        """Test that navigate sends correct payload fields."""
        client = MissionPlannerClient()
        mock_resp = _mock_response(json_data={"success": True, "mission_name": "m"})
        mock_post = AsyncMock(return_value=mock_resp)

        with patch.object(client._client, 'post', new=mock_post):
            await client.navigate(
                robot_name="robot_1",
                target_x=10.0,
                target_y=20.0
            )

        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["json"]["robot_name"] == "robot_1"
        assert call_kwargs["json"]["target_x"] == 10.0
        assert call_kwargs["json"]["target_y"] == 20.0
        assert call_kwargs["json"]["timeout_seconds"] == 300

    async def test_navigate_with_mission_name(self):
        """Test navigation request with custom mission name."""
        client = MissionPlannerClient()
        mock_resp = _mock_response(json_data={"success": True, "mission_name": "custom_mission"})
        mock_post = AsyncMock(return_value=mock_resp)

        with patch.object(client._client, 'post', new=mock_post):
            result = await client.navigate(
                robot_name="robot_1",
                target_x=10.0,
                target_y=20.0,
                mission_name="custom_mission"
            )

        assert result["mission_name"] == "custom_mission"
        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["json"]["mission_name"] == "custom_mission"

    async def test_navigate_with_custom_timeout(self):
        """Test navigation request with custom timeout_seconds."""
        client = MissionPlannerClient()
        mock_resp = _mock_response(json_data={"success": True, "mission_name": "m"})
        mock_post = AsyncMock(return_value=mock_resp)

        with patch.object(client._client, 'post', new=mock_post):
            await client.navigate(
                robot_name="robot_1",
                target_x=10.0,
                target_y=20.0,
                timeout_seconds=600
            )

        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["json"]["timeout_seconds"] == 600

    async def test_navigate_failure(self):
        """Test navigation request that returns success=False."""
        client = MissionPlannerClient()
        mock_resp = _mock_response(json_data={"success": False, "error": "No path found"})

        with patch.object(client._client, 'post', new=AsyncMock(return_value=mock_resp)):
            result = await client.navigate(
                robot_name="robot_1",
                target_x=10.0,
                target_y=20.0
            )

        assert result["success"] is False
        assert "error" in result

    async def test_navigate_http_error(self):
        """Test navigation request with HTTP error propagates."""
        client = MissionPlannerClient()
        mock_resp = _mock_response(
            status_code=500,
            raise_for_status=httpx.HTTPStatusError(
                "Server error",
                request=MagicMock(),
                response=MagicMock(status_code=500)
            )
        )

        with patch.object(client._client, 'post', new=AsyncMock(return_value=mock_resp)):
            with pytest.raises(httpx.HTTPStatusError):
                await client.navigate(robot_name="robot_1", target_x=10.0, target_y=20.0)

    async def test_navigate_connection_error(self):
        """Test navigation request with connection error propagates."""
        client = MissionPlannerClient()

        with patch.object(client._client, 'post',
                          new=AsyncMock(side_effect=httpx.ConnectError("Connection refused"))):
            with pytest.raises(httpx.ConnectError):
                await client.navigate(robot_name="robot_1", target_x=10.0, target_y=20.0)

    async def test_navigate_timeout(self):
        """Test navigation request timeout propagates."""
        client = MissionPlannerClient()

        with patch.object(client._client, 'post',
                          new=AsyncMock(side_effect=httpx.TimeoutException("Request timeout"))):
            with pytest.raises(httpx.TimeoutException):
                await client.navigate(robot_name="robot_1", target_x=10.0, target_y=20.0)


@pytest.mark.unit
class TestMissionPlannerClientHealthCheck:
    """Test MissionPlannerClient.health_check() method."""

    async def test_health_check_healthy(self):
        """Test health check when service is healthy."""
        client = MissionPlannerClient()
        mock_resp = _mock_response(json_data={"status": "healthy"})

        with patch.object(client._client, 'get', new=AsyncMock(return_value=mock_resp)):
            result = await client.health_check()

        assert result is True

    async def test_health_check_unhealthy_status(self):
        """Test health check when service returns unhealthy status."""
        client = MissionPlannerClient()
        mock_resp = _mock_response(json_data={"status": "unhealthy"})

        with patch.object(client._client, 'get', new=AsyncMock(return_value=mock_resp)):
            result = await client.health_check()

        assert result is False

    async def test_health_check_non_200_status(self):
        """Test health check when service returns non-200 status."""
        client = MissionPlannerClient()
        mock_resp = _mock_response(status_code=503)

        with patch.object(client._client, 'get', new=AsyncMock(return_value=mock_resp)):
            result = await client.health_check()

        assert result is False

    async def test_health_check_connection_error(self):
        """Test health check when connection fails returns False."""
        client = MissionPlannerClient()

        with patch.object(client._client, 'get',
                          new=AsyncMock(side_effect=httpx.ConnectError("Connection refused"))):
            result = await client.health_check()

        assert result is False

    async def test_health_check_timeout(self):
        """Test health check timeout returns False."""
        client = MissionPlannerClient()

        with patch.object(client._client, 'get',
                          new=AsyncMock(side_effect=httpx.TimeoutException("timeout"))):
            result = await client.health_check()

        assert result is False

    async def test_health_check_invalid_json(self):
        """Test health check with invalid JSON response returns False."""
        client = MissionPlannerClient()
        mock_resp = _mock_response(status_code=200)
        mock_resp.json.side_effect = ValueError("Invalid JSON")

        with patch.object(client._client, 'get', new=AsyncMock(return_value=mock_resp)):
            result = await client.health_check()

        assert result is False


@pytest.mark.unit
class TestMissionPlannerClientGetStats:
    """Test MissionPlannerClient.get_stats() method."""

    async def test_get_stats_success(self):
        """Test getting stats successfully."""
        client = MissionPlannerClient()
        mock_resp = _mock_response(json_data={
            "total_missions": 100,
            "active_missions": 5,
            "completed_missions": 95
        })

        with patch.object(client._client, 'get', new=AsyncMock(return_value=mock_resp)):
            result = await client.get_stats()

        assert result is not None
        assert result["total_missions"] == 100
        assert result["active_missions"] == 5

    async def test_get_stats_http_error(self):
        """Test getting stats with HTTP error returns None."""
        client = MissionPlannerClient()
        mock_resp = _mock_response(
            status_code=500,
            raise_for_status=httpx.HTTPStatusError(
                "Server error",
                request=MagicMock(),
                response=MagicMock(status_code=500)
            )
        )

        with patch.object(client._client, 'get', new=AsyncMock(return_value=mock_resp)):
            result = await client.get_stats()

        assert result is None

    async def test_get_stats_connection_error(self):
        """Test getting stats with connection error returns None."""
        client = MissionPlannerClient()

        with patch.object(client._client, 'get',
                          new=AsyncMock(side_effect=httpx.ConnectError("Connection refused"))):
            result = await client.get_stats()

        assert result is None

    async def test_get_stats_timeout(self):
        """Test getting stats timeout returns None."""
        client = MissionPlannerClient()

        with patch.object(client._client, 'get',
                          new=AsyncMock(side_effect=httpx.TimeoutException("timeout"))):
            result = await client.get_stats()

        assert result is None
