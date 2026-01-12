# Voice OAN API

An AI-powered agricultural assistant API that provides real-time, streaming responses to agricultural queries. Supports multiple languages (Hindi, Marathi, English) and offers a wide range of agricultural services including weather forecasts, market prices, scheme information, and more.

## Technology Stack

- **Framework**: FastAPI, Uvicorn, Gunicorn
- **AI/LLM**: Pydantic AI, OpenAI/Azure OpenAI/vLLM
- **Cache & Search**: Redis, Marqo (Vector Search)
- **Database**: PostgreSQL (SQLAlchemy)
- **Authentication**: JWT (PyJWT)
- **Monitoring**: Logfire
- **Infrastructure**: Docker, Supervisor
- **Language**: Python 3.10+

## Documentation

- [Voice API Documentation](./docs/VOICE_API_DOCUMENTATION.md) - Complete API reference and usage guide

### Prerequisites

- Docker and Docker Compose
- Python 3.8+ (for local development)

### Docker Setup

#### 1. Create a Docker Network
```bash
docker network create svanetwork
```

#### 2. Run Redis Stack
```bash
docker run -d --name redis-stack --network svanetwork -p 6379:6379 -p 8001:8001 redis/redis-stack:latest
```

#### 3. Run Marqo (Vector Search)
```bash
docker run --name marqo -p 8882:8882 \
    -e MARQO_MAX_CONCURRENT_SEARCH=50 \
    -e VESPA_POOL_SIZE=50 \
    marqoai/marqo:latest
```

#### 4. Start the Application
```bash
docker compose up --build --force-recreate --detach
```

#### 5. View Logs
```bash
docker logs -f sva_app
```

### Stop the Application
```bash
docker compose down --remove-orphans
```

## Development

### Installation

1. Clone the repository
2. Install dependencies:
```bash
pip install -r requirements.txt
```

### Running Locally

See the [API Documentation](./docs/VOICE_API_DOCUMENTATION.md) for detailed information about endpoints, request parameters, and response formats.

## Maintenance

### Clean Up Docker Volumes
```bash
docker system prune -a --volumes
```

### Restart Services
```bash
docker compose down --remove-orphans
docker compose up --build --force-recreate --detach
```

