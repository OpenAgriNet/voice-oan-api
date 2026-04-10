from typing import AsyncGenerator, Dict, Any
import json
import time
import uuid
from app.models.openai_models import ChatCompletionRequest
from app.services.voice import stream_voice_message
from app.utils import _get_message_history
from app.observability.voice import safe_update_observation, voice_output_summary
from app.observability.langfuse_client import (
    safe_flush,
    safe_propagate_attributes,
    safe_start_observation,
)

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

    existing_history = await _get_message_history(session_id, target_lang=target_lang, user_id=user_id)
    last_chunk = ""
    langfuse_tags = ["voice", "openai_compat", "stream"]

    with safe_start_observation(
        as_type="span",
        name="voice.chat_completions.stream",
        input={"query": query, "target_lang": target_lang, "model": request.model},
        tags=langfuse_tags,
    ) as root_obs:
        with safe_propagate_attributes(
            user_id=user_id,
            session_id=session_id,
            tags=langfuse_tags,
        ):
            try:
                with safe_start_observation(
                    as_type="generation",
                    name="voice.response.generation",
                    model=request.model,
                    input=query
                ) as generation_obs:
                    async for chunk in stream_voice_message(
                        query=query,
                        session_id=session_id,
                        source_lang=target_lang,
                        target_lang=target_lang,
                        user_id=user_id,
                        history=existing_history,
                    ):
                        if chunk:
                            last_chunk = chunk
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
                    safe_update_observation(generation_obs, voice_output_summary(last_chunk))
            except Exception:
                safe_update_observation(root_obs, {"audio": "", "end_interaction": False})
                raise
            finally:
                safe_update_observation(root_obs, voice_output_summary(last_chunk))
                # Flush at end of stream to avoid dropping buffered spans.
                safe_flush()

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
    target_lang: str = "en",
    tenant_id: str | None = None,
) -> Dict[str, Any]:
    """
    Generate OpenAI-compatible non-streaming response.
    Returns the final complete JSON output as content.
    """
    completion_id = f"chatcmpl-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    created_timestamp = int(time.time())
    query = [msg.content for msg in request.messages if msg.role == "user"][-1]

    existing_history = await _get_message_history(session_id, target_lang=target_lang, user_id=user_id)

    last_chunk = ""
    langfuse_tags = ["voice", "openai_compat", "non_stream"]
    if tenant_id:
        langfuse_tags.append(f"tenant:{tenant_id}")

    with safe_start_observation(
        as_type="span",
        name="voice.chat_completions",
        input={"query": query, "target_lang": target_lang, "model": request.model},
        tags=langfuse_tags,
    ) as root_obs:
        with safe_propagate_attributes(
            user_id=user_id,
            session_id=session_id,
            tags=langfuse_tags,
        ):
            with safe_start_observation(
                as_type="generation",
                name="voice.response.generation",
                model=request.model,
                input=query,
            ) as generation_obs:
                async for chunk in stream_voice_message(
                    query=query,
                    session_id=session_id,
                    source_lang=target_lang,
                    target_lang=target_lang,
                    user_id=user_id,
                    history=existing_history,
                ):
                    if chunk:
                        last_chunk = chunk
                safe_update_observation(generation_obs, voice_output_summary(last_chunk))
        safe_update_observation(root_obs, voice_output_summary(last_chunk))
        safe_flush()

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