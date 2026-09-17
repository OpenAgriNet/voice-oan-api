"""
Tool for booking an artificial insemination (beech daan) call for a farmer.
One booking per session (30-min cooldown via Redis).
"""
import json
import os

from pydantic_ai import RunContext

from agents.deps import FarmerContext
from agents.models.ai_call import AICallRequestModel, AISpecies
from agents.tools.farmer_animal_backends import create_ai_call_api
from app.core.cache import cache, try_reserve, release_reservation
from agents.services.farmer_identity import match_technician
from app.models.union import UNION_BANNED_MESSAGE, any_union_banned_from_ai_calls
from helpers.utils import get_logger

logger = get_logger(__name__)

AI_CALL_COOLDOWN_TTL = 60 * 30  # 30 minutes
AI_CALL_CACHE_NAMESPACE = "ai_call_booked"

async def create_ai_call(
    ctx: RunContext[FarmerContext],
    technician_name: str,
    species: AISpecies,
) -> str:
    """
    Book an artificial insemination (beech daan / બીજ દાન) call for a farmer.

    Pass the full name of the technician the farmer chose, exactly as it appears
    in the AI technician context. All codes are read from context — you do not
    supply them, and you must never ask the farmer to speak an internal ID.
    Ask whether the booking is for a cow (ગાય) or buffalo (ભેંસ) before calling.
    If Farmer Profile says AI call booking is not allowed for this union, tell the
    farmer exactly: Kindly contact your Milk Society to book the service. Do not
    ask which technician and do not book.

    Args:
        ctx: The run context (automatically provided).
        technician_name: Full name of the technician the farmer chose.
        species: Species to book the AI call for. Use `cow` or `buffalo`.

    Returns:
        str: Formatted result with assigned AIT details and ticket number,
             or a message if booking fails or was already done this session.
    """
    session_id = ctx.deps.session_id
    logger.info(
        "AI call tool invoked: session=%s technician_name=%r species=%s",
        session_id, technician_name, species.value,
    )

    # Moderation runs concurrently with the agent, so this booking write must
    # block on the verdict: a rejected query must never create a real booking.
    if not await ctx.deps.ensure_in_scope():
        logger.info("AI call blocked: query failed moderation; session=%s", session_id)
        return "This helpline only handles dairy farming and animal husbandry questions."

    # Union ban is a policy gate, not a booking write: refuse before Redis
    # reservation and before PashuGPT. farmer_unions may be missing on test
    # stubs and on unsigned-in turns — those are not banned.
    farmer_unions = getattr(ctx.deps, "farmer_unions", []) if ctx and ctx.deps else []
    if any_union_banned_from_ai_calls(farmer_unions):
        logger.info(
            "AI call blocked: union banned from AI-call booking unions=%s session=%s",
            farmer_unions,
            session_id,
        )
        return UNION_BANNED_MESSAGE

    # The technician's group carries the account to book against, so one lookup
    # yields both the technician id and the codes.
    technician, problem = match_technician(
        getattr(ctx.deps, "ai_technicians", None) or [], technician_name
    )
    if technician is None:
        logger.info("AI call not booked: %s; session=%s", problem, session_id)
        return problem

    union_code = technician.union_code or ""
    society_code = technician.society_code or ""
    farmer_code = technician.farmer_code or ""
    user_id = technician.user_id or ""

    token = os.getenv("PASHUGPT_TOKEN")
    if not token:
        logger.error("PASHUGPT_TOKEN is not set")
        return "Artificial insemination call booking failed. Service is not configured."

    request = AICallRequestModel(
        unionCode=union_code,
        societyCode=society_code,
        farmerCode=farmer_code,
        userId=user_id,
        species=species,
    )

    # Atomic reservation immediately before the write: first caller wins; a
    # concurrent/duplicate submit OR a fallback re-run for the same session
    # short-circuits instead of double-booking (Redis SET NX, shared across
    # containers). Released below if the booking API itself fails.
    _reserved = False
    if session_id:
        if not await try_reserve(session_id, AI_CALL_CACHE_NAMESPACE, AI_CALL_COOLDOWN_TTL):
            logger.info("AI call already booked/in-flight for session %s, skipping", session_id)
            return (
                "This session already has an active artificial insemination booking. "
                "Please try again later or contact your society for assistance."
            )
        _reserved = True

    response = await create_ai_call_api(request, token)
    if response is None:
        if _reserved:
            await release_reservation(session_id, AI_CALL_CACHE_NAMESPACE)
        logger.info("AI call API failed for session=%s", session_id)
        return "Artificial insemination call booking failed. Unable to create booking at the moment."

    # Mark session as booked
    if session_id:
        try:
            await cache.set(
                session_id,
                {"ticket": response.ticket_number, "species": species.value},
                ttl=AI_CALL_COOLDOWN_TTL,
                namespace=AI_CALL_CACHE_NAMESPACE,
            )
        except Exception as e:
            logger.warning("Failed to set AI call cooldown: %s", e)

    formatted = json.dumps(response.model_dump(), indent=2, ensure_ascii=False)
    logger.info(
        "AI call booked: session=%s ticket=%s ait=%s",
        session_id, response.ticket_number, response.ait_name,
    )
    return f"Artificial insemination call booked successfully:\n\n{formatted}"
