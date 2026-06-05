import os
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Optional, Literal
# from fastapi import BackgroundTasks
from pydantic_ai.exceptions import UsageLimitExceeded

from agents.models import (
    AZURE_FALLBACK_DEPLOYMENT,
    AZURE_LLM_MODEL,
    LLM_AGRINET_MODEL_NAME,
    LLM_PROVIDER,
)
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
    return settings.llm_provider or os.getenv("LLM_PROVIDER") or LLM_PROVIDER


def _langfuse_vllm_model() -> str:
    if settings.llm_model_name:
        return settings.llm_model_name
    return LLM_AGRINET_MODEL_NAME or os.getenv("LLM_AGRINET_MODEL_NAME") or "agrinet-model"


def _langfuse_azure_model() -> str:
    return AZURE_FALLBACK_DEPLOYMENT or os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME") or "gpt-4.1"


def _langfuse_llm_model() -> Optional[str]:
    """Default/primary model label (vLLM) for trace tags before the run completes."""
    provider = (_langfuse_llm_provider() or "vllm").lower()
    if provider == "azure-openai":
        return _langfuse_azure_model()
    return _langfuse_vllm_model()


def _infer_langfuse_model_from_response(response: Any) -> str:
    """Pick Langfuse model from the last model response in the run (handles HTTP fallback)."""
    azure_model = _langfuse_azure_model()
    vllm_model = _langfuse_vllm_model()
    try:
        messages = response.all_messages() if hasattr(response, "all_messages") else []
        for msg in reversed(messages):
            model_name = getattr(msg, "model_name", None)
            if not model_name:
                continue
            if azure_model in model_name or "gpt-4" in model_name:
                return azure_model
            if vllm_model in model_name or "agrinet" in model_name:
                return vllm_model
    except Exception:
        logger.debug("Could not infer Langfuse model from run messages", exc_info=True)
    return vllm_model


def _langfuse_record_model_used(
    lf_client: Any,
    lf_obs: Any,
    lf_gen: Any,
    tags: list[str],
    model_used: str,
) -> None:
    """Record the model that actually served the request (Langfuse SDK v4)."""
    if lf_gen is not None:
        try:
            lf_gen.update(name=model_used, model=model_used)
        except Exception:
            logger.debug("Langfuse generation model update failed", exc_info=True)

    if lf_obs is not None:
        try:
            lf_obs.update(metadata={"llm_model": model_used})
        except Exception:
            logger.debug("Langfuse observation metadata update failed", exc_info=True)

    trace_id = getattr(lf_obs, "trace_id", None)
    if not trace_id:
        return

    updated_tags = [t for t in tags if not t.startswith("llm_model:")]
    updated_tags.append(f"llm_model:{model_used}")

    # Langfuse 4.x has no public update_trace(); tag updates go via ingestion.
    create_trace_tags = getattr(lf_client, "_create_trace_tags_via_ingestion", None)
    if create_trace_tags is None:
        return
    try:
        create_trace_tags(trace_id=trace_id, tags=updated_tags)
    except Exception:
        logger.debug("Langfuse trace tag update failed", exc_info=True)


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


@dataclass
class VoiceAgentRun:
    response: Any
    langfuse_model: str


