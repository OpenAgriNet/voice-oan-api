import hashlib
import re
from typing import Optional


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
