"""
Feedback flow: rating parsing, messages, and send_feedback API (blackbox).
Uses a small LLM to classify whether the user's response is a 1-5 feedback rating
or a normal message to the agent; only accepted feedback is recorded and counts
toward the one-feedback-per-session limit.
"""
import asyncio
import json
import os
import re
from pathlib import Path
from typing import Optional, Literal

from helpers.utils import get_logger
from app.config import settings

logger = get_logger(__name__)

# Load feedback messages once at module load (no disk I/O at request time)
_FEEDBACK_MESSAGES_PATH = Path(__file__).resolve().parent.parent.parent / "assets" / "feedback_messages.json"
with open(_FEEDBACK_MESSAGES_PATH, "r", encoding="utf-8") as _f:
    _FEEDBACK_MESSAGES: dict = json.load(_f)

# Rating mappings for voice transcription (English and Gujarati)
RATING_MAP = {
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5,
    "૧": 1, "૨": 2, "૩": 3, "૪": 4, "૫": 5,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "એક": 1, "બે": 2, "ત્રણ": 3, "ચાર": 4, "પાંચ": 5,
    "પંચ": 5,  # alternate spelling
}


def get_feedback_question(lang_code: str = "gu") -> str:
    """Get the feedback question in the given language."""
    return _FEEDBACK_MESSAGES["question"].get(lang_code, _FEEDBACK_MESSAGES["question"]["gu"])


def get_feedback_ack(rating: int, lang_code: str = "gu") -> str:
    """Get the acknowledgment message based on rating (1-3 vs 4-5)."""
    key = "ack_low" if rating <= 3 else "ack_high"
    return _FEEDBACK_MESSAGES[key].get(lang_code, _FEEDBACK_MESSAGES[key]["gu"])


def parse_rating_from_text(text: str) -> Optional[int]:
    """
    Parse a 1-5 rating from user's voice input. Handles digits, words in English and Gujarati.
    Returns None if unparseable.
    """
    if not text or not isinstance(text, str):
        return None
    cleaned = re.sub(r"\s+", " ", text.strip()).lower()
    if not cleaned:
        return None
    # Direct match
    if cleaned in RATING_MAP:
        return RATING_MAP[cleaned]
    # Extract first digit or word
    for token in re.split(r"[\s,]+", cleaned):
        if token in RATING_MAP:
            return RATING_MAP[token]
    # Try standalone digit
    match = re.search(r"[1-5૧૨૩૪૫]", text)
    if match:
        char = match.group()
        return RATING_MAP.get(char)
    return None


FEEDBACK_PARSE_SYSTEM = """You classify the user's reply after a voice assistant asked: "On a scale of 1 to 5, how helpful was this call?"

Respond with JSON only: {"is_feedback": true|false, "rating": 1-5 or null}.

- is_feedback: true only if the user is clearly giving a rating for the call (1-5 in any form: digit, word in English or Gujarati, or clear intent like "very helpful" = 5, "not helpful" = 1). false if they are asking a new question, giving a comment, saying something unrelated, or the intent is ambiguous.
- rating: integer 1-5 only when is_feedback is true; otherwise null. Map verbal feedback to scale: "not helpful"/"bad" -> 1-2, "ok"/"average" -> 3, "helpful"/"good"/"very helpful" -> 4-5."""

FEEDBACK_PARSE_USER_TEMPLATE = """User's reply (possibly in Gujarati or English): "{text}"

Respond with exactly one JSON object: {"is_feedback": <true|false>, "rating": <1-5|null>}."""


