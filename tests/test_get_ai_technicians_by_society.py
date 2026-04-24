import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.tools.farmer_animal_backends import AITechnicianBySocietyRecord
from agents.tools.get_ai_technicians_by_society import get_ai_technicians_by_society


class TestGetAITechniciansBySociety:
    def test_returns_service_not_configured_when_token_missing(self, monkeypatch):
        monkeypatch.delenv("PASHUGPT_TOKEN", raising=False)

        result = asyncio.run(get_ai_technicians_by_society("2021", "1066"))
        assert result == "AI technician lookup failed. Service is not configured."

    def test_returns_formatted_json_when_records_exist(self, monkeypatch):
        monkeypatch.setenv("PASHUGPT_TOKEN", "test-token")

        async def _fake_api(query, token):
            assert query.union_code == "2021"
            assert query.society_code == "1066"
            assert token == "test-token"
            return [
                AITechnicianBySocietyRecord.model_validate(
                    {
                        "aitName": "Ramesh Patel",
                        "aitMobileNo": "9876543210",
                        "unionCode": "2021",
                        "societyCode": "1066",
                    }
                )
            ]

        monkeypatch.setattr(
            "agents.tools.get_ai_technicians_by_society.get_ai_technicians_by_society_api",
            _fake_api,
        )

        result = asyncio.run(get_ai_technicians_by_society("2021", "1066"))
        assert "AI technician details for union 2021 and society 1066" in result
        assert "Ramesh Patel" in result
        assert "9876543210" in result

    def test_returns_not_found_message_when_api_is_empty(self, monkeypatch):
        monkeypatch.setenv("PASHUGPT_TOKEN", "test-token")

        async def _fake_api(query, token):
            return []

        monkeypatch.setattr(
            "agents.tools.get_ai_technicians_by_society.get_ai_technicians_by_society_api",
            _fake_api,
        )

        result = asyncio.run(get_ai_technicians_by_society("2021", "1066"))
        assert "No AI technician data found for this society." in result
