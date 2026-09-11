"""The farmer backends must distinguish "upstream broke" from "no such farmer".

Collapsing the two is what let a transient 500 / a dead fallback token be cached
as a 2h `not_found`, which is the root of the September-2026 AI-call booking
failures (empty farmer context -> invented identifiers -> partner 500).
"""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import agents.tools.farmer as farmer_tool
from agents.tools.farmer_animal_backends import (
    BackendUnavailableError,
    _parse_farmer_response,
)


def _resp(status, text):
    return httpx.Response(status_code=status, text=text, request=httpx.Request("GET", "http://x"))


def test_error_status_raises_not_empty():
    for status in (401, 403, 500, 502, 503):
        with pytest.raises(BackendUnavailableError):
            _parse_farmer_response(_resp(status, '{"error":"nope"}'), provider="p")


def test_unparseable_200_raises():
    with pytest.raises(BackendUnavailableError):
        _parse_farmer_response(_resp(200, "<html>gateway</html>"), provider="p")


def test_clean_empty_is_none_not_an_error():
    assert _parse_farmer_response(_resp(204, ""), provider="p") is None
    assert _parse_farmer_response(_resp(200, ""), provider="p") is None
    assert _parse_farmer_response(_resp(200, "[]"), provider="p") is None
    assert _parse_farmer_response(_resp(200, '{"data": []}'), provider="p") is None


def test_records_are_returned():
    assert _parse_farmer_response(_resp(200, '[{"farmerCode":"0112"}]'), provider="p") == [
        {"farmerCode": "0112"}
    ]
    assert _parse_farmer_response(
        _resp(200, json.dumps({"data": [{"farmerCode": "0112"}]})), provider="p"
    ) == [{"farmerCode": "0112"}]


def test_fetch_farmer_info_raw_raises_when_every_backend_fails():
    """The exact September shape: primary 500s, herdman 401s, caller is cold."""
    with patch.dict("os.environ", {"PASHUGPT_TOKEN": "t1", "PASHUGPT_TOKEN_3": "t3"}), \
         patch.object(
             farmer_tool, "fetch_farmer_amulpashudhan",
             new=AsyncMock(side_effect=BackendUnavailableError("amulpashudhan", "HTTP 500")),
         ), \
         patch.object(
             farmer_tool, "fetch_farmer_herdman",
             new=AsyncMock(side_effect=BackendUnavailableError("herdman", "HTTP 401")),
         ):
        with pytest.raises(BackendUnavailableError):
            asyncio.run(farmer_tool.fetch_farmer_info_raw("9999999999"))


def test_fetch_farmer_info_raw_returns_none_on_genuine_absence():
    with patch.dict("os.environ", {"PASHUGPT_TOKEN": "t1", "PASHUGPT_TOKEN_3": "t3"}), \
         patch.object(farmer_tool, "fetch_farmer_amulpashudhan", new=AsyncMock(return_value=None)), \
         patch.object(farmer_tool, "fetch_farmer_herdman", new=AsyncMock(return_value=None)):
        assert asyncio.run(farmer_tool.fetch_farmer_info_raw("9999999999")) is None


def test_dead_fallback_does_not_mask_a_good_primary():
    """herdman 401 while amulpashudhan answers: records win, no error raised."""
    with patch.dict("os.environ", {"PASHUGPT_TOKEN": "t1", "PASHUGPT_TOKEN_3": "t3"}), \
         patch.object(
             farmer_tool, "fetch_farmer_amulpashudhan",
             new=AsyncMock(return_value=[{"farmerCode": "0112", "societyName": "S"}]),
         ), \
         patch.object(
             farmer_tool, "fetch_farmer_herdman",
             new=AsyncMock(side_effect=BackendUnavailableError("herdman", "HTTP 401")),
         ):
        records = asyncio.run(farmer_tool.fetch_farmer_info_raw("9999999999"))
    assert records and records[0].farmerCode == "0112"


def test_get_farmer_by_mobile_unpacks_the_dual_backend_result():
    """Regression: _fetch_farmer_records_dual_backend returns (records, failed).
    Both of its callers must unpack it — treating the tuple as a record list
    makes `if not records` always false and then blows up in has_content()."""
    with patch.dict("os.environ", {"PASHUGPT_TOKEN": "t1", "PASHUGPT_TOKEN_3": "t3"}), \
         patch.object(
             farmer_tool, "_fetch_farmer_records_dual_backend",
             new=AsyncMock(return_value=([{"farmerName": "Ramesh", "societyName": "S"}], False)),
         ):
        out = asyncio.run(farmer_tool.get_farmer_by_mobile("9999999999"))
    assert "Ramesh" in out


def test_get_farmer_by_mobile_distinguishes_outage_from_absence():
    with patch.dict("os.environ", {"PASHUGPT_TOKEN": "t1", "PASHUGPT_TOKEN_3": "t3"}), \
         patch.object(
             farmer_tool, "_fetch_farmer_records_dual_backend",
             new=AsyncMock(return_value=([], True)),
         ):
        out = asyncio.run(farmer_tool.get_farmer_by_mobile("9999999999"))
    assert "could not be looked up" in out
    assert "No farmer data found" not in out

    with patch.dict("os.environ", {"PASHUGPT_TOKEN": "t1", "PASHUGPT_TOKEN_3": "t3"}), \
         patch.object(
             farmer_tool, "_fetch_farmer_records_dual_backend",
             new=AsyncMock(return_value=([], False)),
         ):
        out = asyncio.run(farmer_tool.get_farmer_by_mobile("9999999999"))
    assert "No farmer data found" in out
