"""
Pytest configuration and shared fixtures for cloud_server tests.

This file contains:
- Pytest configuration
- Shared fixtures for all tests
- Docker container fixtures
- Database fixtures
- Service client fixtures
"""

import os
import sys
import time
import pytest
import requests
from typing import Generator, Dict, Any
from pathlib import Path

try:
    import docker
    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False
    docker = None

# Add project root to Python path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load secrets from .env before any test module imports config.py
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / '.env', override=False)
except ImportError:
    pass


# ==================== Pytest Configuration ====================

def pytest_configure(config):
    """Configure pytest with custom markers."""
    config.addinivalue_line(
        "markers", "unit: Unit tests (fast, isolated, mocked dependencies)"
    )
    config.addinivalue_line(
        "markers", "integration: Integration tests (medium speed, real dependencies)"
    )
    config.addinivalue_line(
        "markers", "e2e: End-to-end tests (slow, full system)"
    )
    config.addinivalue_line(
        "markers", "scenario: Scenario tests (business workflows and use cases)"
    )
    config.addinivalue_line(
        "markers", "performance: Performance/load tests"
    )
    config.addinivalue_line(
        "markers", "slow: Slow tests (> 10 seconds)"
    )
    config.addinivalue_line(
        "markers", "requires_docker: Tests that require Docker containers"
    )


# ==================== Docker Fixtures ====================

@pytest.fixture(scope="session")
def arangodb_container() -> Generator[Dict[str, Any], None, None]:
    """
    Use ArangoDB container from Docker Compose for testing.

    Yields:
        dict: Container info with 'host', 'port', 'url'
    """
    # Use environment variables or defaults for Docker Compose services
    host = os.getenv("ARANGODB_HOST", "localhost")
    port = int(os.getenv("ARANGODB_PORT", "8529"))
    url = f"http://{host}:{port}"

    # Wait for ArangoDB to be ready
    max_retries = 30
    for i in range(max_retries):
        try:
            response = requests.get(f"{url}/_api/version", timeout=1)
            if response.status_code == 200:
                break
        except requests.exceptions.RequestException:
            pass
        time.sleep(1)
    else:
        pytest.skip("ArangoDB container not available")

    yield {
        "host": host,
        "port": port,
        "url": url,
    }


@pytest.fixture(scope="session")
def minio_container() -> Generator[Dict[str, Any], None, None]:
    """
    Use MinIO container from Docker Compose for testing.

    Yields:
        dict: Container info with 'host', 'port', 'url', 'access_key', 'secret_key'
    """
    # Use environment variables or defaults for Docker Compose services
    host = os.getenv("MINIO_HOST", "localhost")
    port = int(os.getenv("MINIO_PORT", "9000"))
    access_key = os.getenv("MINIO_ROOT_USER", "minioadmin")
    secret_key = os.getenv("MINIO_ROOT_PASSWORD", "minioadmin")
    url = f"http://{host}:{port}"

    # Wait for MinIO to be ready
    max_retries = 30
    for i in range(max_retries):
        try:
            response = requests.get(f"{url}/minio/health/live", timeout=1)
            if response.status_code == 200:
                break
        except requests.exceptions.RequestException:
            pass
        time.sleep(1)
    else:
        pytest.skip("MinIO container not available")

    yield {
        "host": host,
        "port": port,
        "url": url,
        "access_key": access_key,
        "secret_key": secret_key,
    }


@pytest.fixture(scope="session")
def postgres_container() -> Generator[Dict[str, Any], None, None]:
    """
    Use PostgreSQL container from Docker Compose for testing.

    Yields:
        dict: Container info with 'host', 'port', 'database', 'user', 'password'
    """
    # Use environment variables or defaults for Docker Compose services
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    database = os.getenv("POSTGRES_DB", "test_db")
    user = os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "postgres")

    # Wait for PostgreSQL to be ready
    max_retries = 30
    for i in range(max_retries):
        try:
            # Try to connect using psycopg2 if available
            import psycopg2
            conn = psycopg2.connect(
                host=host,
                port=port,
                database=database,
                user=user,
                password=password
            )
            conn.close()
            break
        except Exception:
            pass
        time.sleep(1)
    else:
        pytest.skip("PostgreSQL container not available")

    yield {
        "host": host,
        "port": port,
        "database": database,
        "user": user,
        "password": password,
    }


# ==================== Service Client Fixtures ====================

@pytest.fixture(scope="session")
def graph_db_service(arangodb_container) -> Generator[Dict[str, Any], None, None]:
    """
    Provide ArangoDB connection info for graph DB tests.

    Yields:
        dict: ArangoDB connection info
    """
    yield arangodb_container


