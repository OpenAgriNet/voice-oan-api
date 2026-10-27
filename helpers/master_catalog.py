"""Read docs-pipeline's scheme catalog snapshot from the shared Redis instance."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_REDIS_KEY_PREFIX = "master-catalog"
_DEV_ENVIRONMENTS = ("local", "dev", "development")
_redis_client = None
_redis_client_initialized = False
_last_seen_version: dict[str, Optional[int]] = {}


def _settings():
    from app.config import settings

    return settings


def _get_redis_client():
    global _redis_client, _redis_client_initialized
    if _redis_client_initialized:
        return _redis_client

    _redis_client_initialized = True
    try:
        import redis

        settings = _settings()
        _redis_client = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            decode_responses=True,
            socket_connect_timeout=settings.redis_socket_connect_timeout,
            socket_timeout=settings.redis_socket_timeout,
        )
    except Exception as exc:
        logger.warning("Could not create master catalog Redis client: %s", exc)
        _redis_client = None
    return _redis_client


def _tier_for_environment() -> str:
    return "dev" if _settings().environment.strip().lower() in _DEV_ENVIRONMENTS else "live"


def _redis_key_for_tier(tier: str) -> str:
    return _settings().master_catalog_redis_key or f"{_REDIS_KEY_PREFIX}:{tier}:snapshot"


def get_master_catalog_snapshot(tier: Optional[str] = None) -> Optional[dict[str, Any]]:
    tier = tier or _tier_for_environment()
    client = _get_redis_client()
    if client is None:
        return None

    try:
        raw = client.get(_redis_key_for_tier(tier))
        payload = json.loads(raw) if raw else None
        if not isinstance(payload, dict):
            return None
        _log_if_new_sync(tier, payload)
        return payload
    except Exception as exc:
        logger.warning("Master catalog snapshot read failed (tier=%s): %s", tier, exc)
        return None


def _log_if_new_sync(tier: str, payload: dict[str, Any]) -> None:
    version = payload.get("version")
    if _last_seen_version.get(tier) == version:
        return
    _last_seen_version[tier] = version

    entries = payload.get("entries")
    entries = entries if isinstance(entries, list) else []
    logger.info(
        "Scheme catalog synced: tier=%s version=%s updated_at=%s entry_count=%d",
        tier,
        version,
        payload.get("updated_at"),
        len(entries),
    )


def get_vector_scheme_entries(
    snapshot: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    snapshot = snapshot if snapshot is not None else get_master_catalog_snapshot()
    if not snapshot:
        return []
    entries = snapshot.get("entries")
    if not isinstance(entries, list):
        return []
    return [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("tool_name") == "search_schemes"
    ]


def get_vector_schemes_prompt_block() -> dict[str, Any]:
    snapshot = get_master_catalog_snapshot()
    prompt = (snapshot or {}).get("prompt") if snapshot else None
    if not isinstance(prompt, dict):
        return {
            "vector_schemes_bullets": "",
            "vector_schemes_identifiers": "",
            "vector_scheme_count": 0,
        }
    return prompt