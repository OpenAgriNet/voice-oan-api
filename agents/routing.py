"""
Session-sticky canary routing between the Gemma vLLM model and the default
(Azure/OpenAI) model.

A session is deterministically bucketed into the canary group by hashing its
session_id (stable across requests and worker processes, unlike Python's
salted built-in hash()). Sessions in the canary group are routed to Gemma
only while its vLLM engine has spare capacity (per /metrics); otherwise, and
for all other sessions, requests go to the default model.
"""
import hashlib
import logging
import os
import re

import httpx
from dotenv import load_dotenv

from agents.models import GEMMA_MODEL, LLM_AGRINET_MODEL
from app.core.cache import cache

load_dotenv()

logger = logging.getLogger(__name__)

GEMMA_CANARY_PERCENT = int(os.getenv('GEMMA_CANARY_PERCENT', '10'))
GEMMA_MAX_CONCURRENCY = int(os.getenv('AGRINET_GEMMA_MAX_CONCURRENCY', '10'))
GEMMA_METRICS_URL = os.getenv(
    'AGRINET_GEMMA_METRICS_URL',
    re.sub(r'/v1/?$', '', os.getenv('AGRINET_GEMMA_BASE_URL') or '') + '/metrics',
)
GEMMA_METRICS_CACHE_TTL = int(os.getenv('AGRINET_GEMMA_METRICS_CACHE_TTL', '2'))
GEMMA_METRICS_CACHE_KEY = 'gemma_concurrency'

_NUM_RE = re.compile(r'^(vllm:num_requests_running|vllm:num_requests_waiting)\{.*\}\s+([\d.eE+-]+)$')


def is_gemma_candidate(session_id: str) -> bool:
    """Deterministically decide whether a session is in the canary bucket."""
    digest = hashlib.sha256(session_id.encode()).hexdigest()
    return (int(digest, 16) % 100) < GEMMA_CANARY_PERCENT


async def _fetch_gemma_concurrency() -> int | None:
    """Sum of running + waiting requests on the Gemma vLLM engine(s), or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(GEMMA_METRICS_URL)
            response.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        logger.warning(f"Failed to fetch Gemma metrics from {GEMMA_METRICS_URL}: {e}")
        return None

    total = 0
    for line in response.text.splitlines():
        match = _NUM_RE.match(line)
        if match:
            total += int(float(match.group(2)))
    return total


async def get_gemma_concurrency() -> int | None:
    """Cached (short-TTL, shared via Redis) read of Gemma's current concurrency.

    Redis errors are swallowed - a cache outage should degrade to a direct
    metrics fetch per request, not break routing (and therefore voice turns).
    """
    try:
        cached = await cache.get(GEMMA_METRICS_CACHE_KEY)
    except Exception as e:
        logger.warning(f"Gemma concurrency cache read failed: {e}")
        cached = None

    if cached is not None:
        return cached

    concurrency = await _fetch_gemma_concurrency()
    if concurrency is not None:
        try:
            await cache.set(GEMMA_METRICS_CACHE_KEY, concurrency, ttl=GEMMA_METRICS_CACHE_TTL)
        except Exception as e:
            logger.warning(f"Gemma concurrency cache write failed: {e}")
    return concurrency


async def select_model_for_session(session_id: str):
    """Return (model, route_label) for this turn.

    route_label is one of: 'azure' (not in canary bucket, or Gemma unconfigured),
    'gemma' (canary bucket + capacity available), 'azure_fallback' (canary bucket
    but Gemma is at/over capacity or its metrics couldn't be read).
    """
    if GEMMA_MODEL is None or not session_id or not is_gemma_candidate(session_id):
        return LLM_AGRINET_MODEL, 'azure'

    concurrency = await get_gemma_concurrency()
    if concurrency is not None and concurrency < GEMMA_MAX_CONCURRENCY:
        return GEMMA_MODEL, 'gemma'

    return LLM_AGRINET_MODEL, 'azure_fallback'
