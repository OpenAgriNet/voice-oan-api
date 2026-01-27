from fastapi import APIRouter, Depends, BackgroundTasks
from fastapi.responses import StreamingResponse
from app.services.voice import stream_voice_message
from app.utils import _get_message_history
from app.models.requests import ChatRequest
from app.auth.jwt_auth import get_current_user_optional
from helpers.utils import get_logger
from typing import Optional, Dict, Any
import uuid

logger = get_logger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])

@router.get("/")
async def voice_endpoint(
#    background_tasks: BackgroundTasks,
    request: ChatRequest = Depends(),
    jwt_token: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
):
    """
    Chat endpoint that streams responses back to the client.
    JWT authentication is optional - if provided, farmer info will be extracted and used.
    """
    session_id = request.session_id or str(uuid.uuid4())
    
    logger.info(
        f"Chat request received - session_id: {session_id}, user_id: {request.user_id}, "
        f"source_lang: {request.source_lang}, "
        f"target_lang: {request.target_lang}, provider: {request.provider}, "
        f"process_id: {request.process_id}, query: {request.query}, "
        f"jwt_token_present: {jwt_token is not None}"
    )
    
    history = await _get_message_history(session_id)
    logger.debug(f"Retrieved message history for session {session_id} - length: {len(history)}")
        
    return StreamingResponse(
        stream_voice_message(
            query=request.query,
            session_id=session_id,
            source_lang=request.source_lang,
            target_lang=request.target_lang,
            user_id=request.user_id,
            history=history,
            provider=request.provider,
            process_id=request.process_id,
            user_info=jwt_token,
#            background_tasks=background_tasks
        ),
        media_type='text/event-stream'
    ) 