from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from app.models.openai_models import ChatCompletionRequest
from app.services.openai_service import generate_openai_stream, generate_openai_response
from helpers.utils import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])

@router.post("/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_user_id: str = Header(..., alias="X-User-ID"),
    x_session_id: str = Header(..., alias="X-Session-ID"),
    x_language: str = Header("hi", alias="X-Language"),
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
    - X-Language: Language code (optional, defaults to 'hi'). Supported: 'en', 'hi'
    
    The response includes special payloads for Samvaad integration:
    - { "audio": "..." } for normal responses
    - { "end_interaction": true, "audio": "..." } for ending conversations
    """
    # Use header values directly
    user_id = x_user_id
    tenant_id = x_tenant_id
    session_id = x_session_id
    target_lang = x_language
    
    # Validate language code
    valid_languages = ["en", "hi"]
    if target_lang not in valid_languages:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid language code '{target_lang}'. Supported languages: {', '.join(valid_languages)}"
        )
    
    logger.info(
        f"OpenAI chat completion request - user_id: {user_id}, tenant_id: {tenant_id}, "
        f"session_id: {session_id}, language: {target_lang}, stream: {request.stream}, model: {request.model}"
    )
    
    # Validate messages
    if not request.messages:
        raise HTTPException(status_code=400, detail="messages field is required")
    
    # Check if there's at least one user message
    user_messages = [msg for msg in request.messages if msg.role == "user"]
    if not user_messages:
        raise HTTPException(status_code=400, detail="At least one user message is required")
    
    # Handle streaming response
    if request.stream:
        return StreamingResponse(
            generate_openai_stream(
                request=request,
                session_id=session_id,
                user_id=user_id,
                target_lang=target_lang
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
    else:
        # Non-streaming response
        response = await generate_openai_response(
            request=request,
            session_id=session_id,
            user_id=user_id,
            target_lang=target_lang
        )
        return response

@router.get("/")
async def openai_root():
    """Root endpoint for OpenAI-compatible API"""
    return {
        "message": "OpenAI-compatible API server",
        "endpoint": "/v1/chat/completions",
        "version": "1.0"
    }
