"""With an empty AI-technician list the agent offered "Ramesh Patel or Suresh
Patel" — the example names from its own system prompt — as real options: 199
turns / 68 callers in 7d on voice-production (to 2026-09-08), 199/199 with no
technician in context. Callers repeated the names back and the agent booked with
invented identifiers (29 partner calls, 29 HTTP 500). Three defences, one class each."""

import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL_NAME", "gpt-test")

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.models.ai_call import AISpecies
from agents.models.farmer import FarmerDataEnvelope, FarmerRecord
from agents.deps import FarmerAccount, FarmerTechnician
from agents.services import farmer_identity as fi
from agents.tools import ai_call as ai_mod
from app.services.voice import _build_ai_technician_summary

PROMPTS = Path(__file__).resolve().parents[1] / "assets" / "prompts"
PROMPT_FILES = [
    "voice_system_translation_pipeline_en.md",
    "voice_system_translation_pipeline_gpt5_1_en.md",
    "voice_system_translation_pipeline_gemma4_en.md",
]
LEAKED_NAMES = ["Ramesh Patel", "Suresh Patel", "Mahesh Parmar", "રાકેશ પટેલ", "સુરેશ પટેલ"]
VALID_TECH_ID = "YWl0LXRlY2gtMDAwMDAwMQ=="  # 24 base64 chars — the real prod shape


@pytest.mark.parametrize("filename", PROMPT_FILES)
def test_prompt_has_no_speakable_technician_name(filename):
    text = (PROMPTS / filename).read_text(encoding="utf-8")
    for name in LEAKED_NAMES:
        assert name not in text, f"{filename} still offers {name!r} for the model to speak"
    assert "never a name from these instructions or an example" in text


def _envelope(technicians):
    return FarmerDataEnvelope(
        farmers=[FarmerRecord(farmerName="Rameshbhai", farmerCode="F123",
                              societyCode="1066", unionCode="2021")],
        aiTechnicians=technicians,
    )


def test_empty_technician_context_forbids_naming():
    summary = _build_ai_technician_summary(_envelope([]))
    assert "Do NOT name any technician" in summary


def test_populated_technician_context_is_unchanged():
    summary = _build_ai_technician_summary(_envelope([{
        "farmerName": "Rameshbhai", "societyName": "Anand Dairy Society",
        "societyCode": "1066", "unionCode": "2021",
        "technicians": [{"userId": VALID_TECH_ID, "fullName": "Real Name",
                         "mobileNumber": "9876543210"}],
    }]))
    assert f"id={VALID_TECH_ID} full_name=Real Name" in summary
    assert "Do NOT name any technician" not in summary


# The model no longer supplies identifiers at all: create_ai_call takes a
# technician name and reads every code from context. These pin that.

_TECHNICIAN = FarmerTechnician(
    user_id=VALID_TECH_ID, full_name="Rakesh Solanki", farmer_name="Rameshbhai",
    union_code="159", society_code="00002", farmer_code="5058",
)


@pytest.mark.parametrize("spoken", [
    "MISSING", "UNKNOWN", "T55667", "Hiteshbhai Patel", "",
])
def test_unknown_technician_name_books_nothing(spoken):
    technician, problem = fi.match_technician([_TECHNICIAN], spoken)
    assert technician is None
    assert "Rakesh Solanki" in problem


def test_spoken_name_resolves_to_the_real_id_and_codes():
    technician, problem = fi.match_technician([_TECHNICIAN], "rakesh solanki")
    assert problem is None
    assert technician.user_id == VALID_TECH_ID
    assert (technician.union_code, technician.society_code, technician.farmer_code) == (
        "159", "00002", "5058",
    )


def test_ambiguous_name_asks_instead_of_picking():
    other = _TECHNICIAN.model_copy(update={"farmer_name": "Sureshbhai", "farmer_code": "5059"})
    technician, problem = fi.match_technician([_TECHNICIAN, other], "Rakesh Solanki")
    assert technician is None
    assert "Rameshbhai" in problem and "Sureshbhai" in problem


def test_no_technicians_books_nothing():
    technician, problem = fi.match_technician([], "Rakesh Solanki")
    assert technician is None
    assert "No AI technician is available" in problem


def test_unmatched_technician_never_reaches_the_partner_api(monkeypatch):
    calls = {"n": 0}

    async def fake_api(request, token):  # pragma: no cover - must not run
        calls["n"] += 1

    monkeypatch.setattr(ai_mod, "create_ai_call_api", fake_api)
    monkeypatch.setenv("PASHUGPT_TOKEN", "tok")

    async def in_scope():
        return True

    ctx = SimpleNamespace(deps=SimpleNamespace(
        session_id="s1", ensure_in_scope=in_scope, farmer_unions=[],
        ai_technicians=[_TECHNICIAN],
    ))
    out = asyncio.run(ai_mod.create_ai_call(ctx, "MISSING", next(iter(AISpecies))))
    assert calls["n"] == 0
    assert "No technician matched" in out


def test_health_call_without_accounts_books_nothing(monkeypatch):
    from agents.tools import health_call as hc_mod

    calls = {"n": 0}

    async def fake_api(request, token):  # pragma: no cover - must not run
        calls["n"] += 1

    monkeypatch.setattr(hc_mod, "create_health_call_api", fake_api)
    monkeypatch.setenv("PASHUGPT_TOKEN", "tok")

    async def in_scope():
        return True

    ctx = SimpleNamespace(deps=SimpleNamespace(
        session_id="s1", ensure_in_scope=in_scope, farmer_accounts=[],
    ))
    from agents.models.health_call import HealthCaseType
    out = asyncio.run(hc_mod.create_health_call(
        ctx, next(iter(AISpecies)), next(iter(HealthCaseType)), "fever",
    ))
    assert calls["n"] == 0
    assert "No farmer account is available" in out


def test_health_call_asks_which_farmer_when_several(monkeypatch):
    from agents.tools import health_call as hc_mod
    from agents.models.health_call import HealthCaseType

    calls = {"n": 0}

    async def fake_api(request, token):  # pragma: no cover - must not run
        calls["n"] += 1

    monkeypatch.setattr(hc_mod, "create_health_call_api", fake_api)
    monkeypatch.setenv("PASHUGPT_TOKEN", "tok")

    async def in_scope():
        return True

    accounts = [
        FarmerAccount(union_code="159", society_code="00002", farmer_code="5058", farmer_name="Rameshbhai"),
        FarmerAccount(union_code="159", society_code="00002", farmer_code="5059", farmer_name="Sureshbhai"),
    ]
    ctx = SimpleNamespace(deps=SimpleNamespace(
        session_id="s1", ensure_in_scope=in_scope, farmer_accounts=accounts,
    ))
    out = asyncio.run(hc_mod.create_health_call(
        ctx, next(iter(AISpecies)), next(iter(HealthCaseType)), "fever",
    ))
    assert calls["n"] == 0
    assert "Rameshbhai" in out and "Sureshbhai" in out
