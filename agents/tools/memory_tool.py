from pydantic_ai import RunContext
from agents.deps import FarmerContext
from helpers.utils import get_logger

logger = get_logger(__name__)


async def recall_farmer_context(ctx: RunContext[FarmerContext], query: str) -> str:
    """Fetch detailed farmer memories beyond the call-start snapshot.

    Call this when the farmer refers to a past conversation or a detail not already
    in the profile snapshot (e.g. past pest issues, previous advice given).
    Do NOT call this for mandi prices, weather, or scheme details — use live tools.

    Args:
        query: What to search for in memory, e.g. "past pest discussion" or "previous crop advice".

    Returns:
        Relevant past memories as a bullet list, or a note that none were found.
    """
    user_id = ctx.deps.user_id
    if not user_id:
        return "No farmer memory available for this session."

    from app.services.memory import memory_service

    result = await memory_service.search(
        query=query,
        user_id=user_id,
        top_k=5,
        threshold=0.3,
    )
    logger.info("recall_farmer_context for user %s query=%r -> %d chars", user_id, query, len(result))
    return result or "No relevant past memories found."
