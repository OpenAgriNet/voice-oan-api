import os
from typing import AsyncGenerator, Optional, Literal
# from fastapi import BackgroundTasks
from agents.voice import agrinet_vllm_usage_limits, voice_agent
from helpers.utils import get_logger
from app.langfuse_client import get_langfuse, traced_voice_request
from app.config import settings
from app.utils import (
    update_message_history, 
    trim_history, 
    format_message_pairs,
    clean_message_history_for_openai
)
from agents.deps import FarmerContext
from app.services.translation import translation_service

logger = get_logger(__name__)

def _langfuse_llm_provider() -> Optional[str]:
    return settings.llm_provider or os.getenv("LLM_PROVIDER")


def _langfuse_llm_model() -> Optional[str]:
    return (
        settings.llm_model_name
        or os.getenv("LLM_MODEL_NAME")
        # Common in this repo when using Azure OpenAI.
        or os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")
    )


def _langfuse_usage_details(run_result: object) -> Optional[dict[str, object]]:
    """
    Best-effort extraction of PydanticAI usage for Langfuse.

    Langfuse expects OpenAI-shaped keys like `input_tokens`/`output_tokens`/`total_tokens`.
    """
    usage = getattr(run_result, "usage", None)
    if callable(usage):
        usage = usage()
    if usage is None:
        return None

    def _get_int(name: str) -> int:
        v = getattr(usage, name, 0)
        return int(v or 0)

    input_tokens = _get_int("input_tokens")
    output_tokens = _get_int("output_tokens")

    out: dict[str, object] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }

    # Only send if we have any signal.
    if (input_tokens + output_tokens) > 0:
        return out
    return None


def _langfuse_kv_tags(**key_values: object) -> list[str]:
    """Build Langfuse tags as key:value strings; skip None and empty."""
    tags: list[str] = []
    for key, value in key_values.items():
        if value is None or value == "":
            continue
        tags.append(f"{key}:{value}")
    return tags


def _sse_encode(text: str) -> str:
    """Format a payload as one SSE event (see docs/VOICE_API_DOCUMENTATION.md)."""
    lines = (text or "").splitlines()
    if not lines:
        return "data: \n\n"
    return "".join(f"data: {line}\n" for line in lines) + "\n"


# Default trim history configuration for voice endpoints
def _trim_voice_history(history: list) -> list:
    """
    Helper function to trim history with standard voice endpoint settings.
    
    Args:
        history: Message history to trim
        
    Returns:
        Trimmed message history
    """
    return trim_history(
        history,
        max_tokens=80_000,
        include_system_prompts=True,
        include_tool_calls=True
    )

async def stream_voice_message(
    query: str,
    session_id: str,
    source_lang: str,
    target_lang: str,
    history: list,
    provider: Optional[Literal['RAYA', 'RINGG']] = None,
    process_id: Optional[str] = None,
    user_id: Optional[str] = None,
    user_info: Optional[dict] = None,
) -> AsyncGenerator[str, None]:
    """Async generator for streaming chat messages."""
    # Generate a unique content ID for this query
    content_id = f"query_{session_id}_{len(history)//2 + 1}"
    deps = FarmerContext(query=query,
                         lang_code=source_lang,
                         target_lang=target_lang,
                         provider=provider,
                         session_id=session_id,
                         process_id=process_id
                         )

    tags = [
        "voice",
        *_langfuse_kv_tags(
            environment=settings.environment or os.getenv("ENVIRONMENT"),
            source_lang=source_lang,
            target_lang=target_lang,
            provider=provider,
            history_message_count=len(history),
            content_id=content_id,
            llm_provider=_langfuse_llm_provider(),
            llm_model=_langfuse_llm_model(),
        ),
    ]

    with traced_voice_request(
        trace_name="voice",
        session_id=session_id,
        # Avoid sending PII to Langfuse by default.
        user_id=None,
        tags=tags,
        observation_name="voice-agent-stream",
        trace_input=query,
    ) as lf_obs:
        if lf_obs is not None:
            # Make tool observations robust to async context loss by explicitly
            # passing the trace context via deps.
            deps.langfuse_trace_id = getattr(lf_obs, "trace_id", None)
            deps.langfuse_root_observation_id = getattr(lf_obs, "id", None)

        message_pairs = "\n\n".join(format_message_pairs(history, 3))
        logger.info(f"Message pairs: {message_pairs}")

        user_message = deps.get_user_message()
        logger.info(f"Running agent with user message: {user_message}")

        # Clean message history to remove orphaned tool calls BEFORE passing to OpenAI API
        cleaned_history = clean_message_history_for_openai(history)
        if len(cleaned_history) != len(history):
            logger.warning(f"Cleaned {len(history) - len(cleaned_history)} orphaned tool calls from history")
            # Update the history in cache with cleaned version
            await update_message_history(session_id, cleaned_history)
            history = cleaned_history

        # Run the main agent
        trimmed_history = _trim_voice_history(history)
        logger.info(f"Trimmed history length: {len(trimmed_history)} messages")

        # One-shot SSE: clients expect `data: ...\n\n` (not raw text). Use `run()` so tool loops
        # finish reliably on OpenAI-compatible vLLM; `run_stream`/`get_output` can omit text there.
        final_text = ""
        new_messages: list = []
        lf_client = get_langfuse()
        try:
            if lf_obs is not None and lf_client is not None:
                with lf_client.start_as_current_observation(
                    as_type="generation",
                    name=_langfuse_llm_model() or "llm",
                    model=_langfuse_llm_model(),
                    input={"user_prompt": user_message},
                ) as lf_gen:
                    response = await voice_agent.run(
                        user_prompt=user_message,
                        message_history=trimmed_history,
                        deps=deps,
                        usage_limits=agrinet_vllm_usage_limits,
                    )
                    raw_out = getattr(response, "output", None)
                    final_text = "" if raw_out is None else str(raw_out)
                    new_messages = (
                        response.new_messages() if hasattr(response, "new_messages") else []
                    )
                    lf_gen.update(
                        output=final_text,
                        usage_details=_langfuse_usage_details(response),
                    )
            else:
                response = await voice_agent.run(
                    user_prompt=user_message,
                    message_history=trimmed_history,
                    deps=deps,
                    usage_limits=agrinet_vllm_usage_limits,
                )
                raw_out = getattr(response, "output", None)
                final_text = "" if raw_out is None else str(raw_out)
                new_messages = (
                    response.new_messages() if hasattr(response, "new_messages") else []
                )

            logger.info(
                "Voice agent finished for session %s, output_len=%s",
                session_id,
                len(final_text),
            )
            yield _sse_encode(final_text)
        except Exception:
            logger.exception("Voice agent run failed for session %s", session_id)
            yield _sse_encode(
                "क्षमा करा, प्रतिसाद तयार करता आला नाही. कृपया पुन्हा प्रयत्न करा."
            )

        messages = [
            *history,
            *new_messages,
        ]

        logger.info(f"Updating message history for session {session_id} with {len(messages)} messages")
        await update_message_history(session_id, messages)

        if lf_obs is not None:
            lf_obs.update(output=final_text)


