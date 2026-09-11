from fastapi import APIRouter, Header, HTTPException, Depends
from fastapi.responses import StreamingResponse
from app.models.openai_models import ChatCompletionRequest
from app.services.openai_service import generate_openai_stream, generate_openai_response
from app.auth.jwt_auth import get_current_user
from app.core.languages import parse_iso_language_header
from helpers.utils import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])

@router.post("/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_user_id: str = Header(..., alias="X-User-ID"),
    x_session_id: str = Header(..., alias="X-Session-ID"),
    x_language: str = Header(..., alias="X-Language"),
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
    - X-Language: Sarvam-detected language code (required)

    X-Language is the authoritative language-detection result. Supported values
    are: en, hi, od, pa, ta, te, kn, ml, gu, mr, bn.
    """
    # Use header values directly
    user_id = x_user_id
    tenant_id = x_tenant_id
    session_id = x_session_id
    try:
        language_code = parse_iso_language_header(x_language)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info(
        f"Voice API chat completions request - session_id: {session_id}, "
        f"language: {x_language.strip().lower()}, stream: {request.stream}, "
        f"model: {request.model}"
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
                    language_code=language_code,
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
                tenant_id=tenant_id,
                language_code=language_code,
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
