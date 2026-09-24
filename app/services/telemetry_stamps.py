"""Stable provenance stamps for telemetry emitted by this service."""


VOICE_TELEMETRY_SCHEMA_VERSION = "voice.turn.v1"
VOICE_TELEMETRY_SERVICE = "voice-oan-api"


def forward_voice_telemetry_metadata(release: str | None) -> dict[str, str]:
    return {
        "amul.schema_version": VOICE_TELEMETRY_SCHEMA_VERSION,
        "service": VOICE_TELEMETRY_SERVICE,
        "release": release or "unknown",
    }
