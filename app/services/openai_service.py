from typing import AsyncGenerator, Dict, Any, List
import json
import time
import uuid
from helpers.utils import get_logger
from app.models.openai_models import ChatCompletionRequest, Message
from app.services.voice import stream_voice_message
from app.utils import _get_message_history
from fastapi import BackgroundTasks

logger = get_logger(__name__)

def _convert_openai_messages_to_history(messages: List[Message]) -> list:
    """Convert OpenAI format messages to internal history format."""
    history = []
    for msg in messages:
        if msg.role == "system":
            # System messages are handled separately in the agent
            continue
        elif msg.role == "user":
            history.append({"role": "user", "content": msg.content})
        elif msg.role == "assistant":
            history.append({"role": "assistant", "content": msg.content})
    return history

def _extract_audio_payload(content: str) -> Dict[str, Any]:
    """
    Extract audio payload from content if it's in the format:
    { "audio": "..." } or { "end_interaction": true, "audio": "..." }
    """
    content = content.strip()
    if content.startswith("{") and content.endswith("}"):
        try:
            payload = json.loads(content)
            if "audio" in payload:
                return payload
        except json.JSONDecodeError:
            pass
    
    # If not a JSON payload, wrap it as audio
    return {"audio": content}

async def generate_openai_stream(
    request: ChatCompletionRequest,
    session_id: str,
    user_id: str,
    target_lang: str = "en",
    background_tasks: BackgroundTasks = None
) -> AsyncGenerator[str, None]:
    """
    Generate OpenAI-compatible SSE streaming response.
    Integrates with the existing voice agent and formats responses with audio payloads.
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
    # The existing history contains the conversation context
    # OpenAI messages in the request are just for the current turn
    existing_history = await _get_message_history(session_id)
    
    # Stream response from voice agent
    accumulated_content = ""
    audio_payload_sent = False
    
    async for chunk in stream_voice_message(
        query=query,
        session_id=session_id,
        source_lang=target_lang,
        target_lang=target_lang,
        user_id=user_id,
        history=existing_history,
        background_tasks=background_tasks
    ):
        if chunk:
            accumulated_content += chunk
            
            # Check if chunk is a JSON payload (from stream_voice_message)
            # stream_voice_message yields a complete JSON string like {"audio": "...", "end_interaction": true}
            chunk_stripped = chunk.strip()
            if chunk_stripped.startswith("{") and chunk_stripped.endswith("}"):
                try:
                    # Parse the JSON payload
                    payload = json.loads(chunk_stripped)
                    if "audio" in payload:
                        # Send audio payload in delta (not as content)
                        audio_payload_sent = True
                        audio_chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created_timestamp,
                            "model": request.model,
                            "choices": [{
                                "index": 0,
                                "delta": payload,  # Send the parsed payload directly
                                "finish_reason": None
                            }]
                        }
                        yield f"data: {json.dumps(audio_chunk)}\n\n"
                        continue  # Skip streaming as content
                except json.JSONDecodeError:
                    # If JSON parsing fails, treat as regular content
                    logger.warning(f"Failed to parse JSON chunk: {chunk[:100]}")
                    pass
            
            # Stream as regular content chunk (for non-JSON chunks)
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
    
    # If no audio payload was sent during streaming, extract and send it now (fallback)
    if accumulated_content and not audio_payload_sent:
        audio_payload = _extract_audio_payload(accumulated_content)
        audio_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created_timestamp,
            "model": request.model,
            "choices": [{
                "index": 0,
                "delta": audio_payload,
                "finish_reason": None
            }]
        }
        yield f"data: {json.dumps(audio_chunk)}\n\n"
    
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
    target_lang: str = "en",
    background_tasks: BackgroundTasks = None
) -> Dict[str, Any]:
    """
    Generate OpenAI-compatible non-streaming response.
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
    # The existing history contains the conversation context
    # OpenAI messages in the request are just for the current turn
    existing_history = await _get_message_history(session_id)
    
    # Collect response from voice agent
    accumulated_content = ""
    
    async for chunk in stream_voice_message(
        query=query,
        session_id=session_id,
        source_lang=target_lang,
        target_lang=target_lang,
        user_id=user_id,
        history=existing_history,
        background_tasks=background_tasks
    ):
        if chunk:
            accumulated_content += chunk
    
    # Extract audio payload
    audio_payload = _extract_audio_payload(accumulated_content)
    
    # Calculate token usage (approximate)
    prompt_tokens = sum(len(msg.content.split()) for msg in request.messages) * 1.3  # Rough estimate
    completion_tokens = len(accumulated_content.split()) * 1.3  # Rough estimate
    
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created_timestamp,
        "model": request.model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": accumulated_content,
                **audio_payload  # Include audio payload in message
            },
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "total_tokens": int(prompt_tokens + completion_tokens)
        }
    }