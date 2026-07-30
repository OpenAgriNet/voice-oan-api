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


# --- fixtures must match the REAL tool return shapes ------------------------
# Verified against production tool observations (voice-production, 2026-07-30).
# These are consumed by the agent, which renders the spoken sentence itself —
# so they are deliberately NOT prose.


def test_milk_fixture_matches_formatter_shape(monkeypatch):
    """Mirrors _format_milk_collection_summary: labelled, one record per line."""
    df = _reload_fixtures(monkeypatch, enabled="true", ids="8035454078")
    text = df.milk_collection_summary()
    assert text.startswith("Milk collection records (")
    # Field labels are what stop the small OSS model confusing qty/fat/SNF/amount.
    for label in ("quantity", "liters", "fat", "SNF", "amount", "rupees"):
        assert label in text
    assert "Deduction records (" in text
    body = [ln for ln in text.splitlines() if ln.startswith("  ")]
    assert len(body) >= 2


def test_ai_call_fixture_matches_prod_shape(monkeypatch):
    """Real shape: prefix line, blank line, JSON with ait_name + ticket_number."""
    import json

    df = _reload_fixtures(monkeypatch, enabled="true", ids="8035454078")
    text = df.ai_call_booked("cow")
    prefix, _, payload = text.partition("\n\n")
    assert prefix == "Artificial insemination call booked successfully:"
    parsed = json.loads(payload)
    assert set(parsed) == {"ait_name", "ticket_number"}
    # prod ait_name looks like "518 HARESHKUMAR-GANESHBHAI-PATEL"
    code, _, name = parsed["ait_name"].partition(" ")
    assert code.isdigit() and name.isupper() and "-" in name
    assert parsed["ticket_number"].isdigit()


def test_health_call_fixture_matches_prod_shape(monkeypatch):
    df = _reload_fixtures(monkeypatch, enabled="true", ids="8035454078")
    text = df.health_call_booked("normal")
    assert text.startswith("Health call booked successfully. Ticket number: ")
    ticket = text.rsplit(": ", 1)[1]
    # Prod tickets are DDMMYYYY + a 4-digit serial.
    assert ticket.isdigit() and len(ticket) == 12
