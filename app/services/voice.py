from typing import AsyncGenerator
import asyncio
import json
import os
import re
from agents.voice import voice_agent
from agents.deps import FarmerContext
from agents.model_registry import get_registry
from agents.models import VOICE_USE_CASE, get_voice_model
from agents.routing import resolve_voice_route, set_session_voice_route
from app.core.languages import get_language, iso_language_code
from helpers.telemetry import (
    TelemetryRequest,
    create_voice_response_event,
    generate_voice_question_id,
    post_telemetry_payload,
)
from helpers.utils import get_logger
from app.utils import update_message_history, trim_history
from app.observability.langfuse_client import safe_propagate_attributes, safe_start_agent_observation
from app.observability.voice import safe_update_observation

logger = get_logger(__name__)


async def _run_events_with_timeout(**kwargs):
    deadline = asyncio.get_running_loop().time() + get_registry().timeout(VOICE_USE_CASE)
    events = voice_agent.run_stream_events(**kwargs)
    try:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Voice model attempt timed out")
            try:
                yield await asyncio.wait_for(events.__anext__(), timeout=remaining)
            except StopAsyncIteration:
                return
    finally:
        await events.aclose()


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

def _get_recording_message(lang: str | None) -> str:
    """Get the recording disclaimer in the response language."""
    return get_language(lang).recording_disclaimer


def _prefix_disclaimer(disclaimer: str, audio: str) -> str:
    if not disclaimer:
        return audio
    if not audio:
        return disclaimer
    return f"{disclaimer} {audio.lstrip()}"

def _extract_audio_from_partial_json(text: str) -> str:
    """Extract the audio field value from partial/incomplete JSON text during streaming."""
    match = re.search(r'"audio"\s*:\s*"((?:[^"\\]|\\.)*)', text)
    return match.group(1) if match else ""


def _voice_output_dict(audio: str, end_interaction: bool, language: str | None) -> dict:
    """Build the voice response with its public Sarvam language code."""
    return {"audio": audio, "end_interaction": end_interaction, "language": language}


async def stream_voice_message(
    query: str,
    session_id: str,
    user_id: str,
    history: list,
    language_code: str,
    *,
    emit_partial: bool = True,
) -> AsyncGenerator[str, None]:
    """Async generator for streaming voice messages using run_stream_events()."""
    response_language = iso_language_code(language_code)
    deps = FarmerContext(
        query=query,
        session_id=session_id,
        user_id=user_id,
        language_code=language_code,
    )
    user_message = deps.get_user_message()
    voice_qid = generate_voice_question_id()
    logger.info(
        "Running agent (language=%s, voice_qid=%s)", response_language, voice_qid
    )

    trimmed_history = trim_history(history, max_tokens=80_000)
    logger.info(f"Trimmed history: {len(trimmed_history)} messages")

    is_first_message = _is_first_user_message(history)
    registry = get_registry()
    decision = await resolve_voice_route(
        session_id,
        has_history=_user_message_count(history) > 1,
    )
    model_route = decision.route
    model = get_voice_model(model_route)
    model_name = getattr(model, "model_name", "unknown")
    logger.info(
        "Routing session %s to model_route=%s model=%s source=%s",
        session_id,
        model_route,
        model_name,
        decision.source,
    )

    # Retry the alias-level fallback only before output reaches a streaming client.
    # Non-streaming calls suppress partial output, so they can safely retry through
    # the end of the model attempt.
    candidates = [(model, model_route)]
    fallback_alias = registry.fallback(model_route)
    if fallback_alias:
        candidates.append((get_voice_model(fallback_alias), fallback_alias))

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
            "language": response_language,
            "session_id": session_id,
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
            final_output = None
            new_messages = None
            text_buffer = ""
            prev_audio = ""
            any_chunk_yielded = False
            is_last_attempt = attempt_index == len(candidates) - 1
            try:
                async for event in _run_events_with_timeout(
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
                            if emit_partial and audio and audio != prev_audio:
                                any_chunk_yielded = True
                                prev_audio = audio
                                recording_prefix = _get_recording_message(language_code) if is_first_message else ""
                                output_dict = _voice_output_dict(
                                    _prefix_disclaimer(recording_prefix, audio),
                                    False,
                                    response_language,
                                )
                                yield json.dumps(output_dict, ensure_ascii=False)

                    elif kind == 'function_tool_result':
                        # Reset text buffer for next model turn
                        text_buffer = ""
                        prev_audio = ""

                    elif kind == 'agent_run_result':
                        agent_result = event.result
                        final_output = agent_result.output
                        new_messages = agent_result.new_messages()
                if final_output is None:
                    raise RuntimeError("Voice model returned no final output")
            except Exception as e:
                fallback_errors = registry.fallback_errors(attempt_route)
                if any_chunk_yielded or is_last_attempt or not isinstance(e, fallback_errors):
                    raise
                logger.warning(
                    f"model_route={attempt_route} call failed before streaming any output "
                    f"(session={session_id}): {e}. Retrying with fallback model."
                )
                continue

            if attempt_route != model_route:
                await set_session_voice_route(session_id, attempt_route)
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

    async def _send_voice_turn_telemetry(
        agent_response: str | None, response_language: str
    ) -> None:
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
                source_lang=response_language,
                target_lang=response_language,
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

    if final_output:
        if isinstance(final_output, dict):
            end_flag = final_output.get("end_interaction", False)
            raw_audio = final_output.get("audio") or ""
        else:
            end_flag = getattr(final_output, "end_interaction", False)
            raw_audio = final_output.audio or ""

        final_recording_prefix = (
            _get_recording_message(language_code) if is_first_message else ""
        )
        audio_text = _prefix_disclaimer(final_recording_prefix, raw_audio)
        output_dict = _voice_output_dict(audio_text, end_flag, response_language)
        yield json.dumps(output_dict, ensure_ascii=False)
        logger.info(
            "Streaming complete - end_interaction: %s, language: %s",
            end_flag,
            response_language,
        )

    asyncio.create_task(
        _send_voice_turn_telemetry(agent_response_text, response_language)
    )

    # Update message history
    if new_messages:
        await update_message_history(session_id, [*history, *new_messages])
