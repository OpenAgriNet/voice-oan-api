from typing import AsyncGenerator
import json
import re
from agents.voice import voice_agent, VoiceOutput
from agents.deps import FarmerContext
from helpers.utils import get_logger
from app.utils import update_message_history, trim_history

logger = get_logger(__name__)

def _is_first_user_message(history: list) -> bool:
    """Check if this is the first user message after welcome messages."""
    if not history:
        return True

    # Count user messages in history
    user_message_count = 0
    for msg in history:
        for part in msg.parts:
            if getattr(part, "part_kind", "") == "user-prompt":
                user_message_count += 1
                break

    # If there's only 1 user message (the welcome one), this is the first real user message
    return user_message_count == 1

def _get_recording_message(target_lang: str) -> str:
    """Get the recording message in the target language."""
    recording_messages = {
        "hi": "यह कॉल रिकॉर्ड की जा रही है। ",
        "en": "This call is being recorded. "
    }
    return recording_messages.get(target_lang, recording_messages["en"])

def _extract_audio_from_partial_json(text: str) -> str:
    """Extract the audio field value from partial/incomplete JSON text during streaming."""
    match = re.search(r'"audio"\s*:\s*"((?:[^"\\]|\\.)*)', text)
    return match.group(1) if match else ""

async def stream_voice_message(
    query: str,
    session_id: str,
    source_lang: str,
    target_lang: str,
    user_id: str,
    history: list
) -> AsyncGenerator[str, None]:
    """Async generator for streaming voice messages using run_stream_events()."""
    deps = FarmerContext(query=query, lang_code=target_lang, session_id=session_id)
    user_message = deps.get_user_message()
    logger.info(f"Running agent with user message: {user_message}")

    trimmed_history = trim_history(history, max_tokens=80_000)
    logger.info(f"Trimmed history: {len(trimmed_history)} messages")

    is_first_message = _is_first_user_message(history)
    recording_prefix = _get_recording_message(target_lang) if is_first_message else ""

    final_output = None
    new_messages = None
    text_buffer = ""
    prev_audio = ""

    async for event in voice_agent.run_stream_events(
        user_prompt=user_message,
        message_history=trimmed_history,
        deps=deps
    ):
        kind = getattr(event, 'event_kind', '')

        if kind == 'part_delta':
            delta = event.delta
            if getattr(delta, 'part_delta_kind', '') == 'text':
                text_buffer += delta.content_delta
                audio = _extract_audio_from_partial_json(text_buffer)
                if audio and audio != prev_audio:
                    prev_audio = audio
                    output_dict = {"audio": recording_prefix + audio, "end_interaction": False}
                    yield json.dumps(output_dict, ensure_ascii=False)

        elif kind == 'function_tool_result':
            # Reset text buffer for next model turn
            text_buffer = ""
            prev_audio = ""

        elif kind == 'agent_run_result':
            agent_result = event.result
            final_output = agent_result.output
            new_messages = agent_result.new_messages()

    # Yield the final complete output
    if final_output:
        audio_text = recording_prefix + (final_output.audio or "")
        output_dict = {"audio": audio_text, "end_interaction": final_output.end_interaction}
        yield json.dumps(output_dict, ensure_ascii=False)
        logger.info(f"Streaming complete - end_interaction: {final_output.end_interaction}")

    # Update message history
    if new_messages:
        await update_message_history(session_id, [*history, *new_messages])
