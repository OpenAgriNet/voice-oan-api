from fastapi import APIRouter, Depends, BackgroundTasks
from fastapi.responses import StreamingResponse
from app.auth.jwt_auth import get_current_user
from app.config import settings
from app.services.voice import stream_voice_message
from app.utils import _get_message_history
from app.models.requests import ChatRequest
from helpers.utils import get_logger
import uuid

logger = get_logger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])

@router.get("/")
async def voice_endpoint(
    request: ChatRequest = Depends(),
    user_info: dict = Depends(get_current_user),
):
    """
    Voice endpoint that streams responses back to the client.
    Requires JWT authentication (Authorization: Bearer <token>).
    JWT is validated using the public key from JWT_PUBLIC_KEY env or JWT_PUBLIC_KEY_PATH file.
    session_id is used for message history and Langfuse Sessions: same ID groups all agent runs for one conversation.
    """
    session_id = request.session_id or str(uuid.uuid4())
    use_translation_pipeline = settings.enable_translation_pipeline and bool(request.use_translation_pipeline)
    if request.use_translation_pipeline and not settings.enable_translation_pipeline:
        logger.warning("Translation pipeline requested but disabled by ENABLE_TRANSLATION_PIPELINE")

    logger.info(
        f"Voice request received - session_id: {session_id}, user_id: {request.user_id}, "
        f"source_lang: {request.source_lang}, "
        f"target_lang: {request.target_lang}, provider: {request.provider}, process_id: {request.process_id}, "
        f"use_translation_pipeline: {use_translation_pipeline}, query: {request.query}"
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
            user_info=user_info,
            use_translation_pipeline=use_translation_pipeline,
        ),
        media_type='text/event-stream'
    ) 
