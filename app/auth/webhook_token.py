"""Shared-secret auth for inbound partner webhooks.

Not JWT — partners send a static token in ``X-Webhook-Token``. Comparison uses
``hmac.compare_digest`` to avoid timing leaks. When ``WEBHOOK_SHARED_TOKEN`` is
unset or empty, every request is rejected (fail closed).
"""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from app.config import settings
from helpers.utils import get_logger

logger = get_logger(__name__)

WEBHOOK_TOKEN_HEADER = "X-Webhook-Token"


async def require_webhook_token(
    x_webhook_token: str | None = Header(
        default=None,
        alias=WEBHOOK_TOKEN_HEADER,
        description="Shared secret configured as WEBHOOK_SHARED_TOKEN",
    ),
) -> None:
    """FastAPI dependency: require a matching ``X-Webhook-Token`` header."""
    expected = (settings.webhook_shared_token or "").strip()
    if not expected:
        logger.error("WEBHOOK_SHARED_TOKEN is not configured; rejecting webhook")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook auth is not configured",
        )

    provided = (x_webhook_token or "").strip()
    # compare_digest requires equal-length str/bytes; unequal lengths still
    # return False without raising. Empty provided is rejected first so we do
    # not treat a missing header as a failed secret match in logs.
    if not provided:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing webhook token",
        )
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing webhook token",
        )
