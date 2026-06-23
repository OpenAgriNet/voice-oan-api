import asyncio
import logging

from app.services.voice_trace import create_voice_trace, sanitize_text


def test_sanitize_text_preview_hash(monkeypatch):
    monkeypatch.setattr("app.services.voice_trace.settings.voice_trace_text_mode", "preview_hash", raising=False)
    monkeypatch.setattr("app.services.voice_trace.settings.voice_trace_preview_chars", 5, raising=False)

    payload = sanitize_text("hello farmer")

    assert payload["chars"] == 12
    assert payload["preview"] == "hello"
    assert payload["sha256"]
    assert "text" not in payload


def test_sanitize_text_full(monkeypatch):
    monkeypatch.setattr("app.services.voice_trace.settings.voice_trace_text_mode", "full", raising=False)

    payload = sanitize_text("hello farmer")

    assert payload["text"] == "hello farmer"
    assert payload["sha256"]


def test_sanitize_text_none(monkeypatch):
    monkeypatch.setattr("app.services.voice_trace.settings.voice_trace_text_mode", "none", raising=False)

    payload = sanitize_text("hello farmer")

    assert payload["chars"] == 12
    assert payload["sha256"]
    assert "preview" not in payload
    assert "text" not in payload


def test_stage_and_finish_emit_summary(caplog, monkeypatch):
    monkeypatch.setattr("app.services.voice_trace.settings.enable_voice_tracing", True, raising=False)
    monkeypatch.setattr("app.services.voice_trace.settings.voice_trace_log_summary", True, raising=False)
    caplog.set_level(logging.INFO, logger="app.services.voice_trace")

    trace = create_voice_trace(
        session_id="trace-test",
        user_id="user-1",
        query="hello",
        source_lang="en",
        target_lang="en",
        provider=None,
        process_id="proc-1",
    )

    with trace.stage("unit_stage", metadata={"example": "yes"}):
        pass
    trace.record_emit("first answer")
    trace.finish("success")
    trace.finish("ignored")

    assert trace.stage_totals_ms["unit_stage"] >= 0
    assert trace.timings_ms["ttft_ms"] >= 0
    assert trace.timings_ms["ttfr_ms"] >= 0
    assert trace.outcome == "success"
    assert sum("VOICE_TRACE_SUMMARY" in r.message for r in caplog.records) == 1


def test_greeting_stream_records_trace(monkeypatch):
    from app.services import voice as voice_module

    async def _update_message_history(*args, **kwargs):
        return None

    async def _render_text_for_caller(text_en, target_lang):
        return text_en

    monkeypatch.setattr(voice_module, "update_message_history", _update_message_history)
    monkeypatch.setattr(voice_module, "_render_text_for_caller", _render_text_for_caller)
    monkeypatch.setattr(voice_module.settings, "voice_trace_log_summary", False, raising=False)

    trace = create_voice_trace(
        session_id="greeting-trace",
        user_id="anonymous",
        query="hello",
        source_lang="en",
        target_lang="en",
        provider=None,
        process_id="proc-1",
    )

    async def _collect():
        chunks = []
        async for chunk in voice_module.stream_voice_message(
            query="hello",
            session_id="greeting-trace",
            source_lang="en",
            target_lang="en",
            user_id="anonymous",
            history=[],
            provider=None,
            process_id="proc-1",
            user_info={},
            owner=None,
            http_request=None,
            trace=trace,
        ):
            chunks.append(chunk)
        return "".join(chunks)

    output = asyncio.run(_collect())

    assert "Hello, I am Sarlaben" in output
    assert trace.route == "greeting_fast_path"
    assert trace.outcome == "greeting_fast_path"
    assert "ttft_ms" in trace.timings_ms
    assert "ttfr_ms" in trace.timings_ms


def test_set_pretranslation_records_requested_vs_actual_fields():
    trace = create_voice_trace(
        session_id="pretranslation-trace",
        user_id="user-1",
        query="namaste",
        source_lang="hi",
        target_lang="en",
        provider=None,
        process_id="proc-1",
    )

    trace.set_pretranslation(
        text="hello",
        provider="vllm",
        fallback_used=True,
        requested_tier="oss",
        requested_provider="vllm",
        requested_model="oss-pretranslate",
        actual_tier="managed",
        actual_provider="openai",
        actual_model="gpt5-mini",
        attempts=[
            {"tier": "oss", "status": "error", "error_reason": "timeout"},
            {"tier": "managed", "status": "ok"},
        ],
    )

    payload = trace.metadata["pretranslation"]
    assert payload["requested_tier"] == "oss"
    assert payload["requested_provider"] == "vllm"
    assert payload["requested_model"] == "oss-pretranslate"
    assert payload["actual_tier"] == "managed"
    assert payload["actual_provider"] == "openai"
    assert payload["actual_model"] == "gpt5-mini"
    assert payload["fallback_used"] is True
    assert payload["attempts"][0]["status"] == "error"
    assert payload["attempts"][1]["status"] == "ok"


