"""
Core cache instance configuration using Redis and aiocache.

This module provides the cache instance that other parts of the application can use.
Uses enhanced Redis configuration with connection pooling and timeouts.
"""
from aiocache import Cache
from aiocache.serializers import JsonSerializer
from redis.asyncio import Redis
from app.config import settings
from helpers.utils import get_logger

logger = get_logger(__name__)

# Configure the cache instance with enhanced settings from Django
cache = Cache(
    Cache.REDIS,
    endpoint=settings.redis_host,
    port=settings.redis_port,
    db=settings.redis_db,
    serializer=JsonSerializer(),
    ttl=settings.default_cache_ttl,
    # Enhanced connection settings
    timeout=settings.redis_socket_timeout,
    pool_max_size=settings.redis_max_connections,
    # Add key prefix support
    key_builder=lambda key, namespace: f"{settings.redis_key_prefix}{namespace}:{key}" if namespace else f"{settings.redis_key_prefix}{key}",
)

redis_client = Redis(
    host=settings.redis_host,
    port=settings.redis_port,
    db=settings.redis_db,
    socket_connect_timeout=settings.redis_socket_connect_timeout,
    socket_timeout=settings.redis_socket_timeout,
    max_connections=settings.redis_max_connections,
    retry_on_timeout=settings.redis_retry_on_timeout,
    decode_responses=True,
)


def build_cache_key(key: str, namespace: str | None = None) -> str:
    if namespace:
        return f"{settings.redis_key_prefix}{namespace}:{key}"
    return f"{settings.redis_key_prefix}{key}"


async def try_reserve(key: str, namespace: str, ttl: int) -> bool:
    """Atomically reserve a key (Redis SET NX). Returns True if newly reserved
    (caller owns it and should proceed), False if a reservation already exists
    (caller should skip). On a cache error, returns True (proceed UNGUARDED) — a
    booking must not be blocked by a transient cache blip. Makes side-effecting
    tools safe against concurrent duplicate submits + fallback re-runs across
    containers (shared Redis)."""
    try:
        await cache.add(key, True, ttl=ttl, namespace=namespace)
        return True
    except ValueError:
        return False
    except Exception as e:  # cache unavailable -> fail-open on the guard
        logger.warning("Reservation add failed (%s:%s): %s; proceeding unguarded", namespace, key, str(e))
        return True


async def release_reservation(key: str, namespace: str) -> None:
    """Release a reservation so a genuine retry can proceed (e.g. the booking API
    call failed). Best-effort."""
    try:
        await cache.delete(key, namespace=namespace)
    except Exception as e:
        logger.warning("Reservation release failed (%s:%s): %s", namespace, key, str(e))

logger.info(
    f"Cache configured with Redis at {settings.redis_host}:{settings.redis_port} "
    f"(DB: {settings.redis_db}, Prefix: {settings.redis_key_prefix}, "
    f"Max Connections: {settings.redis_max_connections})"
)
