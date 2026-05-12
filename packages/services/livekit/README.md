# LiveKit Service

A microservice for generating LiveKit access tokens to enable video conferencing capabilities in the robot fleet management system.

## Overview

The LiveKit service provides JWT token generation for LiveKit video conferencing. It allows participants to join LiveKit rooms with configurable permissions for publishing/subscribing to video, audio, and data tracks.

## Features

- **Token Generation**: Creates JWT access tokens for LiveKit room access
- **Configurable Permissions**: Control publish/subscribe permissions per token
- **Flexible TTL**: Configurable token time-to-live
- **Health Monitoring**: Built-in health check and statistics endpoints
- **RESTful API**: FastAPI-based HTTP endpoints

## Architecture

The service follows the standard microservice pattern:

- `server.py`: Core business logic for token generation
- `main.py`: FastAPI application with HTTP endpoints
- `client.py`: Client library for other services to interact with this service
- `BUILD`: Bazel build configuration
- `Dockerfile`: Container image definition

## API Endpoints

### Create Token

**POST** `/api/createToken`

Generate a LiveKit access token for a participant to join a room.

**Request Body:**
```json
{
  "participantName": "user123",
  "roomName": "quickstart-room",
  "ttl": 36000,
  "metadata": "optional metadata",
  "canPublish": true,
  "canSubscribe": true,
  "canPublishData": true
}
```

**Response:**
```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "ttl": 36000,
  "server_url": "wss://your-livekit-server.livekit.cloud",
  "participant_name": "user123",
  "room_name": "quickstart-room"
}
```

### Health Check

**GET** `/health`

Returns the health status of the service.

**Response:**
```json
{
  "status": "healthy",
  "service": "livekit",
  "timestamp": "2024-01-01T00:00:00Z"
}
```

### Statistics

**GET** `/stats`

Returns service statistics and configuration.

**Response:**
```json
{
  "service": "livekit",
  "server_url": "wss://your-livekit-server.livekit.cloud",
  "default_ttl": 36000
}
```

## Configuration

The service is configured via environment variables with defaults from the development configuration:

| Variable | Description | Default |
|----------|-------------|---------|
| `LIVEKIT_API_KEY` | LiveKit API key | `APItMSWMP3TVdfZ` |
| `LIVEKIT_API_SECRET` | LiveKit API secret | `5aTOwvNSIY8Jvi5lEeBrffpb7P8n6MchhdysejZ8gcwD` |
| `LIVEKIT_SERVER_URL` | LiveKit server WebSocket URL | `wss://satinav-b22o2lgk.livekit.cloud` |
| `LIVEKIT_TTL` | Default token time-to-live in seconds | 36000 (10 hours) |
| `LIVEKIT_PORT` | Service port | 8006 |

**Note:** The default values are configured for the Satinav development LiveKit server. Override these environment variables for production or different environments.

## Usage

### Running Locally

```bash
# Run with default configuration (Satinav dev server)
python -m packages.services.livekit.main --port 8006 --host 0.0.0.0

# Or override with custom environment variables
export LIVEKIT_API_KEY="your-api-key"
export LIVEKIT_API_SECRET="your-api-secret"
export LIVEKIT_SERVER_URL="wss://your-server.livekit.cloud"
python -m packages.services.livekit.main --port 8006 --host 0.0.0.0
```

### Using Docker

```bash
docker run -p 8006:8006 \
  -e LIVEKIT_API_KEY="your-api-key" \
  -e LIVEKIT_API_SECRET="your-api-secret" \
  -e LIVEKIT_SERVER_URL="wss://your-server.livekit.cloud" \
  livekit_service:latest
```

### Using Docker Compose

The service is included in the mission dispatch services stack and will use default values automatically:

```bash
# Start all services with default configuration
docker-compose -f docker_compose/mission_dispatch_services_dev.yaml up

# Or override with custom environment variables
export LIVEKIT_API_KEY="your-api-key"
export LIVEKIT_API_SECRET="your-api-secret"
export LIVEKIT_SERVER_URL="wss://your-server.livekit.cloud"
docker-compose -f docker_compose/mission_dispatch_services_dev.yaml up
```

### Using the Client Library

```python
from packages.services.livekit.client import LiveKitClient

# Initialize client
client = LiveKitClient(url="http://localhost:8006")

# Create a token
result = client.create_token(
    participant_name="user123",
    room_name="my-room",
    ttl=3600,
    can_publish=True,
    can_subscribe=True
)

print(f"Token: {result['token']}")
print(f"Server URL: {result['server_url']}")
```

## Integration with API Delegation Service

The LiveKit service is integrated with the API Delegation Service, which provides a unified gateway for all microservices. Clients can access the LiveKit token creation endpoint through the API gateway:

**POST** `http://localhost:8000/api/createToken`

This endpoint proxies requests to the LiveKit service and returns the same response format.

## Dependencies

- `livekit`: LiveKit Python SDK for token generation
- `fastapi`: Web framework
- `uvicorn`: ASGI server
- `pydantic`: Data validation
- `requests`: HTTP client library

## Development

### Building with Bazel

```bash
bazel build //packages/services/livekit:livekit_service
```

### Running Tests

```bash
# TODO: Add tests
```

## Security Considerations

- **API Keys**: Keep `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` secure and never commit them to version control
- **Token TTL**: Use appropriate TTL values based on your use case
- **Permissions**: Configure publish/subscribe permissions based on participant roles
- **HTTPS**: Use HTTPS in production to protect tokens in transit

## Troubleshooting

### Service won't start

- Verify all required environment variables are set
- Check that port 8006 is not already in use
- Ensure LiveKit credentials are valid

### Token generation fails

- Verify `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` are correct
- Check that `LIVEKIT_SERVER_URL` is accessible
- Review service logs for detailed error messages

## References

- [LiveKit Documentation](https://docs.livekit.io/)
- [LiveKit Python SDK](https://github.com/livekit/python-sdks)
- [JWT Token Authentication](https://docs.livekit.io/guides/access-tokens/)

