import asyncio
from contextlib import nullcontext
from functools import lru_cache
import time
from typing import AsyncGenerator, Optional, Literal
import re
from fastapi import Request

import regex
# from fastapi import BackgroundTasks
from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart, TextPart

from agents.voice import voice_agent
from agents.tools.farmer import normalize_phone_to_mobile, fetch_farmer_info_raw
from agents.tools.common import get_random_nudge_message, send_nudge_message_raya
from helpers.utils import get_logger, clean_output_by_language
from app.config import settings
from app.utils import (
    update_message_history,
    trim_history,
    format_message_pairs,
    clean_message_history_for_openai,
    get_feedback_state,
    set_feedback_initiated,
    set_feedback_rating_received,
    clear_feedback_initiated,
    extract_conversation_events_from_messages,
    SessionRequestOwner,
    is_session_request_owner,
    refresh_session_request_ownership,
    release_session_request_ownership,
)
from app.services.feedback import (
    get_feedback_ack,
    get_feedback_question,
    parse_feedback_with_llm,
    send_feedback,
)
from app.services.translation import (
    INDIAN_LANGUAGES,
    OPENAI_PRETRANSLATION_MODEL,
    translate_text,
    translate_text_stream_fast,
    translate_to_english_with_gpt5_mini,
)
# NOTE: Removing telemetry for now.
# from app.tasks.telemetry import send_telemetry
from agents.deps import FarmerContext

logger = get_logger(__name__)


class SentenceSegmenter:
    sep = 'ŽžŽžSentenceSeparatorŽžŽž'
    latin_terminals = '!?.'
    jap_zh_terminals = '。！？'
    terminals = latin_terminals + jap_zh_terminals

    def __init__(self):
        terminals = self.terminals
        self._re = [
            (regex.compile(r'(\P{N})([' + terminals + r'])(\p{Z}*)'), r'\1\2\3' + self.sep),
            (regex.compile(r'(' + terminals + r')(\P{N})'), r'\1' + self.sep + r'\2'),
        ]

    @lru_cache(maxsize=2**16)
    def __call__(self, line: str):
        for (_re, repl) in self._re:
            line = _re.sub(repl, line)
        return [t for t in line.split(self.sep) if t != '']


sentence_segmenter = SentenceSegmenter()


def extract_complete_sentences(text: str):
    if not text:
        return [], ""
    sentences = sentence_segmenter(text)
    if len(sentences) <= 1:
        return [], text
    return sentences[:-1], sentences[-1]


def _batch_starts_new_line_or_list(text: str) -> bool:
    if not text or not text.strip():
        return False
    stripped = text.lstrip()
    if text != stripped:
        return True
    if stripped.startswith(("-", "•")) and (len(stripped) == 1 or stripped[1:2].isspace() or stripped[1:2] == "."):
        return True
    if stripped.startswith("*") and (len(stripped) == 1 or stripped[1:2].isspace() or stripped[1:2] == "."):
        return True
    return bool(re.match(r"^\d+\.\s", stripped))


def should_translate_batch(batch_text: str, word_count: int) -> bool:
    min_words = 15
    max_words = 80

    if word_count < min_words:
        text_end = batch_text.rstrip()
        return text_end.endswith(('.', '!', '?')) and word_count >= 5
    if word_count >= max_words:
        return True

    text_end = batch_text.rstrip()
    if text_end.endswith('\n\n'):
        return True
    if text_end.endswith('\n') and len(batch_text.split('\n')) > 1:
        last_line = batch_text.rstrip('\n').split('\n')[-1].strip()
        if last_line.startswith(('-', '*', '•')) or re.match(r'^\d+\.', last_line):
            return True
    return text_end.endswith(('.', '!', '?'))

