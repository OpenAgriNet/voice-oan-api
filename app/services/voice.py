from typing import AsyncGenerator
import asyncio
import httpx
import json
import os
import re
from agents.voice import voice_agent
from agents.deps import FarmerContext
from agents.models import LLM_AGRINET_MODEL
from agents.routing import select_model_for_session
from agents.tools.language import LANGUAGE_CACHE_SUFFIX, LANGUAGE_CACHE_TTL
from helpers.telemetry import (
    TelemetryRequest,
    create_voice_response_event,
    generate_voice_question_id,
    post_telemetry_payload,
)
from helpers.utils import get_logger
from app.utils import update_message_history, trim_history
from app.core.cache import cache
from app.observability.langfuse_client import safe_propagate_attributes, safe_start_agent_observation
from app.observability.voice import safe_update_observation
from langcodes import Language

logger = get_logger(__name__)

INTERNAL_AGENT_LANG = "en"

def _user_message_count(history: list) -> int:
    """Count user messages in history (messages that have a user-prompt part)."""
    if not history:
        return 0
    count = 0
    for msg in history:
        for part in msg.parts:
            if getattr(part, "part_kind", "") == "user-prompt":
                count += 1
                break
    return count


def _is_first_user_message(history: list) -> bool:
    """Check if this is the first user message after welcome messages."""
    return _user_message_count(history) == 1

def _recording_lang(lang: str | None) -> str:
    """Normalize language for recording message: hi → hi, en → en, anything else → en."""
    return lang if lang in ("en", "hi") else "en"


def _get_recording_message(lang: str | None) -> str:
    """Get the recording message by language: hi → Hindi, en → English, default → English."""
    recording_messages = {
        "hi": "यह कॉल प्रशिक्षण और गुणवत्ता सुधार हेतु रिकॉर्ड की जा रही है। आपकी जानकारी सुरक्षित रहेगी।",
        "en": "This call is being recorded for training and quality purposes. Your personal information will not be shared with any third party.",
    }
    return recording_messages.get(_recording_lang(lang), recording_messages["en"])


def _normalize_lang_code(lang: str | None) -> str | None:
    """Normalize a language code to its primary language subtag."""
    if not lang:
        return None
    candidate = lang.strip()
    if not candidate or candidate.lower() == "none":
        return None
    try:
        normalized = Language.get(candidate).language
        return (normalized or candidate).lower()
    except Exception:
        return candidate.lower()


async def _translate_text(text: str, source_lang: str | None, target_lang: str | None) -> str:
    """Translate text with Bhashini when both languages are known and differ."""
    if not text or not source_lang or not target_lang or source_lang == target_lang:
        return text

    api_key = os.getenv("MEITY_API_KEY_VALUE")
    if not api_key:
        logger.warning(
            "Translation skipped because MEITY_API_KEY_VALUE is unset (source=%s, target=%s)",
            source_lang,
            target_lang,
        )
        return text

    def _translate_sync() -> str:
        response = httpx.post(
            "https://dhruva-api.bhashini.gov.in/services/inference/pipeline",
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
            },
            json={
                "pipelineTasks": [
                    {
                        "taskType": "translation",
                        "config": {
                            "serviceId": "bhashini/ai4bharat/indictrans-v3",
                            "language": {
                                "sourceLanguage": source_lang,
                                "targetLanguage": target_lang,
                            },
                        },
                    }
                ],
                "inputData": {
                    "input": [{"source": text}],
                },
            },
            timeout=httpx.Timeout(10.0, read=30.0),
        )
        response.raise_for_status()
        response_json = response.json()
        return response_json["pipelineResponse"][0]["output"][0]["target"]

    try:
        return await asyncio.to_thread(_translate_sync)
    except Exception:
        logger.exception(
            "Translation failed, falling back to original text (source=%s, target=%s)",
            source_lang,
            target_lang,
        )
        return text

def _extract_audio_from_partial_json(text: str) -> str:
    """Extract the audio field value from partial/incomplete JSON text during streaming."""
    match = re.search(r'"audio"\s*:\s*"((?:[^"\\]|\\.)*)', text)
    return match.group(1) if match else ""


def _voice_output_dict(audio: str, end_interaction: bool, language: str | None) -> dict:
    """Build the voice response dict (audio, end_interaction, language). language may be None when asking for preference."""
    return {"audio": audio, "end_interaction": end_interaction, "language": language}


