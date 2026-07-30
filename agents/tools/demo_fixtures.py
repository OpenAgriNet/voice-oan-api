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
# These mirror the REAL tool return shapes, verified against production tool
# observations in Langfuse (voice-production, 2026-07-30). They are NOT prose:
# each tool hands the agent a structured/labelled block and the agent renders
# the spoken sentence itself. Returning prose here would bypass that step and
# put my wording in Sarlaben's mouth instead of the model's.
#
# Reference observations:
#   create_ai_call     trace 0a071d0c8b001e5d15efa1e4bcf677ab
#   create_health_call trace c403567dd00de45adb22d6c81d978d4a
#   milk               agents/tools/milk_collection.py:_format_milk_collection_summary

import json

# Ticket numbers observed in prod are DDMMYYYY + a 4-digit serial,
# e.g. "300720263150" booked on 30-07-2026.
_DEMO_TICKET = "310720264417"


def milk_collection_summary() -> str:
    """
    Same flat, labelled, one-record-per-line shape `_format_milk_collection_summary`
    produces — the small OSS model relies on the field labels to avoid confusing
    quantity with fat/SNF/amount.
    """
    records = [
        ("2026-07-29", "Evening", "7.5", "4.2", "8.6", "312"),
        ("2026-07-29", "Morning", "8.1", "4.0", "8.5", "334"),
        ("2026-07-28", "Evening", "7.2", "4.1", "8.6", "299"),
        ("2026-07-28", "Morning", "8.4", "4.3", "8.7", "352"),
        ("2026-07-27", "Evening", "7.8", "4.1", "8.5", "324"),
    ]
    lines = [f"Milk collection records ({len(records)}):"]
    for i, (date, shift, qty, fat, snf, amount) in enumerate(records, 1):
        lines.append(
            f"  {i}. Date {date}, {shift} shift: "
            f"quantity {qty} liters, fat {fat}, SNF {snf}, amount {amount} rupees."
        )
    lines.append("Deduction records (1):")
    lines.append("  1. Date 2026-07-25, cattle feed: amount 450 rupees.")
    return "\n".join(lines)


def ai_call_booked(species: Any = None) -> str:
    """
    Real shape: a prefix line, blank line, then a JSON object with `ait_name`
    and `ticket_number`. `ait_name` in prod looks like
    "518 HARESHKUMAR-GANESHBHAI-PATEL" — a numeric code then an
    uppercase, hyphenated name.
    """
    payload = {
        "ait_name": "407 RAMESHBHAI-KANTIBHAI-PATEL",
        "ticket_number": _DEMO_TICKET,
    }
    return (
        "Artificial insemination call booked successfully:\n\n"
        + json.dumps(payload, indent=2)
    )


def health_call_booked(case_type: Any = None) -> str:
    """Real shape: one line, digits not words — the agent spells them aloud."""
    return f"Health call booked successfully. Ticket number: {_DEMO_TICKET}"


def log_fixture_served(tool: str, ctx: Any) -> None:
    """Make mocked turns obvious in the logs so nobody mistakes them for real."""
    deps = getattr(ctx, "deps", None)
    session_id = getattr(deps, "session_id", None) if deps else None
    logger.warning(
        "DEMO MOCK: served fixture for %s (session_id=%s) — no real API call made",
        tool,
        session_id,
    )
