from app.core.languages import (
    LANGUAGES,
    get_language,
    normalize_language,
    sarvam_language_code,
)


EXPECTED_CODES = {
    "en", "hi", "od", "pa", "ta", "te", "kn", "ml", "gu", "mr", "bn"
}


def test_canonical_language_set() -> None:
    assert set(LANGUAGES) == EXPECTED_CODES
    assert len(LANGUAGES) == 11


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


def test_unknown_language_falls_back_to_hindi() -> None:
    assert normalize_language("as") == "hi"
    assert normalize_language(None) == "hi"
