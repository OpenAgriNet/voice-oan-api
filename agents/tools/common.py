import os
import json
import random
import asyncio
import httpx
from pydantic import BaseModel, Field
from typing import List, Dict, Any
from helpers.utils import get_logger
from app.config import settings

logger = get_logger(__name__)

# Per-session last nudge per tool — avoids repeating the same hold line back-to-back.
_last_nudge_by_session: dict[tuple[str, str], str] = {}

# One hold message per turn (session + process_id), including parallel tool calls.
_nudge_sent_this_turn: set[str] = set()
_nudge_turn_locks: dict[str, asyncio.Lock] = {}

# NOTE: No need to cache right now.
# Loading a small json and finding the message for the tool and language code should be very fast anyway now.


def _turn_key(session_id: str, process_id: str | None) -> str:
    return f"{session_id}:{process_id or ''}"


def _turn_lock(key: str) -> asyncio.Lock:
    if key not in _nudge_turn_locks:
        _nudge_turn_locks[key] = asyncio.Lock()
    return _nudge_turn_locks[key]


def get_nudge_message(
    tool: str,
    lang_code: str = "en",
    session_id: str | None = None,
) -> str:
    """Get a nudge message for a specific tool and action in the specified language.
    Load json and then return the message for the tool and language code.
    If the message is a list (e.g. mr variants), one is chosen at random.
    When session_id is set, the last message used for that session+tool is skipped
    so consecutive tool calls in the same session get a different hold line.
    """
    with open('assets/nudge_messages.json', 'r') as f:
        nudge_data = json.load(f)
    message = nudge_data[tool][lang_code]
    if not isinstance(message, list):
        return message

    if session_id:
        key = (session_id, tool)
        last = _last_nudge_by_session.get(key)
        pool = [m for m in message if m != last] if last else message
        picked = random.choice(pool or message)
        _last_nudge_by_session[key] = picked
        return picked

    return random.choice(message)


async def send_nudge_message_raya(message: str, session_id: str, process_id: str = None) -> None:
    """Send a hold/nudge message to the voice vendor. At most one per turn."""
    if not session_id:
        return

    key = _turn_key(session_id, process_id)
    async with _turn_lock(key):
        if key in _nudge_sent_this_turn:
            logger.info(f"Skipping duplicate hold message for turn {key}")
            return
        _nudge_sent_this_turn.add(key)
        prefix = f"{session_id}:"
        for stale in [k for k in list(_nudge_sent_this_turn) if k.startswith(prefix) and k != key]:
            _nudge_sent_this_turn.discard(stale)

    try:
        nudge_url = settings.nudge_api_url
        payload = {
            "message": message,
            "session_id": session_id
        }
        if process_id:
            payload["process_id"] = process_id

        response = httpx.post(
            nudge_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=5
        )

        if response.status_code == 200:
            logger.info(f"Nudge message sent successfully: {message}")
        else:
            logger.warning(f"Failed to send nudge message. Status: {response.status_code}")

    except httpx.RequestException as e:
        logger.error(f"Error sending nudge message: {e}")
    except Exception as e:
        logger.error(f"Unexpected error sending nudge message: {e}")
