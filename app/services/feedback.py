"""
Feedback flow: rating parsing, messages, and send_feedback API (blackbox).
"""
import json
import re
from pathlib import Path
from typing import Optional, Literal

from helpers.utils import get_logger

logger = get_logger(__name__)

# Rating mappings for voice transcription (English and Gujarati)
RATING_MAP = {
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5,
    "૧": 1, "૨": 2, "૩": 3, "૪": 4, "૫": 5,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "એક": 1, "બે": 2, "ત્રણ": 3, "ચાર": 4, "પાંચ": 5,
    "પંચ": 5,  # alternate spelling
}


def _load_feedback_messages() -> dict:
    # assets/ is at project root (sibling of app/)
    path = Path(__file__).resolve().parent.parent.parent / "assets" / "feedback_messages.json"
    with open(path, "r") as f:
        return json.load(f)


def get_feedback_question(lang_code: str = "gu") -> str:
    """Get the feedback question in the given language."""
    data = _load_feedback_messages()
    return data["question"].get(lang_code, data["question"]["gu"])


def get_feedback_ack(rating: int, lang_code: str = "gu") -> str:
    """Get the acknowledgment message based on rating (1-3 vs 4-5)."""
    data = _load_feedback_messages()
    key = "ack_low" if rating <= 3 else "ack_high"
    return data[key].get(lang_code, data[key]["gu"])


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


async def send_feedback(
    session_id: str,
    user_id: str,
    process_id: Optional[str],
    rating: int,
    trigger: Literal["conversation_closing", "user_frustration"],
    source_lang: str,
    target_lang: str,
    message_history_summary: Optional[str] = None,
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

    # Store in Langfuse as session-level score (matches propagate_attributes session_id)
    try:
        from app.observability import langfuse_client

        if langfuse_client is not None:
            # Value as float (1-5 scale); Langfuse supports NUMERIC for 0-5 range
            langfuse_client.create_score(
                session_id=session_id,
                name="user-feedback",
                value=float(rating),
                comment=comment,
                data_type="NUMERIC",
                score_id=f"{session_id}-user-feedback",  # idempotency: one feedback per session
            )
            langfuse_client.flush()
            logger.info(
                f"Feedback stored in Langfuse: session_id={session_id}, rating={rating}"
                + (f" (unparseable raw_input)" if rating == 0 and raw_input else "")
            )
    except Exception as e:
        logger.warning(f"Failed to store feedback in Langfuse: {e}")

    logger.info(f"send_feedback: session_id={session_id}, rating={rating}, trigger={trigger}")
