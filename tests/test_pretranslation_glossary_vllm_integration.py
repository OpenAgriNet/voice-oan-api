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

COMMON_EXPECTED_ALIASES = {
    "abcess": ["abscess"],
    "agalctia": ["agalactia"],
    "allopacia": ["alopecia"],
    "anti inflammatory": ["anti-inflammatory"],
    "anti pyretic": ["antipyretic", "anti-pyretic"],
    "artificial insemination": ["insemination"],
    "oestrus": ["estrus", "heat"],
    "placenta expulsion": ["afterbirth expulsion", "expulsion of placenta", "afterbirth"],
    "udder oedema": ["udder edema"],
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


def _normalize_english(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    text = text.replace("&", " and ")
    text = text.replace("'", "")
    text = re.sub(r"[\u2010-\u2015-]+", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


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
    if normalized not in {_normalize_english(item) for item in aliases}:
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
        _add_alias(aliases, alias)

    return tuple(aliases)


def _contains_expected_alias(translation: str, expected_any: tuple[str, ...]) -> str | None:
    normalized_translation = f" {_normalize_english(translation)} "
    for expected in expected_any:
        normalized_expected = _normalize_english(expected)
        if normalized_expected and f" {normalized_expected} " in normalized_translation:
            return expected
    return None


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
    assert matched_expected is not None, (
        f"Pretranslation did not contain the expected glossary term or alias.\n"
        f"case_id={case.case_id}\n"
        f"source_text={case.source_text!r}\n"
        f"expected_any={case.expected_any!r}\n"
        f"translation={translation!r}\n"
        f"confidence={confidence!r}"
    )

    PretranslationRegressionResult(
        case_id=case.case_id,
        translation=translation,
        confidence=confidence,
        matched_expected=matched_expected,
    )
