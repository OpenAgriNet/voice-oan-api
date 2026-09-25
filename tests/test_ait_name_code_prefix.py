"""Internal society/route numbers must never reach the model or the caller.

Partner records ship names like "1712 NARENDRAKUMAR-NARAYANDAS-PANDOR". TTS
reads the number aloud — one dev replay spoke "1730 Sanjaykumar" as a time of
day ("સવારે પાંચ: ત્રણશૂન્ય વાગ્યે"). Measured on prod: 20.8% of technician
options carry the prefix, no name has digits anywhere else, none is digits only.
"""
import pytest

from agents.models.ai_call import AICallResponseModel, strip_ait_name_code_prefix
from agents.models.farmer import FarmerDataEnvelope, FarmerRecord
from app.services.voice import _build_ai_technician_summary


@pytest.mark.parametrize("raw,expected", [
    ("1712 NARENDRAKUMAR-NARAYANDAS-PANDOR", "NARENDRAKUMAR-NARAYANDAS-PANDOR"),
    ("59 POPATBHAI-BABUBHAI-PATEL", "POPATBHAI-BABUBHAI-PATEL"),
    ("918 MITULKUMAR-RAMESHBHAI-PATEL", "MITULKUMAR-RAMESHBHAI-PATEL"),
    ("1730-Sanjaykumar Laxmanbhai Vanjara", "Sanjaykumar Laxmanbhai Vanjara"),
    ("  204   TUSHARKUMAR-HASMUKHBHAI  ", "TUSHARKUMAR-HASMUKHBHAI"),
])
def test_prefix_is_stripped(raw, expected):
    assert strip_ait_name_code_prefix(raw) == expected


@pytest.mark.parametrize("raw", [
    "SANJAYKUMAR-CHIMANBHAI-PATEL",      # no prefix at all
    "DINESHJI-RAMESHJI-PARMAR",
    "K2-RAMESHBHAI-PATEL",               # digits not leading — left alone
])
def test_real_names_are_untouched(raw):
    assert strip_ait_name_code_prefix(raw) == raw


@pytest.mark.parametrize("raw", ["", None])
def test_empty_passes_through(raw):
    assert strip_ait_name_code_prefix(raw) == raw


def test_all_digits_keeps_the_original_rather_than_blanking():
    """A degenerate record must still identify someone, not vanish."""
    assert strip_ait_name_code_prefix("1712") == "1712"
    # The guard returns the input untouched — trailing space and all — rather
    # than a blank that would identify nobody.
    assert strip_ait_name_code_prefix("1712 ") == "1712 "


def test_booking_response_name_is_cleaned_too():
    """The name spoken back after booking comes from a different field."""
    model = AICallResponseModel.model_validate(
        {"aitName": "918 MITULKUMAR-RAMESHBHAI-PATEL", "ticketNumber": "T1"}
    )
    assert model.ait_name == "MITULKUMAR-RAMESHBHAI-PATEL"


def test_phone_normalisation_and_strip_compose():
    """ait_name can arrive as "<phone>(<name>)".

    The leading digits there are the phone number and must survive; the code
    rides on the name inside the parens and must not.
    """
    model = AICallResponseModel.model_validate(
        {"aitName": "919876543210(918 MITULKUMAR-RAMESHBHAI)", "ticketNumber": "T1"}
    )
    assert model.ait_name == "9876543210(MITULKUMAR-RAMESHBHAI)"


# --- the site that matters: what the agent actually receives ----------------

def _summary_with(*names):
    envelope = FarmerDataEnvelope(
        farmers=[FarmerRecord(farmerName="Rameshbhai", societyName="Anand Dairy Society", farmerCode="F123")],
        aiTechnicians=[{
            "farmerName": "Rameshbhai", "farmerCode": "F123",
            "societyName": "Anand Dairy Society", "societyCode": "1066", "unionCode": "2021",
            "technicians": [
                {"fullName": n, "mobileNumber": f"98765432{i:02d}", "userId": f"tech-{i}"}
                for i, n in enumerate(names)
            ],
        }],
        source="cache",
    )
    return _build_ai_technician_summary(envelope)


def test_context_never_carries_the_code():
    """The prompt the agent sees must not contain the internal number."""
    summary = _summary_with("1712 NARENDRAKUMAR-NARAYANDAS-PANDOR")
    assert "full_name=NARENDRAKUMAR-NARAYANDAS-PANDOR" in summary
    assert "1712 NARENDRAKUMAR" not in summary


def test_context_keeps_the_technician_id_which_the_tool_call_needs():
    """Only the spoken name is cleaned — id= is how create_ai_call is addressed."""
    summary = _summary_with("1712 NARENDRAKUMAR-NARAYANDAS-PANDOR")
    assert "id=tech-0" in summary


def test_context_leaves_unprefixed_names_alone():
    summary = _summary_with("SANJAYKUMAR-CHIMANBHAI-PATEL")
    assert "full_name=SANJAYKUMAR-CHIMANBHAI-PATEL" in summary


def test_two_technicians_differing_only_by_code_stay_distinguishable():
    """Stripping must not collapse the options into one indistinguishable pair."""
    summary = _summary_with("204 HASMUKHBHAI-VIRSANGBHAI-PATEL", "809 VISHNUBHAI-KANTIBHAI-PATEL")
    assert "full_name=HASMUKHBHAI-VIRSANGBHAI-PATEL" in summary
    assert "full_name=VISHNUBHAI-KANTIBHAI-PATEL" in summary
    assert "204" not in summary and "809" not in summary
