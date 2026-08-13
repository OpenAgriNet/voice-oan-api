from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from app.auth.jwt_auth import get_current_user
from app.models.openai_models import ChatCompletionRequest
from app.services.openai_service import generate_openai_stream, generate_openai_response
from app.auth.jwt_auth import get_current_user
from helpers.utils import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])

@router.post("/chat-dev/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_user_id: str = Header(..., alias="X-User-ID"),
    x_session_id: str = Header(..., alias="X-Session-ID"),
    x_language: str = Header("none", alias="X-Language", deprecated=True),
    current_user=Depends(get_current_user),
):
    """
    OpenAI-compatible chat completions endpoint with streaming support.
    
    This endpoint accepts messages in the standard OpenAI API format and returns
    responses compatible with OpenAI's chat completion API. Supports both streaming
    and non-streaming responses.
    
    Headers:
    - X-Tenant-ID: Tenant identifier (required)
    - X-User-ID: User identifier (required)
    - X-Session-ID: Session identifier (required)
    - X-Language: DEPRECATED and ignored. The assistant detects the conversation
      language from the farmer's own words and replies in it. Still accepted (any
      value) so existing callers do not break.

    The response reports the language that was actually used — read it to pick the
    text-to-speech voice:
    - { "audio": "...", "language": "<ISO 639-1>", "end_interaction": false } for normal responses
    - { "audio": "...", "language": "<ISO 639-1>", "end_interaction": true } for ending conversations

    `language` is one of: 'en', 'hi', 'bn', 'te', 'mr', 'ta', 'gu', 'kn', 'ml', 'as'.
    """
    # Use header values directly
    user_id = x_user_id
    tenant_id = x_tenant_id
    session_id = x_session_id

    logger.info(
        f"Voice API chat completions request - session_id: {session_id}, "
        f"stream: {request.stream}, model: {request.model}"
    )

    # X-Language is no longer validated or acted on — the language is detected from
    # the user's message. Log it only to see which callers still send it.
    if x_language and x_language != "none":
        logger.info(
            f"Voice API ignoring deprecated X-Language header '{x_language}', "
            f"session_id: {session_id}"
        )

    if not request.messages:
        logger.error(f"Voice API missing messages field, session_id: {session_id}", stack_info=True)
        raise HTTPException(status_code=400, detail="messages field is required")

    user_messages = [msg for msg in request.messages if msg.role == "user"]
    if not user_messages:
        logger.error(f"Voice API no user message in request, session_id: {session_id}", stack_info=True)
        raise HTTPException(status_code=400, detail="At least one user message is required")

    if request.stream:
        logger.info(f"Voice API starting streaming response, session_id: {session_id}")
        try:
            return StreamingResponse(
                generate_openai_stream(
                    request=request,
                    session_id=session_id,
                    user_id=user_id,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                }
            )
        except Exception as e:
            logger.error(
                f"Voice API streaming error, session_id: {session_id}, error: {e!r}",
                exc_info=True,
            )
            raise
    else:
        logger.info(f"Voice API starting non-streaming response, session_id: {session_id}")
        try:
            response = await generate_openai_response(
                request=request,
                session_id=session_id,
                user_id=user_id,
            )
            logger.info(f"Voice API non-streaming response ready, session_id: {session_id}")
            return response
        except Exception as e:
            logger.error(
                f"Voice API non-streaming error, session_id: {session_id}, error: {e!r}",
                exc_info=True,
            )
            raise

@router.get("/")
async def openai_root():
    """Root endpoint for OpenAI-compatible API"""
    return {
        "message": "OpenAI-compatible API server",
        "endpoint": "/v1/chat/completions",
        "version": "1.0"
    }
