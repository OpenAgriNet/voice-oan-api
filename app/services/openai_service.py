from typing import AsyncGenerator, Dict, Any
import json
import time
import uuid
from helpers.utils import get_logger
from app.models.openai_models import ChatCompletionRequest
from app.services.voice import stream_voice_message
from app.utils import _get_message_history
from app.observability.langfuse_client import (
    safe_flush,
    safe_propagate_attributes,
    safe_start_observation,
)

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
    last_chunk = ""
    langfuse_tags = ["voice", "openai_compat", "stream"]

    # Root observation for the whole voice interaction (one HTTP request).
    with safe_start_observation(
        as_type="span",
        name="voice.chat_completions.stream",
        input={"query": query, "target_lang": target_lang, "model": request.model},
        metadata={"completion_id": completion_id, "stream": True},
        tags=langfuse_tags,
    ) as root_obs:
        with safe_propagate_attributes(
            user_id=user_id,
            session_id=session_id,
            tags=langfuse_tags,
        ):
            try:
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
            except Exception as e:
                if root_obs is not None:
                    try:
                        root_obs.update(
                            metadata={"error": repr(e), "completion_id": completion_id}
                        )
                    except Exception:
                        pass
                raise
            finally:
                # Update root observation output with a small summary.
                if root_obs is not None:
                    try:
                        root_obs.update(
                            output={
                                "last_chunk_preview": (last_chunk or "")[:500],
                                "last_chunk_len": len(last_chunk or ""),
                            }
                        )
                    except Exception:
                        pass
                # Best-effort flush for streaming requests (helps when workers restart).
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

    existing_history = await _get_message_history(session_id, target_lang=target_lang)

    # Only keep the last chunk (each chunk is a complete JSON object, not a delta)
    last_chunk = ""
    langfuse_tags = ["voice", "openai_compat", "non_stream"]
    if tenant_id:
        langfuse_tags.append(f"tenant:{tenant_id}")

    with safe_start_observation(
        as_type="span",
        name="voice.chat_completions",
        input={"query": query, "target_lang": target_lang, "model": request.model},
        metadata={"stream": False},
        tags=langfuse_tags,
    ) as root_obs:
        with safe_propagate_attributes(
            user_id=user_id,
            session_id=session_id,
            tags=langfuse_tags,
            metadata={"tenant_id": tenant_id} if tenant_id else None,
        ):
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
        if root_obs is not None:
            try:
                root_obs.update(
                    output={
                        "last_chunk_preview": (last_chunk or "")[:500],
                        "last_chunk_len": len(last_chunk or ""),
                    }
                )
            except Exception:
                pass
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