import json
from enum import Enum

from pydantic import BaseModel, Field
from pydantic_ai import ModelRetry, RunContext
from rapidfuzz import fuzz

from agents.deps import FarmerContext

# Load term pairs from JSON file with UTF-8 encoding
term_pairs = json.load(open('assets/glossary_terms.json', 'r', encoding='utf-8'))

MAX_SEARCH_TERMS_CALLS = 5

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


def _calls_this_turn(ctx: RunContext[FarmerContext], tool_name: str = "search_terms") -> int:
    """Count tool calls for tool_name since the latest farmer message in this run."""
    messages = ctx.messages
    if not messages:
        return 0

    turn_start = 0
    for i in range(len(messages) - 1, -1, -1):
        for part in messages[i].parts:
            if getattr(part, "part_kind", None) == "user-prompt":
                turn_start = i
                break
        else:
            continue
        break

    count = 0
    for message in messages[turn_start:]:
        for part in message.parts:
            if (
                getattr(part, "part_kind", None) == "tool-call"
                and getattr(part, "tool_name", None) == tool_name
            ):
                count += 1
    return count


async def search_terms(
    ctx: RunContext[FarmerContext],
    text: str,
    max_results: int = 5,
    similarity_threshold: float = 0.7,
    language: Language = None,
) -> str:
    """
    Search for terms using fuzzy partial string matching across all fields.

    Args:
        ctx: The context containing conversation history for per-turn call limits
        text: The text to search for
        max_results: Maximum number of results to return
        similarity_threshold: Minimum similarity score (0-1) to consider a match
        language: Optional language to restrict search to (en/mr/transliteration)

    Returns:
        Formatted string with matching results and their scores
    """
    if _calls_this_turn(ctx) > MAX_SEARCH_TERMS_CALLS:
        raise ModelRetry(
            "You have reached the maximum number of search_terms calls for this turn. "
            "Do not call search_terms again. Proceed with search_documents using the best "
            "terms you already have, or answer the farmer from results already retrieved."
        )

    if not 0 <= similarity_threshold <= 1:
        raise ValueError("similarity_threshold must be between 0 and 1")

    matches = []
    text = text.lower()

    for term_pair in TERM_PAIRS:
        max_score = 0

        # Check English term if no language specified or language is English
        if language in [None, Language.ENGLISH]:
            en_score = fuzz.ratio(text, term_pair.en.lower()) / 100.0
            max_score = max(max_score, en_score)

        # Check Marathi term if no language specified or language is Marathi
        if language in [None, Language.MARATHI]:
            mr_score = fuzz.ratio(text, term_pair.mr.lower()) / 100.0
            max_score = max(max_score, mr_score)

        # Check transliteration if no language specified or language is transliteration
        if language in [None, Language.TRANSLITERATION]:
            tr_score = fuzz.ratio(text, term_pair.transliteration.lower()) / 100.0
            max_score = max(max_score, tr_score)

        if max_score >= similarity_threshold:
            matches.append((term_pair, max_score))

    # Sort by score descending
    matches.sort(key=lambda x: x[1], reverse=True)

    if len(matches) > 0:
        matches = matches[:max_results]
        return f"Matching Terms for `{text}`\n\n" + "\n".join(
            [f"{match[0]} [{match[1]:.0%}]" for match in matches]
        )
    return f"No matching terms found for `{text}`"
