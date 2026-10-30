# Bharat Vistaar Voice AI

## Python runtime

Docker and GitHub Actions use **Python 3.15**. Use the same version for local
development.

Python 3.15 wheel compatibility pins match the main API: pandas 3.1.0,
pydantic 2.14.1 (which selects pydantic-core 2.50.1), rapidfuzz 3.14.6, and
zope-interface 8.7. Existing Pydantic AI and OpenTelemetry constraints are
unchanged.

Docker and CI install `requirements-docker.in`, which includes
`requirements.txt` and selects the official **Torch 2.14.0+cpu** CPython 3.15
Linux amd64 wheel with its SHA256. This avoids the missing Torch distribution
that causes pip to backtrack through `sentence-transformers` releases. The
platform-specific wheel is kept out of the general requirements file.

Docker and CI build only `marisa-trie==1.4.1` and `PyYAML==6.0.3` from source,
because they do not publish Python 3.15 wheels. They install C++/libyaml headers
and hash-locked build tools from `requirements-build.lock` first, then disable
build isolation. Every other application dependency must have a compatible wheel.
Both paths run `pip check`, import Torch and SentenceTransformer, verify that
Torch is CPU-only, and check native imports/language data. Docker does this
before downloading the embedding model.

Unlike the main API, this service still uses a partially pinned
`requirements.txt`, not a full application hash lock. Only build tools use
`--require-hashes`; the explicit Torch URL also verifies its SHA256.
Remaining application dependency resolution is the responsibility of CI.

No packages or images were installed locally for this change. Source builds,
dependency resolution, and the image smoke check must pass in CI before deployment.

## Delete all volumes
```
docker system prune -a --volumes
```

----
# Create a new network
```
docker network create svanetwork
```
# Run seperate Redis
```
docker run -d --name redis-stack --network svanetwork -p 6379:6379 -p 8001:8001 redis/redis-stack:latest
```
# Docker Setup
```
docker compose up --build --force-recreate --detach
```
# Stop
```
docker compose down --remove-orphans
```
docker compose down --remove-orphans
docker compose up --build --force-recreate --detach
docker logs -f sva_app
# Marqo

```
docker run --name marqo -p 8882:8882 \
    -e MARQO_MAX_CONCURRENT_SEARCH=50 \
    -e VESPA_POOL_SIZE=50 \
    marqoai/marqo:latest
```


TODO:

1. get  it up running
2. it should run with supervised
3. celery implementatioin
3. Docker setup
4. requirements update
5. authentication

finally get it up running

## Voice Tool Integrations

The cross-network scheme, status, fertilizer, and seed tools use the following environment settings:

- `BAP_ENDPOINT`, `BAP_ID`, and `BAP_URI` for the Vistaar BAP.
- `BPP_ID` and `BPP_URI` for status, GFR, and SATHI stock requests.
- `MH_BPP_ID` for MahaVistaar's targeted NDKSP scheme requests.
- `SATHI_MASTER_BASE_URL` and `SATHI_MASTER_API_KEY` for SATHI crop group and crop lookup.
- `AMUL_SCHEME_DOMAIN` and `AMUL_SEARCH_TTL` are optional AmulVistaar overrides.

Scheme search reads the docs-pipeline catalog snapshot from the Redis instance configured by `MASTER_CATALOG_REDIS_HOST`, `MASTER_CATALOG_REDIS_PORT`, `MASTER_CATALOG_REDIS_DB`, and `MASTER_CATALOG_REDIS_PASSWORD`. docs-pipeline writes the snapshot only to bharat-oan-api's Redis, so point these at that instance; any that are unset fall back to `REDIS_HOST`, `REDIS_PORT`, and `REDIS_DB`. The voice cache keeps using `REDIS_*`. Development environments read `master-catalog:dev:snapshot`; other environments read `master-catalog:live:snapshot`. Set `MASTER_CATALOG_REDIS_KEY` only when the snapshot uses a custom key.

Keep API keys and credentials in the deployment environment; do not commit them.