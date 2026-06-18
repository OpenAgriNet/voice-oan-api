from fastapi import APIRouter, HTTPException, status
from app.utils import cache
from app.config import settings
import time
import os
import asyncio
import httpx
from typing import Dict, Any

router = APIRouter(prefix="/health", tags=["health"])

# Track when the application started
START_TIME = time.time()

async def check_cache_connection() -> Dict[str, Any]:
    """Check Redis cache connection"""
    try:
        test_key = "health_check_test"
        test_value = "test"
        await cache.set(test_key, test_value, ttl=5)
        cached_value = await cache.get(test_key)
        return {
            "status": "healthy" if cached_value == test_value else "unhealthy",
            "latency_ms": 0  # TODO: Add actual latency measurement
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e)
        }

@router.get("/live", status_code=status.HTTP_200_OK)
async def liveness():
    """
    Liveness probe - simple check to see if the application is running
    Used by Kubernetes to know when to restart the pod
    """
    return {"status": "alive"}

@router.get("/ready", status_code=status.HTTP_200_OK)
async def readiness():
    """
    Readiness probe - checks if the application is ready to handle traffic
    Used by Kubernetes to know when to send traffic to the pod
    """
    cache_health = await check_cache_connection()
    
    if cache_health["status"] != "healthy":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "not ready", "cache": cache_health}
        )
    
    return {"status": "ready", "cache": cache_health}

@router.get("/", status_code=status.HTTP_200_OK)
async def health_check():
    """
    Health check that includes:
    - Application metadata (version, uptime)
    - Service dependencies (Redis cache)
    """
    cache_health = await check_cache_connection()
    uptime_seconds = int(time.time() - START_TIME)
    
    health_status = {
        "app": {
            "name": settings.app_name,
            "environment": settings.environment,
            "uptime_seconds": uptime_seconds
        },
        "dependencies": {
            "cache": cache_health
        }
    }
    
    # If any critical dependency is unhealthy, return 503
    if cache_health["status"] != "healthy":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=health_status
        )
    
    return health_status


async def _check_bap_endpoint() -> Dict[str, Any]:
    """Check the BAP (Beckn Application Platform) endpoint reachability."""
    bap_endpoint = os.getenv("BAP_ENDPOINT")
    if not bap_endpoint:
        return {"status": "misconfigured", "error": "BAP_ENDPOINT env var not set"}
    start = time.monotonic()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(bap_endpoint, timeout=5.0)
        latency_ms = round((time.monotonic() - start) * 1000)
        # BAP may return 4xx for GET on an action endpoint — reachable is enough
        return {"status": "healthy", "http_status": response.status_code, "latency_ms": latency_ms}
    except httpx.TimeoutException:
        return {"status": "unhealthy", "error": "timeout"}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


async def _check_marqo() -> Dict[str, Any]:
    """Check Marqo vector search service health."""
    endpoint = os.getenv("MARQO_ENDPOINT_URL")
    if not endpoint:
        return {"status": "misconfigured", "error": "MARQO_ENDPOINT_URL env var not set"}
    start = time.monotonic()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{endpoint.rstrip('/')}/health", timeout=5.0)
        latency_ms = round((time.monotonic() - start) * 1000)
        healthy = response.status_code == 200
        return {
            "status": "healthy" if healthy else "unhealthy",
            "http_status": response.status_code,
            "latency_ms": latency_ms,
        }
    except httpx.TimeoutException:
        return {"status": "unhealthy", "error": "timeout"}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


async def _check_mapbox() -> Dict[str, Any]:
    """Check Mapbox geocoding API reachability."""
    token = os.getenv("MAPBOX_API_TOKEN")
    if not token:
        return {"status": "misconfigured", "error": "MAPBOX_API_TOKEN env var not set"}
    # Use a lightweight token-validation endpoint
    url = f"https://api.mapbox.com/tokens/v2?access_token={token}"
    start = time.monotonic()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=5.0)
        latency_ms = round((time.monotonic() - start) * 1000)
        healthy = response.status_code == 200
        return {
            "status": "healthy" if healthy else "unhealthy",
            "http_status": response.status_code,
            "latency_ms": latency_ms,
        }
    except httpx.TimeoutException:
        return {"status": "unhealthy", "error": "timeout"}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


@router.get("/tools", status_code=status.HTTP_200_OK)
async def tools_health_check():
    """
    Check the health of all external tool APIs:
    - BAP endpoint (mandi, weather, warehouse, scheme_info, agri_services, staff_contact)
    - Marqo (search / vector DB)
    - Mapbox (maps / geocoding)
    """
    bap_result, marqo_result, mapbox_result = await asyncio.gather(
        _check_bap_endpoint(),
        _check_marqo(),
        _check_mapbox(),
    )

    tools = {
        "bap_endpoint": bap_result,
        "marqo": marqo_result,
        "mapbox": mapbox_result,
    }

    overall_healthy = all(v["status"] == "healthy" for v in tools.values())
    response_body = {"status": "healthy" if overall_healthy else "degraded", "tools": tools}

    if not overall_healthy:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=response_body,
        )

    return response_body
