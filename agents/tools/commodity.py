import json
from pathlib import Path
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

# Load commodity codes from JSON (path relative to project root, works from any cwd e.g. Docker /app)
_assets_dir = Path(__file__).resolve().parent.parent.parent / "assets"
_commodity_path = _assets_dir / "commodity_codes.json"
try:
    _raw = json.load(open(_commodity_path, "r", encoding="utf-8"))
except FileNotFoundError:
    _raw = []


class CommodityEntry(BaseModel):
    code: int = Field(description="AGMKT commodity code")
    name: str = Field(description="Original commodity name from AGMKT master")
    terms: list[str] = Field(description="Search terms: English, Hindi, transliterations")

    def __str__(self):
        terms_str = ", ".join(self.terms)
        return f"| **{self.name}** | {terms_str} | `{self.code}` |"


class CommoditySearchResult(BaseModel):
    entry: CommodityEntry
    score: float = Field(description="Best match score (0-1)")
    matched_term: str = Field(description="The specific term that matched")

    def __str__(self):
        return f"| **{self.entry.name}** | {self.matched_term} ({self.score:.0%}) | `{self.entry.code}` |"


# Pre-load all entries
COMMODITY_ENTRIES: list[CommodityEntry] = [CommodityEntry(**e) for e in _raw]


async def search_commodity(
    query: str,
    max_results: int = 5,
    threshold: float = 0.65,
) -> str:
    """Search commodity codes by fuzzy matching across all terms (English, Hindi, transliterations).

    Args:
        query: The commodity name to search for (any language/script)
        max_results: Maximum number of results to return (default 5)
        threshold: Minimum similarity score 0-1 to consider a match (default 0.6)

    Returns:
        Formatted markdown table of matching commodities with codes and scores
    """
    if not 0 <= threshold <= 1:
        raise ValueError("threshold must be between 0 and 1")

    query_lower = query.lower().strip()
    seen_codes: set[int] = set()
    matches: list[CommoditySearchResult] = []

    for entry in COMMODITY_ENTRIES:
        best_score = 0.0
        best_term = ""

        for term in entry.terms:
            term_lower = term.lower()
            # Primary: straight ratio (penalizes length mismatches naturally)
            r = fuzz.ratio(query_lower, term_lower) / 100.0
            # Secondary: partial_ratio only when term is longer than query (substring match)
            # Dampen it to avoid short-token false positives like "us" matching "kapus"
            pr = fuzz.partial_ratio(query_lower, term_lower) / 100.0
            len_ratio = min(len(query_lower), len(term_lower)) / max(len(query_lower), len(term_lower), 1)
            pr_dampened = pr * (0.5 + 0.5 * len_ratio)
            score = max(r, pr_dampened)
            if score > best_score:
                best_score = score
                best_term = term

        if best_score >= threshold and entry.code not in seen_codes:
            seen_codes.add(entry.code)
            matches.append(CommoditySearchResult(
                entry=entry,
                score=best_score,
                matched_term=best_term,
            ))

    matches.sort(key=lambda m: m.score, reverse=True)
    matches = matches[:max_results]

    if matches:
        header = f"### Commodity matches for `{query}`\n\n| Commodity | Matched Term (Score) | Code |\n|---|---|---|"
        rows = "\n".join(str(m) for m in matches)
        return f"{header}\n{rows}"
    else:
        return f"No commodity matches found for `{query}`"