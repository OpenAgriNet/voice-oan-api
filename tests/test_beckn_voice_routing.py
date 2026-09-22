"""Voice Beckn routing and adapter contracts that must not regress.

Intentionally small:
1. read paths may fall back to direct APIs when Beckn is unavailable
2. booking paths must NOT fall back (duplicate SMS risk)
3. milk adapter must map Beckn tags into the legacy response model shape
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional
from unittest.mock import AsyncMock

import httpx

from agents.models.ai_call import AICallRequestModel, AICallResponseModel, AISpecies
from agents.tools import farmer_animal_backends as backends
from agents.tools.beckn.operations import BecknActionResult, BecknOperation, OperationState
from agents.tools.beckn.voice import (
    BecknProviderUnavailable,
    get_farmer_milk_collection_details,
)
from app.models.milk_collection import (
    FarmerMilkCollectionRequestModel,
    FarmerMilkCollectionResponseModel,
)


class _FakeAsyncClient:
    """Captures whether the legacy direct HTTP path was used."""

    calls: list[dict[str, Any]] = []
    response = httpx.Response(200, json={}, request=httpx.Request("GET", "https://example.test"))

    def __init__(self, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None, headers=None):
        self.calls.append({"method": "GET", "url": url, "params": params})
        return self.response

    async def post(self, url, params=None, headers=None, json=None):
        self.calls.append({"method": "POST", "url": url, "params": params})
        return self.response


def _milk_request() -> FarmerMilkCollectionRequestModel:
    return FarmerMilkCollectionRequestModel(
        unionCode="0201",
        societyCode="001066",
        farmerCode="000123",
        fromdate="2026-04-01",
        todate="2026-04-01",
    )


def _ai_request() -> AICallRequestModel:
    return AICallRequestModel(
        unionCode="0201",
        societyCode="001066",
        farmerCode="000123",
        userId="AAAAAAAAAAAAAAAAAAAAAA==",
        species=AISpecies.COW,
    )


def _operation(*, state: OperationState, callback: Optional[dict] = None) -> BecknOperation:
    return BecknOperation(
        operation_id="op",
        transaction_id="txn",
        message_id="msg",
        action="init",
        expected_callback="on_init",
        domain="services:amul-milk-collection",
        bap_id="bap",
        bpp_id="bpp",
        session_id="session",
        tool_call_id="tool",
        request_hash="hash",
        idempotency_key="idem",
        state=state,
        callback=callback,
    )


def test_milk_beckn_success_skips_direct_http(monkeypatch):
    monkeypatch.setattr(backends.settings, "voice_beckn_enabled", True)
    _FakeAsyncClient.calls = []
    monkeypatch.setattr(backends.httpx, "AsyncClient", _FakeAsyncClient)

    beckn_result = FarmerMilkCollectionResponseModel.model_validate(
        {"result": "ok", "milk": [{"date": "2026-04-01", "qty": 5}], "deduction": []}
    )
    monkeypatch.setattr(
        backends,
        "get_farmer_milk_collection_details_beckn",
        AsyncMock(return_value=beckn_result),
    )

    result = asyncio.run(backends.get_farmer_milk_collection_details_api(_milk_request(), "token"))

    assert result is beckn_result
    assert _FakeAsyncClient.calls == []


def test_milk_falls_back_to_direct_when_beckn_unavailable(monkeypatch):
    monkeypatch.setattr(backends.settings, "voice_beckn_enabled", True)
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response = httpx.Response(
        200,
        json={"result": "success", "milk": [{"date": "2026-04-01", "qty": 3}], "deduction": []},
        request=httpx.Request("GET", "https://example.test"),
    )
    monkeypatch.setattr(backends.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        backends,
        "get_farmer_milk_collection_details_beckn",
        AsyncMock(side_effect=BecknProviderUnavailable("callback is still pending")),
    )

    result = asyncio.run(backends.get_farmer_milk_collection_details_api(_milk_request(), "token"))

    assert result is not None
    assert result.milk[0].qty == 3
    assert len(_FakeAsyncClient.calls) == 1
    assert _FakeAsyncClient.calls[0]["url"].endswith("/FarmerMilkCollectionDetails")


def test_ai_booking_does_not_fall_back_when_beckn_is_uncertain(monkeypatch):
    """Irreversible booking must never submit a second path after a Beckn miss."""
    monkeypatch.setattr(backends.settings, "voice_beckn_enabled", True)
    _FakeAsyncClient.calls = []
    monkeypatch.setattr(backends.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        backends,
        "create_ai_call_booking",
        AsyncMock(side_effect=BecknProviderUnavailable("callback is still pending")),
    )

    result = asyncio.run(backends.create_ai_call_api(_ai_request(), "token"))

    assert result is None
    assert _FakeAsyncClient.calls == []


def test_ai_booking_uses_beckn_result_when_enabled(monkeypatch):
    monkeypatch.setattr(backends.settings, "voice_beckn_enabled", True)
    _FakeAsyncClient.calls = []
    monkeypatch.setattr(backends.httpx, "AsyncClient", _FakeAsyncClient)
    booked = AICallResponseModel(aitName="AIT One", ticketNumber="TICKET-9")
    monkeypatch.setattr(backends, "create_ai_call_booking", AsyncMock(return_value=booked))

    result = asyncio.run(backends.create_ai_call_api(_ai_request(), "token"))

    assert result is booked
    assert result.ticket_number == "TICKET-9"
    assert _FakeAsyncClient.calls == []


def test_milk_adapter_maps_beckn_tags_to_legacy_response_shape(monkeypatch):
    """Protect the accountname alias and milk/deduction tag-group mapping."""
    from agents.tools.beckn import voice as voice_adapter

    payload = {
        "message": {
            "order": {
                "items": [
                    {
                        "tags": [
                            {
                                "descriptor": {"code": "query-period"},
                                "list": [{"descriptor": {"code": "result"}, "value": "success"}],
                            },
                            {
                                "descriptor": {"code": "milk-record"},
                                "list": [
                                    {"descriptor": {"code": "date"}, "value": "2026-04-01"},
                                    {"descriptor": {"code": "qty"}, "value": "7.5"},
                                    {"descriptor": {"code": "fat"}, "value": "4.1"},
                                ],
                            },
                            {
                                "descriptor": {"code": "deduction-record"},
                                "list": [
                                    {"descriptor": {"code": "date"}, "value": "2026-04-01"},
                                    {"descriptor": {"code": "account_name"}, "value": "Feed"},
                                    {"descriptor": {"code": "amount"}, "value": "25"},
                                ],
                            },
                        ]
                    }
                ]
            }
        }
    }

    class Client:
        async def init_milk_collection(self, **kwargs):
            return BecknActionResult(_operation(state=OperationState.SUCCEEDED, callback=payload), payload)

    monkeypatch.setattr(voice_adapter, "get_beckn_operation_client", lambda: Client())

    result = asyncio.run(get_farmer_milk_collection_details(_milk_request()))

    assert result.result == "success"
    assert result.milk[0].qty == 7.5
    assert result.deduction[0].account_name == "Feed"
    assert result.deduction[0].amount == 25
