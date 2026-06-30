"""Supported conversation languages for the voice assistant.

Single source of truth for which `X-Language` codes the API accepts and how the
agent resolves them to a prompt / response language.

A language is *accepted* by the API as soon as its code is listed here. It becomes
*fully live* (the bot actually speaks it) once a `voice_<code>.md` prompt file
exists in the prompt directory. Until then, requests for that language are served
gracefully in Hindi (see ``resolve_render_language``).
"""
import os

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

# Codes accepted on the X-Language header (does not include the "no preference" sentinel).
SUPPORTED_LANGUAGE_CODES: frozenset[str] = frozenset(SUPPORTED_LANGUAGES)

# Sentinel meaning "client did not specify a language" -> triggers the language gate.
NO_PREFERENCE = "none"

# Fallback language used when a supported language has no prompt file yet.
DEFAULT_LANGUAGE = "hi"

PROMPT_DIR = "assets/prompts"


def is_supported(lang: str | None) -> bool:
    """True if ``lang`` is one of the supported conversation language codes."""
    return lang in SUPPORTED_LANGUAGE_CODES


def prompt_exists(lang: str | None) -> bool:
    """True if a ``voice_<lang>.md`` prompt file exists for this language."""
    if not lang:
        return False
    return os.path.isfile(os.path.join(PROMPT_DIR, f"voice_{lang}.md"))


def resolve_render_language(lang: str | None) -> str:
    """The language the bot can actually respond in.

    Returns ``lang`` when it is supported *and* has a prompt file; otherwise falls
    back to Hindi. This keeps the response, the recording disclaimer, and the
    reported ``language`` field consistent when a language is accepted but not yet
    translated.
    """
    if is_supported(lang) and prompt_exists(lang):
        return lang  # type: ignore[return-value]
    return DEFAULT_LANGUAGE
