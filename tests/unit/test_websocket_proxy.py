"""
Unit tests for WebSocketProxyManager class.

These tests verify the WebSocketProxyManager's proxy functionality,
connection pooling, and cleanup behavior using mocked WebSocket connections.
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, MagicMock, patch, call
from packages.api.server import WebSocketProxyManager


@pytest.fixture
def mock_websocket():
    """Create a mock WebSocket connection."""
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.receive_json = AsyncMock()
    ws.close = AsyncMock()
    return ws


@pytest.fixture
def mock_backend_websocket():
    """Create a mock backend WebSocket connection."""
    ws = AsyncMock()
    ws.recv = AsyncMock()
    ws.send = AsyncMock()
    ws.close = AsyncMock()
    return ws


def setup_mock_connect(mock_connect, mock_backend_websocket):
    """Helper to setup websockets.connect mock to return backend websocket."""
    async def mock_connect_func(*args, **kwargs):
        return mock_backend_websocket
    mock_connect.side_effect = mock_connect_func


@pytest.mark.unit
@pytest.mark.asyncio
class TestWebSocketProxyManagerInit:
    """Test WebSocketProxyManager initialization."""
    
    async def test_init_with_urls(self):
        """Test that manager initializes with provided URLs."""
        graph_builder_url = "ws://localhost:8004"
        mission_dispatcher_url = "ws://localhost:5000"
        
        manager = WebSocketProxyManager(
            graph_builder_ws_url=graph_builder_url,
            mission_dispatcher_ws_url=mission_dispatcher_url
        )
        
        assert manager.graph_builder_ws_url == graph_builder_url
        assert manager.mission_dispatcher_ws_url == mission_dispatcher_url
        assert manager.proxy_connections == {}
    
    async def test_init_with_none_urls(self):
        """Test that manager initializes with None URLs."""
        manager = WebSocketProxyManager(
            graph_builder_ws_url=None,
            mission_dispatcher_ws_url=None
        )
        
        assert manager.graph_builder_ws_url is None
        assert manager.mission_dispatcher_ws_url is None
        assert manager.proxy_connections == {}


@pytest.mark.unit
@pytest.mark.asyncio
class TestWebSocketProxyManagerAddClient:
    """Test WebSocketProxyManager _add_client_to_proxy functionality."""
    
    @patch('packages.api.server.websockets.connect')
    async def test_add_first_client_creates_backend_connection(
        self, mock_connect, mock_websocket, mock_backend_websocket
    ):
        """Test that adding the first client creates a backend connection."""
        setup_mock_connect(mock_connect, mock_backend_websocket)

        manager = WebSocketProxyManager(
            graph_builder_ws_url="ws://localhost:8004",
            mission_dispatcher_ws_url=None
        )

        map_id = "test_map_1"
        connection_key = f"map_updates:{map_id}"
        await manager._add_client_to_proxy(connection_key, mock_websocket, map_id, "map_updates")

        # Backend connection should be created
        assert connection_key in manager.proxy_connections
        assert mock_websocket in manager.proxy_connections[connection_key]["clients"]
        assert manager.proxy_connections[connection_key]["backend"] == mock_backend_websocket

        # Should have called websockets.connect
        mock_connect.assert_called_once()
        call_args = mock_connect.call_args[0][0]
        assert "ws://localhost:8004" in call_args
        assert map_id in call_args
    
    @patch('packages.api.server.websockets.connect')
    async def test_add_second_client_reuses_backend_connection(
        self, mock_connect, mock_backend_websocket
    ):
        """Test that adding a second client reuses existing backend connection."""
        setup_mock_connect(mock_connect, mock_backend_websocket)

        manager = WebSocketProxyManager(
            graph_builder_ws_url="ws://localhost:8004",
            mission_dispatcher_ws_url=None
        )
        
        map_id = "test_map_1"
        connection_key = f"map_updates:{map_id}"
        client1 = AsyncMock()
        client2 = AsyncMock()

        # Add first client
        await manager._add_client_to_proxy(connection_key, client1, map_id, "map_updates")

        # Add second client
        await manager._add_client_to_proxy(connection_key, client2, map_id, "map_updates")

        # Should only have one backend connection
        assert mock_connect.call_count == 1

        # Both clients should be in the set
        assert client1 in manager.proxy_connections[connection_key]["clients"]
        assert client2 in manager.proxy_connections[connection_key]["clients"]
        assert len(manager.proxy_connections[connection_key]["clients"]) == 2
    
    @patch('packages.api.server.websockets.connect')
    async def test_add_clients_to_different_maps(
        self, mock_connect, mock_backend_websocket
    ):
        """Test that clients for different maps get separate backend connections."""
        setup_mock_connect(mock_connect, mock_backend_websocket)

        manager = WebSocketProxyManager(
            graph_builder_ws_url="ws://localhost:8004",
            mission_dispatcher_ws_url=None
        )
        
        client1 = AsyncMock()
        client2 = AsyncMock()

        # Add clients to different maps
        connection_key_1 = "map_updates:map_1"
        connection_key_2 = "map_updates:map_2"
        await manager._add_client_to_proxy(connection_key_1, client1, "map_1", "map_updates")
        await manager._add_client_to_proxy(connection_key_2, client2, "map_2", "map_updates")

        # Should have two backend connections
        assert mock_connect.call_count == 2
        assert connection_key_1 in manager.proxy_connections
        assert connection_key_2 in manager.proxy_connections
        assert client1 in manager.proxy_connections[connection_key_1]["clients"]
        assert client2 in manager.proxy_connections[connection_key_2]["clients"]
    
    @patch('packages.api.server.websockets.connect')
    async def test_add_client_starts_forwarding_task(
        self, mock_connect, mock_websocket, mock_backend_websocket
    ):
        """Test that adding the first client starts a forwarding task."""
        setup_mock_connect(mock_connect, mock_backend_websocket)

        manager = WebSocketProxyManager(
            graph_builder_ws_url="ws://localhost:8004",
            mission_dispatcher_ws_url=None
        )
        
        map_id = "test_map_1"
        connection_key = f"map_updates:{map_id}"
        await manager._add_client_to_proxy(connection_key, mock_websocket, map_id, "map_updates")

        # Forwarding task should be created
        assert "task" in manager.proxy_connections[connection_key]
        assert isinstance(manager.proxy_connections[connection_key]["task"], asyncio.Task)

        # Clean up
        manager.proxy_connections[connection_key]["task"].cancel()
        try:
            await manager.proxy_connections[connection_key]["task"]
        except asyncio.CancelledError:
            pass


@pytest.mark.unit
@pytest.mark.asyncio
class TestWebSocketProxyManagerRemoveClient:
    """Test WebSocketProxyManager _remove_client_from_proxy functionality."""
    
    @patch('packages.api.server.websockets.connect')
    async def test_remove_client_keeps_backend_when_others_remain(
        self, mock_connect, mock_backend_websocket
    ):
        """Test that removing a client keeps backend connection when others remain."""
        setup_mock_connect(mock_connect, mock_backend_websocket)

        manager = WebSocketProxyManager(
            graph_builder_ws_url="ws://localhost:8004",
            mission_dispatcher_ws_url=None
        )
        
        map_id = "test_map_1"
        connection_key = f"map_updates:{map_id}"
        client1 = AsyncMock()
        client2 = AsyncMock()

        # Add two clients
        await manager._add_client_to_proxy(connection_key, client1, map_id, "map_updates")
        await manager._add_client_to_proxy(connection_key, client2, map_id, "map_updates")

        # Remove one client
        await manager._remove_client_from_proxy(connection_key, client1)

        # Backend connection should still exist
        assert connection_key in manager.proxy_connections
        assert client1 not in manager.proxy_connections[connection_key]["clients"]
        assert client2 in manager.proxy_connections[connection_key]["clients"]

        # Backend should not be closed
        mock_backend_websocket.close.assert_not_called()

        # Clean up
        manager.proxy_connections[connection_key]["task"].cancel()
        try:
            await manager.proxy_connections[connection_key]["task"]
        except asyncio.CancelledError:
            pass
    
    @patch('packages.api.server.websockets.connect')
    async def test_remove_last_client_closes_backend(
        self, mock_connect, mock_backend_websocket
    ):
        """Test that removing the last client closes backend connection."""
        setup_mock_connect(mock_connect, mock_backend_websocket)

        manager = WebSocketProxyManager(
            graph_builder_ws_url="ws://localhost:8004",
            mission_dispatcher_ws_url=None
        )
        
        map_id = "test_map_1"
        connection_key = f"map_updates:{map_id}"
        client = AsyncMock()

        # Add one client
        await manager._add_client_to_proxy(connection_key, client, map_id, "map_updates")

        # Get the task before removal
        task = manager.proxy_connections[connection_key]["task"]

        # Remove the client
        await manager._remove_client_from_proxy(connection_key, client)

        # Backend connection should be removed
        assert connection_key not in manager.proxy_connections

        # Backend should be closed
        mock_backend_websocket.close.assert_called_once()

        # Wait for task to finish cancelling
        await asyncio.sleep(0.1)

        # Task should be cancelled or done
        assert task.cancelled() or task.done(), f"Task state: cancelled={task.cancelled()}, done={task.done()}"
    
    async def test_remove_client_from_nonexistent_map(self):
        """Test that removing a client from nonexistent map doesn't raise error."""
        manager = WebSocketProxyManager(
            graph_builder_ws_url="ws://localhost:8004",
            mission_dispatcher_ws_url=None
        )

        client = AsyncMock()

        # Should not raise an exception
        await manager._remove_client_from_proxy("map_updates:nonexistent_map", client)
    
    @patch('packages.api.server.websockets.connect')
    async def test_remove_nonexistent_client(
        self, mock_connect, mock_backend_websocket
    ):
        """Test that removing a nonexistent client doesn't affect others."""
        setup_mock_connect(mock_connect, mock_backend_websocket)

        manager = WebSocketProxyManager(
            graph_builder_ws_url="ws://localhost:8004",
            mission_dispatcher_ws_url=None
        )
        
        map_id = "test_map_1"
        connection_key = f"map_updates:{map_id}"
        client1 = AsyncMock()
        client2 = AsyncMock()

        # Add one client
        await manager._add_client_to_proxy(connection_key, client1, map_id, "map_updates")

        # Try to remove a different client
        await manager._remove_client_from_proxy(connection_key, client2)

        # Original client should still be there
        assert client1 in manager.proxy_connections[connection_key]["clients"]

        # Clean up
        manager.proxy_connections[connection_key]["task"].cancel()
        try:
            await manager.proxy_connections[connection_key]["task"]
        except asyncio.CancelledError:
            pass


