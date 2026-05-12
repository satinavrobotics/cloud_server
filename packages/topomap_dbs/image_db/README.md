# Image Database Service

Image database service using MinIO object storage for topological map images.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                  Image Database Service                  │
│                                                           │
│  ┌──────────────┐                                        │
│  │   FastAPI    │  REST API (port 6002)                  │
│  │   (main.py)  │                                        │
│  └──────┬───────┘                                        │
│         │                                                 │
│  ┌──────▼────────────────┐                               │
│  │  ImageDatabaseService │  Business Logic               │
│  │     (server.py)       │                               │
│  └──────┬────────────────┘                               │
│         │                                                 │
│  ┌──────▼────────────────┐                               │
│  │   MinIO Client        │  Object Storage               │
│  └──────┬────────────────┘                               │
└─────────┼───────────────────────────────────────────────┘
          │
          ▼
    ┌─────────────┐
    │    MinIO    │  Persistent Object Storage
    │   Server    │  (S3-compatible)
    └─────────────┘
```

## Storage Structure

Images are organized by maps and nodes, with each map having its own bucket:

```
MinIO
├── map-default/
│   ├── node_001/images/
│   │   ├── front.jpg
│   │   └── back.jpg
│   └── node_002/images/
│       └── front.jpg
├── map-warehouse/
│   ├── node_001/images/
│   │   ├── front.jpg
│   │   └── back.jpg
│   └── node_002/images/
│       └── front.jpg
└── map-factory/
    └── node_001/images/
        └── front.jpg
```

**Path Structure:** `/{map_id}/{node_id}/images/{image_id}`

## Key Features

✅ **Simple Structure**: Images organized by map_id and image_id  
✅ **S3-Compatible**: Uses MinIO (S3-compatible object storage)  
✅ **REST API**: Simple HTTP interface for all operations  
✅ **Client Library**: Easy-to-use Python client  
✅ **Metadata Support**: Store metadata with images  
✅ **Scalable**: MinIO can scale to petabytes of data  

## Files

```
packages/topomap_dbs/image_db/
├── main.py              # FastAPI application (REST API)
├── server.py            # ImageDatabaseService (business logic)
├── client.py            # ImageDatabaseClient (client library)
├── requirements.txt     # Python dependencies
├── Dockerfile           # Docker image
├── BUILD                # Bazel build file
└── README.md            # This file
```

## Quick Start

### 1. Start MinIO

```bash
docker run -d \
  --name minio \
  -p 9000:9000 \
  -p 9001:9001 \
  -e MINIO_ROOT_USER=minioadmin \
  -e MINIO_ROOT_PASSWORD=minioadmin \
  minio/minio server /data --console-address ":9001"
```

MinIO Console: http://localhost:9001 (login: minioadmin/minioadmin)

### 2. Start Image Database Service

```bash
cd packages/topomap_dbs/image_db
python main.py --port 6002
```

### 3. Use the Client

```python
from packages.topomap_dbs.image_db.client import ImageDatabaseClient

# Connect to service
client = ImageDatabaseClient(url="http://localhost:6002")

# Store an image
with open("image.jpg", "rb") as f:
    image_data = f.read()
    client.store_image(
        image_data=image_data,
        image_id="front.jpg",
        node_id="node_001",
        map_id="warehouse"
    )

# Retrieve an image
image_data = client.get_image(
    image_id="front.jpg",
    node_id="node_001",
    map_id="warehouse"
)
if image_data:
    with open("retrieved.jpg", "wb") as f:
        f.write(image_data)

# List all images in a map
images = client.list_images(map_id="warehouse")
for img in images:
    print(f"{img['node_id']}/images/{img['image_id']}")

# List images for a specific node
node_images = client.list_node_images(node_id="node_001", map_id="warehouse")
print(f"Node images: {node_images}")

