"""A caller hanging up mid-answer is recorded as client_disconnected, not success
(amul-telemetry#7)."""

import asyncio

import pytest

from app.services.voice_trace import create_voice_trace

_QUERY = {"gu": "મારી ગાયને તાવ છે", "en": "my cow has a fever"}


def _patch_pipeline(monkeypatch, *, answer_blocks):
    """Stand-ins for everything around the agent. The agent streams one sentence;
    with answer_blocks it then waits forever, like a long answer still coming."""
    from app.services import voice as voice_module

    monkeypatch.setattr(voice_module.settings, "fallback_enabled", False, raising=False)
    monkeypatch.setattr(voice_module.settings, "enable_voice_nudges", False, raising=False)
    monkeypatch.setattr(voice_module.settings, "voice_trace_log_summary", False, raising=False)

    async def _update_message_history(*args, **kwargs):
        return None

    async def _check_moderation(*args, **kwargs):
        return voice_module.ModerationVerdict(category="in_scope", reason="ok")

    async def _check_non_meaningful_streak(*args, **kwargs):
        return voice_module.NonMeaningfulVerdict(five_consecutive_non_meaningful=False, reason="ok")

    async def _pretranslate(*args, **kwargs):
        return "my cow has a fever"

    streaming = asyncio.Event()

    class _Stream:
        async def stream_text(self, **kwargs):
            yield "Give her plenty of water and call the vet. "
            streaming.set()
            if answer_blocks:
                await asyncio.Event().wait()

        def new_messages(self):
            return []

    class _Agent:
        def run_stream(self, **kwargs):
            return self

        async def __aenter__(self):
            return _Stream()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(voice_module, "update_message_history", _update_message_history)
    monkeypatch.setattr(voice_module, "check_moderation", _check_moderation)
    monkeypatch.setattr(voice_module, "check_non_meaningful_streak", _check_non_meaningful_streak)
    monkeypatch.setattr(voice_module, "voice_agent", _Agent())
    monkeypatch.setattr(voice_module, "voice_agent_signed_in", _Agent())
    monkeypatch.setattr(voice_module, "translate_to_english_with_gpt5_mini", _pretranslate)
    monkeypatch.setattr(voice_module, "translate_to_english_with_oss_vllm", _pretranslate)
    return streaming


def _turn(trace, lang):
    from app.services import voice as voice_module

    return voice_module.stream_voice_message(
        query=_QUERY[lang],
        session_id="hangup",
        source_lang=lang,
        target_lang=lang,
        user_id="user-hangup",
        history=[],
        provider=None,
        process_id="proc-hangup",
        user_info={},
        owner=None,
        http_request=None,
        trace=trace,
    )


def _trace(lang):
    return create_voice_trace(
        session_id="hangup",
        user_id="user-hangup",
        query=_QUERY[lang],
        source_lang=lang,
        target_lang=lang,
        provider=None,
        process_id="proc-hangup",
    )


def test_a_turn_cancelled_mid_answer_is_client_disconnected(monkeypatch):
    streaming = _patch_pipeline(monkeypatch, answer_blocks=True)
    trace = _trace("gu")

    async def _hang_up():
        async def _listen():
            async for _ in _turn(trace, "gu"):
                pass

        task = asyncio.create_task(_listen())
        await asyncio.wait_for(streaming.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_hang_up())

    assert trace.finished
    assert trace.outcome == "client_disconnected"


def test_a_turn_closed_at_a_yield_is_client_disconnected(monkeypatch):
    _patch_pipeline(monkeypatch, answer_blocks=True)
    # English, so the first sentence reaches the caller without a translation step.
    trace = _trace("en")

    async def _hang_up():
        stream = _turn(trace, "en")
        # In this task, as Starlette would: the turn's context vars are reset
        # where they were set.
        await stream.__anext__()
        await stream.aclose()

    asyncio.run(_hang_up())

    assert trace.finished
    assert trace.outcome == "client_disconnected"


def test_a_turn_that_finishes_is_still_success(monkeypatch):
    _patch_pipeline(monkeypatch, answer_blocks=False)
    trace = _trace("gu")

    async def _listen():
        async for _ in _turn(trace, "gu"):
            pass

    asyncio.run(_listen())

    assert trace.finished
    assert trace.outcome == "success"
