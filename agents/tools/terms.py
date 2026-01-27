import os
import json
from enum import Enum
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

# Load term pairs from JSON file with UTF-8 encoding
term_pairs = json.load(open('assets/glossary_terms.json', 'r', encoding='utf-8'))

class Language(str, Enum):
    ENGLISH = "en"
    GUJARATI = "gu"
    TRANSLITERATION = "transliteration"

class TermPair(BaseModel):
    en: str = Field(description="English term")
    gu: str = Field(default="", description="Gujarati term")
    mr: str = Field(default="", description="Marathi term (legacy, will be mapped to gu)")
    transliteration: str = Field(description="Transliteration of Gujarati term to English")

    def __str__(self):
        gu_term = self.gu if self.gu else self.mr
        return f"{self.en} -> {gu_term} ({self.transliteration})"
    
    def get_gujarati_term(self) -> str:
        """Get Gujarati term, falling back to Marathi if Gujarati not available."""
        return self.gu if self.gu else self.mr

# Convert raw dictionaries to TermPair objects, handling both mr and gu fields
TERM_PAIRS = []
for pair in term_pairs:
    # If pair has 'mr' but not 'gu', use 'mr' as 'gu' for backward compatibility
    if 'mr' in pair and 'gu' not in pair:
        pair['gu'] = pair['mr']
    TERM_PAIRS.append(TermPair(**pair))

async def search_terms(
    text: str, 
    max_results: int = 5,
    similarity_threshold: float = 0.7,
    language: Language = None
) -> str:
    """
    Search for terms using fuzzy partial string matching across all fields.
    
    Args:
        text: The text to search for
        max_results: Maximum number of results to return
        similarity_threshold: Minimum similarity score (0-1) to consider a match
        language: Optional language to restrict search to (en/gu/transliteration)
        
    Returns:
        Formatted string with matching results and their scores
    """
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
            
        # Check Gujarati term if no language specified or language is Gujarati    
        if language in [None, Language.GUJARATI]:
            gu_term = term_pair.get_gujarati_term()
            if gu_term:
                gu_score = fuzz.ratio(text, gu_term.lower()) / 100.0
                max_score = max(max_score, gu_score)
            
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
        return f"Matching Terms for `{text}`\n\n" + "\n".join([f"{match[0]} [{match[1]:.0%}]" for match in matches])
    else:
        return f"No matching terms found for `{text}`"