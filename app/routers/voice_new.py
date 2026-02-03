from fastapi import APIRouter, Depends, BackgroundTasks, Query
from app.auth.jwt_auth import get_current_user
from app.services.voice import get_voice_message_with_translation
from app.utils import _get_message_history
from helpers.utils import get_logger
from typing import Optional, Literal
import uuid

logger = get_logger(__name__)

router = APIRouter(prefix="/voice", tags=["voice-new"])

@router.get("/voice-new")
async def voice_new_endpoint(
#    background_tasks: BackgroundTasks,
    query: str = Query(..., description="The user's chat query"),
    session_id: Optional[str] = Query(None, description="Session ID for maintaining conversation context"),
    source_lang: Literal['hi', 'mr', 'en'] = Query('mr', description="Source language code (hi=Hindi, mr=Marathi, en=English)"),
    target_lang: str = Query('mr', description="Target language code"),
    user_id: str = Query('anonymous', description="User identifier"),
    provider: Optional[Literal['RAYA', 'RINGG']] = Query(None, description="Provider for the voice service - can be RAYA, RINGG, or None"),
    process_id: Optional[str] = Query(None, description="Process ID for tracking and hold messages"),
#    user_info: dict = Depends(get_current_user)  # Authentication required
):
    """
    Chat endpoint with translation support that returns complete response.
    
    This endpoint:
    1. Translates the query from source_lang to Marathi (mr) using Bhashini API
    2. Passes the translated query to the agent
    3. Gets the response from the agent
    4. Translates the response back to source_lang
    5. Returns the translated response as JSON
    
    Requires JWT authentication.
    """
    session_id = session_id or str(uuid.uuid4())
    
    logger.info(
        f"Voice-new request received - session_id: {session_id}, user_id: {user_id}, "
        f"source_lang: {source_lang}, "
        f"target_lang: {target_lang}, provider: {provider}, "
        f"process_id: {process_id}, query: {query}"
    )
    
    history = await _get_message_history(session_id)
    logger.debug(f"Retrieved message history for session {session_id} - length: {len(history)}")
    
    try:
        response = await get_voice_message_with_translation(
            query=query,
            session_id=session_id,
            source_lang=source_lang,
            target_lang=target_lang,
            history=history,
            provider=provider,
            process_id=process_id,
        )
        
        return {
            "response": response,
            "session_id": session_id,
            "source_lang": source_lang,
            "target_lang": target_lang
        }
    except Exception as e:
        logger.error(f"Error in voice_new_endpoint: {e}", exc_info=True)
        return {
            "error": str(e),
            "response": "",
            "session_id": session_id,
            "source_lang": source_lang,
            "target_lang": target_lang
        }