def test_set_agent_records_requested_actual_and_commit_fields():
    trace = create_voice_trace(
        session_id="agent-trace",
        user_id="user-1",
        query="help me",
        source_lang="en",
        target_lang="en",
        provider=None,
        process_id="proc-1",
    )

    trace.set_agent(
        signed_in=True,
        output="ok",
        new_messages=[],
        requested_tier="oss",
        requested_provider="vllm",
        requested_model="gemma",
        actual_tier="managed",
        actual_provider="openai",
        actual_model="gpt-5",
        first_token_committed_tier="managed",
        first_token_committed_provider="openai",
        first_token_committed_model="gpt-5",
        fallback_used=True,
        attempts=[
            {"tier": "oss", "status": "error", "error_reason": "connection"},
            {"tier": "managed", "status": "ok", "committed": True},
        ],
    )

    payload = trace.metadata["agent"]
    assert payload["requested_tier"] == "oss"
    assert payload["requested_provider"] == "vllm"
    assert payload["requested_model"] == "gemma"
    assert payload["actual_tier"] == "managed"
    assert payload["actual_provider"] == "openai"
    assert payload["actual_model"] == "gpt-5"
    assert payload["first_token_committed_tier"] == "managed"
    assert payload["first_token_committed_provider"] == "openai"
    assert payload["first_token_committed_model"] == "gpt-5"
    assert payload["fallback_used"] is True
    assert payload["attempts"][0]["status"] == "error"
    assert payload["attempts"][1]["status"] == "ok"


def test_pretranslation_context_passthrough_for_oss_and_managed(monkeypatch):
    from app.services import voice as voice_module

    monkeypatch.setattr(voice_module.settings, "fallback_enabled", False, raising=False)
    monkeypatch.setattr(voice_module.settings, "enable_voice_nudges", False, raising=False)
    monkeypatch.setattr(voice_module.settings, "voice_trace_log_summary", False, raising=False)

    async def _update_message_history(*args, **kwargs):
        return None

    async def _check_moderation(*args, **kwargs):
        return voice_module.ModerationVerdict(category="in_scope", reason="ok")

    async def _check_non_meaningful_streak(*args, **kwargs):
        return voice_module.NonMeaningfulVerdict(
            five_consecutive_non_meaningful=False,
            reason="ok",
        )

    class _FakeStream:
        async def stream_text(self, **kwargs):
            if False:
                yield ""

        def new_messages(self):
            return []

    class _FakeAgent:
        def run_stream(self, **kwargs):
            return self

        async def __aenter__(self):
            return _FakeStream()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(voice_module, "update_message_history", _update_message_history)
    monkeypatch.setattr(voice_module, "check_moderation", _check_moderation)
    monkeypatch.setattr(voice_module, "check_non_meaningful_streak", _check_non_meaningful_streak)
    monkeypatch.setattr(voice_module, "voice_agent", _FakeAgent())
    monkeypatch.setattr(voice_module, "voice_agent_signed_in", _FakeAgent())

    captures: list[tuple[str, dict]] = []

    async def _fake_managed_pretranslation(*args, **kwargs):
        captures.append(("managed", kwargs))
        return "translated"

    async def _fake_oss_pretranslation(*args, **kwargs):
        captures.append(("oss", kwargs))
        return "translated"

    monkeypatch.setattr(voice_module, "translate_to_english_with_gpt5_mini", _fake_managed_pretranslation)
    monkeypatch.setattr(voice_module, "translate_to_english_with_oss_vllm", _fake_oss_pretranslation)

    async def _run_variant(variant: str):
        trace = create_voice_trace(
            session_id=f"ctx-{variant}",
            user_id="user-ctx",
            query="મારી ગાયને તાવ છે અને દૂધ ઓછું આવે છે",
            source_lang="gu",
            target_lang="gu",
            provider=None,
            process_id="proc-ctx",
        )
        async for _ in voice_module.stream_voice_message(
            query="મારી ગાયને તાવ છે અને દૂધ ઓછું આવે છે",
            session_id=f"ctx-{variant}",
            source_lang="gu",
            target_lang="gu",
            user_id="user-ctx",
            history=[],
            provider=None,
            process_id="proc-ctx",
            user_info={},
            owner=None,
            http_request=None,
            trace=trace,
            pipeline_variant=variant,
        ):
            pass
        return trace

    legacy_trace = asyncio.run(_run_variant("legacy"))
    oss_trace = asyncio.run(_run_variant("oss"))

    managed_call = next(kwargs for kind, kwargs in captures if kind == "managed")
    oss_call = next(kwargs for kind, kwargs in captures if kind == "oss")

    for kwargs, variant in ((managed_call, "legacy"), (oss_call, "oss")):
        assert kwargs["session_id"] == f"ctx-{variant}"
        assert kwargs["user_id"] == "user-ctx"
        assert kwargs["process_id"] == "proc-ctx"
        assert kwargs["pipeline_variant"] == variant

    legacy_agent = legacy_trace.metadata["agent"]
    oss_agent = oss_trace.metadata["agent"]
    assert legacy_agent["attempts"][0]["tier"] == "managed"
    assert legacy_agent["attempts"][0]["status"] == "ok_no_output"
    assert oss_agent["attempts"][0]["tier"] == "oss"
    assert oss_agent["attempts"][0]["status"] == "ok_no_output"
