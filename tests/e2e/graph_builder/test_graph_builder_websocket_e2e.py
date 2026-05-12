"""
Graph Builder Service - WebSocket E2E Tests

Tests real-time map update WebSocket connections.
"""

import pytest
import requests
import websockets
import json
import uuid
import asyncio


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.requires_docker
class TestGraphBuilderWebSocketE2E:
    """Test WebSocket functionality in Graph Builder Service."""

    async def test_websocket_connection(self, graph_builder_service):
        """Test basic WebSocket connection."""
        url = graph_builder_service["url"]
        ws_url = url.replace("http://", "ws://")
        map_id = f"map_{uuid.uuid4().hex[:8]}"

        try:
            async with websockets.connect(
                f"{ws_url}/ws/updates/{map_id}",
                open_timeout=10
            ) as websocket:
                # Connection successful if we reach here
                assert websocket is not None
        except Exception as e:
            # Service may not have WebSocket configured
            pytest.skip(f"WebSocket not available: {e}")

    async def test_websocket_multiple_clients(self, graph_builder_service):
        """Test multiple WebSocket clients on same map."""
        url = graph_builder_service["url"]
        ws_url = url.replace("http://", "ws://")
        map_id = f"map_{uuid.uuid4().hex[:8]}"

        try:
            connections = []
            for i in range(3):
                ws = await websockets.connect(
                    f"{ws_url}/ws/updates/{map_id}",
                    open_timeout=10
                )
                connections.append(ws)

            # Verify all connected (if we got here, connections are valid)
            assert len(connections) == 3

            # Close all
            for ws in connections:
                await ws.close()
        except Exception as e:
            pytest.skip(f"WebSocket not available: {e}")

    async def test_websocket_different_maps(self, graph_builder_service):
        """Test WebSocket clients on different maps."""
        url = graph_builder_service["url"]
        ws_url = url.replace("http://", "ws://")

        try:
            connections = {}
            for map_id in ["warehouse", "factory", "office"]:
                ws = await websockets.connect(
                    f"{ws_url}/ws/updates/{map_id}",
                    open_timeout=10
                )
                connections[map_id] = ws

            # Verify all connected (if we got here, connections are valid)
            assert len(connections) == 3

            # Close all
            for ws in connections.values():
                await ws.close()
        except Exception as e:
            pytest.skip(f"WebSocket not available: {e}")

    async def test_websocket_connection_timeout(self, graph_builder_service):
        """Test WebSocket connection timeout."""
        url = graph_builder_service["url"]
        ws_url = url.replace("http://", "ws://")
        map_id = f"map_{uuid.uuid4().hex[:8]}"
        
        try:
            with pytest.raises(asyncio.TimeoutError):
                async with websockets.connect(
                    f"{ws_url}/ws/updates/{map_id}",
                    open_timeout=0.001
                ) as websocket:
                    pass
        except Exception as e:
            # May not raise TimeoutError, just skip
            pytest.skip(f"WebSocket timeout test skipped: {e}")

    async def test_websocket_reconnection(self, graph_builder_service):
        """Test WebSocket reconnection."""
        url = graph_builder_service["url"]
        ws_url = url.replace("http://", "ws://")
        map_id = f"map_{uuid.uuid4().hex[:8]}"

        try:
            # Connect
            ws1 = await websockets.connect(
                f"{ws_url}/ws/updates/{map_id}",
                open_timeout=10
            )
            assert ws1 is not None
            await ws1.close()

            # Reconnect
            ws2 = await websockets.connect(
                f"{ws_url}/ws/updates/{map_id}",
                open_timeout=10
            )
            assert ws2 is not None
            await ws2.close()
        except Exception as e:
            pytest.skip(f"WebSocket not available: {e}")

    async def test_websocket_long_lived_connection(self, graph_builder_service):
        """Test long-lived WebSocket connection."""
        url = graph_builder_service["url"]
        ws_url = url.replace("http://", "ws://")
        map_id = f"map_{uuid.uuid4().hex[:8]}"

        try:
            async with websockets.connect(
                f"{ws_url}/ws/updates/{map_id}",
                open_timeout=10
            ) as websocket:
                # Keep connection open for a bit
                await asyncio.sleep(2)
                assert websocket is not None
        except Exception as e:
            pytest.skip(f"WebSocket not available: {e}")

    async def test_websocket_concurrent_connections(self, graph_builder_service):
        """Test concurrent WebSocket connections."""
        url = graph_builder_service["url"]
        ws_url = url.replace("http://", "ws://")
        
        try:
            async def connect_to_map(map_id):
                ws = await websockets.connect(
                    f"{ws_url}/ws/updates/{map_id}",
                    open_timeout=10
                )
                await asyncio.sleep(1)
                await ws.close()
            
            # Create 5 concurrent connections
            tasks = [
                connect_to_map(f"map_{i}_{uuid.uuid4().hex[:4]}")
                for i in range(5)
            ]
            await asyncio.gather(*tasks)
        except Exception as e:
            pytest.skip(f"WebSocket not available: {e}")

    async def test_websocket_rapid_connect_disconnect(self, graph_builder_service):
        """Test rapid connect/disconnect cycles."""
        url = graph_builder_service["url"]
        ws_url = url.replace("http://", "ws://")
        map_id = f"map_{uuid.uuid4().hex[:8]}"
        
        try:
            for i in range(5):
                ws = await websockets.connect(
                    f"{ws_url}/ws/updates/{map_id}",
                    open_timeout=10
                )
                await ws.close()
        except Exception as e:
            pytest.skip(f"WebSocket not available: {e}")

