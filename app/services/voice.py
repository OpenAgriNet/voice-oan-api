from typing import AsyncGenerator
import json
import re
from agents.voice import voice_agent
from agents.deps import FarmerContext
from app.config import settings
from app.core.cache import cache
from app.core.languages import get_language, normalize_language
from helpers.utils import get_logger
from app.utils import update_message_history, trim_history

logger = get_logger(__name__)

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

LANGUAGE_CACHE_SUFFIX = "_LANGUAGE"


def _get_recording_message(lang: str | None) -> str:
    """Get the recording disclaimer for the language the bot actually answered in."""
    return get_language(lang).recording_disclaimer


def _prefix_disclaimer(disclaimer: str, audio: str) -> str:
    """Join the disclaimer to the response with a space so TTS doesn't run them together."""
    if not disclaimer:
        return audio
    if not audio:
        return disclaimer
    return f"{disclaimer} {audio.lstrip()}"

def _extract_audio_from_partial_json(text: str) -> str:
    """Extract the audio field value from partial/incomplete JSON text during streaming."""
    match = re.search(r'"audio"\s*:\s*"((?:[^"\\]|\\.)*)', text)
    return match.group(1) if match else ""


def _extract_language_from_partial_json(text: str) -> str | None:
    """Extract the completed language field from partial JSON during streaming.

    ``language`` is the first field of VoiceOutput, so it closes before any audio
    arrives — which is what lets the disclaimer be picked in the detected language.
    Returns None until the closing quote is seen, so a half-streamed "t" of "ta"
    is never mistaken for a complete code.
    """
    match = re.search(r'"language"\s*:\s*"([a-zA-Z-]{2,8})"', text)
    return match.group(1) if match else None


def _voice_output_dict(audio: str, end_interaction: bool, language: str | None) -> dict:
    """Build the voice response dict (audio, end_interaction, language). language may be None when asking for preference."""
    return {"audio": audio, "end_interaction": end_interaction, "language": language}


async def stream_voice_message(
    query: str,
    session_id: str,
    user_id: str,
    history: list
) -> AsyncGenerator[str, None]:
    """Async generator for streaming voice messages using run_stream_events()."""
    language_cache_key = f"{session_id}{LANGUAGE_CACHE_SUFFIX}"
    cached_language = await cache.get(language_cache_key)
    locked_language = normalize_language(cached_language, fallback="") or None
    deps = FarmerContext(
        query=query,
        session_id=session_id,
        user_id=user_id,
        language_code=locked_language,
    )
    user_message = deps.get_user_message()
    logger.info("Running voice agent (locked_language=%s)", locked_language)

    trimmed_history = trim_history(history, max_tokens=80_000)
    logger.info(f"Trimmed history: {len(trimmed_history)} messages")

    is_first_message = _is_first_user_message(history)

    final_output = None
    new_messages = None
    text_buffer = ""
    prev_audio = ""
    # Filled in from the streamed JSON as soon as the language field closes.
    detected_lang: str | None = locked_language

    async for event in voice_agent.run_stream_events(
        user_prompt=user_message,
        message_history=trimmed_history,
        deps=deps,
    ):
        kind = getattr(event, "event_kind", "")
        if kind == "part_delta":
            delta = event.delta
            if getattr(delta, "part_delta_kind", "") == "text":
                text_buffer += delta.content_delta
                if detected_lang is None:
                    detected_lang = _extract_language_from_partial_json(text_buffer)
                audio = _extract_audio_from_partial_json(text_buffer)
                if audio and audio != prev_audio:
                    prev_audio = audio
                    lang = locked_language or normalize_language(detected_lang)
                    prefix = _get_recording_message(lang) if is_first_message else ""
                    output_dict = _voice_output_dict(
                        _prefix_disclaimer(prefix, audio), False, lang
                    )
                    yield json.dumps(output_dict, ensure_ascii=False)
        elif kind == "function_tool_result":
            text_buffer = ""
            prev_audio = ""
        elif kind == "agent_run_result":
            agent_result = event.result
            final_output = agent_result.output
            new_messages = agent_result.new_messages()

    # Yield the final complete output. The language is whatever the agent detected
    # and answered in; it drives both the disclaimer and the caller's TTS voice.
    if final_output:
        if isinstance(final_output, dict):
            end_flag = final_output.get("end_interaction", False)
            raw_audio = final_output.get("audio") or ""
            reported_lang = final_output.get("language")
            should_lock = bool(final_output.get("lock_language", False))
        else:
            end_flag = getattr(final_output, "end_interaction", False)
            raw_audio = final_output.audio or ""
            reported_lang = getattr(final_output, "language", None)
            should_lock = bool(getattr(final_output, "lock_language", False))
        out_lang = locked_language or normalize_language(reported_lang or detected_lang)
        if reported_lang and out_lang != reported_lang:
            logger.warning(
                "Agent reported language '%s' but session uses '%s'",
                reported_lang,
                out_lang,
            )
        if locked_language is None and should_lock:
            await cache.set(
                language_cache_key,
                out_lang,
                ttl=settings.default_cache_ttl,
            )
            locked_language = out_lang
            logger.info("Locked session language to %s", out_lang)
        final_recording_prefix = _get_recording_message(out_lang) if is_first_message else ""
        audio_text = _prefix_disclaimer(final_recording_prefix, raw_audio)
        output_dict = _voice_output_dict(audio_text, end_flag, out_lang)
        yield json.dumps(output_dict, ensure_ascii=False)
        logger.info(f"Streaming complete - end_interaction: {end_flag}, language: {out_lang}")

    # Update message history
    if new_messages:
        await update_message_history(session_id, [*history, *new_messages])
