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

    if result and not result.startswith("No relevant"):
        return result

    # Semantic search found nothing — typical for open-ended recall like
    # "what did we talk about last time?", where the query matches no specific
    # topic. Fall back to the most recent call summaries so the farmer never
    # hears "no record" while summaries exist.
    items = await memory_service.get_all(user_id)
    if items:
        items.sort(key=lambda m: m.get("created_at") or "", reverse=True)
        recent = [m["memory"] for m in items[:2] if m.get("memory")]
        if recent:
            logger.info("recall_farmer_context fallback: returning %d recent summaries", len(recent))
            return (
                "No memory matched that exact topic; the most recent call summaries are:\n"
                + "\n---\n".join(recent)
            )

    return "No relevant past memories found."
