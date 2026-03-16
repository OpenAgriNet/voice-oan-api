import asyncio
import contextvars
import json
import random
import httpx
from pathlib import Path
from typing import List, Dict, Any, Optional
from helpers.utils import get_logger
from app.config import settings

logger = get_logger(__name__)

# ── Tool-call nudge signaling ───────────────────────────────────────────
# A per-request asyncio.Event stored in a ContextVar.  Any tool wrapper
# can call fire_tool_call_nudge() to tell the nudge task "a tool was
# invoked – send the hold message now instead of waiting for the timer".
_tool_call_nudge_event: contextvars.ContextVar[Optional[asyncio.Event]] = contextvars.ContextVar(
    "_tool_call_nudge_event", default=None
)


def set_tool_call_nudge_event(event: asyncio.Event) -> contextvars.Token:
    """Set the nudge event for the current async context (call once per request)."""
    return _tool_call_nudge_event.set(event)


def fire_tool_call_nudge() -> None:
    """Signal that a tool call has started – the nudge task should fire immediately."""
    event = _tool_call_nudge_event.get(None)
    if event is not None and not event.is_set():
        event.set()


# Load nudge messages once at module load (no disk I/O at request time)
_NUDGE_MESSAGES_PATH = Path(__file__).resolve().parent.parent.parent / "assets" / "nudge_messages.json"
with open(_NUDGE_MESSAGES_PATH, "r", encoding="utf-8") as _f:
    _NUDGE_MESSAGES_DATA: dict = json.load(_f)


def get_random_nudge_message(lang_code: str = "en") -> str:
    """Get a random nudge (hold) message for the given language from in-memory nudge_messages.
    Expects a "hold_messages" key with per-lang lists of messages; falls back to "en" if lang missing.
    """
    hold = _NUDGE_MESSAGES_DATA.get("hold_messages", {})
    messages = hold.get(lang_code) or hold.get("en") or []
    if not messages:
        return "Please hold."
    return random.choice(messages)


def get_nudge_message(tool: str, lang_code: str = "en") -> str:
    """Get a nudge message for a specific tool and action in the specified language."""
    return _NUDGE_MESSAGES_DATA[tool][lang_code]


async def send_feedback_prompt_raya(
    session_id: str, process_id: Optional[str], lang_code: str = "gu"
) -> None:
    """Send the feedback question (1-5 scale) to the user via RAYA nudge API."""
    from app.services.feedback import get_feedback_question

    message = get_feedback_question(lang_code)
    await send_nudge_message_raya(message, session_id, process_id)


async def send_nudge_message_raya(message: str, session_id: str, process_id: str = None) -> None:
    """Send nudge message via RAYA API (async, non-blocking)."""
    try:
        nudge_url = settings.nudge_api_url
        payload: Dict[str, Any] = {
            "message": message,
            "session_id": session_id,
        }
        if process_id:
            payload["process_id"] = process_id

        logger.info(
            "Nudge API request sent; session_id=%s process_id=%s url=%s payload=%s",
            session_id,
            process_id,
            nudge_url,
            payload,
        )
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                nudge_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        response_body = response.text
        logger.info(
            "Nudge API response; session_id=%s process_id=%s status=%s body=%s",
            session_id,
            process_id,
            response.status_code,
            response_body,
        )
        if response.status_code == 200:
            logger.info(
                "Nudge message sent; session_id=%s process_id=%s api_status=%s response=%s",
                session_id,
                process_id,
                response.status_code,
                response_body,
            )
        else:
            logger.warning(
                "Nudge message API failed; session_id=%s process_id=%s api_status=%s response=%s",
                session_id,
                process_id,
                response.status_code,
                response_body,
            )
    except httpx.HTTPError as e:
        logger.error(
            "Error sending nudge message; session_id=%s process_id=%s error=%s",
            session_id,
            process_id,
            e,
        )
    except Exception as e:
        logger.error(
            "Unexpected error sending nudge message; session_id=%s process_id=%s error=%s",
            session_id,
            process_id,
            e,
        )