"""
Live vLLM-backed pretranslation glossary regressions.

Run with:
  PRETRANSLATION_GLOSSARY_INTEGRATION=1 \
  PRETRANSLATION_PROVIDER=vllm \
  INFERENCE_ENDPOINT_URL=http://YOUR_VLLM_HOST/v1 \
  PRETRANSLATION_MODEL=YOUR_MODEL_NAME \
  pytest tests/test_pretranslation_glossary_vllm_integration.py -q -rs

This file intentionally makes real model calls for every active glossary row.
It is skipped unless PRETRANSLATION_GLOSSARY_INTEGRATION is set.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

import pytest


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


if not _env_flag("PRETRANSLATION_GLOSSARY_INTEGRATION"):
    pytest.skip(
        "Set PRETRANSLATION_GLOSSARY_INTEGRATION=1 to run live vLLM glossary regressions",
        allow_module_level=True,
    )

if os.getenv("PRETRANSLATION_PROVIDER", "").strip().lower() != "vllm":
    pytest.fail(
        "PRETRANSLATION_PROVIDER must be set to 'vllm' before importing the pretranslation service",
        pytrace=False,
    )

if not os.getenv("INFERENCE_ENDPOINT_URL", "").strip():
    pytest.fail(
        "INFERENCE_ENDPOINT_URL is required for vLLM pretranslation integration tests",
        pytrace=False,
    )

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agents.tools.terms import (
    ALLOWED_ALIASES_BY_EN,
    INPUT_ALIASES_BY_EN,
    TERM_PAIRS,
)
from app.services.translation import (
    _get_glossary_hints_for_gu_query,
    translate_to_english_with_gpt5_mini,
)


VALID_CONFIDENCE = {"high", "low", "unknown"}
MATCH_STOPWORDS = {
    "a",
    "an",
    "and",
    "about",
    "for",
    "from",
    "in",
    "into",
    "of",
    "on",
    "the",
    "to",
    "with",
}

COMMON_EXPECTED_ALIASES = {
    "abcess": ["abscess"],
    "agalctia": [
        "agalactia",
        "drop in milk production",
        "decrease in milk production",
        "reduced milk production",
    ],
    "agalactia": ["drop in milk production", "decrease in milk production", "reduced milk production"],
    "allopacia": ["alopecia", "hair loss"],
    "alopecia": ["hair loss"],
    "anti inflammatory": ["anti-inflammatory"],
    "anti pyretic": ["antipyretic", "anti-pyretic"],
    "artificial insemination": ["insemination"],
    "catechu herb": ["catechu"],
    "catechu katha herb": ["catechu", "katha", "katha herb"],
    "cattle": ["animal", "animals"],
    "dairy farming": ["animal husbandry"],
    "dystocia": ["calving difficulty"],
    "eczema": ["itching"],
    "epistaxis": ["blood coming from nose", "blood coming from the nose", "bleeding from nose"],
    "female male calf": ["heifer calf", "heifer", "calf"],
    "fetus": ["pregnancy"],
    "first milk streams fore stripping from teat": ["fore-stripping", "fore stripping"],
    "fmd": ["foot and mouth disease"],
    "hypocalcemia": ["calcium deficiency"],
    "johne s disease": ["JD"],
    "livestock": ["animal", "animals"],
    "livestock health": ["animal health"],
    "mehsani buffalo breed": ["Mehsani"],
    "metritis": ["swelling of uterine wall", "swelling of the uterine wall", "uterine wall swelling"],
    "mummified fetus": ["dead fetus"],
    "murrah buffalo breed": ["Murrah"],
    "oestrus": ["estrus", "heat"],
    "optimal ai time": ["optimal time for artificial insemination", "optimal artificial insemination time"],
    "parity calving number lactation round": ["lactation cycle"],
    "placenta expulsion": ["afterbirth expulsion", "expulsion of placenta", "afterbirth"],
    "pregnant": ["pregnancy"],
    "pregnancy diagnosis pregnancy check in livestock": ["pregnancy check", "pregnancy diagnosis"],
    "quarantine": ["keeping the animal away from other animals", "away from other animals"],
    "rathi cattle breed": ["Rathi"],
    "reproduction": ["breeding"],
    "retention of placenta afterbirth retained placenta not expelled": [
        "retained placenta",
        "afterbirth retention",
        "afterbirth not coming out",
        "afterbirth stuck",
    ],
    "ringworm fungus trichophyton verrucosum in cattle": ["Trichophyton verrucosum fungus"],
    "snf": ["solids not fat"],
    "solids not fat": ["SNF"],
    "tdn total digestible nutrients": ["TDN", "Total Digestible Nutrients"],
    "teat orifice teat end": ["teat"],
    "theileriosis treatment drug": ["Buparvaquone"],
    "to breed to inseminate": ["breeding"],
    "to breed to inseminate livestock": ["breeding"],
    "udder oedema": ["udder edema"],
    "udder edema": ["udder swelling"],
    "udder infection": ["udder swelling"],
    "udder plural": ["udder", "udders"],
    "urea molasses block": ["urea molasses mineral block"],
    "ventilated well ventilated cattle shed": ["ventilation"],
    "veterinary guidance": ["veterinary advice"],
}


class GlossaryRegressionCase(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str = Field(min_length=1)
    source_text: str = Field(min_length=1)
    glossary_en: str = Field(min_length=1)
    glossary_gu: str = Field(min_length=1)
    expected_any: tuple[str, ...] = Field(min_length=1)

    @field_validator("expected_any")
    @classmethod
    def _expected_any_must_have_content(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not any(_normalize_english(item) for item in value):
            raise ValueError("expected_any must include at least one non-empty normalized alias")
        return value


class PretranslationRegressionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str = Field(min_length=1)
    translation: str = Field(min_length=1)
    confidence: str = Field(pattern="^(high|low|unknown)$")
    matched_expected: str = Field(min_length=1)


class GlossaryFailureLedgerEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str = Field(min_length=1)
    source_text: str = Field(min_length=1)
    glossary_en: str = Field(min_length=1)
    glossary_gu: str = Field(min_length=1)
    expected_any: tuple[str, ...] = Field(min_length=1)
    translation: str = Field(min_length=1)
    confidence: str = Field(pattern="^(high|low|unknown)$")
    hints: str


def _normalize_english(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    text = text.replace("&", " and ")
    text = text.replace("'", "")
    text = re.sub(r"[\u2010-\u2015-]+", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> list[str]:
    return [
        token
        for token in _normalize_english(text).split()
        if token and token not in MATCH_STOPWORDS
    ]


def _token_forms(token: str) -> set[str]:
    forms = {token}
    if len(token) > 3 and token.endswith("ies"):
        forms.add(token[:-3] + "y")
    if len(token) > 4 and token.endswith("es"):
        forms.add(token[:-2])
    if len(token) > 3 and token.endswith("s"):
        forms.add(token[:-1])
    if len(token) > 5 and token.endswith("ing"):
        forms.add(token[:-3])
        forms.add(token[:-3] + "e")
    if len(token) > 4 and token.endswith("ed"):
        forms.add(token[:-2])
        forms.add(token[:-1])
    return {form for form in forms if len(form) >= 3}


def _token_form_set(text: str) -> set[str]:
    forms: set[str] = set()
    for token in _tokens(text):
        forms.update(_token_forms(token))
    return forms


def _normalized_contains(translation: str, expected: str) -> bool:
    normalized_expected = _normalize_english(expected)
    if not normalized_expected:
        return False

    normalized_translation = f" {_normalize_english(translation)} "
    if f" {normalized_expected} " in normalized_translation:
        return True

    expected_tokens = _tokens(expected)
    if not expected_tokens:
        return False

    translation_forms = _token_form_set(translation)
    expected_forms_by_token = [_token_forms(token) for token in expected_tokens]
    return all(forms and forms.intersection(translation_forms) for forms in expected_forms_by_token)


def _strip_parenthetical_suffix(text: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()


def _split_alias_parts(text: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"\s*/\s*|,|;|\bor\b", text, flags=re.IGNORECASE)
        if part.strip()
    ]


def _add_alias(aliases: list[str], alias: str) -> None:
    normalized = _normalize_english(alias)
    if len(normalized) < 3:
        return
    _add_alias_unchecked(aliases, alias)


def _add_alias_unchecked(aliases: list[str], alias: str) -> None:
    normalized = _normalize_english(alias)
    if normalized and normalized not in {_normalize_english(item) for item in aliases}:
        aliases.append(alias.strip())


def _accepted_aliases_for_english_term(english_term: str) -> tuple[str, ...]:
    aliases: list[str] = []
    base = english_term.strip()
    stripped = _strip_parenthetical_suffix(base)

    for candidate in [base, stripped]:
        _add_alias(aliases, candidate)
        for part in _split_alias_parts(candidate):
            _add_alias(aliases, part)

    normalized_base = _normalize_english(stripped)
    for alias in INPUT_ALIASES_BY_EN.get(normalized_base, []):
        _add_alias(aliases, alias)
    for alias in ALLOWED_ALIASES_BY_EN.get(normalized_base, []):
        _add_alias(aliases, alias)
    for alias in COMMON_EXPECTED_ALIASES.get(normalized_base, []):
        _add_alias_unchecked(aliases, alias)

    return tuple(aliases)


def _contains_expected_alias(translation: str, expected_any: tuple[str, ...]) -> str | None:
    for expected in expected_any:
        if _normalized_contains(translation, expected):
            return expected
    return None


def _failure_ledger(
    *,
    case: GlossaryRegressionCase,
    hints: str,
    translation: str,
    confidence: str,
) -> str:
    entry = GlossaryFailureLedgerEntry(
        case_id=case.case_id,
        source_text=case.source_text,
        glossary_en=case.glossary_en,
        glossary_gu=case.glossary_gu,
        expected_any=case.expected_any,
        translation=translation,
        confidence=confidence,
        hints=hints,
    )
    return json.dumps(
        entry.model_dump(),
        ensure_ascii=False,
        sort_keys=True,
    )


def _glossary_case_id(index: int, english_term: str, gujarati_term: str) -> str:
    readable_en = re.sub(r"[^A-Za-z0-9]+", "-", english_term).strip("-").lower()[:50]
    readable_gu = re.sub(r"\s+", "-", gujarati_term).strip("-")[:24]
    return f"{index:03d}-{readable_en}-{readable_gu}"


def _build_glossary_cases() -> list[GlossaryRegressionCase]:
    cases: list[GlossaryRegressionCase] = []
    for index, term_pair in enumerate(TERM_PAIRS, start=1):
        english_term = term_pair.en.strip()
        gujarati_term = term_pair.gu.strip()
        cases.append(
            GlossaryRegressionCase(
                case_id=_glossary_case_id(index, english_term, gujarati_term),
                source_text=f"મારે {gujarati_term} વિશે પૂછવું છે",
                glossary_en=english_term,
                glossary_gu=gujarati_term,
                expected_any=_accepted_aliases_for_english_term(english_term),
            )
        )
    return cases


GLOSSARY_CASES = _build_glossary_cases()


def test_pretranslation_glossary_cases_cover_all_active_terms():
    assert len(GLOSSARY_CASES) == len(TERM_PAIRS)
    assert GLOSSARY_CASES


@pytest.mark.parametrize("case", GLOSSARY_CASES, ids=lambda case: case.case_id)
def test_vllm_pretranslation_uses_glossary_term(case: GlossaryRegressionCase):
    hints = _get_glossary_hints_for_gu_query(case.source_text, max_results=20)
    assert _normalize_english(case.glossary_en) in _normalize_english(hints), (
        f"Glossary hints did not include expected English term.\n"
        f"case_id={case.case_id}\n"
        f"source_text={case.source_text!r}\n"
        f"expected={case.glossary_en!r}\n"
        f"hints={hints!r}"
    )

    translation, confidence = asyncio.run(translate_to_english_with_gpt5_mini(case.source_text, "gu"))
    assert translation.strip(), f"Empty pretranslation for {case.case_id}"
    assert confidence in VALID_CONFIDENCE

    matched_expected = _contains_expected_alias(translation, case.expected_any)
    failure_ledger = _failure_ledger(
        case=case,
        hints=hints,
        translation=translation,
        confidence=confidence,
    )
    assert matched_expected is not None, (
        f"Pretranslation did not contain the expected glossary term or alias.\n"
        f"case_id={case.case_id}\n"
        f"source_text={case.source_text!r}\n"
        f"expected_any={case.expected_any!r}\n"
        f"translation={translation!r}\n"
        f"confidence={confidence!r}\n"
        f"failure_ledger_json={failure_ledger}"
    )

    PretranslationRegressionResult(
        case_id=case.case_id,
        translation=translation,
        confidence=confidence,
        matched_expected=matched_expected,
    )
