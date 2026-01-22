from typing import AsyncGenerator, Dict, Any
import json
import time
import uuid
from helpers.utils import get_logger
from app.models.openai_models import ChatCompletionRequest
from app.services.voice import stream_voice_message
from app.utils import _get_message_history

logger = get_logger(__name__)

async def generate_openai_stream(
    request: ChatCompletionRequest,
    session_id: str,
    user_id: str,
    target_lang: str = "en"
) -> AsyncGenerator[str, None]:
    """
    Generate OpenAI-compatible SSE streaming response.
    Streams structured JSON output incrementally as content deltas.
    
    The response format follows OpenAI's streaming structured output pattern:
    - Each chunk contains a partial JSON string in the content delta
    - Client reassembles the complete JSON from all content chunks
    """
    completion_id = f"chatcmpl-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    created_timestamp = int(time.time())
    
    # Get the last user message (this will be the current query)
    user_messages = [msg.content for msg in request.messages if msg.role == "user"]
    if not user_messages:
        yield f"data: {json.dumps({'error': 'No user message found'})}\n\n"
        yield "data: [DONE]\n\n"
        return
    
    query = user_messages[-1]
    
    # Get existing history for the session (already in ModelMessage format)
    existing_history = await _get_message_history(session_id)
    
    # Stream response from voice agent - each chunk is a small piece of the JSON
    async for chunk in stream_voice_message(
        query=query,
        session_id=session_id,
        source_lang=target_lang,
        target_lang=target_lang,
        user_id=user_id,
        history=existing_history
    ):
        if chunk:
            # Stream each chunk as content delta (standard OpenAI streaming format)
            chunk_data = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created_timestamp,
                "model": request.model,
                "choices": [{
                    "index": 0,
                    "delta": {
                        "content": chunk
                    },
                    "finish_reason": None
                }]
            }
            yield f"data: {json.dumps(chunk_data)}\n\n"
    
    # Send finish chunk with empty delta
    finish_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created_timestamp,
        "model": request.model,
        "choices": [{
            "index": 0,
            "delta": {},
            "finish_reason": "stop"
        }]
    }
    yield f"data: {json.dumps(finish_chunk)}\n\n"
    
    # Send [DONE]
    yield "data: [DONE]\n\n"

async def generate_openai_response(
    request: ChatCompletionRequest,
    session_id: str,
    user_id: str,
    target_lang: str = "en"
) -> Dict[str, Any]:
    """
    Generate OpenAI-compatible non-streaming response.
    Returns the complete structured JSON output as content.
    """
    completion_id = f"chatcmpl-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    created_timestamp = int(time.time())
    
    # Get the last user message (this will be the current query)
    user_messages = [msg.content for msg in request.messages if msg.role == "user"]
    if not user_messages:
        return {
            "error": "No user message found"
        }
    
    query = user_messages[-1]
    
    # Get existing history for the session (already in ModelMessage format)
    existing_history = await _get_message_history(session_id)
    
    # Collect response from voice agent (accumulates small JSON chunks)
    accumulated_content = ""
    
    async for chunk in stream_voice_message(
        query=query,
        session_id=session_id,
        source_lang=target_lang,
        target_lang=target_lang,
        user_id=user_id,
        history=existing_history
    ):
        if chunk:
            accumulated_content += chunk
    
    # Calculate token usage (approximate)
    prompt_tokens = sum(len(msg.content.split()) for msg in request.messages) * 1.3
    completion_tokens = len(accumulated_content.split()) * 1.3
    
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created_timestamp,
        "model": request.model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": accumulated_content
            },
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "total_tokens": int(prompt_tokens + completion_tokens)
        }
    }