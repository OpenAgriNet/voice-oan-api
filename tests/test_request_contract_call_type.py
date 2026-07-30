"""Contract tests for the optional `call_type` query param Raya may send.

`call_type` is accepted but deliberately unused for now — these tests pin the
contract so a later consumer can rely on the default and the allowed values.
"""

import pytest
from pydantic import ValidationError

from app.models.requests import ChatRequest


def test_call_type_defaults_to_inbound_when_absent():
    request = ChatRequest(query="test")
    assert request.call_type == "inbound"


@pytest.mark.parametrize("value", ["inbound", "outbound"])
def test_call_type_accepts_valid_values(value):
    request = ChatRequest(query="test", call_type=value)
    assert request.call_type == value


def test_call_type_rejects_unknown_value():
    with pytest.raises(ValidationError):
        ChatRequest(query="test", call_type="transfer")


def test_existing_fields_unchanged_by_call_type_addition():
    request = ChatRequest(query="test")
    assert request.source_lang == "gu"
    assert request.target_lang == "gu"
    assert request.user_id == "anonymous"
    assert request.provider is None
    assert request.process_id is None
