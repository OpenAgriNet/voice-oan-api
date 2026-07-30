"""
Demo mock mode — canned tool responses for a single, explicitly listed caller.

Why this exists
---------------
``create_ai_call`` and ``create_health_call`` are real ``POST``s to
``CreateAICall`` / ``CreateHealthCall``. At a public demo table every attendee who
asks to book a technician would dispatch a real AI technician or vet against a
farmer who does not exist. Mocking removes a live write path; it is a safety
control, not a convenience.

``get_farmer_milk_collection_details`` is mocked for a different reason: there is
no real milk data for the demo number, and it is the slowest tool in the stack
(20s deadline timeouts, and it can hang into the 60s nginx cut that drops the
call).

Everything else the demo needs — farmer profile, herd, tags, schemes, loan — is
served from the farmer cache, so it is seeded in Redis rather than mocked here.
``search_documents`` is deliberately left real: it is read-only, it works, and the
grounded answer is the most interesting thing to show.

Safety contract
---------------
Fixtures are returned only when BOTH:
  * ``DEMO_MOCK_ENABLED`` is truthy, and
  * the caller's normalized mobile appears in ``DEMO_MOCK_USER_IDS``.

Either condition failing means the tool runs its real path, unchanged. Both
default to off/empty, so a deploy that forgets the env vars is inert.
"""

import os
from typing import Any, Optional

from helpers.utils import get_logger

logger = get_logger(__name__)


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


DEMO_MOCK_ENABLED = _truthy(os.getenv("DEMO_MOCK_ENABLED", "false"))

# Comma-separated normalized mobiles, e.g. "8035454078". RAYA strips the country
# code, so list the 10-digit form.
DEMO_MOCK_USER_IDS = {
    part.strip()
    for part in os.getenv("DEMO_MOCK_USER_IDS", "").split(",")
    if part.strip()
}


def is_demo_caller(ctx: Any) -> bool:
    """
    True only when mock mode is on AND this caller is explicitly listed.

    Deliberately conservative: any missing context, or a caller not on the list,
    returns False so the real tool path runs untouched.
    """
    if not DEMO_MOCK_ENABLED or not DEMO_MOCK_USER_IDS:
        return False

    deps = getattr(ctx, "deps", None)
    mobile: Optional[str] = getattr(deps, "mobile", None) if deps else None
    if not mobile:
        return False

    # Normalize the way RAYA does: compare on the trailing 10 digits so a
    # country-coded variant still matches its listed form.
    digits = "".join(c for c in str(mobile) if c.isdigit())
    candidates = {digits, digits[-10:]} if len(digits) >= 10 else {digits}
    return bool(candidates & DEMO_MOCK_USER_IDS)


# --- fixtures ---------------------------------------------------------------
# Written to read aloud naturally: numbers spelled the way the voice prompt asks
# for, no markdown, no brackets. These are spoken by TTS, not rendered.


def milk_collection_summary() -> str:
    """Roughly a month of collections, believable fat/SNF and a payment total."""
    return (
        "Here is your milk collection summary for the last thirty days. "
        "You supplied a total of two hundred and eighty four litres, "
        "with an average fat of four point one and average SNF of eight point six. "
        "Your total payment for this period is eleven thousand three hundred and sixty rupees. "
        "The last collection was yesterday evening, seven point five litres, "
        "fat four point two."
    )


def ai_call_booked(species: Any = None) -> str:
    """Mirrors the real tool's success string so downstream handling is identical."""
    animal = "buffalo" if str(species).lower().endswith("buffalo") else "cow"
    return (
        f"Artificial insemination call booked successfully for your {animal}. "
        "Technician Rameshbhai Patel will visit tomorrow morning between "
        "eight and ten. You will get a confirmation call before the visit."
    )


def health_call_booked(case_type: Any = None) -> str:
    """Mirrors the real tool's ticket-number success string."""
    return (
        "Health call booked successfully. Ticket number four four seven two. "
        "Doctor Nileshbhai Patel will visit today between four and six in the evening. "
        "Please keep the animal in the shed and do not milk before the visit."
    )


def log_fixture_served(tool: str, ctx: Any) -> None:
    """Make mocked turns obvious in the logs so nobody mistakes them for real."""
    deps = getattr(ctx, "deps", None)
    session_id = getattr(deps, "session_id", None) if deps else None
    logger.warning(
        "DEMO MOCK: served fixture for %s (session_id=%s) — no real API call made",
        tool,
        session_id,
    )
