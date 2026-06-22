"""
Post-call webhook — triggered by telephony (RAYA/RINGG) when a call ends.

Loads the session transcript from Redis and runs the extraction LLM to save
durable facts to Qdrant. This is a background operation; the HTTP response
returns immediately so telephony is not blocked.
"""
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel, Field
from typing import Optional
import uuid

from helpers.utils import get_logger
from app.utils import _get_message_history

logger = get_logger(__name__)

router = APIRouter(prefix="/voice", tags=["voice-webhook"])


class CallEndedRequest(BaseModel):
    session_id: str = Field(..., description="Session ID of the completed call")
    user_id: str = Field(..., description="Resolved farmer user_id (sha256 of phone)")
    run_id: Optional[str] = Field(None, description="Unique call identifier for provenance tagging")


async def _run_post_call_extraction(session_id: str, user_id: str, run_id: str) -> None:
    """Load transcript and persist facts via extraction LLM. Runs in background."""
    if not user_id or user_id == 'anonymous':
        logger.info("post-call: skipping memory for anonymous user (session %s)", session_id)
        return
    try:
        history = await _get_message_history(session_id)
        if not history:
            logger.info("post-call: no transcript found for session %s", session_id)
            return

        from app.services.memory import memory_service
        await memory_service.extract_and_save(
            user_id=user_id,
            run_id=run_id,
            history=history,
        )
    except Exception:
        logger.error(
            "post-call extraction failed for session %s user %s",
            session_id,
            user_id,
            exc_info=True,
        )


@router.post("/call-ended")
async def call_ended(request: CallEndedRequest, background_tasks: BackgroundTasks):
    """
    Webhook called by telephony when a call ends.
    Triggers background fact extraction from the call transcript.
    """
    run_id = request.run_id or request.session_id
    logger.info(
        "call-ended webhook received: session=%s user=%s run=%s",
        request.session_id,
        request.user_id,
        run_id,
    )
    background_tasks.add_task(
        _run_post_call_extraction,
        session_id=request.session_id,
        user_id=request.user_id,
        run_id=run_id,
    )
    return {"status": "accepted", "run_id": run_id}
