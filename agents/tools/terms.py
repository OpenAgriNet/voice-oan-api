import json
import asyncio
import re
from pathlib import Path
from enum import Enum

from pydantic import BaseModel, Field
from pydantic_ai import RunContext
from rapidfuzz import fuzz

from agents.deps import FarmerContext
from helpers.utils import get_logger

logger = get_logger(__name__)

# Load term pairs from JSON file with UTF-8 encoding. Use an absolute path
# derived from __file__ so this works regardless of the process CWD (which
# differs between local uvicorn and the supervisord-launched container).
_GLOSSARY_PATH = Path(__file__).resolve().parent.parent.parent / "assets" / "glossary_terms.json"
try:
    with open(_GLOSSARY_PATH, "r", encoding="utf-8") as _f:
        term_pairs = json.load(_f)
except FileNotFoundError:
    logger.error(f"glossary_terms.json not found at {_GLOSSARY_PATH}; search_terms disabled")
    term_pairs = []

class Language(str, Enum):
    ENGLISH = "en"
    MARATHI = "mr"
    TRANSLITERATION = "transliteration"

class TermPair(BaseModel):
    en: str = Field(description="English term")
    mr: str = Field(description="Marathi term")
    transliteration: str = Field(description="Transliteration of Marathi term to English")

    def __str__(self):
        return f"{self.en} -> {self.mr} ({self.transliteration})"

# Convert raw dictionaries to TermPair objects
TERM_PAIRS = [TermPair(**pair) for pair in term_pairs]

# Vowel-length is not meaningful in Marathi->Latin transliteration: कापूस is
# written "kaapoos" in the glossary but ASR just as often gives "kapus". Plain
# edit distance scores that pair at 0.67 — under the 0.7 threshold — so a farmer
# saying "cotton" got no glossary match at all. Fold the spelling variants
# together before comparing so the threshold measures the word, not the vowels.
_VOWEL_FOLDS = (("aa", "a"), ("ee", "i"), ("oo", "u"), ("ii", "i"), ("uu", "u"))


def _fold_translit(text: str) -> str:
    """Normalize Latin transliteration spelling variants (kaapoos -> kapus)."""
    folded = text.lower()
    for long_form, short_form in _VOWEL_FOLDS:
        folded = folded.replace(long_form, short_form)
    # Any remaining doubled letter is a spelling choice, not a distinction.
    return re.sub(r"(.)\1+", r"\1", folded)


# Precompute the folded forms once — 6k terms x 3 fields on every lookup would
# otherwise be pure waste, since the glossary never changes at runtime.
_FOLDED_PAIRS = [
    (pair, _fold_translit(pair.en), pair.mr.lower(), _fold_translit(pair.transliteration))
    for pair in TERM_PAIRS
]


async def search_terms(
    ctx: RunContext[FarmerContext],
    text: str,
    max_results: int = 5,
    similarity_threshold: float = 0.7,
    language: Language = None,
) -> str:
    """
    Look up agricultural terms in the glossary using fuzzy matching.

    Pass **every** crop, pest, disease, and farm-input name in the farmer's
    question in a single call, separated by commas — not just one of them.
    One call handles all of them, so do not make a separate call per term and
    do not retry the same term with different spellings or transliterations.

    Args:
        ctx: The context containing session information
        text: Term to look up, or several terms separated by commas
            (e.g. "wild boar, sugarcane, leaf blight")
        max_results: Maximum number of results to return per term
        similarity_threshold: Minimum similarity score (0-1) to consider a match
        language: Optional language to restrict search to (en/mr/transliteration)

    Returns:
        Formatted string with matching results and their scores, one section per term
    """
    if not 0 <= similarity_threshold <= 1:
        raise ValueError("similarity_threshold must be between 0 and 1")

    lang = language
    pairs = _FOLDED_PAIRS

    # One call now carries every term in the farmer's question, comma separated.
    # Matching is local rapidfuzz over the glossary, so the cost of covering all
    # terms is negligible — what used to be expensive was the LLM round-trip per
    # term, which is exactly what batching removes.
    terms: list[str] = []
    for raw in text.split(","):
        term = raw.strip().lower()
        if term and term not in terms:
            terms.append(term)
    if not terms:
        return f"No matching terms found for `{text}`"

    def _fuzzy_match(term: str) -> list[tuple[TermPair, float]]:
        # Fold the farmer's term the same way the glossary was folded, so
        # "kapus" and "kaapoos" meet in the middle.
        folded_term = _fold_translit(term)
        matches = []
        for term_pair, en, mr, translit in pairs:
            max_score = 0
            if lang in [None, Language.ENGLISH]:
                max_score = max(max_score, fuzz.ratio(folded_term, en) / 100.0)
            if lang in [None, Language.MARATHI]:
                max_score = max(max_score, fuzz.ratio(term, mr) / 100.0)
            if lang in [None, Language.TRANSLITERATION]:
                max_score = max(max_score, fuzz.ratio(folded_term, translit) / 100.0)
            if max_score >= similarity_threshold:
                matches.append((term_pair, max_score))
        matches.sort(key=lambda x: x[1], reverse=True)
        return matches[:max_results]

    def _match_all() -> list[tuple[str, list[tuple[TermPair, float]]]]:
        return [(term, _fuzzy_match(term)) for term in terms]

    results = await asyncio.to_thread(_match_all)

    # Per-term sections, each keeping the original single-term wording so the
    # prompts' "No matching terms found" handling still applies per term.
    sections = []
    for term, matches in results:
        if matches:
            sections.append(
                f"Matching Terms for `{term}`\n\n"
                + "\n".join([f"{match[0]} [{match[1]:.0%}]" for match in matches])
            )
        else:
            sections.append(f"No matching terms found for `{term}`")
    return "\n\n".join(sections)