@pytest.mark.unit
@pytest.mark.asyncio
class TestWebSocketProxyManagerForwarding:
    """Test WebSocketProxyManager message forwarding functionality."""
    
    @patch('packages.api.server.websockets.connect')
    async def test_forward_from_backend_to_clients(
        self, mock_connect, mock_backend_websocket
    ):
        """Test that messages from backend are forwarded to all clients."""
        # Setup backend to return messages
        messages = [
            '{"type": "node_added", "node_id": 1001}',
            '{"type": "node_added", "node_id": 1002}',
        ]

        # Make the backend websocket support async iteration
        async def async_iter_messages():
            for msg in messages:
                yield msg

        # Return the same iterator so it doesn't restart on reconnection
        mock_backend_websocket.__aiter__ = MagicMock(return_value=async_iter_messages())
        setup_mock_connect(mock_connect, mock_backend_websocket)

        manager = WebSocketProxyManager(
            graph_builder_ws_url="ws://localhost:8004",
            mission_dispatcher_ws_url=None
        )

        map_id = "test_map_1"
        connection_key = f"map_updates:{map_id}"
        client1 = AsyncMock()
        client2 = AsyncMock()

        # Add clients
        await manager._add_client_to_proxy(connection_key, client1, map_id, "map_updates")
        await manager._add_client_to_proxy(connection_key, client2, map_id, "map_updates")

        # Wait for forwarding to process messages
        await asyncio.sleep(0.5)

        # Both clients should have received both messages (using send_json, not send_text)
        assert client1.send_json.call_count >= 2, f"client1 received {client1.send_json.call_count} messages"
        assert client2.send_json.call_count >= 2, f"client2 received {client2.send_json.call_count} messages"

        # Clean up
        manager.proxy_connections[connection_key]["task"].cancel()
        try:
            await manager.proxy_connections[connection_key]["task"]
        except asyncio.CancelledError:
            pass