@pytest.fixture(scope="session")
def graph_database_service(graph_db_service) -> Generator[Dict[str, Any], None, None]:
    """
    Alias for graph_db_service for backward compatibility.

    Yields:
        dict: ArangoDB connection info
    """
    yield graph_db_service


@pytest.fixture(scope="session")
def image_db_service() -> Generator[Dict[str, Any], None, None]:
    """
    Provide MinIO connection info for integration tests.

    Yields:
        dict: MinIO connection parameters
    """
    minio_host = os.getenv("MINIO_HOST", "localhost")
    minio_port = int(os.getenv("MINIO_PORT", "9000"))
    minio_access_key = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
    minio_secret_key = os.getenv("MINIO_SECRET_KEY", "minioadmin")

    # Wait for MinIO to be ready
    import urllib.request
    max_retries = 60
    for i in range(max_retries):
        try:
            urllib.request.urlopen(f"http://{minio_host}:{minio_port}/minio/health/live", timeout=1)
            break
        except Exception:
            pass
        time.sleep(1)
    else:
        pytest.skip("MinIO not available")

    yield {
        "minio_host": minio_host,
        "minio_port": minio_port,
        "minio_access_key": minio_access_key,
        "minio_secret_key": minio_secret_key,
    }


@pytest.fixture(scope="session")
def image_database_service(image_db_service) -> Generator[Dict[str, Any], None, None]:
    """Alias for image_db_service for backward compatibility."""
    yield image_db_service



@pytest.fixture(scope="session")
def graph_builder_service() -> Generator[Dict[str, Any], None, None]:
    """
    Use Graph Builder Service container from Docker Compose for testing.

    Yields:
        dict: Service info with 'url'
    """
    url = os.getenv("GRAPH_BUILDER_URL", "http://localhost:8004")

    # Wait for service to be ready
    max_retries = 60
    for i in range(max_retries):
        try:
            response = requests.get(f"{url}/health", timeout=1)
            if response.status_code == 200:
                break
        except requests.exceptions.RequestException:
            pass
        time.sleep(1)
    else:
        pytest.skip("Graph Builder Service container not available")

    yield {"url": url}


@pytest.fixture(scope="session")
def mission_planner_service() -> Generator[Dict[str, Any], None, None]:
    """
    Use Mission Planner Service container from Docker Compose for testing.

    Yields:
        dict: Service info with 'url'
    """
    url = os.getenv("MISSION_PLANNER_URL", "http://localhost:8005")

    # Wait for service to be ready
    max_retries = 60
    for i in range(max_retries):
        try:
            response = requests.get(f"{url}/health", timeout=1)
            if response.status_code == 200:
                break
        except requests.exceptions.RequestException:
            pass
        time.sleep(1)
    else:
        pytest.skip("Mission Planner Service container not available")

    yield {"url": url}


@pytest.fixture(scope="session")
def api_delegation_service() -> Generator[Dict[str, Any], None, None]:
    """
    Use API Delegation Service container from Docker Compose for testing.

    Yields:
        dict: Service info with 'url'
    """
    url = os.getenv("API_DELEGATION_URL", "http://localhost:8000")

    # Wait for service to be ready
    max_retries = 120  # Increased from 60 to 120 for slower environments
    for i in range(max_retries):
        try:
            response = requests.get(f"{url}/health", timeout=2)
            if response.status_code == 200:
                print(f"\n✓ API Delegation Service is ready at {url}")
                break
        except requests.exceptions.RequestException as e:
            if i % 10 == 0:  # Log every 10 attempts
                print(f"Waiting for API Delegation Service... ({i}/{max_retries})")
        time.sleep(1)
    else:
        print(f"\n✗ API Delegation Service not available at {url}")
        pytest.skip("API Delegation Service container not available")

    yield {"url": url}


@pytest.fixture(scope="session")
def mission_database_service() -> Generator[Dict[str, Any], None, None]:
    """
    Use Mission Database Service container from Docker Compose for testing.

    Yields:
        dict: Service info with 'url'
    """
    url = os.getenv("MISSION_DATABASE_URL", "http://localhost:5000")

    # Wait for service to be ready
    max_retries = 60
    for i in range(max_retries):
        try:
            response = requests.get(f"{url}/health", timeout=1)
            if response.status_code == 200:
                break
        except requests.exceptions.RequestException:
            pass
        time.sleep(1)
    else:
        pytest.skip("Mission Database Service container not available")

    yield {"url": url}


