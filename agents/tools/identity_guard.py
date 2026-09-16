"""One place that decides whether an identifier the model supplied can be real.

Context: with no farmer context in the system prompt the model does not stop —
it invents identifiers and calls the tool anyway (`MISSING`, `UNKNOWN`,
`not_provided`, `F12345/S67890/U11223`, `UNION_CODE_FROM_CONTEXT`, farmer names
in `farmerCode`). Measured 2026-09-01..09-14 on voice-production, the rate is
not specific to booking:

    create_health_call                      33 / 163   = 20.2%
    create_ai_call                         400 / 3,828 = 10.4%
    get_farmer_milk_collection_details     117 / 1,148 = 10.2%

The guard added in dbc2d23 covered create_ai_call only, which is why health call
is the worst of the three. This module exists so a tool cannot be added without
one; see issue #282 for the full attribution.

⚠️ This is a backstop, not the fix. The real fix is to not offer an
identity-taking tool on a turn whose farmer identity is unresolved (#282 fix 1);
by the time we are here the model has already decided to act on nothing.
"""
import re
from typing import Any, Optional, Sequence

# Real codes are NOT always numeric — M001 and NA4192 book fine. Patterns
# validated against 9,945 successful prod bookings (30d to 2026-09-08).
CODE_PATTERN = re.compile(r"^[A-Za-z0-9/-]{1,12}$")
TECHNICIAN_ID_PATTERN = re.compile(r"^[A-Za-z0-9+/]{22}==$")


def invalid_code_field(
    union_code: str,
    society_code: str,
    farmer_code: str,
) -> Optional[str]:
    """Name of the first code that cannot be real, else None."""
    for field, value in (
        ("union_code", union_code),
        ("society_code", society_code),
        ("farmer_code", farmer_code),
    ):
        cleaned = (value or "").strip()
        if not CODE_PATTERN.match(cleaned) or not _HAS_DIGIT.search(cleaned):
            return field
    return None


# Every real code carries at least one digit. Verified against 28,089 successful
# AI bookings (22 distinct unions, 2,032 societies, 2,548 farmer codes) and 110
# successful health calls over 90 days: zero exceptions. This is what separates
# MISSING / UNKNOWN / not_provided / PLACEHOLDER from M001 and NA4192 — the
# shape check alone passes all of them, which is why the dbc2d23 guard only
# worked for create_ai_call (the technician-id check did the real work there)
# and why health call, which has no technician id, kept leaking at 20%.
_HAS_DIGIT = re.compile(r"[0-9]")


def codes_absent_from_context(
    accounts: "Sequence[Any]",
    union_code: str,
    society_code: str,
    farmer_code: str,
) -> Optional[str]:
    """Why these codes cannot be trusted against the caller's own accounts.

    Strongest available check, and structural rather than cosmetic: the model is
    told to copy these out of the farmer context, so if the context holds no
    accounts there is nowhere a real code could have come from, and if it holds
    accounts the supplied triple must be one of them.
    """
    if not accounts:
        # No accounts resolved is NOT by itself proof of invention: the tools
        # deliberately fall back to model-supplied codes for callers whose
        # context did not load (see test_falls_back_to_supplied_codes_when_no_
        # accounts_in_context). Shape + digit is what screens those.
        return None
    supplied = (str(union_code or "").strip(),
                str(society_code or "").strip(),
                str(farmer_code or "").strip())
    known = {
        (str(a.union_code or "").strip(),
         str(a.society_code or "").strip(),
         str(a.farmer_code or "").strip())
        for a in accounts
    }
    if supplied not in known:
        return "the supplied codes do not match any account on this caller's mobile"
    return None


def invalid_technician_id(user_id: str) -> bool:
    """True when the technician id cannot be a real one (24 base64 chars, '==')."""
    return not TECHNICIAN_ID_PATTERN.match((user_id or "").strip())
