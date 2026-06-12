import os
import json
import random
import asyncio
from pathlib import Path
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

# Load nudge messages once at import time so a missing file (or wrong CWD on
# the server) surfaces at startup rather than on every tool call. Using an
# absolute path derived from __file__ also makes this resilient to the
# working directory the process happens to be launched from.
_NUDGE_MESSAGES_PATH = Path(__file__).resolve().parent.parent.parent / "assets" / "nudge_messages.json"
try:
    with open(_NUDGE_MESSAGES_PATH, "r", encoding="utf-8") as _f:
        _NUDGE_DATA: dict = json.load(_f)
except FileNotFoundError:
    logger.error(f"nudge_messages.json not found at {_NUDGE_MESSAGES_PATH}; nudge messages disabled")
    _NUDGE_DATA = {}

_NUDGE_FALLBACK = ""

# Bounded LRU for stale turn keys so _nudge_sent_this_turn doesn't grow without bound.
_NUDGE_TURN_MAX = 10_000


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
    The JSON is loaded once at module import. If the tool/language combo is missing,
    returns an empty string so callers can still proceed.
    If the message is a list (e.g. mr variants), one is chosen at random.
    When session_id is set, the last message used for that session+tool is skipped
    so consecutive tool calls in the same session get a different hold line.
    """
    tool_messages = _NUDGE_DATA.get(tool) if _NUDGE_DATA else None
    if not tool_messages:
        return _NUDGE_FALLBACK
    message = tool_messages.get(lang_code) or tool_messages.get("en") or _NUDGE_FALLBACK
    if not message:
        return _NUDGE_FALLBACK
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
        # Bound the set so it doesn't grow without bound across many sessions.
        if len(_nudge_sent_this_turn) > _NUDGE_TURN_MAX:
            # Drop ~10% of the oldest entries (FIFO).
            for stale in list(_nudge_sent_this_turn)[: _NUDGE_TURN_MAX // 10]:
                _nudge_sent_this_turn.discard(stale)

    try:
        nudge_url = settings.nudge_api_url
        payload = {
            "message": message,
            "session_id": session_id
        }
        if process_id:
            payload["process_id"] = process_id

        async with httpx.AsyncClient() as _client:
            response = await _client.post(
                nudge_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=5
            )

        if response.status_code == 200:
            logger.info(f"Nudge message sent successfully: {message}")
        else:
            logger.warning(f"Failed to send nudge message. Status: {response.status_code}")

    except httpx.RequestError as e:
        logger.error(f"Error sending nudge message: {e}")
    except Exception as e:
        logger.error(f"Unexpected error sending nudge message: {e}")
