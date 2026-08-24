import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL_NAME", "gpt-test")

import pytest

from agents.deps import FarmerAccount, FarmerContext
from agents.tools.access import (
    FarmerAccessDenied,
    require_authenticated_farmer,
    require_owned_technician,
    resolve_owned_account,
)


def _deps(**overrides):
    values = dict(
        query="book",
        signed_in=True,
        identity_verified=True,
        subject_id="subject-hash",
        mobile="9924457046",
        farmer_accounts=[FarmerAccount(union_code="U1", society_code="S1", farmer_code="F1")],
        ai_technician_ids=["TECH-1"],
    )
    values.update(overrides)
    return FarmerContext(**values)


def test_private_access_requires_verified_identity():
    with pytest.raises(FarmerAccessDenied):
        require_authenticated_farmer(_deps(identity_verified=False))


def test_account_codes_must_match_authenticated_context():
    account = resolve_owned_account(_deps(), "U1", "S1", "F1")
    assert account.farmer_code == "F1"
    with pytest.raises(FarmerAccessDenied):
        resolve_owned_account(_deps(), "U1", "S1", "OTHER")


def test_technician_must_be_in_authenticated_context():
    assert require_owned_technician(_deps(), "TECH-1") == "TECH-1"
    with pytest.raises(FarmerAccessDenied):
        require_owned_technician(_deps(), "TECH-2")
