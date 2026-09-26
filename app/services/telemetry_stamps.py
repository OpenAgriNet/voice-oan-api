"""Stable provenance stamps for telemetry emitted by this service."""

import os
from functools import lru_cache
from pathlib import Path


VOICE_TELEMETRY_SCHEMA_VERSION = "voice.turn.v1"
VOICE_TELEMETRY_SERVICE = "voice-oan-api"

_REPO = Path(__file__).resolve().parents[2]


def forward_voice_telemetry_metadata(release: str | None) -> dict[str, str]:
    return {
        "amul.schema_version": VOICE_TELEMETRY_SCHEMA_VERSION,
        "service": VOICE_TELEMETRY_SERVICE,
        "release": release or "unknown",
    }


@lru_cache(maxsize=1)
def running_release() -> str | None:
    """Git commit of the running code.

    The checkout's HEAD when the repo is mounted (docker-compose), otherwise GIT_SHA
    from the image build. Read once, so a pull without a restart doesn't change it.
    """
    return read_git_head(_REPO / ".git") or os.getenv("GIT_SHA") or None


def read_git_head(git_dir: Path) -> str | None:
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head or None
        ref = head.removeprefix("ref: ")
        loose = git_dir / ref
        if loose.is_file():
            return loose.read_text(encoding="utf-8").strip() or None
        for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
            sha, _, name = line.partition(" ")
            if name == ref:
                return sha
    except OSError:
        return None
    return None
