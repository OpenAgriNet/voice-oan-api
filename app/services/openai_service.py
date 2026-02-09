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

    Each chunk contains the latest complete JSON payload in the content delta.
    The client should use the most recent chunk as the current state.
    """
    completion_id = f"chatcmpl-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    created_timestamp = int(time.time())
    query = [msg.content for msg in request.messages if msg.role == "user"][-1]

    existing_history = await _get_message_history(session_id, target_lang=target_lang)

    async for chunk in stream_voice_message(
        query=query,
        session_id=session_id,
        source_lang=target_lang,
        target_lang=target_lang,
        user_id=user_id,
        history=existing_history
    ):
        if chunk:
            chunk_data = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created_timestamp,
                "model": request.model,
                "choices": [{
                    "index": 0,
                    "delta": {"content": chunk},
                    "finish_reason": None
                }]
            }
            yield f"data: {json.dumps(chunk_data)}\n\n"

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
    yield "data: [DONE]\n\n"

async def generate_openai_response(
    request: ChatCompletionRequest,
    session_id: str,
    user_id: str,
    target_lang: str = "en"
) -> Dict[str, Any]:
    """
    Generate OpenAI-compatible non-streaming response.
    Returns the final complete JSON output as content.
    """
    completion_id = f"chatcmpl-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    created_timestamp = int(time.time())
    query = [msg.content for msg in request.messages if msg.role == "user"][-1]

    existing_history = await _get_message_history(session_id, target_lang=target_lang)

    # Only keep the last chunk (each chunk is a complete JSON object, not a delta)
    last_chunk = ""
    async for chunk in stream_voice_message(
        query=query,
        session_id=session_id,
        source_lang=target_lang,
        target_lang=target_lang,
        user_id=user_id,
        history=existing_history
    ):
        if chunk:
            last_chunk = chunk

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created_timestamp,
        "model": request.model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": last_chunk
            },
            "finish_reason": "stop"
        }]
    }