async def _run_voice_agent(
    *,
    user_prompt: str,
    message_history: list,
    deps: FarmerContext,
    usage_limits: Any = agrinet_vllm_usage_limits,
) -> VoiceAgentRun:
    """
    Run the voice agent on vLLM (with HTTP-level Azure fallback).

    On UsageLimitExceeded (cumulative tokens across tool loops), retry once on Azure
    with a smaller history so the run stays within limits.
    """
    try:
        response = await voice_agent.run(
            user_prompt=user_prompt,
            message_history=message_history,
            deps=deps,
            usage_limits=usage_limits,
        )
        return VoiceAgentRun(
            response=response,
            langfuse_model=_infer_langfuse_model_from_response(response),
        )
    except UsageLimitExceeded as exc:
        if AZURE_LLM_MODEL is None:
            raise
        reduced_history = trim_history(
            message_history,
            max_tokens=8_000,
            include_system_prompts=False,
            include_tool_calls=False,
        )
        logger.warning(
            "Usage limit exceeded on vLLM (%s); retrying on Azure %s "
            "(history %s -> %s messages)",
            exc,
            AZURE_FALLBACK_DEPLOYMENT,
            len(message_history),
            len(reduced_history),
        )
        with voice_agent.override(model=AZURE_LLM_MODEL):
            response = await voice_agent.run(
                user_prompt=user_prompt,
                message_history=reduced_history,
                deps=deps,
                usage_limits=usage_limits,
            )
        return VoiceAgentRun(response=response, langfuse_model=_langfuse_azure_model())


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
    # Fallback scopes hold-message dedup to this request when the client omits process_id.
    effective_process_id = process_id or content_id
    deps = FarmerContext(query=query,
                         lang_code=source_lang,
                         target_lang=target_lang,
                         provider=provider,
                         session_id=session_id,
                         process_id=effective_process_id
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

        # Plain-text one-shot response via run(), not run_stream(). On pydantic-ai 0.2.4
        # with vLLM (e.g. Qwen), run_stream omits final text after tool-call loops.
        final_text = ""
        new_messages: list = []
        lf_client = get_langfuse()
        model_used = _langfuse_vllm_model()
        try:
            if lf_obs is not None and lf_client is not None:
                with lf_client.start_as_current_observation(
                    as_type="generation",
                    name=_langfuse_vllm_model(),
                    model=_langfuse_vllm_model(),
                    input={"user_prompt": user_message},
                ) as lf_gen:
                    agent_run = await _run_voice_agent(
                        user_prompt=user_message,
                        message_history=trimmed_history,
                        deps=deps,
                    )
                    model_used = agent_run.langfuse_model
                    response = agent_run.response
                    raw_out = getattr(response, "output", None)
                    final_text = "" if raw_out is None else str(raw_out)
                    new_messages = (
                        response.new_messages() if hasattr(response, "new_messages") else []
                    )
                    lf_gen.update(
                        output=final_text,
                        model=model_used,
                        usage_details=_langfuse_usage_details(response),
                    )
                    _langfuse_record_model_used(lf_client, lf_obs, lf_gen, tags, model_used)
            else:
                agent_run = await _run_voice_agent(
                    user_prompt=user_message,
                    message_history=trimmed_history,
                    deps=deps,
                )
                model_used = agent_run.langfuse_model
                response = agent_run.response
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
            yield final_text
        except Exception:
            logger.exception("Voice agent run failed for session %s", session_id)
            yield "क्षमा करा, प्रतिसाद तयार करता आला नाही. कृपया पुन्हा प्रयत्न करा."

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
        effective_process_id = process_id or f"query_{session_id}_{len(history)//2 + 1}"
        deps = FarmerContext(
            query=translated_query,
            lang_code='mr',
            target_lang='mr',
            provider=provider,
            session_id=session_id,
            process_id=effective_process_id
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
        model_used = _langfuse_vllm_model()
        if lf_obs is not None and lf_client is not None:
            with lf_client.start_as_current_observation(
                as_type="generation",
                name=_langfuse_vllm_model(),
                model=_langfuse_vllm_model(),
                input={"user_prompt": user_message},
            ) as lf_gen:
                agent_run = await _run_voice_agent(
                    user_prompt=user_message,
                    message_history=trimmed_history,
                    deps=deps,
                )
                model_used = agent_run.langfuse_model
                response = agent_run.response
                lf_gen.update(
                    output=getattr(response, "output", None),
                    model=model_used,
                    usage_details=_langfuse_usage_details(response),
                )
                _langfuse_record_model_used(lf_client, lf_obs, lf_gen, tags, model_used)
        else:
            agent_run = await _run_voice_agent(
                user_prompt=user_message,
                message_history=trimmed_history,
                deps=deps,
            )
            model_used = agent_run.langfuse_model
            response = agent_run.response
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