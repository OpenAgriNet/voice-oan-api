import asyncio
import json
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.auth.jwt_auth import get_current_user
from app.config import settings
from app.models.requests import ChatRequest
from app.services.voice import stream_voice_message
from app.utils import _get_message_history, claim_session_request_ownership
from helpers.utils import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


async def sse_wrapper(stream: AsyncIterator[Any]) -> AsyncIterator[str]:
    """
    Transport layer: wrap raw async text (or dict) chunks as SSE frames.
    Each chunk is one `data:` field; stream ends with an explicit `end` event.
    """
    first_logged = False
    async for chunk in stream:
        if not first_logged:
            logger.info(
                "sse_wrapper: first chunk received (type=%s)",
                type(chunk).__name__,
            )
            first_logged = True
        if isinstance(chunk, dict):
            payload = json.dumps(chunk, ensure_ascii=False)
        else:
            payload = str(chunk)
        yield f"data: {payload}\n\n"

    logger.info("sse_wrapper: stream ended")
    yield "event: end\ndata: done\n\n"


@router.get("/")
async def voice_endpoint(
    http_request: Request,
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
    use_translation_pipeline = settings.enable_translation_pipeline

    logger.info(
        f"Voice request received - session_id: {session_id}, user_id: {request.user_id}, "
        f"source_lang: {request.source_lang}, "
        f"target_lang: {request.target_lang}, provider: {request.provider}, process_id: {request.process_id}, "
        f"use_translation_pipeline: {use_translation_pipeline}, query: {request.query}"
    )
    owner = await claim_session_request_ownership(session_id)
    logger.info(
        "Session ownership claimed - session_id=%s epoch=%s token=%s process_id=%s",
        session_id,
        owner.epoch,
        owner.request_token,
        request.process_id,
    )

    history = await _get_message_history(session_id)
    logger.debug(f"Retrieved message history for session {session_id} - length: {len(history)}")

    return StreamingResponse(
        sse_wrapper(
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
                owner=owner,
                http_request=http_request,
            )
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )

#this is created for minimal testing of the SSE connection
# @router.get("/test-stream")
# async def voice_test_stream(_user: dict = Depends(get_current_user)):
#     """
#     Temporary: isolated SSE sanity check (dummy chunks, no LLM/voice business logic).
#     Same auth as /voice/ for consistency.
#     """

#     async def dummy_stream():
#         for i in range(5):
#             await asyncio.sleep(0.3)
#             yield f"voice test chunk {i + 1}"

#     return StreamingResponse(
#         sse_wrapper(dummy_stream()),
#         media_type="text/event-stream",
#         headers=SSE_HEADERS,
#     )
