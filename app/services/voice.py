from typing import AsyncGenerator
import asyncio
import json
import os
import re
from agents.voice import voice_agent
from agents.deps import FarmerContext
from agents.tools.language import LANGUAGE_CACHE_SUFFIX
from app.core.languages import (
    NO_PREFERENCE,
    is_supported,
    resolve_render_language,
)
from helpers.telemetry import (
    TelemetryRequest,
    create_voice_response_event,
    generate_voice_question_id,
    post_telemetry_payload,
)
from helpers.utils import get_logger
from app.utils import update_message_history, trim_history
from app.core.cache import cache
from app.observability.langfuse_client import safe_start_agent_observation
from app.observability.voice import safe_update_observation

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

# Call-recording disclaimer per language. Languages without an entry fall back to
# Hindi (which matches the response language, since untranslated languages are
# also served via the Hindi prompt — see app/core/languages.resolve_render_language).
_RECORDING_MESSAGES = {
    "hi": "यह कॉल प्रशिक्षण और गुणवत्ता सुधार हेतु रिकॉर्ड की जा रही है। आपकी जानकारी सुरक्षित रहेगी।",
    "en": "This call is being recorded for training and quality purposes. Your personal information will not be shared with any third party.",
    "mr": "हा कॉल प्रशिक्षण आणि गुणवत्ता सुधारणेसाठी रेकॉर्ड केला जात आहे. तुमची माहिती सुरक्षित राहील.",
    "bn": "এই কলটি প্রশিক্ষণ এবং গুণমান উন্নয়নের জন্য রেকর্ড করা হচ্ছে। আপনার তথ্য সুরক্ষিত থাকবে।",
    "te": "ఈ కాల్ శిక్షణ మరియు నాణ్యత మెరుగుదల కోసం రికార్డ్ చేయబడుతోంది. మీ వ్యక్తిగత సమాచారం సురక్షితంగా ఉంటుంది।",
    "ta": "இந்த அழைப்பு பயிற்சி மற்றும் தர மேம்பாட்டிற்காக பதிவு செய்யப்படுகிறது. உங்கள் தகவல்கள் பாதுகாக்கப்படும்।",
    "gu": "આ કૉલ પ્રશિક્ષણ અને ગુણવત્તા સુધારણા માટે રેકૉર્ડ કરવામાં આવી રહ્યો છે. તમારી માહિતી સુરક્ષિત રહેશે.",
    "kn": "ಈ ಕರೆಯನ್ನು ತರಬೇತಿ ಮತ್ತು ಗುಣಮಟ್ಟ ಸುಧಾರಣೆಗಾಗಿ ರೆಕಾರ್ಡ್ ಮಾಡಲಾಗುತ್ತಿದೆ. ನಿಮ್ಮ ಮಾಹಿತಿ ಸುರಕ್ಷಿತವಾಗಿರುತ್ತದೆ.",
    "ml": "ഈ കോൾ പരിശീലനത്തിനും ഗുണനിലവാര മെച്ചപ്പെടുത്തലിനുമായി റെക്കോർഡ് ചെയ്യുന്നു. നിങ്ങളുടെ വ്യക്തിഗത വിവരങ്ങൾ സുരക്ഷിതമായിരിക്കും.",
    "as": "এই কলটো প্ৰশিক্ষণ আৰু গুণমান উন্নতিৰ বাবে ৰেকৰ্ড কৰা হৈছে। আপোনাৰ তথ্য সুৰক্ষিত থাকিব।",
}


def _get_recording_message(lang: str | None) -> str:
    """Get the recording disclaimer for the language the bot will actually speak."""
    render_lang = resolve_render_language(lang)
    return _RECORDING_MESSAGES.get(render_lang, _RECORDING_MESSAGES["hi"])

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
    # Language resolution (header-with-fallback-gate):
    #  1. If the client sent a supported X-Language code, trust it.
    #  2. Otherwise fall back to a language the user explicitly chose via the gate
    #     (set_language, cached as en/hi).
    #  3. Otherwise no preference yet -> the agent will run the language gate.
    cached_lang: str | None = await cache.get(f"{session_id}{LANGUAGE_CACHE_SUFFIX}")
    if is_supported(target_lang):
        effective_lang = target_lang
    elif cached_lang in ("en", "hi"):
        effective_lang = cached_lang
    else:
        effective_lang = NO_PREFERENCE
    # The language the bot can actually speak (falls back to Hindi for accepted-but
    # -untranslated languages); None while the gate is still asking for a preference.
    response_lang = resolve_render_language(effective_lang) if is_supported(effective_lang) else None
    deps = FarmerContext(query=query, lang_code=effective_lang, session_id=session_id, user_id=user_id)
    user_message = deps.get_user_message()
    voice_qid = generate_voice_question_id()
    logger.info(f"Running agent (effective_lang={effective_lang}, voice_qid={voice_qid})")

    trimmed_history = trim_history(history, max_tokens=80_000)
    logger.info(f"Trimmed history: {len(trimmed_history)} messages")

    is_first_message = _is_first_user_message(history)
    # Use effective_lang for recording message so it matches the frozen session language
    recording_prefix = _get_recording_message(effective_lang) if is_first_message else ""

    final_output = None
    new_messages = None
    text_buffer = ""
    prev_audio = ""

    agent_slug = (voice_agent.name or "voice").replace(" ", "_").lower()
    with safe_start_agent_observation(
        name=f"agent.{agent_slug}",
        input={
            "query": query,
            "effective_lang": effective_lang,
            "session_id": session_id,
            "target_lang": target_lang,
        },
        metadata={
            "agent_name": voice_agent.name,
            "voice_qid": voice_qid,
            "user_id": user_id,
        },
        tags=["voice", "pydantic_ai", f"agent:{agent_slug}"],
    ) as agent_obs:
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
                        output_dict = _voice_output_dict(recording_prefix + audio, False, response_lang)
                        yield json.dumps(output_dict, ensure_ascii=False)

            elif kind == 'function_tool_result':
                # Reset text buffer for next model turn
                text_buffer = ""
                prev_audio = ""

            elif kind == 'agent_run_result':
                agent_result = event.result
                final_output = agent_result.output
                new_messages = agent_result.new_messages()

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
        # Prefer a language explicitly chosen via the gate (set_language); otherwise
        # use the resolved response language (None while the gate is still asking).
        out_lang = deps.selected_language or response_lang
        # Final recording message by response language: hi → Hindi, en → English, else → Hindi
        final_recording_prefix = _get_recording_message(out_lang) if is_first_message else ""
        audio_text = final_recording_prefix + raw_audio
        output_dict = _voice_output_dict(audio_text, end_flag, out_lang)
        yield json.dumps(output_dict, ensure_ascii=False)
        logger.info(f"Streaming complete - end_interaction: {end_flag}, language: {out_lang}")

    # Update message history
    if new_messages:
        await update_message_history(session_id, [*history, *new_messages])
