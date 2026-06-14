from fastapi import APIRouter, Depends, BackgroundTasks
from fastapi.responses import StreamingResponse
from app.services.voice import stream_voice_message
from app.utils import _get_message_history
from app.models.requests import ChatRequest
from helpers.utils import get_logger
import uuid
import asyncio
from typing import AsyncGenerator

logger = get_logger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])

_session_locks: dict[str, asyncio.Lock] = {}


def _get_session_lock(session_id: str) -> asyncio.Lock:
    if session_id not in _session_locks:
        _session_locks[session_id] = asyncio.Lock()
    return _session_locks[session_id]


async def _locked_stream(
    lock: asyncio.Lock,
    generator: AsyncGenerator[str, None],
    session_id: str,
) -> AsyncGenerator[str, None]:
    """Hold the session lock for the entire lifetime of the stream."""
    async with lock:
        async for chunk in generator:
            yield chunk


@router.get("/")
async def voice_endpoint(
#    background_tasks: BackgroundTasks,
    request: ChatRequest = Depends(),
):
    """
    Chat endpoint that streams responses back to the client.
    Authentication disabled.
    """
    session_id = request.session_id or str(uuid.uuid4())
    
    logger.info(
        f"Chat request received - session_id: {session_id}, user_id: {request.user_id}, "
        f"source_lang: {request.source_lang}, target_lang: {request.target_lang}, "
        f"provider: {request.provider}, process_id: {request.process_id}, query: {request.query}"
    )
    
    lock = _get_session_lock(session_id)
    if lock.locked():
        logger.info(f"Request queued for session {session_id} (another request in progress)")
    
    history = await _get_message_history(session_id, target_lang=request.target_lang)
    logger.debug(f"Retrieved message history for session {session_id} - length: {len(history)}")
        
    return StreamingResponse(
        _locked_stream(
            lock,
            stream_voice_message(
                query=request.query,
                session_id=session_id,
                source_lang=request.source_lang,
                target_lang=request.target_lang,
                history=history,
                provider=request.provider,
                process_id=request.process_id,
            ),
            session_id,
        ),
        media_type='text/plain; charset=utf-8'
    )