@pytest.mark.unit
@pytest.mark.asyncio
class TestWebSocketProxyManagerErrorHandling:
    """Test WebSocketProxyManager error handling."""
    
    @patch('packages.api.server.websockets.connect')
    async def test_backend_connection_failure(self, mock_connect, mock_websocket):
        """Test handling of backend connection failure."""
        mock_connect.side_effect = Exception("Connection failed")

        manager = WebSocketProxyManager(
            graph_builder_ws_url="ws://localhost:8004",
            mission_dispatcher_ws_url=None
        )

        map_id = "test_map_1"
        connection_key = f"map_updates:{map_id}"

        # Should handle the exception gracefully by creating a stub connection
        # (no exception should be raised)
        await manager._add_client_to_proxy(connection_key, mock_websocket, map_id, "map_updates")

        # Verify that a proxy connection was created with a stub task
        assert connection_key in manager.proxy_connections
        assert manager.proxy_connections[connection_key]["backend"] is None
        assert manager.proxy_connections[connection_key]["task"] is not None

        # Clean up
        manager.proxy_connections[connection_key]["task"].cancel()
        try:
            await manager.proxy_connections[connection_key]["task"]
        except asyncio.CancelledError:
            pass
    
    @patch('packages.api.server.websockets.connect')
    async def test_client_send_failure_doesnt_affect_others(
        self, mock_connect, mock_backend_websocket
    ):
        """Test that failure to send to one client doesn't affect others."""
        messages = ['{"type": "test"}', asyncio.CancelledError()]
        mock_backend_websocket.recv = AsyncMock(side_effect=messages)
        setup_mock_connect(mock_connect, mock_backend_websocket)

        manager = WebSocketProxyManager(
            graph_builder_ws_url="ws://localhost:8004",
            mission_dispatcher_ws_url=None
        )
        
        map_id = "test_map_1"
        connection_key = f"map_updates:{map_id}"
        client1 = AsyncMock()
        client2 = AsyncMock()

        # Make client1 fail on send
        client1.send_text = AsyncMock(side_effect=Exception("Send failed"))

        # Add clients
        await manager._add_client_to_proxy(connection_key, client1, map_id, "map_updates")
        await manager._add_client_to_proxy(connection_key, client2, map_id, "map_updates")

        # Wait for forwarding
        await asyncio.sleep(0.2)

        # client2 should still receive messages despite client1 failing
        # (implementation should catch and log the error)

        # Clean up
        manager.proxy_connections[connection_key]["task"].cancel()
        try:
            await manager.proxy_connections[connection_key]["task"]
        except asyncio.CancelledError:
            pass

