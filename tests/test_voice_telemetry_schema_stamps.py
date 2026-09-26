import pytest

from app.services import telemetry_stamps, voice_trace
from app.services.telemetry_stamps import forward_voice_telemetry_metadata, read_git_head, running_release
from app.services.voice_trace import VoiceTrace

SHA = "0123456789abcdef0123456789abcdef01234567"


def test_forward_telemetry_metadata_stamps_schema_service_and_release():
    assert forward_voice_telemetry_metadata("test-release-sha") == {
        "amul.schema_version": "voice.turn.v1",
        "service": "voice-oan-api",
        "release": "test-release-sha",
    }


def test_forward_telemetry_metadata_marks_an_unknown_release():
    assert forward_voice_telemetry_metadata(None)["release"] == "unknown"


def test_every_voice_trace_carries_the_stamp(monkeypatch):
    monkeypatch.setattr(voice_trace, "running_release", lambda: "test-release-sha")

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


def _git_dir(tmp_path, head, *, loose=None, packed=None):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text(head, encoding="utf-8")
    if loose is not None:
        (git_dir / "refs" / "heads").mkdir(parents=True)
        (git_dir / "refs" / "heads" / "amul-prod").write_text(loose, encoding="utf-8")
    if packed is not None:
        (git_dir / "packed-refs").write_text(packed, encoding="utf-8")
    return git_dir


def test_release_is_the_branch_commit(tmp_path):
    assert read_git_head(_git_dir(tmp_path, "ref: refs/heads/amul-prod\n", loose=f"{SHA}\n")) == SHA


def test_release_is_found_in_packed_refs(tmp_path):
    packed = f"# pack-refs with: peeled fully-peeled sorted\n{'f' * 40} refs/heads/amul-dev\n{SHA} refs/heads/amul-prod\n"

    assert read_git_head(_git_dir(tmp_path, "ref: refs/heads/amul-prod\n", packed=packed)) == SHA


def test_release_is_the_commit_of_a_detached_checkout(tmp_path):
    assert read_git_head(_git_dir(tmp_path, f"{SHA}\n")) == SHA


def test_no_git_dir_means_no_release(tmp_path):
    assert read_git_head(tmp_path / ".git") is None


@pytest.fixture
def fresh_release():
    running_release.cache_clear()
    yield
    running_release.cache_clear()


def test_a_mounted_checkout_wins_over_the_build_arg(monkeypatch, fresh_release):
    monkeypatch.setattr(telemetry_stamps, "read_git_head", lambda git_dir: SHA)
    monkeypatch.setenv("GIT_SHA", "stale-image-sha")

    assert running_release() == SHA


def test_the_build_arg_is_used_when_there_is_no_checkout(monkeypatch, fresh_release):
    monkeypatch.setattr(telemetry_stamps, "read_git_head", lambda git_dir: None)
    monkeypatch.setenv("GIT_SHA", SHA)

    assert running_release() == SHA


def test_an_empty_build_arg_is_no_release(monkeypatch, fresh_release):
    monkeypatch.setattr(telemetry_stamps, "read_git_head", lambda git_dir: None)
    monkeypatch.setenv("GIT_SHA", "")

    assert running_release() is None
