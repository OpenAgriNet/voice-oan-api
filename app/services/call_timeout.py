"""
Inactivity-based call-end detection (for demo/POC).

Instead of relying on the telephony vendor to POST /voice/call-ended, we treat a
call as "ended" when no new user turn arrives within CALL_INACTIVITY_TIMEOUT
seconds of the last turn. On timeout we run the same post-call extraction.

Each turn resets the timer (debounce). This is in-memory and per-process, so it
is intended for single-worker demo setups, not multi-worker production where the
vendor webhook remains the source of truth.
"""
from __future__ import annotations

import asyncio
import os

from helpers.utils import get_logger

logger = get_logger(__name__)

_INACTIVITY_TIMEOUT = int(os.getenv("CALL_INACTIVITY_TIMEOUT", "60"))

# session_id -> pending timer task
_timers: dict[str, asyncio.Task] = {}


def cancel(session_id: str) -> None:
    """Cancel a pending inactivity timer (called when a new turn arrives)."""
    task = _timers.pop(session_id, None)
    if task and not task.done():
        task.cancel()


def schedule(session_id: str, user_id: str | None) -> None:
    """(Re)start the inactivity timer for a session after a completed turn."""
    if not user_id:
        return  # no identity -> nothing to save
    cancel(session_id)

    async def _wait_then_save() -> None:
        try:
            await asyncio.sleep(_INACTIVITY_TIMEOUT)
        except asyncio.CancelledError:
            return
        _timers.pop(session_id, None)
        logger.info(
            "Inactivity timeout (%ss) for session %s — running post-call save",
            _INACTIVITY_TIMEOUT,
            session_id,
        )
        # Imported lazily to avoid a circular import at module load.
        from app.routers.voice_webhook import _run_post_call_extraction

        await _run_post_call_extraction(
            session_id=session_id,
            user_id=user_id,
            run_id=session_id,
        )

    _timers[session_id] = asyncio.create_task(_wait_then_save())
