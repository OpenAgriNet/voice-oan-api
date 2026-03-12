from typing import Literal
from pydantic_ai import RunContext
from agents.deps import FarmerContext
from app.core.cache import cache

LANGUAGE_CACHE_SUFFIX = "_LANG"
LANGUAGE_CACHE_TTL = 60 * 60 * 4  # 4 hours — matches typical session lifetime


async def set_language(ctx: RunContext[FarmerContext], language: Literal["en", "hi"]) -> str:
    """Set the conversation language based on the user's explicit choice.

    Call this tool ONLY when the user explicitly states their preferred language:
    - "en" for English (user says: English, Angrezi, अंग्रेज़ी, etc.)
    - "hi" for Hindi (user says: Hindi, हिंदी, Hindi me, etc.)

    Do NOT call this tool when asking the user which language they prefer.
    Call it only once the user has clearly stated their choice.
    Once set, the language is frozen for the entire session and cannot be changed.
    """
    # Freeze: if already set this session, ignore the new value
    if ctx.deps.selected_language is not None:
        name = "English" if ctx.deps.selected_language == "en" else "Hindi"
        return f"Language is already set to {name} for this session."

    # Persist to Redis so language survives across turns
    await cache.set(
        f"{ctx.deps.session_id}{LANGUAGE_CACHE_SUFFIX}",
        language,
        ttl=LANGUAGE_CACHE_TTL,
    )
    ctx.deps.selected_language = language
    name = "English" if language == "en" else "Hindi"
    return f"Language set to {name} for this session."
