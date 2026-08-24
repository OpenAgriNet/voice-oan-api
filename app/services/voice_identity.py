"""Authoritative caller and session identity handling for voice requests.

The telephony ``user_id`` query parameter is routing metadata, not proof of
identity. Farmer data and operations are therefore bound only to a phone claim
from the verified JWT. A caller-supplied phone may agree with that claim for
backwards compatibility, but can never override it.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from fastapi import HTTPException, status

from app.config import settings
from app.core.cache import build_cache_key, redis_client
from helpers.utils import get_logger

logger = get_logger(__name__)

_PHONE_CLAIMS = ("phone", "mobile", "phone_number", "mobile_number")
_ANONYMOUS_IDS = {"", "anon", "anonymous"}


def _normalize_mobile(raw: str) -> str | None:
    value = str(raw or "").strip()
    if not value or value.lower() in _ANONYMOUS_IDS:
        return None
    try:
        uuid.UUID(value)
        return None
    except (ValueError, AttributeError, TypeError):
        pass
    digits = re.sub(r"\D", "", value)
    return digits[-10:] if len(digits) >= 10 else None


@dataclass(frozen=True)
class CallerIdentity:
    mobile: str | None
    subject_id: str
    verified: bool

    @property
    def signed_in(self) -> bool:
        return bool(self.verified and self.mobile)

    @property
    def trace_id(self) -> str:
        """Non-reversible identifier suitable for logs and traces."""
        return self.subject_id[:16]


def _subject_id(user_info: Mapping[str, Any], mobile: str | None) -> str:
    stable_subject = "|".join((
        str(user_info.get("sub") or "").strip(),
        str(mobile or "").strip(),
    )) or "anonymous"
    return hashlib.sha256(stable_subject.encode("utf-8")).hexdigest()


def _jwt_mobile(user_info: Mapping[str, Any]) -> str | None:
    for claim in _PHONE_CLAIMS:
        mobile = _normalize_mobile(str(user_info.get(claim) or ""))
        if mobile:
            return mobile

    # Amul-issued farmer tokens use the phone as ``sub`` as well. Accept it only
    # when it is phone-shaped; UUID/user-name subjects are never treated as phone.
    return _normalize_mobile(str(user_info.get("sub") or ""))


def resolve_caller_identity(
    user_info: Mapping[str, Any] | None,
    requested_user_id: str | None,
) -> CallerIdentity:
    """Resolve identity from verified JWT claims and reject parameter mismatch."""
    claims = user_info or {}
    mobile = _jwt_mobile(claims)
    requested = str(requested_user_id or "").strip()
    requested_mobile = _normalize_mobile(requested)
    requested_is_anonymous = requested.lower() in _ANONYMOUS_IDS

    if requested_mobile and not mobile:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The requested farmer identity is not present in the authenticated token",
        )
    if requested_mobile and mobile and not hmac.compare_digest(requested_mobile, mobile):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The requested farmer identity does not match the authenticated caller",
        )
    if requested and not requested_is_anonymous and not requested_mobile:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="user_id must be anonymous or the authenticated farmer mobile",
        )

    return CallerIdentity(
        mobile=mobile,
        subject_id=_subject_id(claims, mobile),
        verified=bool(claims),
    )


def _session_identity_key(session_id: str) -> str:
    return build_cache_key(f"voice_session_identity:{session_id}")


async def bind_session_identity(session_id: str, identity: CallerIdentity) -> None:
    """Atomically bind a conversation session to its authenticated subject.

    This check is intentionally fail-closed: history and pending operations are
    private data, so a Redis outage must not silently disable ownership checks.
    """
    key = _session_identity_key(session_id)
    try:
        created = await redis_client.set(
            key,
            identity.subject_id,
            ex=settings.beckn_session_identity_ttl_seconds,
            nx=True,
        )
        if created:
            return
        existing = await redis_client.get(key)
    except Exception as exc:
        logger.error("Voice session identity binding unavailable: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session identity service is temporarily unavailable",
        ) from exc

    if not existing or not hmac.compare_digest(str(existing), identity.subject_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This session belongs to a different authenticated caller",
        )

    # Refresh only an already-matching binding.
    await redis_client.expire(key, settings.beckn_session_identity_ttl_seconds)
