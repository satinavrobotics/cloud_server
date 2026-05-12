"""
Unit tests for UpdatePublisher class.

These tests verify the UpdatePublisher's subscribe/unsubscribe functionality
and message broadcasting behavior in isolation.
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock
from packages.services.graph_builder.server import UpdatePublisher


@pytest.mark.unit
@pytest.mark.asyncio
class TestUpdatePublisherSubscribe:
    """Test UpdatePublisher subscribe functionality."""
    
    async def test_subscribe_creates_new_map_entry(self):
        """Test that subscribing to a new map creates a new entry."""
        publisher = UpdatePublisher()
        queue = asyncio.Queue()
        map_id = "test_map_1"
        
        publisher.subscribe(map_id, queue)
        
        assert map_id in publisher.subscribers
        assert queue in publisher.subscribers[map_id]
        assert len(publisher.subscribers[map_id]) == 1
    
    async def test_subscribe_adds_to_existing_map(self):
        """Test that subscribing to an existing map adds to the set."""
        publisher = UpdatePublisher()
        queue1 = asyncio.Queue()
        queue2 = asyncio.Queue()
        map_id = "test_map_1"
        
        publisher.subscribe(map_id, queue1)
        publisher.subscribe(map_id, queue2)
        
        assert map_id in publisher.subscribers
        assert queue1 in publisher.subscribers[map_id]
        assert queue2 in publisher.subscribers[map_id]
        assert len(publisher.subscribers[map_id]) == 2
    
    async def test_subscribe_same_queue_twice_is_idempotent(self):
        """Test that subscribing the same queue twice doesn't duplicate it."""
        publisher = UpdatePublisher()
        queue = asyncio.Queue()
        map_id = "test_map_1"
        
        publisher.subscribe(map_id, queue)
        publisher.subscribe(map_id, queue)
        
        assert len(publisher.subscribers[map_id]) == 1
    
    async def test_subscribe_different_maps(self):
        """Test that subscribing to different maps creates separate entries."""
        publisher = UpdatePublisher()
        queue1 = asyncio.Queue()
        queue2 = asyncio.Queue()
        
        publisher.subscribe("map_1", queue1)
        publisher.subscribe("map_2", queue2)
        
        assert "map_1" in publisher.subscribers
        assert "map_2" in publisher.subscribers
        assert queue1 in publisher.subscribers["map_1"]
        assert queue2 in publisher.subscribers["map_2"]
        assert queue1 not in publisher.subscribers["map_2"]
        assert queue2 not in publisher.subscribers["map_1"]


@pytest.mark.unit
@pytest.mark.asyncio
class TestUpdatePublisherUnsubscribe:
    """Test UpdatePublisher unsubscribe functionality."""
    
    async def test_unsubscribe_removes_queue(self):
        """Test that unsubscribing removes the queue from subscribers."""
        publisher = UpdatePublisher()
        queue = asyncio.Queue()
        map_id = "test_map_1"
        
        publisher.subscribe(map_id, queue)
        publisher.unsubscribe(map_id, queue)
        
        assert queue not in publisher.subscribers.get(map_id, set())
    
    async def test_unsubscribe_removes_empty_map_entry(self):
        """Test that unsubscribing the last queue removes the map entry."""
        publisher = UpdatePublisher()
        queue = asyncio.Queue()
        map_id = "test_map_1"
        
        publisher.subscribe(map_id, queue)
        publisher.unsubscribe(map_id, queue)
        
        assert map_id not in publisher.subscribers
    
    async def test_unsubscribe_keeps_other_queues(self):
        """Test that unsubscribing one queue doesn't affect others."""
        publisher = UpdatePublisher()
        queue1 = asyncio.Queue()
        queue2 = asyncio.Queue()
        map_id = "test_map_1"
        
        publisher.subscribe(map_id, queue1)
        publisher.subscribe(map_id, queue2)
        publisher.unsubscribe(map_id, queue1)
        
        assert map_id in publisher.subscribers
        assert queue1 not in publisher.subscribers[map_id]
        assert queue2 in publisher.subscribers[map_id]
        assert len(publisher.subscribers[map_id]) == 1
    
    async def test_unsubscribe_nonexistent_map(self):
        """Test that unsubscribing from a nonexistent map doesn't raise error."""
        publisher = UpdatePublisher()
        queue = asyncio.Queue()
        
        # Should not raise an exception
        publisher.unsubscribe("nonexistent_map", queue)
    
    async def test_unsubscribe_nonexistent_queue(self):
        """Test that unsubscribing a nonexistent queue doesn't raise error."""
        publisher = UpdatePublisher()
        queue1 = asyncio.Queue()
        queue2 = asyncio.Queue()
        map_id = "test_map_1"
        
        publisher.subscribe(map_id, queue1)
        
        # Should not raise an exception
        publisher.unsubscribe(map_id, queue2)
        
        # queue1 should still be subscribed
        assert queue1 in publisher.subscribers[map_id]


