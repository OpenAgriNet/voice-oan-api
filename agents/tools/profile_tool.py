from typing import Literal

from pydantic_ai import RunContext

from agents.deps import FarmerContext
from helpers.utils import get_logger

logger = get_logger(__name__)

# Fields the agent may update by voice. Crops are added by name; richer crop
# detail (variety, sowing date, area) is captured post-call from the transcript.
ProfileField = Literal[
    "name", "village", "district", "state", "preferred_mandi",
    "crop", "land_area_acres", "irrigation", "soil_type",
    "livestock", "language", "preferred_call_time", "scheme", "note",
]

# Map the tool's farmer-friendly field names onto the FarmerProfile schema.
_FIELD_MAP = {
    "crop": "crops",
    "livestock": "livestock",
    "scheme": "schemes",
    "note": "notes",
}


def _to_partial(field: str, value: str) -> dict:
    if field == "crop":
        return {"crops": [{"name": value}]}
    target = _FIELD_MAP.get(field, field)
    if target in ("livestock", "schemes", "notes"):
        return {target: [value]}
    if field == "land_area_acres":
        try:
            return {"land_area_acres": float("".join(c for c in value if c.isdigit() or c == "."))}
        except ValueError:
            return {"notes": [f"land area: {value}"]}
    return {field: value}


async def update_farmer_profile(
    ctx: RunContext[FarmerContext], field: ProfileField, value: str
) -> str:
    """Save or correct a durable fact about the farmer in their profile.

    Call this when the farmer states a stable fact about themselves or their farm
    that should be remembered for future calls — e.g. their main crop, village,
    irrigation type, or preferred mandi. Do NOT use this for one-off questions
    (prices, weather, a scheme they're merely asking about).

    Args:
        field: Which profile field to set (e.g. "crop", "village", "irrigation").
        value: The value to store, e.g. "cotton", "Wardha", "drip".

    Returns:
        A short confirmation, or a note that no profile is available.
    """
    user_id = ctx.deps.user_id
    if not user_id:
        return "No farmer profile available for this session."

    from app.services.profile import profile_store

    await profile_store.apply_update(user_id, _to_partial(field, value))
    logger.info("update_farmer_profile user=%s field=%s value=%r", user_id, field, value)
    return f"Saved {field}: {value}."


async def forget_farmer_detail(
    ctx: RunContext[FarmerContext], field: ProfileField, value: str = ""
) -> str:
    """Remove a previously stored fact when the farmer says it's no longer true.

    Use when the farmer corrects their profile, e.g. "I don't grow cotton anymore"
    -> forget_farmer_detail(field="crop", value="cotton").

    Args:
        field: Which profile field to clear (e.g. "crop", "irrigation").
        value: For list fields (crop, livestock, scheme), the specific entry to
            remove. Leave empty to clear a single-value field entirely.

    Returns:
        A short confirmation of what was removed.
    """
    user_id = ctx.deps.user_id
    if not user_id:
        return "No farmer profile available for this session."

    from app.services.profile import profile_store

    target = _FIELD_MAP.get(field, field)
    changed = await profile_store.forget(user_id, target, value or None)
    logger.info("forget_farmer_detail user=%s field=%s value=%r changed=%s",
                user_id, field, value, changed)
    if not changed:
        return f"Nothing to remove for {field}."
    return f"Removed {field}{f': {value}' if value else ''}."