@pytest.fixture(scope="session")
def mission_dispatch_service() -> Generator[Dict[str, Any], None, None]:
    """
    Use Mission Dispatch Service container from Docker Compose for testing.

    Note: Mission Dispatch is an MQTT-based service that doesn't expose HTTP endpoints.
    We verify the service is ready by checking MQTT connectivity and waiting for
    the service to fully initialize.

    Yields:
        dict: Service info with 'url'
    """
    url = os.getenv("MISSION_DISPATCH_URL", "http://localhost:8080")

    # Wait for MQTT broker to be available (which Mission Dispatch depends on)
    import socket
    mqtt_host = os.getenv("MQTT_HOST", "localhost")
    mqtt_port = int(os.getenv("MQTT_PORT", "1883"))

    max_retries = 60
    mqtt_ready = False

    for i in range(max_retries):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex((mqtt_host, mqtt_port))
            sock.close()
            if result == 0:
                mqtt_ready = True
                break
        except Exception:
            pass
        time.sleep(1)

    if not mqtt_ready:
        pytest.skip("Mission Dispatch Service container not available (MQTT broker not responding)")

    # Mission Dispatcher takes additional time to fully initialize after MQTT is available
    # The service needs to:
    # 1. Connect to MQTT broker
    # 2. Subscribe to VDA5050 state topics
    # 3. Connect to Mission Database
    # 4. Initialize internal state
    # Based on the healthcheck configuration (start_period=10s, interval=5s, retries=30),
    # the service can take up to 10s + (5s * retries) to become healthy.
    # We wait 15 seconds to ensure the service is fully initialized.
    time.sleep(15)

    yield {"url": url}


@pytest.fixture
def graph_db_client(arangodb_container):
    """
    Provide GraphDatabaseService connected directly to ArangoDB.

    This fixture:
    - Creates a fresh service instance for each test
    - Cleans up test data after each test
    """
    from packages.topomap_dbs.graph_db.server import GraphDatabaseService

    client = GraphDatabaseService(
        arango_host=arangodb_container["host"],
        arango_port=arangodb_container["port"],
        arango_username=os.getenv("ARANGO_USERNAME", "root"),
        arango_password=os.getenv("ARANGO_PASSWORD", "test"),
        database_name=os.getenv("DATABASE_NAME", "test_topomap_db"),
    )

    yield client

    # Cleanup: Delete all test maps
    try:
        maps = client.list_maps()
        for map_id in maps:
            # Only delete test maps (those starting with "test_")
            if map_id.startswith("test_"):
                client.delete_map(map_id)
    except Exception as e:
        # Log but don't fail the test if cleanup fails
        print(f"Warning: Failed to cleanup test maps: {e}")


@pytest.fixture
def rosbag_db_client(image_db_service):
    """
    Provide RosbagDatabaseService connected directly to MinIO.

    Reuses the same MinIO instance as image_db_service.
    Cleans up any test buckets after each test.
    """
    from packages.topomap_dbs.rosbag_db.server import RosbagDatabaseService

    client = RosbagDatabaseService(
        minio_host=image_db_service["minio_host"],
        minio_port=image_db_service["minio_port"],
        minio_access_key=image_db_service["minio_access_key"],
        minio_secret_key=image_db_service["minio_secret_key"],
        presign_expiry_seconds=3600,
    )

    yield client

    try:
        for map_id in client.list_maps():
            if map_id.startswith("test_"):
                client.delete_map_bags(map_id)
    except Exception as e:
        print(f"Warning: Failed to cleanup rosbag test buckets: {e}")


@pytest.fixture
def sample_bag():
    """Provide a small synthetic ROS bag bytes blob for testing."""
    # Minimal bytes that look like a ROS bag header
    return b"ROSBAG V2.0\x00" + b"\x00" * 1024


@pytest.fixture
def image_db_client(image_db_service):
    """
    Provide ImageDatabaseService connected directly to MinIO.

    This fixture:
    - Creates a fresh service instance for each test
    - Cleans up test data after each test
    """
    from packages.topomap_dbs.image_db.server import ImageDatabaseService

    client = ImageDatabaseService(
        minio_host=image_db_service["minio_host"],
        minio_port=image_db_service["minio_port"],
        minio_access_key=image_db_service["minio_access_key"],
        minio_secret_key=image_db_service["minio_secret_key"],
    )

    yield client

    # Cleanup: Delete all test maps
    try:
        maps = client.list_maps()
        for map_id in maps:
            if map_id.startswith("test_"):
                client.delete_map(map_id)
    except Exception as e:
        print(f"Warning: Failed to cleanup test maps from image DB: {e}")


# ==================== Test Data Fixtures ====================

@pytest.fixture
def sample_map_simple():
    """Provide a simple test map with 3 nodes."""
    return {
        "map_id": "test_map_simple",
        "nodes": [
            {"id": "node_1", "x": 0.0, "y": 0.0, "theta": 0.0},
            {"id": "node_2", "x": 10.0, "y": 0.0, "theta": 0.0},
            {"id": "node_3", "x": 10.0, "y": 10.0, "theta": 1.57},
        ],
        "edges": [
            # Bidirectional edges for pathfinding
            {"from": "node_1", "to": "node_2", "weight": 10.0},
            {"from": "node_2", "to": "node_1", "weight": 10.0},
            {"from": "node_2", "to": "node_3", "weight": 10.0},
            {"from": "node_3", "to": "node_2", "weight": 10.0},
        ]
    }


