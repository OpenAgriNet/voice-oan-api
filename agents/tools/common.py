import os
import json
import random
import httpx
import asyncio
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
from helpers.utils import get_logger
from app.config import settings

logger = get_logger(__name__)

# NOTE: No need to cache right now.
# Loading a small json and finding the message for the tool and language code should be very fast anyway now.

# Resolve assets path from this file so it works regardless of process cwd (e.g. Docker, different CWD).
_NUDGE_MESSAGES_PATH = Path(__file__).resolve().parent.parent.parent / "assets" / "nudge_messages.json"


def get_random_nudge_message(lang_code: str = "en") -> str:
    """Get a random nudge (hold) message for the given language from assets/nudge_messages.json.
    Expects a "hold_messages" key with per-lang lists of messages; falls back to "en" if lang missing.
    """
    with open(_NUDGE_MESSAGES_PATH, "r") as f:
        data = json.load(f)
    hold = data.get("hold_messages", {})
    messages = hold.get(lang_code) or hold.get("en") or []
    if not messages:
        return "Please hold."
    return random.choice(messages)


def get_nudge_message(tool: str, lang_code: str = "en") -> str:
    """Get a nudge message for a specific tool and action in the specified language.
    Load json and then return the message for the tool and language code.
    """
    with open(_NUDGE_MESSAGES_PATH, "r") as f:
        nudge_data = json.load(f)
    return nudge_data[tool][lang_code]

async def send_feedback_prompt_raya(
    session_id: str, process_id: Optional[str], lang_code: str = "gu"
) -> None:
    """Send the feedback question (1-5 scale) to the user via RAYA nudge API."""
    from app.services.feedback import get_feedback_question

    message = get_feedback_question(lang_code)
    await send_nudge_message_raya(message, session_id, process_id)


async def send_nudge_message_raya(message: str, session_id: str, process_id: str = None) -> None:
    """Internal function to send nudge message synchronously."""
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