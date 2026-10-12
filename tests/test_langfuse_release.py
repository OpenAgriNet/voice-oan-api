"""Voice traces carry the release of the build that served them
(amul-telemetry#8). Run in a subprocess: app.observability sets up Langfuse
when it's first imported."""

import os
import subprocess
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Imports app.observability with a stand-in get_client(), which records the
# release the real client would read, then prints it and the release stamp.
_START = (
    "import os, sys, types;"
    "seen = {};"
    "client = type('Client', (), {'auth_check': lambda self: False});"
    "fake = types.ModuleType('langfuse');"
    "fake.get_client = lambda: seen.update(release=os.environ.get('LANGFUSE_RELEASE')) or client();"
    "sys.modules['langfuse'] = fake;"
    "import app.observability;"
    "from app.services.telemetry_stamps import forward_voice_telemetry_metadata, running_release;"
    "print(seen['release']);"
    "print(forward_voice_telemetry_metadata(running_release())['release'])"
)


def _start(tmp_path, **env):
    # No LANGFUSE_* from the environment, and a working directory without a .env.
    clean = {key: value for key, value in os.environ.items() if not key.startswith("LANGFUSE_")}
    result = subprocess.run(
        [sys.executable, "-c", _START],
        cwd=tmp_path,
        env={
            **clean,
            "PYTHONPATH": _REPO_ROOT,
            "LANGFUSE_PUBLIC_KEY": "pk-test",
            "LANGFUSE_SECRET_KEY": "sk-test",
            **env,
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    release, stamp = result.stdout.strip().splitlines()[-2:]
    return release, stamp


def test_release_is_the_running_commit_by_default(tmp_path):
    # The checkout's HEAD, or this GIT_SHA where there's no .git to read.
    release, stamp = _start(tmp_path, GIT_SHA="0123abc")

    assert release == stamp
    assert release not in ("voice-oan-api", "unknown")


def test_release_comes_from_the_deployment_when_set(tmp_path):
    release, _ = _start(tmp_path, LANGFUSE_RELEASE="build-123")

    assert release == "build-123"