# Get statistics
stats = client.get_stats()
print(f"Total maps: {stats['total_maps']}")
print(f"Total images: {stats['total_images']}")
print(f"Total nodes: {stats['total_nodes']}")
```

## API Endpoints

### Health & Stats

- `GET /health` - Health check
- `GET /stats?map_id={map_id}&node_id={node_id}` - Get statistics

### Image Operations

- `POST /images` - Upload an image (requires: file, image_id, node_id, optional: map_id)
- `GET /images/{image_id}?node_id={node_id}&map_id={map_id}` - Download an image
- `DELETE /images/{image_id}?node_id={node_id}&map_id={map_id}` - Delete an image
- `GET /images?node_id={node_id}&map_id={map_id}` - List images (node_id optional for filtering)

### Node Operations

- `DELETE /nodes/{node_id}/images?map_id={map_id}` - Delete all images for a node

### Map Operations

- `GET /maps` - List all maps
- `DELETE /maps/{map_id}` - Delete a map and all its images

## Configuration

Environment variables:

```bash
MINIO_HOST=localhost          # MinIO server host
MINIO_PORT=9000               # MinIO server port
MINIO_ACCESS_KEY=minioadmin   # MinIO access key
MINIO_SECRET_KEY=minioadmin   # MinIO secret key
MINIO_SECURE=false            # Use HTTPS (true/false)
DEFAULT_MAP_ID=default        # Default map ID
```

## Usage Examples

### Store Image with Metadata

```python
client = ImageDatabaseClient(url="http://localhost:6002")

with open("camera_front.jpg", "rb") as f:
    image_data = f.read()

client.store_image(
    image_data=image_data,
    image_id="camera_front.jpg",
    node_id="node_001",
    map_id="warehouse",
    content_type="image/jpeg",
    metadata={
        "camera": "front",
        "timestamp": "2024-01-15T10:30:00Z"
    }
)
```

### List All Images in a Map

```python
# List all images
all_images = client.list_images(map_id="warehouse")

# List images with prefix
node_001_images = client.list_images(
    map_id="warehouse",
    prefix="node_001"
)
```

### Delete a Map

```python
# Delete map and all its images
client.delete_map(map_id="old_warehouse")
```

## Integration with Graph Database

The Image Database Service is designed to work alongside the Graph Database Service:

```python
from packages.topomap_dbs.graph_db.client import GraphDatabaseClient
from packages.topomap_dbs.image_db.client import ImageDatabaseClient

# Initialize clients
graph_client = GraphDatabaseClient(url="http://localhost:6001")
image_client = ImageDatabaseClient(url="http://localhost:6002")

# Create a map
map_id = "warehouse"
graph_client.create_map(map_id)

# Add a node with images
node_id = 1
graph_client.add_node(
    map_id=map_id,
    node_id=node_id,
    x=10.0,
    y=20.0,
    theta=0.0,
    metadata={
        "has_images": True,
        "image_ids": ["node_001_front", "node_001_back"]
    }
)

# Store images for the node
for camera in ["front", "back"]:
    image_id = f"node_{node_id:03d}_{camera}"
    with open(f"{image_id}.jpg", "rb") as f:
        image_client.store_image(
            image_data=f.read(),
            image_id=image_id,
            map_id="warehouse"
        )
```

## Docker Deployment

### Build Image

```bash
cd packages/topomap_dbs/image_db
docker build -t image-db-service:latest .
```

### Run Container

```bash
docker run -d \
  --name image-db-service \
  -p 6002:6002 \
  -e MINIO_HOST=minio \
  -e MINIO_PORT=9000 \
  -e MINIO_ACCESS_KEY=minioadmin \
  -e MINIO_SECRET_KEY=minioadmin \
  --network mission-dispatch-network \
  image-db-service:latest
```

## Performance

MinIO is designed for high-performance object storage:

| Operation | Typical Latency | Notes |
|-----------|----------------|-------|
| **Upload** | 10-50ms | Depends on image size |
| **Download** | 5-30ms | Depends on image size |
| **List** | 5-20ms | Fast metadata queries |
| **Delete** | 5-15ms | Fast deletion |

## Troubleshooting

### Connection Issues

```bash
# Check if MinIO is running
docker ps | grep minio

# Check MinIO logs
docker logs minio

# Test MinIO connection
curl http://localhost:9000/minio/health/live
```

### Bucket Issues

```bash
# List buckets using MinIO client
mc alias set local http://localhost:9000 minioadmin minioadmin
mc ls local
```

## Dependencies

- **minio**: MinIO Python SDK
- **fastapi**: Web framework
- **uvicorn**: ASGI server
- **pydantic**: Data validation
- **python-multipart**: File upload support

## License

Same as the parent project.