async def parse_feedback_with_llm(text: str, lang_code: str = "gu") -> dict:
    """
    Use a small model to decide if the user's response is a valid 1-5 feedback rating.
    Returns {"is_feedback": bool, "rating": int | None}.
    Only when is_feedback is True and rating is in 1-5 should feedback be recorded.

    Tries regex first (fast path), then GPT-5-mini, then falls back to
    regex-only result so feedback is never silently dropped.
    """
    if not text or not isinstance(text, str) or not text.strip():
        return {"is_feedback": False, "rating": None}

    # ── Fast path: regex rating extraction ────────────────────────────
    regex_rating = parse_rating_from_text(text)
    if regex_rating is not None:
        return {"is_feedback": True, "rating": regex_rating}

    # ── LLM path: handles verbal feedback like "very helpful" / "બહુ સારું" ──
    api_key = settings.openai_api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.warning("No OPENAI_API_KEY for feedback parse; treating as non-feedback")
        return {"is_feedback": False, "rating": None}

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=api_key)
        response = await client.chat.completions.create(
            model="gpt-5-mini",
            messages=[
                {"role": "system", "content": FEEDBACK_PARSE_SYSTEM},
                {"role": "user", "content": FEEDBACK_PARSE_USER_TEMPLATE.format(text=text.strip()[:500])},
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=80,
        )
        raw = (response.choices[0].message.content or "").strip()
        if not raw:
            return {"is_feedback": False, "rating": None}
        data = json.loads(raw)
        is_feedback = data.get("is_feedback") is True
        rating = data.get("rating")
        if rating is not None and isinstance(rating, (int, float)):
            r = int(rating)
            if 1 <= r <= 5:
                return {"is_feedback": is_feedback, "rating": r}
        return {"is_feedback": False, "rating": None}
    except Exception as e:
        logger.warning("Feedback parse LLM failed (gpt-5-mini): %s", e)
        return {"is_feedback": False, "rating": None}


async def send_feedback(
    session_id: str,
    user_id: str,
    process_id: Optional[str],
    rating: int,
    trigger: Literal["conversation_closing", "user_frustration"],
    source_lang: str,
    target_lang: str,
    message_history_summary: Optional[dict] = None,
    farmer_info: Optional[dict] = None,
    raw_input: Optional[str] = None,
) -> None:
    """
    Send feedback to Langfuse (and optionally other telemetry). Stores the 1-5 rating
    as a session-level score in Langfuse, linked to the conversation session.
    For unparseable input, pass rating=0 and raw_input with the user's utterance.

    All parameters are provided for telemetry correlation:
    - session_id: groups feedback with the conversation (used for Langfuse session)
    - user_id: typically phone number for farmer identification
    - process_id: RAYA process/trace ID
    - rating: 1-5 (or 0 for unparseable)
    - trigger: what prompted feedback (conversation_closing | user_frustration)
    - source_lang, target_lang: for localization context
    - message_history_summary: optional context (e.g. turn_count) for telemetry
    - raw_input: user's raw utterance when unparseable (stored in comment)
    """
    # Build comment with metadata for correlation
    comment_parts = [f"trigger={trigger}", f"source_lang={source_lang}", f"target_lang={target_lang}"]
    if user_id and user_id != "anonymous":
        comment_parts.append(f"user_id={user_id}")
    if process_id:
        comment_parts.append(f"process_id={process_id}")
    if raw_input:
        # Truncate long inputs; store unparseable utterance for analysis
        raw_preview = (raw_input[:200] + "…") if len(raw_input) > 200 else raw_input
        comment_parts.append(f"raw_input={raw_preview}")
    comment = "; ".join(comment_parts)

    # Store in Langfuse as session-level score (non-blocking: sync SDK runs in thread)
    try:
        from app.observability import langfuse_client

        if langfuse_client is not None:
            def _create_and_flush() -> None:
                langfuse_client.create_score(
                    session_id=session_id,
                    name="user-feedback",
                    value=float(rating),
                    comment=comment,
                    data_type="NUMERIC",
                    score_id=f"{session_id}-user-feedback",  # idempotency: one feedback per session
                )
                langfuse_client.flush()

            await asyncio.to_thread(_create_and_flush)
            logger.info(
                f"Feedback stored in Langfuse: session_id={session_id}, rating={rating}"
                + (f" (unparseable raw_input)" if rating == 0 and raw_input else "")
            )
    except Exception as e:
        logger.warning(f"Failed to store feedback in Langfuse: {e}")

    logger.info(f"send_feedback: session_id={session_id}, rating={rating}, trigger={trigger}")
