"""
Demo mock mode guard.

The only thing that really matters here: a caller who is NOT the demo number, or
any caller at all when the flag is off, must reach the real tool path. Failing
open would silently serve fabricated milk figures and fake booking confirmations
to actual farmers.
"""

import importlib.util
import pathlib
from types import SimpleNamespace

import pytest

# Load by file path rather than `from agents.tools import demo_fixtures`:
# agents/tools/__init__.py imports the whole model stack, which needs a
# pydantic-ai version the local dev venv does not have. The guard under test has
# no such dependency, so keep it runnable anywhere.
_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "agents" / "tools" / "demo_fixtures.py"
)


def _reload_fixtures(monkeypatch, *, enabled: str | None, ids: str | None):
    """(Re)load the module so its module-level env reads are re-evaluated."""
    if enabled is None:
        monkeypatch.delenv("DEMO_MOCK_ENABLED", raising=False)
    else:
        monkeypatch.setenv("DEMO_MOCK_ENABLED", enabled)
    if ids is None:
        monkeypatch.delenv("DEMO_MOCK_USER_IDS", raising=False)
    else:
        monkeypatch.setenv("DEMO_MOCK_USER_IDS", ids)

    spec = importlib.util.spec_from_file_location("demo_fixtures_under_test", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ctx(mobile):
    return SimpleNamespace(deps=SimpleNamespace(mobile=mobile, session_id="s-1"))


# --- the guard must default closed ------------------------------------------


def test_disabled_by_default(monkeypatch):
    df = _reload_fixtures(monkeypatch, enabled=None, ids=None)
    assert df.is_demo_caller(_ctx("8035454078")) is False


def test_flag_on_but_no_ids_listed(monkeypatch):
    df = _reload_fixtures(monkeypatch, enabled="true", ids="")
    assert df.is_demo_caller(_ctx("8035454078")) is False


def test_ids_listed_but_flag_off(monkeypatch):
    df = _reload_fixtures(monkeypatch, enabled="false", ids="8035454078")
    assert df.is_demo_caller(_ctx("8035454078")) is False


# --- only the listed caller matches -----------------------------------------


def test_demo_caller_matches(monkeypatch):
    df = _reload_fixtures(monkeypatch, enabled="true", ids="8035454078")
    assert df.is_demo_caller(_ctx("8035454078")) is True


def test_country_coded_variant_matches(monkeypatch):
    """RAYA strips 91, but a 12-digit form must still match its listed 10-digit."""
    df = _reload_fixtures(monkeypatch, enabled="true", ids="8035454078")
    assert df.is_demo_caller(_ctx("918035454078")) is True
    assert df.is_demo_caller(_ctx("+918035454078")) is True


@pytest.mark.parametrize(
    "mobile",
    ["9978522592", "7990748700", "8035454079", "803545407", "", None],
)
def test_real_farmers_never_match(monkeypatch, mobile):
    df = _reload_fixtures(monkeypatch, enabled="true", ids="8035454078")
    assert df.is_demo_caller(_ctx(mobile)) is False


def test_missing_context_never_matches(monkeypatch):
    df = _reload_fixtures(monkeypatch, enabled="true", ids="8035454078")
    assert df.is_demo_caller(SimpleNamespace(deps=None)) is False
    assert df.is_demo_caller(SimpleNamespace()) is False


def test_multiple_ids(monkeypatch):
    df = _reload_fixtures(monkeypatch, enabled="true", ids="8035454078, 8035451051")
    assert df.is_demo_caller(_ctx("8035451051")) is True
    assert df.is_demo_caller(_ctx("9978522592")) is False


# --- fixtures are voice-shaped ----------------------------------------------


def test_fixtures_are_plain_speakable_strings(monkeypatch):
    df = _reload_fixtures(monkeypatch, enabled="true", ids="8035454078")
    for text in (
        df.milk_collection_summary(),
        df.ai_call_booked("cow"),
        df.health_call_booked("mastitis"),
    ):
        assert isinstance(text, str) and text.strip()
        # The voice prompt forbids markdown/brackets; TTS reads these literally.
        for bad in ("*", "#", "[", "]", "|", "\n\n"):
            assert bad not in text, f"{bad!r} would be spoken aloud: {text!r}"


def test_ai_call_fixture_reflects_species(monkeypatch):
    df = _reload_fixtures(monkeypatch, enabled="true", ids="8035454078")
    assert "buffalo" in df.ai_call_booked("Buffalo").lower()
    assert "cow" in df.ai_call_booked("Cow").lower()
