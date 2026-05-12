#!/usr/bin/env python3
"""
Test script to verify robot status WebSocket updates.

This script connects to the API Delegation Service WebSocket endpoint
and listens for robot status updates.
"""

import asyncio
import websockets
import json
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def test_robot_websocket(robot_name: str = "carter01", duration: int = 30):
    """
    Connect to robot status WebSocket and listen for updates.
    
    Args:
        robot_name: Name of the robot to monitor
        duration: How long to listen for updates (seconds)
    """
    url = f"ws://localhost:8000/ws/robot/{robot_name}"
    
    logger.info(f"Connecting to {url}")
    
    try:
        async with websockets.connect(url) as websocket:
            logger.info(f"✅ Connected to robot status WebSocket for {robot_name}")
            logger.info(f"Listening for updates for {duration} seconds...")
            
            try:
                async with asyncio.timeout(duration):
                    while True:
                        message = await websocket.recv()
                        data = json.loads(message)
                        
                        logger.info("=" * 60)
                        logger.info(f"Received update for {robot_name}:")
                        logger.info(f"  Type: {data.get('type')}")
                        logger.info(f"  Timestamp: {data.get('timestamp')}")
                        
                        status = data.get('status', {})
                        logger.info(f"  Online: {status.get('online')}")
                        logger.info(f"  Battery: {status.get('battery_level')}%")
                        logger.info(f"  State: {status.get('state')}")
                        
                        pose = status.get('pose')
                        if pose:
                            logger.info(f"  Pose: x={pose.get('x'):.2f}, y={pose.get('y'):.2f}, theta={pose.get('theta'):.2f}")
                        
                        logger.info("=" * 60)
                        
            except asyncio.TimeoutError:
                logger.info(f"Timeout reached after {duration} seconds")
                
    except Exception as e:
        logger.error(f"WebSocket connection failed: {e}")
        raise


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Test robot status WebSocket updates")
    parser.add_argument("--robot", default="carter01", help="Robot name to monitor")
    parser.add_argument("--duration", type=int, default=30, help="Duration to listen (seconds)")
    
    args = parser.parse_args()
    
    asyncio.run(test_robot_websocket(args.robot, args.duration))

