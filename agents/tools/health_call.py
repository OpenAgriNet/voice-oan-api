"""
Tool for booking a health call for a farmer.
"""
import os

from pydantic_ai import RunContext

from agents.deps import FarmerContext
from agents.models.ai_call import AISpecies
from agents.models.health_call import HealthCallRequestModel, HealthCaseType
from agents.tools.access import FarmerAccessDenied, resolve_owned_account
from agents.tools.beckn_voice import beckn_health_booking, render_result
from agents.tools.farmer_animal_backends import create_health_call_api
from app.config import settings
from app.core.cache import cache, try_reserve, release_reservation
from helpers.utils import get_logger

logger = get_logger(__name__)

# One booking per session per 30 min (mirrors create_ai_call). Also makes this
# tool idempotent against an agent re-run (OSS->managed streaming fallback).
HEALTH_CALL_COOLDOWN_TTL = 60 * 30  # 30 minutes
HEALTH_CALL_CACHE_NAMESPACE = "health_call_booked"


async def create_health_call(
    ctx: RunContext[FarmerContext],
    union_code: str,
    society_code: str,
    farmer_code: str,
    species: AISpecies,
    case_type: HealthCaseType,
    remark: str | None = None,
) -> str:
    """
    Book a health call for a farmer and return the generated ticket number.

    Args:
        ctx: The run context (automatically provided).
        union_code: Union code for the farmer from farmer context.
        society_code: Society code for the farmer from farmer context.
        farmer_code: Farmer code for the farmer from farmer context.
        species: Species for the call (`cow` or `buffalo`).
        case_type: Case type (`normal` or `emergency`).
        remark: Optional concise issue summary.

    Returns:
        str: Success message containing the ticket number, or a clear failure message.
    """
    session_id = ctx.deps.session_id
    logger.info(
        "Health call tool invoked: session=%s species=%s case_type=%s",
        session_id,
        species.value,
        case_type.value,
    )

    # Moderation runs concurrently with the agent, so this booking write must
    # block on the verdict: a rejected query must never create a real booking.
    if not await ctx.deps.ensure_in_scope():
        logger.info("Health call blocked: query failed moderation; session=%s", session_id)
        return "This helpline only handles dairy farming and animal husbandry questions."

    try:
        account = resolve_owned_account(ctx.deps, union_code, society_code, farmer_code)
    except FarmerAccessDenied as exc:
        logger.warning("Health call ownership gate rejected session=%s", session_id)
        return str(exc)

    if settings.voice_beckn_enabled:
        result = await beckn_health_booking(
            ctx.deps,
            account,
            species.value,
            case_type.value,
            remark,
        )
        return render_result(result, "Health call booking")

    token = os.getenv("PASHUGPT_TOKEN")
    if not token:
        logger.error("PASHUGPT_TOKEN is not set")
        return "Health call booking failed.\n\nPASHUGPT_TOKEN is not configured."

    request = HealthCallRequestModel(
        unionCode=account.union_code or "",
        societyCode=account.society_code or "",
        farmerCode=account.farmer_code or "",
        species=species,
        caseType=case_type,
        remark=remark,
    )

    # Atomic reservation immediately before the write: first caller wins; a
    # concurrent/duplicate submit OR a fallback re-run for the same session
    # short-circuits instead of double-booking (Redis SET NX, shared across
    # containers). Released below if the booking API itself fails.
    _reserved = False
    if session_id:
        if not await try_reserve(session_id, HEALTH_CALL_CACHE_NAMESPACE, HEALTH_CALL_COOLDOWN_TTL):
            logger.info("Health call already booked/in-flight for session %s, skipping", session_id)
            return (
                "This session already has an active health call booking. "
                "Please try again later or contact your society for assistance."
            )
        _reserved = True

    response = await create_health_call_api(request, token)
    if response is None:
        if _reserved:
            await release_reservation(session_id, HEALTH_CALL_CACHE_NAMESPACE)
        logger.info(
            "Health call API failed: session=%s union=%s society=%s farmer=%s species=%s case_type=%s",
            session_id,
            union_code,
            society_code,
            farmer_code,
            species.value,
            case_type.value,
        )
        return "Health call booking failed.\n\nUnable to create health call at the moment."

    # Mark this session as booked so a re-run (or retry) does not double-book.
    if session_id:
        try:
            await cache.set(
                session_id,
                {"ticket": response.ticket_number, "species": species.value},
                ttl=HEALTH_CALL_COOLDOWN_TTL,
                namespace=HEALTH_CALL_CACHE_NAMESPACE,
            )
        except Exception as e:
            logger.warning("Failed to set health call cooldown: %s", e)

    ticket_number = response.ticket_number
    logger.info(
        "Health call booked: session=%s ticket=%s",
        session_id,
        ticket_number,
    )
    if ticket_number:
        return f"Health call booked successfully. Ticket number: {ticket_number}"
    return "Health call booked successfully, but ticket number was not returned."
