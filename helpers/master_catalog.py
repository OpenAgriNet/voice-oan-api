"""Read docs-pipeline's scheme catalog snapshot from the shared Redis instance."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from langfuse import observe

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
        host = settings.master_catalog_redis_host or settings.redis_host
        port = int(settings.master_catalog_redis_port.strip() or settings.redis_port)
        db = int(settings.master_catalog_redis_db.strip() or settings.redis_db)
        _redis_client = redis.Redis(
            host=host,
            port=port,
            db=db,
            password=settings.master_catalog_redis_password or None,
            decode_responses=True,
            socket_connect_timeout=settings.redis_socket_connect_timeout,
            socket_timeout=settings.redis_socket_timeout,
        )
        logger.info("Master catalog Redis: host=%s port=%s db=%s", host, port, db)
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
        key = _redis_key_for_tier(tier)
        raw = client.get(key)
        if not raw:
            logger.warning("Master catalog snapshot missing in Redis (tier=%s key=%s)", tier, key)
            return None
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            logger.warning("Master catalog snapshot is not a JSON object (tier=%s key=%s)", tier, key)
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


@observe(name="tool:master_catalog_redis_check", as_type="tool", capture_output=False)
def get_vector_schemes_prompt_block() -> dict[str, Any]:
    # Runs once per turn while building the system prompt; the span makes the
    # Redis read visible in Langfuse.
    snapshot = get_master_catalog_snapshot()
    prompt = (snapshot or {}).get("prompt") if snapshot else None
    if not isinstance(prompt, dict):
        prompt = {
            "vector_schemes_bullets": "",
            "vector_schemes_identifiers": "",
            "vector_scheme_count": 0,
        }
    try:
        from app.observability.langfuse_client import safe_update_current_span

        entries = get_vector_scheme_entries(snapshot) if snapshot else []
        tier = _tier_for_environment()
        safe_update_current_span(
            input={"tier": tier, "redis_key": _redis_key_for_tier(tier)},
            output={
                "redis_snapshot_found": snapshot is not None,
                "redis_snapshot_version": (snapshot or {}).get("version"),
                "redis_snapshot_updated_at": (snapshot or {}).get("updated_at"),
                "redis_entry_count": len((snapshot or {}).get("entries") or []),
                "vector_scheme_count": prompt.get("vector_scheme_count"),
                "vector_scheme_codes": [entry.get("code") for entry in entries],
            },
        )
    except Exception as exc:
        logger.debug("Master catalog span update skipped: %s", exc)
    return prompt