async def get_voice_message_with_translation(
    query: str,
    session_id: str,
    history: list,
    provider: Optional[Literal['RAYA', 'RINGG']] = None,
    process_id: Optional[str] = None,
) -> str:
    tags = [
        "voice-bhili",
        *_langfuse_kv_tags(
            environment=settings.environment or os.getenv("ENVIRONMENT"),
            provider=provider,
            history_message_count=len(history),
            llm_provider=_langfuse_llm_provider(),
            llm_model=_langfuse_llm_model(),
        ),
    ]
    with traced_voice_request(
        trace_name="voice-bhili",
        session_id=session_id,
        user_id=None,
        tags=tags,
        observation_name="voice-bhili-agent",
        trace_input=query,
    ) as lf_obs:
        logger.info(f"Translating query from `bhb` to `mr` (Bhashini)")
        translated_query = await translation_service.translate_text(
            text=query,
            source_lang='bhb',
            target_lang='mr'
        )
        logger.info(f"Translated query: {translated_query}")

        # Use Marathi for the agent since we translated the query to `mr`
        deps = FarmerContext(
            query=translated_query,
            lang_code='mr',
            target_lang='mr',
            provider=provider,
            session_id=session_id,
            process_id=process_id
        )
        if lf_obs is not None:
            deps.langfuse_trace_id = getattr(lf_obs, "trace_id", None)
            deps.langfuse_root_observation_id = getattr(lf_obs, "id", None)

        message_pairs = "\n\n".join(format_message_pairs(history, 3))
        logger.info(f"Message pairs: {message_pairs}")

        user_message = deps.get_user_message()
        logger.info(f"Running agent with translated user message: {user_message}")

        # Clean message history to remove orphaned tool calls BEFORE passing to OpenAI API
        cleaned_history = clean_message_history_for_openai(history)
        if len(cleaned_history) != len(history):
            logger.warning(f"Cleaned {len(history) - len(cleaned_history)} orphaned tool calls from history")
            # Update the history in cache with cleaned version
            await update_message_history(session_id, cleaned_history)
            history = cleaned_history

        # Run the main agent
        trimmed_history = _trim_voice_history(history)
        logger.info(f"Trimmed history length: {len(trimmed_history)} messages")

        lf_client = get_langfuse()
        if lf_obs is not None and lf_client is not None:
            with lf_client.start_as_current_observation(
                as_type="generation",
                name=_langfuse_llm_model() or "llm",
                model=_langfuse_llm_model(),
                input={"user_prompt": user_message},
            ) as lf_gen:
                response = await voice_agent.run(
                    user_prompt=user_message,
                    message_history=trimmed_history,
                    deps=deps,
                    usage_limits=agrinet_vllm_usage_limits,
                )
                lf_gen.update(
                    output=getattr(response, "output", None),
                    usage_details=_langfuse_usage_details(response),
                )
        else:
            response = await voice_agent.run(
                user_prompt=user_message,
                message_history=trimmed_history,
                deps=deps,
                usage_limits=agrinet_vllm_usage_limits,
            )
        # `pydantic_ai` run results include the messages generated for this run.
        new_messages = response.new_messages() if hasattr(response, "new_messages") else []
        if new_messages:
            messages = [
                *history,
                *new_messages,
            ]
            logger.info(
                f"Updating message history for session {session_id} with {len(messages)} messages"
            )
            await update_message_history(session_id, messages)
        text_response = response.output
        logger.info(f"Text response: {text_response}")

        # Translate the response back to source_lang
        if response.output:
            # Always translate back to source_lang, even if source_lang is 'mr'
            # (translation service will handle no-op case)
            logger.info(f"Translating response from `mr` (Marathi) to `bhb` (Bhashini)")
            translated_response = await translation_service.translate_text(
                text=text_response,
                source_lang='mr',
                target_lang='bhb'
            )
            logger.info(f"Successfully translated response to `bhb`. Length: {len(translated_response)} chars")
            logger.debug(f"Translated response preview: {translated_response[:200]}...")

            if lf_obs is not None:
                lf_obs.update(
                    output={
                        "query_bhb": query,
                        "response_bhb": translated_response,
                        "agent_response_mr": text_response,
                        "query_mr": translated_query,
                    }
                )
            return translated_response

        logger.warning("Empty response from agent, nothing to translate")
        if lf_obs is not None:
            lf_obs.update(output="")
        return ""