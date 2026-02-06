from fastapi import APIRouter, Depends, BackgroundTasks, Query
from app.auth.jwt_auth import get_current_user
from app.services.voice import get_voice_message_with_translation
from app.utils import _get_message_history
from helpers.utils import get_logger
from typing import Optional
import uuid

logger = get_logger(__name__)

router = APIRouter(prefix="/voice-translation", tags=["voice-translation"])

@router.get("/")
async def voice_translation_endpoint(
#    background_tasks: BackgroundTasks,
    query: str = Query(..., description="The user's chat query"),
    session_id: Optional[str] = Query(None, description="Session ID for maintaining conversation context"),
    source_lang: str = Query('mr', description="Source language code"),
    target_lang: str = Query('mr', description="Target language code"),
    # provider: Optional[Literal['RAYA', 'RINGG']] = Query(None, description="Provider for the voice service - can be RAYA, RINGG, or None"),
    # process_id: Optional[str] = Query(None, description="Process ID for tracking and hold messages"),
#    user_info: dict = Depends(get_current_user)  # Authentication required
):
    """
    Chat endpoint with translation support that returns complete response.
    """
    session_id = session_id or str(uuid.uuid4())
    
    logger.info(
        f"Voice-new request received - session_id: {session_id}, "
        f"source_lang: {source_lang}, "
        f"target_lang: {target_lang}, query: {query}"
    )
    
    history = await _get_message_history(session_id)
    logger.debug(f"Retrieved message history for session {session_id} - length: {len(history)}")
    
    response = await get_voice_message_with_translation(
        query=query,
        session_id=session_id,
        source_lang=source_lang,
        target_lang=target_lang,
        history=history,
    )
    
    return {
        "response": response
    }
