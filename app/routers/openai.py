from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Depends
from fastapi.responses import StreamingResponse
from typing import Optional, Dict, Any
import uuid
from app.models.openai_models import ChatCompletionRequest
from app.services.openai_service import generate_openai_stream, generate_openai_response
from app.auth.jwt_auth import get_current_user
from helpers.utils import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])

@router.post("/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    background_tasks: BackgroundTasks,
    current_user: Dict[str, Any] = Depends(get_current_user),
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID"),
    x_language: Optional[str] = Header(None, alias="X-Language"),
):
    """
    OpenAI-compatible chat completions endpoint with streaming support.
    
    This endpoint accepts messages in the standard OpenAI API format and returns
    responses compatible with OpenAI's chat completion API. Supports both streaming
    and non-streaming responses.
    
    Requires authentication via JWT token in Authorization header.
    
    Headers:
    - Authorization: Bearer token (required, for authentication)
    - X-Session-ID: Session identifier (optional, will be generated if not provided)
    - X-Language: Language code (optional, defaults to 'en'). Supported: 'en', 'hi'
    
    The response includes special payloads for Samvaad integration:
    - { "audio": "..." } for normal responses
    - { "end_interaction": true, "audio": "..." } for ending conversations
    """
    # Extract user_id and tenant_id from JWT token
    # Try common JWT claim names: user_id, tenant_id, sub (subject), user (alternative)
    user_id = (
        current_user.get("user_id") or 
        current_user.get("sub") or 
        current_user.get("user") or 
        request.user or 
        "anonymous"
    )
    
    tenant_id = (
        current_user.get("tenant_id") or 
        current_user.get("tenant") or 
        request.tenant_id
    )
    
    # Extract session ID from header or generate one
    if x_session_id:
        session_id = x_session_id
    else:
        # Generate session ID from user_id and tenant_id if not provided
        session_id = f"{tenant_id or 'default'}_{user_id}_{uuid.uuid4().hex[:8]}"
    
    # Extract language from header or default to 'en'
    target_lang = x_language or "en"
    
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
                target_lang=target_lang,
                background_tasks=background_tasks
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
            target_lang=target_lang,
            background_tasks=background_tasks
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