async def stream_voice_message(
    query: str,
    session_id: str,
    source_lang: str,
    target_lang: str,
    user_id: str,
    history: list
) -> AsyncGenerator[str, None]:
    """Async generator for streaming voice messages using run_stream_events()."""
    detected_lang = _normalize_lang_code(target_lang)
    cached_lang = _normalize_lang_code(await cache.get(f"{session_id}{LANGUAGE_CACHE_SUFFIX}"))
    if cached_lang is None and detected_lang is not None:
        await cache.set(f"{session_id}{LANGUAGE_CACHE_SUFFIX}", detected_lang, ttl=LANGUAGE_CACHE_TTL)
        cached_lang = detected_lang

    user_lang = cached_lang or detected_lang
    translated_query = await _translate_text(query, user_lang, INTERNAL_AGENT_LANG)
    deps = FarmerContext(
        query=translated_query,
        lang_code=user_lang or INTERNAL_AGENT_LANG,
        session_id=session_id,
        user_id=user_id,
        selected_language=user_lang,
    )
    user_message = deps.get_user_message()
    voice_qid = generate_voice_question_id()
    logger.info(f"Running agent (user_lang={user_lang}, voice_qid={voice_qid})")

    trimmed_history = trim_history(history, max_tokens=80_000)
    logger.info(f"Trimmed history: {len(trimmed_history)} messages")

    is_first_message = _is_first_user_message(history)
    recording_prefix = _get_recording_message(user_lang) if is_first_message else ""
    translate_output = bool(user_lang and user_lang != INTERNAL_AGENT_LANG)

    model, model_route = await select_model_for_session(session_id)
    model_name = getattr(model, "model_name", "unknown")
    logger.info(f"Routing session {session_id} to model_route={model_route} model={model_name}")

    # Runtime fallback: if the Gemma canary call errors/times out before any audio has
    # been streamed to the client, retry once against the default (Azure/OpenAI) model.
    # Once any output has been sent, a retry can't be done cleanly (the client already
    # heard part of the response), so failures past that point just propagate.
    candidates = [(model, model_route)]
    if model_route == 'gemma':
        candidates.append((LLM_AGRINET_MODEL, 'gemma_runtime_fallback'))

    final_output = None
    new_messages = None

    agent_slug = (voice_agent.name or "voice").replace(" ", "_").lower()
    # Tags are trace-level in Langfuse, so they go through propagate_attributes;
    # observations only carry metadata.
    with safe_propagate_attributes(
        tags=["voice", "pydantic_ai", f"agent:{agent_slug}", f"model_route:{model_route}", f"model:{model_name}"],
    ), safe_start_agent_observation(
        name=f"agent.{agent_slug}",
        input={
            "query": query,
            "effective_lang": INTERNAL_AGENT_LANG,
            "user_lang": user_lang,
            "session_id": session_id,
            "target_lang": target_lang,
        },
        metadata={
            "agent_name": voice_agent.name,
            "voice_qid": voice_qid,
            "user_id": user_id,
            "model_route": model_route,
            "model_name": model_name,
        },
    ) as agent_obs:
        for attempt_index, (attempt_model, attempt_route) in enumerate(candidates):
            text_buffer = ""
            prev_audio = ""
            any_chunk_yielded = False
            is_last_attempt = attempt_index == len(candidates) - 1
            try:
                async for event in voice_agent.run_stream_events(
                    user_prompt=user_message,
                    message_history=trimmed_history,
                    deps=deps,
                    model=attempt_model,
                ):
                    kind = getattr(event, 'event_kind', '')

                    if kind == 'part_delta':
                        delta = event.delta
                        if getattr(delta, 'part_delta_kind', '') == 'text':
                            text_buffer += delta.content_delta
                            audio = _extract_audio_from_partial_json(text_buffer)
                            if audio and audio != prev_audio:
                                any_chunk_yielded = True
                                prev_audio = audio
                                if not translate_output:
                                    output_dict = _voice_output_dict(recording_prefix + audio, False, user_lang or INTERNAL_AGENT_LANG)
                                    yield json.dumps(output_dict, ensure_ascii=False)

                    elif kind == 'function_tool_result':
                        # Reset text buffer for next model turn
                        text_buffer = ""
                        prev_audio = ""

                    elif kind == 'agent_run_result':
                        agent_result = event.result
                        final_output = agent_result.output
                        new_messages = agent_result.new_messages()
            except Exception as e:
                if any_chunk_yielded or is_last_attempt:
                    raise
                logger.warning(
                    f"model_route={attempt_route} call failed before streaming any output "
                    f"(session={session_id}): {e}. Retrying with fallback model."
                )
                continue

            if attempt_route != model_route:
                model_route = attempt_route
                model_name = getattr(attempt_model, "model_name", "unknown")
                # Tag the active agent span so the trace is filterable by the route
                # actually served, not just the one initially selected.
                with safe_propagate_attributes(
                    tags=[f"model_route:{model_route}", f"model:{model_name}"],
                ):
                    logger.info(f"Session {session_id} recovered via runtime fallback to model_route={model_route}")
                try:
                    agent_obs.update(metadata={"model_route": model_route, "model_name": model_name})
                except (TypeError, AttributeError):
                    pass
            break

        if final_output is not None:
            if isinstance(final_output, dict):
                safe_update_observation(
                    agent_obs,
                    {
                        "audio": (final_output.get("audio") or "")[:2000],
                        "end_interaction": bool(final_output.get("end_interaction", False)),
                    },
                )
            else:
                safe_update_observation(
                    agent_obs,
                    {
                        "audio": (getattr(final_output, "audio", None) or "")[:2000],
                        "end_interaction": bool(getattr(final_output, "end_interaction", False)),
                    },
                )

    agent_response_text: str | None = None
    if final_output is not None:
        if isinstance(final_output, dict):
            agent_response_text = final_output.get("audio") or ""
        else:
            agent_response_text = final_output.audio or ""

    async def _send_voice_turn_telemetry(agent_response: str | None) -> None:
        try:
            if not os.getenv("TELEMETRY_API_URL"):
                logger.warning(
                    "Voice telemetry not sent: TELEMETRY_API_URL is unset (set in .env for Docker), qid=%s",
                    voice_qid,
                )
                return
            event = create_voice_response_event(
                uid=user_id or "guest",
                question_text=query,
                session_id=session_id,
                qid=voice_qid,
                source_lang=source_lang,
                target_lang=target_lang,
                response_text=agent_response,
            )
            payload = TelemetryRequest(events=[event]).model_dump(mode="json")
            logger.info(
                "Voice telemetry POST body (OE_VOICE_RESPONSE) qid=%s session_id=%s: %s",
                voice_qid,
                session_id,
                json.dumps(payload, ensure_ascii=False),
            )
            # Shield so a client disconnect / stream teardown is less likely to cancel the HTTP POST.
            resp = await asyncio.shield(
                asyncio.to_thread(post_telemetry_payload, payload)
            )
            if resp is not None and resp.status_code == 200:
                logger.info("Voice telemetry sent (OE_VOICE_RESPONSE), qid=%s", voice_qid)
            elif resp is None:
                logger.warning(
                    "Voice telemetry POST failed or gave up after retries, qid=%s",
                    voice_qid,
                )
            else:
                logger.warning(
                    "Voice telemetry returned HTTP %s, qid=%s",
                    resp.status_code,
                    voice_qid,
                )
        except Exception:
            logger.exception("Voice turn telemetry failed, qid=%s", voice_qid)

    asyncio.create_task(_send_voice_turn_telemetry(agent_response_text))

    # Yield the final complete output; language comes from set_language tool call (deps.selected_language)
    # or falls back to the session's target_lang header value
    if final_output:
        if isinstance(final_output, dict):
            end_flag = final_output.get("end_interaction", False)
            raw_audio = final_output.get("audio") or ""
        else:
            end_flag = getattr(final_output, "end_interaction", False)
            raw_audio = final_output.audio or ""
        out_lang = deps.selected_language or INTERNAL_AGENT_LANG
        audio_body = await _translate_text(raw_audio, INTERNAL_AGENT_LANG, out_lang)
        final_recording_prefix = _get_recording_message(out_lang) if is_first_message else ""
        audio_text = final_recording_prefix + audio_body
        output_dict = _voice_output_dict(audio_text, end_flag, out_lang)
        yield json.dumps(output_dict, ensure_ascii=False)
        logger.info(f"Streaming complete - end_interaction: {end_flag}, language: {out_lang}")

    # Update message history
    if new_messages:
        await update_message_history(session_id, [*history, *new_messages])
