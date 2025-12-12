# sunbird-va-api

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