"""Prompt/moderation invariants for personal bonus amount on voice."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helpers.utils import get_prompt


def test_voice_moderation_allows_personal_bonus_amount():
    text = get_prompt("voice_moderation_en")
    assert "personal bonus" in text.lower() or "મારું બોનસ" in text
    assert "what is my bonus amount?" in text.lower() or "મારું બોનસ કેટલું છે?" in text


def test_mixed_prompt_routes_personal_bonus_to_tool_not_markdown_table():
    text = get_prompt(
        "voice_system_translation_pipeline_en",
        context={
            "creation_date_words": "eleventh February two thousand twenty six",
            "service_channels_words": "chat, voice call, and WhatsApp",
            "helpline_number_words": "zero eight zero three five four five three five four five",
        },
    )
    assert "get_farmer_bonus_amount" in text
    assert "personal bonus" in text.lower() or "બોનસ" in text
    assert "### Bonus Amount" not in text
    assert "passbook" in text.lower()
