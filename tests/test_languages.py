from pathlib import Path
import re
import pytest

from app.core.languages import (
    LANGUAGES,
    get_language,
    iso_language_code,
    normalize_language,
    parse_iso_language_header,
    sarvam_language_code,
    SUPPORTED_ISO_LANGUAGE_CODES,
)


EXPECTED_CODES = {
    "en", "hi", "od", "pa", "ta", "te", "kn", "ml", "gu", "mr", "bn"
}
EXPECTED_ISO_CODES = {
    "en", "hi", "or", "pa", "ta", "te", "kn", "ml", "gu", "mr", "bn"
}


def test_canonical_language_set() -> None:
    assert set(LANGUAGES) == EXPECTED_CODES
    assert len(LANGUAGES) == 11
    assert SUPPORTED_ISO_LANGUAGE_CODES == EXPECTED_ISO_CODES


def test_every_language_has_complete_voice_metadata() -> None:
    for code, language in LANGUAGES.items():
        assert language.code == code
        assert language.sarvam_code == f"{code}-IN"
        assert language.name
        assert language.recording_disclaimer
        assert language.closing_message


def test_odia_legacy_alias_is_normalized_at_boundary() -> None:
    assert normalize_language("or") == "od"
    assert get_language("or") is LANGUAGES["od"]
    assert sarvam_language_code("or") == "od-IN"
    assert parse_iso_language_header("or") == "od"
    assert iso_language_code("od") == "or"


@pytest.mark.parametrize("code", sorted(EXPECTED_ISO_CODES))
def test_standard_iso_header_codes_are_accepted(code: str) -> None:
    internal = parse_iso_language_header(code.upper())
    assert iso_language_code(internal) == code


@pytest.mark.parametrize("code", [None, "", "od", "as", "en-IN", "English"])
def test_nonstandard_or_unsupported_header_values_are_rejected(code: str | None) -> None:
    with pytest.raises(ValueError, match="X-Language"):
        parse_iso_language_header(code)


def test_unknown_language_falls_back_to_hindi() -> None:
    assert normalize_language("as") == "hi"
    assert normalize_language(None) == "hi"


def test_every_supported_language_has_a_dedicated_prompt() -> None:
    prompts = Path(__file__).parents[1] / "assets" / "prompts"
    for code in EXPECTED_CODES:
        prompt = prompts / f"voice_{code}.md"
        assert prompt.is_file(), f"Missing prompt for {code}: {prompt}"
        text = prompt.read_text(encoding="utf-8")
        assert '"language"' in text
        assert "lock_language" in text
        assert "audio" in text
        assert "end_interaction" in text
        assert f"`{code}`" in text


def test_direct_translations_preserve_english_code_literals() -> None:
    prompts = Path(__file__).parents[1] / "assets" / "prompts"
    literal_pattern = re.compile(r"`[^`\n]+`")
    english = set(literal_pattern.findall((prompts / "voice_en.md").read_text()))
    english.discard("`en`")

    for code in EXPECTED_CODES - {"en", "hi"}:
        translated = set(
            literal_pattern.findall(
                (prompts / f"voice_{code}.md").read_text(encoding="utf-8")
            )
        )
        translated.discard(f"`{code}`")
        assert english <= translated, f"Code literals drifted in {code} prompt"
