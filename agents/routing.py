"""Weighted, Redis-sticky voice routing with transient vLLM capacity deflection."""
import hashlib
import math
import random
import re
from dataclasses import dataclass

import httpx

from agents.model_registry import get_registry
from agents.models import VOICE_USE_CASE
from app.core.cache import cache
from helpers.utils import get_logger

logger = get_logger(__name__)
_NUM_RE = re.compile(
    r"^(vllm:num_requests_running|vllm:num_requests_waiting)(?:\{.*\})?\s+([\d.eE+-]+)(?:\s+\d+)?$"
)


@dataclass(frozen=True)
class VoiceRouteDecision:
    route: str
    source: str


def _route_key(session_id: str) -> str:
    return f"voice:route:{session_id}"


async def _get(key: str):
    try:
        return await cache.get(key)
    except Exception as exc:
        logger.warning("Voice routing cache read failed: %s", exc)
        return None


async def set_session_voice_route(session_id: str, route: str) -> None:
    try:
        await cache.set(
            _route_key(session_id),
            route,
            ttl=get_registry().routing_ttl(VOICE_USE_CASE),
        )
    except Exception as exc:
        logger.warning("Voice routing cache write failed: %s", exc)


def choose_weighted_voice_route(randint_fn=random.randint) -> str:
    roll = randint_fn(1, 100)
    cumulative = 0
    registry = get_registry()
    aliases = registry.aliases(VOICE_USE_CASE)
    for alias, weight in zip(aliases, registry.proportions(VOICE_USE_CASE)):
        cumulative += weight
        if roll <= cumulative:
            return alias
    return aliases[-1]


async def get_concurrency(alias: str) -> float | None:
    registry = get_registry()
    metrics_url = registry.metrics_url(alias)
    cache_key = f"voice:concurrency:{alias}:{hashlib.sha256(metrics_url.encode()).hexdigest()[:16]}"
    cached = await _get(cache_key)
    if isinstance(cached, (int, float)) and math.isfinite(cached) and cached >= 0:
        return cached
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(metrics_url)
            response.raise_for_status()
        values = {}
        for line in response.text.splitlines():
            match = _NUM_RE.match(line)
            if match:
                value = float(match.group(2))
                if not math.isfinite(value) or value < 0:
                    return None
                values[match.group(1)] = values.get(match.group(1), 0) + value
        if len(values) != 2:
            return None
        total = sum(values.values())
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Voice model metrics unavailable for %s: %s", alias, exc)
        return None
    try:
        await cache.set(cache_key, total, ttl=registry.metrics_ttl(alias))
    except Exception as exc:
        logger.warning("Voice metrics cache write failed: %s", exc)
    return total


async def resolve_voice_route(session_id: str, *, has_history: bool = False) -> VoiceRouteDecision:
    registry = get_registry()
    aliases = registry.aliases(VOICE_USE_CASE)
    stored = await _get(_route_key(session_id))
    if stored in aliases:
        route, source = stored, "redis"
    elif has_history:
        route, source = registry.default_alias(VOICE_USE_CASE), "state_repair"
        await set_session_voice_route(session_id, route)
    else:
        route, source = choose_weighted_voice_route(), "session_start_weighted"
        await set_session_voice_route(session_id, route)

    limit = registry.concurrency_limit(route)
    if limit is not None:
        concurrency = await get_concurrency(route)
        if concurrency is None or concurrency >= limit:
            return VoiceRouteDecision(registry.fallback(route), "capacity_deflect")
    return VoiceRouteDecision(route, source)
