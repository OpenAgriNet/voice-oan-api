"""The farmer context must not tell the agent to ask an unanswerable question.

A mobile is shared by a household. Of 364 real name pairs the agent offered,
250 share a name token and 19 are byte identical, so the caller answers and the
answer fits both records. 71 of the 265 calls in "AI booking not done" died in
that loop — the largest single remaining source of failed bookings (#282).

Whether the question is worth a turn depends on what differs: technicians come
from (unionCode, societyCode) alone, and 90% of multi-account mobiles have every
record in one society, where the visit is identical whichever is used.
"""
import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL_NAME", "gpt-test")

from agents.models.farmer import FarmerDataEnvelope
from app.services.voice import _build_compact_farmer_summary


def _env(records):
    return FarmerDataEnvelope.from_records(records, source="api", lookup_status="found")


def _rec(farmer_code, name, society_code="00731", society_name="RAMOS", union_code="159"):
    return {
        "unionCode": union_code, "societyCode": society_code,
        "farmerCode": farmer_code, "farmerName": name, "societyName": society_name,
    }


def test_same_village_does_not_ask_which_farmer():
    out = _build_compact_farmer_summary(_env([
        _rec("0554", "Patel Asvinbhai Amrutbhai"),
        _rec("0192", "Patel Nuruben Ashvinbhai A"),
    ]))
    assert "Do NOT ask which farmer name" in out
    assert "Book with Farmer option 1" in out
    assert "ask which farmer name the caller wants to use" not in out


def test_duplicate_records_of_one_person_are_named_as_such():
    out = _build_compact_farmer_summary(_env([
        _rec("0554", "Mukeshbhai Jivrajbhai"),
        _rec("0192", "Mukeshbhai  Jivrajbhai.."),
    ]))
    assert "duplicate records of one farmer" in out
    assert "Do NOT ask which farmer name" in out


def test_different_villages_ask_by_village_not_by_name():
    out = _build_compact_farmer_summary(_env([
        _rec("0554", "Patel Asvinbhai", society_code="00731", society_name="RAMOS"),
        _rec("0192", "Patel Nuruben", society_code="00262", society_name="DHANSURA"),
    ]))
    assert "Ask which village" in out
    assert "RAMOS" in out and "DHANSURA" in out
    assert "Do NOT ask which farmer name" in out


def test_single_record_is_unchanged():
    out = _build_compact_farmer_summary(_env([_rec("0554", "Ramesh")]))
    assert "Multiple farmer records" not in out
    assert "Do NOT ask" not in out


def test_every_record_is_still_listed_for_the_technician_groups():
    """Options are not trimmed: _build_ai_technician_summary keys its groups by
    farmer, so a record dropped here would leave an unreachable group."""
    out = _build_compact_farmer_summary(_env([
        _rec("0554", "Patel Asvinbhai"), _rec("0192", "Patel Nuruben"),
    ]))
    assert "Farmer option 1" in out and "Farmer option 2" in out