# Langfuse Sessions: same session_id groups all traces for one conversation (session replay, session-level metrics).
def _langfuse_session_context(session_id: str, user_id: str, process_id: Optional[str] = None):
    """Set Langfuse session_id so all agent runs for this conversation appear under one Session."""
    try:
        from app.observability import langfuse_client
        from langfuse import propagate_attributes
        if langfuse_client is None:
            return nullcontext()
        # Langfuse Sessions: session_id ≤200 chars (US-ASCII); same ID = one Session in Langfuse UI
        safe_session_id = (session_id or "").strip()[:200]
        kwargs = dict(
            session_id=safe_session_id or None,
            user_id=(user_id or "anonymous")[:200],
        )
        if process_id:
            kwargs["metadata"] = {"process_id": str(process_id)[:200]}
        return propagate_attributes(**kwargs)
    except Exception:
        return nullcontext()


async def stream_voice_message(
    query: str,
    session_id: str,
    source_lang: str,
    target_lang: str,
    user_id: str,
    history: list,
    provider: Optional[Literal['RAYA']] = None,
    process_id: Optional[str] = None,
    user_info: dict = None,
    use_translation_pipeline: bool = False,
    owner: Optional[SessionRequestOwner] = None,
    http_request: Optional[Request] = None,
#    background_tasks: BackgroundTasks,
    
) -> AsyncGenerator[str, None]:
    """Async generator for streaming chat messages."""
    request_started_at = time.monotonic()
    last_owner_refresh_at = 0.0

    async def _request_is_stale(reason: str) -> bool:
        nonlocal last_owner_refresh_at
        if http_request is not None and await http_request.is_disconnected():
            logger.info(
                "Stopping request due to client disconnect - session_id=%s process_id=%s reason=%s",
                session_id,
                process_id,
                reason,
            )
            return True

        now = time.monotonic()
        if owner is not None and (
            last_owner_refresh_at == 0.0
            or now - last_owner_refresh_at >= settings.session_owner_refresh_interval_seconds
        ):
            refreshed = await refresh_session_request_ownership(owner)
            last_owner_refresh_at = now
            if not refreshed:
                logger.info(
                    "Stopping stale request after ownership lost during refresh - session_id=%s process_id=%s epoch=%s reason=%s",
                    session_id,
                    process_id,
                    owner.epoch,
                    reason,
                )
                return True

        if owner is not None and not await is_session_request_owner(owner):
            logger.info(
                "Stopping stale request because a newer request owns the session - session_id=%s process_id=%s epoch=%s reason=%s",
                session_id,
                process_id,
                owner.epoch,
                reason,
            )
            return True

        return False

    try:
        with _langfuse_session_context(session_id, user_id, process_id):
            if await _request_is_stale("before_feedback_check"):
                return

            feedback_state = await get_feedback_state(session_id)
            if feedback_state.get("initiated") and not feedback_state.get("rating_received"):
                target_lang = (target_lang or "gu").strip().lower()
                trigger = feedback_state.get("trigger") or "conversation_closing"
                parsed = await parse_feedback_with_llm(query, target_lang)
                is_feedback = parsed.get("is_feedback") is True
                rating = parsed.get("rating") if isinstance(parsed.get("rating"), int) else None
                valid_rating = rating is not None and 1 <= rating <= 5

                if is_feedback and valid_rating:
                    if await _request_is_stale("before_feedback_ack"):
                        return
                    ack = get_feedback_ack(rating, target_lang)
                    await send_feedback(
                        session_id=session_id,
                        user_id=user_id,
                        process_id=process_id,
                        rating=rating,
                        trigger=trigger,
                        source_lang=source_lang or "gu",
                        target_lang=target_lang,
                        message_history_summary={"turn_count": len(history)},
                        farmer_info=None,
                        raw_input=None,
                    )
                    await set_feedback_rating_received(session_id)

                    feedback_question = get_feedback_question(target_lang)
                    feedback_q_resp = ModelResponse(parts=[TextPart(content=feedback_question)])
                    rating_req = ModelRequest(parts=[UserPromptPart(content=query)])
                    ack_resp = ModelResponse(parts=[TextPart(content=ack)])
                    await update_message_history(session_id, [*history, feedback_q_resp, rating_req, ack_resp])

                    yield ack
                    return

                await clear_feedback_initiated(session_id)
                logger.info(
                    "Feedback not accepted (is_feedback=%s, rating=%s); routing query to agent",
                    is_feedback, rating,
                )

            requested_source_lang = (source_lang or "gu").strip().lower()
            requested_target_lang = (target_lang or "gu").strip().lower()
            needs_output_translation = use_translation_pipeline and requested_target_lang in INDIAN_LANGUAGES

            processing_query = query
            processing_lang = requested_source_lang

            if use_translation_pipeline and requested_source_lang in {"gu", "gujarati"}:
                logger.info(
                    "Translation pipeline enabled; pretranslating %s -> en with %s",
                    requested_source_lang,
                    OPENAI_PRETRANSLATION_MODEL,
                )
                if await _request_is_stale("before_query_pretranslation"):
                    return
                try:
                    processing_query = await translate_to_english_with_gpt5_mini(
                        text=query,
                        source_lang=requested_source_lang,
                    )
                    processing_lang = "en"
                except Exception as e:
                    logger.error(
                        "OpenAI pretranslation failed for session_id=%s source_lang=%s model=%s error=%s",
                        session_id,
                        requested_source_lang,
                        OPENAI_PRETRANSLATION_MODEL,
                        e,
                    )
                    try:
                        logger.info("Falling back to TranslateGemma pretranslation for session_id=%s", session_id)
                        processing_query = await translate_text(
                            text=query,
                            source_lang=requested_source_lang,
                            target_lang="english",
                        )
                        processing_lang = "en"
                    except Exception as fallback_error:
                        logger.error(
                            "TranslateGemma pretranslation fallback failed for session_id=%s error=%s",
                            session_id,
                            fallback_error,
                        )
                        processing_query = query
                        processing_lang = requested_source_lang

            if use_translation_pipeline and needs_output_translation:
                processing_lang = "en"

            mobile = normalize_phone_to_mobile(user_id)
            farmer_info = None
            if mobile:
                records = await fetch_farmer_info_raw(mobile)
                if records:
                    farmer_info = {"farmers": records}
                    logger.info(f"Proactively loaded farmer info for mobile {mobile}")
                else:
                    msg = (
                        "Could not fetch farmer data, continuing to call"
                        if (target_lang or "gu") != "gu"
                        else "કૃષિકાર ડેટા મેળવી શકાયું નથી, કૉલ ચાલુ રાખી રહ્યાં છીએ"
                    )
                    farmer_info = {"message": msg}

            logger.info(f"User info: {user_info}")
            deps = FarmerContext(
                query=processing_query,
                lang_code=processing_lang,
                target_lang=requested_target_lang,
                provider=provider,
                session_id=session_id,
                process_id=process_id,
                farmer_info=farmer_info,
                use_translation_pipeline=use_translation_pipeline,
            )

            message_pairs = "\n\n".join(format_message_pairs(history, 3))
            logger.info(f"Message pairs: {message_pairs}")
            user_message = deps.get_user_message()
            logger.info(f"Running agent with user message: {user_message}")

            cleaned_history = clean_message_history_for_openai(history)
            if len(cleaned_history) != len(history):
                logger.warning(f"Cleaned {len(history) - len(cleaned_history)} orphaned tool calls from history")
                if not await _request_is_stale("before_cleaned_history_write"):
                    await update_message_history(session_id, cleaned_history)
                history = cleaned_history

            trimmed_history = trim_history(
                history,
                max_tokens=80_000,
                include_system_prompts=True,
                include_tool_calls=True,
            )
            logger.info(f"Trimmed history length: {len(trimmed_history)} messages")

            nudge_lang = (requested_target_lang or "en").strip().lower()

            async def send_nudge_after_timeout() -> None:
                try:
                    elapsed = max(0.0, time.monotonic() - request_started_at)
                    remaining = max(0.0, settings.nudge_timeout_seconds - elapsed)
                    logger.info(
                        "Nudge timer armed; session_id=%s process_id=%s elapsed=%.3fs remaining=%.3fs timeout=%.3fs",
                        session_id,
                        process_id,
                        elapsed,
                        remaining,
                        settings.nudge_timeout_seconds,
                    )
                    await asyncio.sleep(remaining)
                    if await _request_is_stale("before_nudge_send"):
                        return
                    nudge_msg = get_random_nudge_message(nudge_lang)
                    await send_nudge_message_raya(nudge_msg, session_id, process_id)
                    logger.info(
                        "Nudge actually sent; session_id=%s process_id=%s total_elapsed=%.3fs",
                        session_id,
                        process_id,
                        time.monotonic() - request_started_at,
                    )
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.warning(
                        "Nudge-after-timeout failed; session_id=%s process_id=%s error=%s",
                        session_id,
                        process_id,
                        e,
                    )

            nudge_task = asyncio.create_task(send_nudge_after_timeout())
            logger.info(
                "Nudge initiated; session_id=%s process_id=%s",
                session_id,
                process_id,
            )

            async with voice_agent.run_stream(
                user_prompt=user_message,
                message_history=trimmed_history,
                deps=deps,
            ) as response_stream:
                stream_iter = response_stream.stream_text(delta=True)
                first_text_chunk_received = False
                sentence_buffer = ""
                translation_batch: list[str] = []
                batch_word_count = 0

                async def _yield_translated_text(text_to_translate: str) -> AsyncGenerator[str, None]:
                    if not text_to_translate:
                        return
                    try:
                        async for translated_chunk in translate_text_stream_fast(
                            text=text_to_translate,
                            source_lang="english",
                            target_lang=requested_target_lang,
                        ):
                            if await _request_is_stale("during_output_translation"):
                                return
                            cleaned_chunk = (
                                clean_output_by_language(translated_chunk, requested_target_lang)
                                if isinstance(translated_chunk, str) and translated_chunk
                                else translated_chunk
                            )
                            yield cleaned_chunk
                    except Exception as e:
                        logger.error(
                            "Translation pipeline output translation failed for session_id=%s error=%s",
                            session_id,
                            e,
                        )
                        yield clean_output_by_language(text_to_translate, "en")

                try:
                    async for chunk in stream_iter:
                        if await _request_is_stale("during_agent_stream"):
                            break

                        if not use_translation_pipeline or not needs_output_translation:
                            if (
                                not first_text_chunk_received
                                and isinstance(chunk, str)
                                and chunk
                                and chunk.strip()
                            ):
                                first_text_chunk_received = True
                                nudge_task.cancel()
                                logger.info(
                                    "Nudge canceled (first text chunk received); session_id=%s process_id=%s chunk_preview=%s",
                                    session_id,
                                    process_id,
                                    chunk[:50] if len(chunk) > 50 else chunk,
                                )
                                try:
                                    await nudge_task
                                except asyncio.CancelledError:
                                    pass

                            cleaned_chunk = (
                                clean_output_by_language(chunk, requested_target_lang)
                                if isinstance(chunk, str) and chunk
                                else chunk
                            )
                            if await _request_is_stale("before_direct_yield"):
                                break
                            yield cleaned_chunk
                            continue

                        sentence_buffer += chunk
                        complete_sentences, remaining = extract_complete_sentences(sentence_buffer)
                        if complete_sentences:
                            for sentence in complete_sentences:
                                translation_batch.append(sentence)
                                batch_word_count += len(sentence.split())

                            batch_text = "".join(translation_batch)
                            if should_translate_batch(batch_text, batch_word_count):
                                async for translated_chunk in _yield_translated_text(batch_text):
                                    if (
                                        not first_text_chunk_received
                                        and isinstance(translated_chunk, str)
                                        and translated_chunk
                                        and translated_chunk.strip()
                                    ):
                                        first_text_chunk_received = True
                                        nudge_task.cancel()
                                        logger.info(
                                            "Nudge canceled (first translated chunk received); session_id=%s process_id=%s",
                                            session_id,
                                            process_id,
                                        )
                                        try:
                                            await nudge_task
                                        except asyncio.CancelledError:
                                            pass
                                    if await _request_is_stale("before_translated_yield"):
                                        break
                                    yield translated_chunk
                                translation_batch = []
                                batch_word_count = 0

                            sentence_buffer = remaining

                    if use_translation_pipeline and needs_output_translation and not await _request_is_stale("before_translation_flush"):
                        if translation_batch:
                            batch_text = "".join(translation_batch)
                            async for translated_chunk in _yield_translated_text(batch_text):
                                if (
                                    not first_text_chunk_received
                                    and isinstance(translated_chunk, str)
                                    and translated_chunk
                                    and translated_chunk.strip()
                                ):
                                    first_text_chunk_received = True
                                    nudge_task.cancel()
                                    logger.info(
                                        "Nudge canceled (final translated batch); session_id=%s process_id=%s",
                                        session_id,
                                        process_id,
                                    )
                                    try:
                                        await nudge_task
                                    except asyncio.CancelledError:
                                        pass
                                if await _request_is_stale("before_final_translated_yield"):
                                    break
                                yield translated_chunk

                        if sentence_buffer.strip():
                            async for translated_chunk in _yield_translated_text(sentence_buffer):
                                if (
                                    not first_text_chunk_received
                                    and isinstance(translated_chunk, str)
                                    and translated_chunk
                                    and translated_chunk.strip()
                                ):
                                    first_text_chunk_received = True
                                    nudge_task.cancel()
                                    logger.info(
                                        "Nudge canceled (tail translated fragment); session_id=%s process_id=%s",
                                        session_id,
                                        process_id,
                                    )
                                    try:
                                        await nudge_task
                                    except asyncio.CancelledError:
                                        pass
                                if await _request_is_stale("before_tail_translated_yield"):
                                    break
                                yield translated_chunk
                except StopAsyncIteration:
                    pass
                except RuntimeError as e:
                    if "StopAsyncIteration" in str(e):
                        logger.warning(
                            "Suppressed stream runtime error during response teardown - session_id=%s process_id=%s error=%s",
                            session_id,
                            process_id,
                            e,
                        )
                    else:
                        raise
                finally:
                    if not nudge_task.done():
                        nudge_task.cancel()
                        logger.info(
                            "Nudge canceled (stream ended); session_id=%s process_id=%s",
                            session_id,
                            process_id,
                        )
                        try:
                            await nudge_task
                        except asyncio.CancelledError:
                            pass

                logger.info(f"Streaming complete for session {session_id}")
                new_messages = response_stream.new_messages()

            if await _request_is_stale("before_history_write"):
                return

            messages = [*history, *new_messages]
            logger.info(f"Updating message history for session {session_id} with {len(messages)} messages")
            await update_message_history(session_id, messages)

            feedback_state = await get_feedback_state(session_id)
            if not feedback_state.get("initiated"):
                events = extract_conversation_events_from_messages(new_messages)
                trigger = None
                for event in events:
                    if event in ("conversation_closing", "user_frustration"):
                        trigger = event
                        break
                if trigger and not await _request_is_stale("before_feedback_prompt"):
                    await set_feedback_initiated(session_id, trigger)
                    feedback_lang = (requested_target_lang or "gu").strip().lower()
                    feedback_question = get_feedback_question(feedback_lang)
                    yield clean_output_by_language(" " + feedback_question, feedback_lang)
                    logger.info(f"Feedback question yielded via stream (trigger={trigger})")
    finally:
        released = await release_session_request_ownership(owner)
        if owner is not None:
            logger.info(
                "Session ownership released - session_id=%s process_id=%s epoch=%s released=%s",
                session_id,
                process_id,
                owner.epoch,
                released,
            )
