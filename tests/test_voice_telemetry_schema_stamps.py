from app.config import settings
from app.services.telemetry_stamps import forward_voice_telemetry_metadata
from app.services.voice_trace import VoiceTrace


def test_forward_telemetry_metadata_stamps_schema_service_and_release():
    assert forward_voice_telemetry_metadata("test-release-sha") == {
        "amul.schema_version": "voice.turn.v1",
        "service": "voice-oan-api",
        "release": "test-release-sha",
    }


def test_forward_telemetry_metadata_marks_an_unknown_release():
    assert forward_voice_telemetry_metadata(None)["release"] == "unknown"


def test_every_voice_trace_carries_the_stamp(monkeypatch):
    monkeypatch.setattr(settings, "langfuse_release", "test-release-sha")

    trace = VoiceTrace(
        session_id="session-redacted",
        user_id="<redacted-user-id>",
        source_lang="gu",
        target_lang="gu",
        query="<redacted question>",
        enabled=False,
    )

    assert trace.metadata["amul.schema_version"] == "voice.turn.v1"
    assert trace.metadata["service"] == "voice-oan-api"
    assert trace.metadata["release"] == "test-release-sha"
    assert "user_id_hash" in trace.metadata and "query" in trace.metadata
