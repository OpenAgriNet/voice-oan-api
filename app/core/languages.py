"""Supported conversation languages for the voice assistant.

Single source of truth for which languages the bot can speak.

The conversation language is *not* supplied by the client. The agent detects it
from the farmer's own words and reports it back on ``VoiceOutput.language``; that
value then selects the recording disclaimer and is handed to the caller for
text-to-speech voice selection. See ``assets/prompts/voice.md``.
"""

# ISO 639-1 code -> display name. Order is informational only.
SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English",
    "hi": "Hindi",
    "bn": "Bengali",
    "te": "Telugu",
    "mr": "Marathi",
    "ta": "Tamil",
    "gu": "Gujarati",
    "kn": "Kannada",
    "ml": "Malayalam",
    "as": "Assamese",
}

SUPPORTED_LANGUAGE_CODES: frozenset[str] = frozenset(SUPPORTED_LANGUAGES)

# Used when the model reports a language we do not support, or reports nothing at all.
DEFAULT_LANGUAGE = "hi"


def is_supported(lang: str | None) -> bool:
    """True if ``lang`` is one of the supported conversation language codes."""
    return lang in SUPPORTED_LANGUAGE_CODES


def normalize_language(lang: str | None) -> str:
    """Coerce a model-reported language code to one we can actually speak.

    The model is instructed to emit one of ``SUPPORTED_LANGUAGE_CODES``, but it is
    free text on the wire, so anything unexpected falls back to Hindi.
    """
    if not lang:
        return DEFAULT_LANGUAGE
    code = lang.strip().lower()[:2]
    return code if is_supported(code) else DEFAULT_LANGUAGE