@pytest.mark.unit
@pytest.mark.asyncio
class TestUpdatePublisherPublish:
    """Test UpdatePublisher publish functionality."""
    
    async def test_publish_sends_to_single_subscriber(self):
        """Test that publishing sends message to a single subscriber."""
        publisher = UpdatePublisher()
        queue = asyncio.Queue()
        map_id = "test_map_1"
        message = {"type": "node_added", "node_id": 1001}
        
        publisher.subscribe(map_id, queue)
        await publisher.publish(map_id, message)
        
        # Wait a bit for async operation
        await asyncio.sleep(0.1)
        
        assert not queue.empty()
        received = await queue.get()
        assert received == message
    
    async def test_publish_sends_to_multiple_subscribers(self):
        """Test that publishing sends message to all subscribers."""
        publisher = UpdatePublisher()
        queue1 = asyncio.Queue()
        queue2 = asyncio.Queue()
        queue3 = asyncio.Queue()
        map_id = "test_map_1"
        message = {"type": "node_added", "node_id": 1001}
        
        publisher.subscribe(map_id, queue1)
        publisher.subscribe(map_id, queue2)
        publisher.subscribe(map_id, queue3)
        
        await publisher.publish(map_id, message)
        
        # Wait a bit for async operation
        await asyncio.sleep(0.1)
        
        # All queues should receive the message
        assert not queue1.empty()
        assert not queue2.empty()
        assert not queue3.empty()
        
        assert await queue1.get() == message
        assert await queue2.get() == message
        assert await queue3.get() == message
    
    async def test_publish_only_to_correct_map(self):
        """Test that publishing only sends to subscribers of the correct map."""
        publisher = UpdatePublisher()
        queue_map1 = asyncio.Queue()
        queue_map2 = asyncio.Queue()
        message = {"type": "node_added", "node_id": 1001}
        
        publisher.subscribe("map_1", queue_map1)
        publisher.subscribe("map_2", queue_map2)
        
        await publisher.publish("map_1", message)
        
        # Wait a bit for async operation
        await asyncio.sleep(0.1)
        
        # Only map_1 subscriber should receive the message
        assert not queue_map1.empty()
        assert queue_map2.empty()
        
        assert await queue_map1.get() == message
    
    async def test_publish_to_nonexistent_map(self):
        """Test that publishing to a nonexistent map doesn't raise error."""
        publisher = UpdatePublisher()
        message = {"type": "node_added", "node_id": 1001}
        
        # Should not raise an exception
        await publisher.publish("nonexistent_map", message)
    
    async def test_publish_to_empty_subscriber_list(self):
        """Test that publishing to a map with no subscribers doesn't raise error."""
        publisher = UpdatePublisher()
        queue = asyncio.Queue()
        map_id = "test_map_1"
        message = {"type": "node_added", "node_id": 1001}
        
        # Subscribe and then unsubscribe
        publisher.subscribe(map_id, queue)
        publisher.unsubscribe(map_id, queue)
        
        # Should not raise an exception
        await publisher.publish(map_id, message)
    
    async def test_publish_multiple_messages_in_order(self):
        """Test that multiple messages are received in order."""
        publisher = UpdatePublisher()
        queue = asyncio.Queue()
        map_id = "test_map_1"
        messages = [
            {"type": "node_added", "node_id": 1001},
            {"type": "node_added", "node_id": 1002},
            {"type": "node_added", "node_id": 1003},
        ]
        
        publisher.subscribe(map_id, queue)
        
        for message in messages:
            await publisher.publish(map_id, message)
        
        # Wait a bit for async operations
        await asyncio.sleep(0.1)
        
        # Messages should be received in order
        for expected_message in messages:
            assert not queue.empty()
            received = await queue.get()
            assert received == expected_message


@pytest.mark.unit
@pytest.mark.asyncio
class TestUpdatePublisherConcurrency:
    """Test UpdatePublisher behavior under concurrent operations."""
    
    async def test_concurrent_subscribes(self):
        """Test that concurrent subscribes work correctly."""
        publisher = UpdatePublisher()
        map_id = "test_map_1"
        queues = [asyncio.Queue() for _ in range(10)]

        # Subscribe concurrently
        async def subscribe_queue(q):
            publisher.subscribe(map_id, q)

        await asyncio.gather(*[
            subscribe_queue(queue)
            for queue in queues
        ])

        assert len(publisher.subscribers[map_id]) == 10
    
    async def test_concurrent_publishes(self):
        """Test that concurrent publishes work correctly."""
        publisher = UpdatePublisher()
        queue = asyncio.Queue()
        map_id = "test_map_1"
        num_messages = 10
        
        publisher.subscribe(map_id, queue)
        
        # Publish concurrently
        await asyncio.gather(*[
            publisher.publish(map_id, {"id": i})
            for i in range(num_messages)
        ])
        
        # Wait a bit for async operations
        await asyncio.sleep(0.2)
        
        # All messages should be received
        received_ids = set()
        while not queue.empty():
            message = await queue.get()
            received_ids.add(message["id"])
        
        assert len(received_ids) == num_messages
    
    async def test_subscribe_unsubscribe_during_publish(self):
        """Test that subscribe/unsubscribe during publish doesn't cause errors."""
        publisher = UpdatePublisher()
        map_id = "test_map_1"
        queue1 = asyncio.Queue()
        queue2 = asyncio.Queue()
        
        publisher.subscribe(map_id, queue1)
        
        async def publish_loop():
            for i in range(20):
                await publisher.publish(map_id, {"id": i})
                await asyncio.sleep(0.01)
        
        async def subscribe_unsubscribe():
            await asyncio.sleep(0.05)
            publisher.subscribe(map_id, queue2)
            await asyncio.sleep(0.05)
            publisher.unsubscribe(map_id, queue2)
        
        # Run both concurrently
        await asyncio.gather(
            publish_loop(),
            subscribe_unsubscribe()
        )
        
        # queue1 should have received all messages
        # queue2 should have received some messages (those published while it was subscribed)
        assert not queue1.empty()