@pytest.fixture
def sample_map_complex():
    """Provide a complex test map with 100 nodes."""
    nodes = []
    edges = []
    
    # Create a 10x10 grid
    for i in range(10):
        for j in range(10):
            node_id = f"node_{i}_{j}"
            nodes.append({
                "id": node_id,
                "x": float(i * 10),
                "y": float(j * 10),
                "theta": 0.0
            })
            
            # Add edges to neighbors
            if i > 0:
                edges.append({
                    "from": f"node_{i-1}_{j}",
                    "to": node_id,
                    "weight": 10.0
                })
            if j > 0:
                edges.append({
                    "from": f"node_{i}_{j-1}",
                    "to": node_id,
                    "weight": 10.0
                })
    
    return {
        "map_id": "test_map_complex",
        "nodes": nodes,
        "edges": edges
    }


@pytest.fixture
def sample_image():
    """Provide a sample test image (1x1 red pixel PNG)."""
    import base64
    # 1x1 red pixel PNG
    png_data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
    )
    return png_data


@pytest.fixture
def sample_robot():
    """Provide a sample robot object for testing."""
    return {
        "name": "test_robot_sample",
        "labels": ["test", "sample"],
        "battery": {
            "critical_level": 10.0,
            "recommended_minimum": 20.0,
            "recommended_maximum": 95.0
        },
        "heartbeat_timeout": 30.0,
        "switch_teleop": False
    }


@pytest.fixture
def sample_mission():
    """Provide a sample mission object for testing."""
    return {
        "name": "test_mission_sample",
        "robot": "test_robot_sample",
        "mission_tree": [
            {
                "name": "root_sequence",
                "parent": "root",
                "sequence": {}
            },
            {
                "name": "navigate_waypoint",
                "parent": "root_sequence",
                "route": {
                    "waypoints": [
                        {
                            "x": 10.0,
                            "y": 20.0,
                            "theta": 0.0,
                            "map_id": "test_map",
                            "allowedDeviationXY": 0.5,
                            "allowedDeviationTheta": 0.1
                        },
                        {
                            "x": 15.0,
                            "y": 25.0,
                            "theta": 1.57,
                            "map_id": "test_map",
                            "allowedDeviationXY": 0.5,
                            "allowedDeviationTheta": 0.1
                        }
                    ]
                }
            }
        ],
        "timeout": 300.0,
        "deadline": None,
        "needs_canceled": False,
        "update_nodes": None
    }


@pytest.fixture
def sample_detection_results():
    """Provide sample detection results for testing."""
    return {
        "name": "test_robot_sample",
        "status": {
            "detected_objects": [
                {
                    "class_name": "person",
                    "confidence": 0.95,
                    "bounding_box": {
                        "x_min": 100,
                        "y_min": 150,
                        "x_max": 200,
                        "y_max": 300
                    },
                    "object_id": "person_001"
                },
                {
                    "class_name": "box",
                    "confidence": 0.88,
                    "bounding_box": {
                        "x_min": 300,
                        "y_min": 200,
                        "x_max": 400,
                        "y_max": 350
                    },
                    "object_id": "box_001"
                }
            ]
        }
    }


# ==================== MQTT Fixtures ====================

@pytest.fixture(scope="session")
def mqtt_broker() -> Generator[Dict[str, Any], None, None]:
    """
    Use MQTT broker container from Docker Compose for testing.

    Yields:
        dict: MQTT broker info with 'host', 'port'
    """
    host = os.getenv("MQTT_HOST", "localhost")
    # Use the MQTT_PORT environment variable if set (for Docker network tests)
    # Otherwise default to 1893 (exposed port for local testing)
    port = int(os.getenv("MQTT_PORT", "1893"))

    # Wait for MQTT broker to be ready
    import socket
    max_retries = 30
    for i in range(max_retries):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex((host, port))
            sock.close()
            if result == 0:
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        pytest.skip("MQTT broker not available")

    yield {
        "host": host,
        "port": port,
    }


@pytest.fixture
def mqtt_client(mqtt_broker):
    """
    Provide MQTT client connected to test broker.

    This fixture:
    - Creates a fresh MQTT client for each test
    - Connects to the broker
    - Disconnects and cleans up after the test
    """
    import paho.mqtt.client as mqtt_client_lib

    client = mqtt_client_lib.Client()
    client.connect(mqtt_broker["host"], mqtt_broker["port"], 60)
    client.loop_start()

    yield client

    # Cleanup
    client.loop_stop()
    client.disconnect()

