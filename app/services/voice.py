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

    Voice replies are short (2–3 sentences), so a small history window keeps
    prefill latency low. Tool calls/results from earlier turns are stripped
    because the model rarely needs to see them on the next user turn — the
    final answer is what the user heard.
    Bump via VOICE_HISTORY_MAX_TOKENS if needed.
    """
    import os
    budget = int(os.getenv("VOICE_HISTORY_MAX_TOKENS", "4_000").replace("_", ""))
    return trim_history(
        history,
        max_tokens=budget,
        include_system_prompts=True,
        include_tool_calls=False,
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


async def _stream_voice_agent(
    *,
    user_prompt: str,
    message_history: list,
    deps: FarmerContext,
    usage_limits: Any = agrinet_vllm_usage_limits,
):
    """
    Async context manager wrapper for voice_agent.run_stream with Azure fallback.

    Yields the run_stream response context manager.
    Raises UsageLimitExceeded if both vLLM and Azure fail.
    """
    try:
        async with voice_agent.run_stream(
            user_prompt=user_prompt,
            message_history=message_history,
            deps=deps,
            usage_limits=usage_limits,
        ) as response_stream:
            yield response_stream
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
            async with voice_agent.run_stream(
                user_prompt=user_prompt,
                message_history=reduced_history,
                deps=deps,
                usage_limits=usage_limits,
            ) as response_stream:
                yield response_stream


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
    import time
    import re as _re
    t_total_start = time.perf_counter()

    content_id = f"query_{session_id}_{len(history)//2 + 1}"
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
        user_id=None,
        tags=tags,
        observation_name="voice-agent-stream",
        trace_input=query,
    ) as lf_obs:
        if lf_obs is not None:
            deps.langfuse_trace_id = getattr(lf_obs, "trace_id", None)
            deps.langfuse_root_observation_id = getattr(lf_obs, "id", None)

        # --- Stage 1: history fetch is already done by caller; log it ---
        t_prep_start = time.perf_counter()

        message_pairs = "\n\n".join(format_message_pairs(history, 3))
        logger.debug(f"Message pairs: {message_pairs}")

        user_message = deps.get_user_message()
        logger.debug(f"Running agent with user message: {user_message}")

        # --- Stage 2: clean + trim history ---
        t_clean_start = time.perf_counter()
        cleaned_history = clean_message_history_for_openai(history)
        if len(cleaned_history) != len(history):
            logger.warning(f"Cleaned {len(history) - len(cleaned_history)} orphaned tool calls from history")
            await update_message_history(session_id, cleaned_history)
            history = cleaned_history
        logger.info(f"[TIMING] clean_hist={int((time.perf_counter()-t_clean_start)*1000)}ms msgs={len(history)}")

        t_trim_start = time.perf_counter()
        trimmed_history = _trim_voice_history(history)
        logger.info(
            f"[TIMING] trim_hist={int((time.perf_counter()-t_trim_start)*1000)}ms "
            f"hist_msgs={len(trimmed_history)} hist_bytes={sum(len(str(m)) for m in trimmed_history)}"
        )

        logger.info(
            f"[TIMING] prep_done={int((time.perf_counter()-t_prep_start)*1000)}ms "
            f"hist_msgs={len(trimmed_history)}"
        )

        is_bhili = source_lang == 'bhb' or target_lang == 'bhb'

        full_output = ""
        request_tokens = 0
        response_tokens = 0
        new_messages: list = []
        model_used = _langfuse_vllm_model()

        # Per-round timing accumulators
        round_num = 0
        first_token_logged = False
        chars_yielded = 0
        total_agent_time_ms = 0
        t_agent_start = time.perf_counter()
        t_first_delta_global = None

        try:
            async for response_stream in _stream_voice_agent(
                user_prompt=user_message,
                message_history=trimmed_history,
                deps=deps,
            ):
                round_num += 1
                t_round_start = time.perf_counter()

                # --- Stage 3: wait for first token from this LLM round ---
                t_round_llm_start = time.perf_counter()
                round_first_token = False
                round_chars = 0

                model_used = _infer_langfuse_model_from_response(response_stream)

                if is_bhili:
                    # Translate-and-yield: split on sentence end so we don't wait for \n\n
                    buffer = ""
                    async for chunk in response_stream.stream_text(delta=True, debounce_by=None):
                        if not round_first_token:
                            t_first_delta = time.perf_counter()
                            round_first_token = True
                            if not first_token_logged:
                                t_first_delta_global = t_first_delta
                                logger.info(
                                    f"[TIMING] llm_ttft={int((t_first_delta-t_agent_start)*1000)}ms "
                                    f"(first delta after first model token, round={round_num})"
                                )
                                first_token_logged = True
                            logger.info(
                                f"[TIMING] round{round_num}_ttft={int((t_first_delta-t_round_llm_start)*1000)}ms "
                                f"(from round {round_num} start)"
                            )
                        buffer += chunk
                        # Yield on sentence boundaries (., !, ?, ।) instead of \n\n
                        while True:
                            m = _re.search(r"([\.\!\?।])", buffer)
                            if not m:
                                break
                            end = m.end()
                            sentence = buffer[:end]
                            buffer = buffer[end:]
                            translated = await translation_service.translate_text(
                                sentence, "mr", "bhb"
                            )
                            full_output += translated
                            chars_yielded += len(translated)
                            round_chars += len(translated)
                            yield translated
                    if buffer.strip():
                        translated_tail = await translation_service.translate_text(
                            buffer, "mr", "bhb"
                        )
                        full_output += translated_tail
                        chars_yielded += len(translated_tail)
                        round_chars += len(translated_tail)
                        yield translated_tail
                else:
                    async for chunk in response_stream.stream_text(delta=True, debounce_by=None):
                        if not round_first_token:
                            t_first_delta = time.perf_counter()
                            round_first_token = True
                            if not first_token_logged:
                                t_first_delta_global = t_first_delta
                                logger.info(
                                    f"[TIMING] llm_ttft={int((t_first_delta-t_agent_start)*1000)}ms "
                                    f"(first delta after first model token, round={round_num})"
                                )
                                first_token_logged = True
                            logger.info(
                                f"[TIMING] round{round_num}_ttft={int((t_first_delta-t_round_llm_start)*1000)}ms "
                                f"(from round {round_num} start)"
                            )
                        full_output += chunk
                        chars_yielded += len(chunk)
                        round_chars += len(chunk)
                        yield chunk

                t_round_end = time.perf_counter()
                round_ms = int((t_round_end - t_round_start) * 1000)
                total_agent_time_ms += round_ms
                logger.info(
                    f"[TIMING] round{round_num}_total={round_ms}ms "
                    f"chars={round_chars} ttft_logged={round_first_token}"
                )
                new_messages = response_stream.new_messages()

                try:
                    usage = response_stream.usage()
                    request_tokens = usage.request_tokens or 0
                    response_tokens = usage.response_tokens or 0
                    logger.info(f"[TIMING] round{round_num}_tokens req={request_tokens} resp={response_tokens}")
                except Exception:
                    pass  # Usage unavailable — tokens reported as 0

        except UsageLimitExceeded:
            logger.exception("Usage limit exceeded even after Azure fallback for session %s", session_id)
            yield "क्षमा करा, प्रतिसाद तयार करता आला नाही. कृपया पुन्हा प्रयत्न करा."
        except Exception:
            logger.exception("Voice agent run failed for session %s", session_id)
            yield "क्षमा करा, प्रतिसाद तयार करता आला नाही. कृपया पुन्हा प्रयत्न करा."

        finally:
            t_total = time.perf_counter() - t_total_start
            post_stream_ms = int((t_total * 1000) - total_agent_time_ms)
            logger.info(
                f"[TIMING] total={int(t_total*1000)}ms session={session_id} "
                f"chars={chars_yielded} rounds={round_num} "
                f"agent_time_ms={total_agent_time_ms} post_stream_ms={post_stream_ms}"
            )
            if lf_obs is not None:
                lf_obs.update(
                    output=full_output,
                    metadata={
                        "model": model_used,
                        "request_tokens": request_tokens,
                        "response_tokens": response_tokens,
                        "rounds": round_num,
                    },
                )

        messages = [
            *history,
            *new_messages,
        ]

        logger.debug(f"Updating message history for session {session_id} with {len(messages)} messages")
        await update_message_history(session_id, messages)


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