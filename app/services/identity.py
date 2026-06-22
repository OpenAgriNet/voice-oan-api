import hashlib
import os
import re
from typing import Optional

# JWT claim names that may carry the farmer's phone number. The vendor's exact
# claim can be pinned via JWT_PHONE_CLAIM; otherwise we try these in order.
_PHONE_CLAIM_CANDIDATES = ["phone", "phone_number", "phoneNumber", "mobile", "msisdn", "sub"]


def normalize_phone(phone: str) -> Optional[str]:
    """Normalize Indian phone number to +91XXXXXXXXXX format."""
    digits = re.sub(r'\D', '', phone)
    if len(digits) == 10:
        digits = '91' + digits
    elif len(digits) == 11 and digits.startswith('0'):
        digits = '91' + digits[1:]
    if len(digits) != 12 or not digits.startswith('91'):
        return None
    return '+' + digits


def resolve_user_id(phone: str) -> Optional[str]:
    """Return sha256 of normalized phone, or None if phone is invalid."""
    if not phone:
        return None
    normalized = normalize_phone(phone)
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode()).hexdigest()


def extract_phone_from_claims(claims: dict) -> Optional[str]:
    """Pull the raw phone string from decoded JWT claims.

    Honors JWT_PHONE_CLAIM if set; otherwise tries common claim names.
    """
    if not claims:
        return None
    pinned = os.getenv("JWT_PHONE_CLAIM")
    candidates = [pinned, *_PHONE_CLAIM_CANDIDATES] if pinned else _PHONE_CLAIM_CANDIDATES
    for key in candidates:
        if key and claims.get(key):
            return str(claims[key])
    return None


def user_id_from_claims(claims: dict) -> Optional[str]:
    """Decoded JWT claims -> hashed user_id (or None if no valid phone)."""
    return resolve_user_id(extract_phone_from_claims(claims) or "")


def to_memory_user_id(value: Optional[str]) -> Optional[str]:
    """Normalize an identifier into the memory user_id.

    If `value` looks like a phone number, hash it (so raw phone never reaches
    Qdrant). If it's already an opaque/hashed id, use it as-is. Empty/anonymous
    -> None (memory skipped).
    """
    if not value or value == "anonymous":
        return None
    hashed = resolve_user_id(value)  # non-None only when value is a valid phone
    return hashed or value
