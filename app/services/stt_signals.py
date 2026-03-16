"""
Handle special STT signals (no-audio, unclear speech) with contextual responses.

When the STT pipeline cannot transcribe the user's audio it sends sentinel
strings instead of real text.  We intercept these early, skip translation
and the main agent, and ask a small LLM (GPT-5-mini) to produce a short,
context-aware prompt back to the user (e.g. "I can't hear you, could you
speak a bit louder?").
"""

import os
import re
from typing import Optional

from openai import AsyncOpenAI
from helpers.utils import get_logger
from app.config import settings

logger = get_logger(__name__)

# ── Signal detection ────────────────────────────────────────────────────
# Strip leading/trailing whitespace and surrounding * markers, then match.
_SIGNAL_NO_AUDIO = "No audio/User is speaking softly"
_SIGNAL_UNCLEAR = "Unclear Speech"

_SIGNALS = {
    _normalize(s): s
    for s in (_SIGNAL_NO_AUDIO, _SIGNAL_UNCLEAR)
}


def _normalize(text: str) -> str:
    return re.sub(r"[*]", "", text).strip().lower()


# Re-build after _normalize is defined
_SIGNALS = {
    _normalize(s): s
    for s in (_SIGNAL_NO_AUDIO, _SIGNAL_UNCLEAR)
}


def detect_stt_signal(query: str) -> Optional[str]:
    """Return the canonical signal name if *query* is a known STT signal, else None."""
    if not query:
        return None
    return _SIGNALS.get(_normalize(query))


# ── Contextual response generation ──────────────────────────────────────
STT_RESPONSE_MODEL = os.getenv("STT_SIGNAL_MODEL", "gpt-5-mini")

_SYSTEM_PROMPT = """\
You are a friendly voice assistant on a phone call with a farmer.
The speech-to-text system reported a problem with the caller's audio.

Your ONLY job is to produce a single short sentence (in the target language) \
politely asking the user to repeat or speak louder.  Take the recent \
conversation into account so the prompt feels natural — for example, if you \
just asked a question you can say "Sorry, I couldn't hear your answer, \
could you say that again?"

Rules:
- One sentence only, conversational tone, no markdown.
- If target language is Gujarati respond in Gujarati script.
- Never answer a question or provide information — just ask to repeat."""

_openai_client: Optional[AsyncOpenAI] = None


def _get_openai_client() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        api_key = settings.openai_api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required for STT signal responses")
        _openai_client = AsyncOpenAI(api_key=api_key)
    return _openai_client


async def generate_stt_signal_response(
    signal: str,
    target_lang: str,
    recent_history_text: str = "",
) -> str:
    """Ask GPT-5-mini for a short, contextual 'please repeat' message.

    Args:
        signal: canonical signal name (from detect_stt_signal).
        target_lang: language code for the response (e.g. "gu", "en").
        recent_history_text: formatted recent conversation turns for context.

    Returns:
        A single-sentence response string.
    """
    lang_label = {"gu": "Gujarati", "en": "English"}.get(target_lang, target_lang)

    user_content = (
        f"STT signal: {signal}\n"
        f"Target language: {lang_label}\n"
    )
    if recent_history_text:
        user_content += f"\nRecent conversation:\n{recent_history_text}\n"

    client = _get_openai_client()
    response = await client.chat.completions.create(
        model=STT_RESPONSE_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        max_tokens=150,
        temperature=0.7,
    )
    text = (response.choices[0].message.content or "").strip()
    if not text:
        # Hardcoded fallback — should never happen
        if target_lang == "gu":
            return "માફ કરશો, હું તમને સાંભળી શકતી નથી. કૃપા કરીને ફરીથી બોલો."
        return "Sorry, I couldn't hear you. Could you please repeat that?"
    return text
