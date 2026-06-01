"""
AI Technician (artificial insemination / beech daan) booking query regressions.

Unit tests (default, no model calls):
  pytest tests/test_pretranslation_ai_technician_booking_regression.py -q

Live Gujarati pretranslation integration (real model calls):
  PRETRANSLATION_AI_BOOKING_INTEGRATION=1 \\
  PRETRANSLATION_PROVIDER=vllm \\
  INFERENCE_ENDPOINT_URL=http://YOUR_VLLM_HOST/v1 \\
  PRETRANSLATION_MODEL=YOUR_MODEL_NAME \\
  pytest tests/test_pretranslation_ai_technician_booking_regression.py -q -rs -m integration
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
from pydantic import BaseModel, ConfigDict, Field, field_validator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.tools.terms import get_ambiguity_hints_for_query
from app.services.translation import (
    _apply_exact_glossary_transliteration_replacements,
    _build_openai_pretranslation_messages,
    _get_glossary_hints_for_gu_query,
    translate_to_english_with_gpt5_mini,
)

# ---------------------------------------------------------------------------
# Live integration gate (mirrors glossary integration file)
# ---------------------------------------------------------------------------

INTEGRATION_ENABLED = os.getenv("PRETRANSLATION_AI_BOOKING_INTEGRATION", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

if INTEGRATION_ENABLED:
    if os.getenv("PRETRANSLATION_PROVIDER", "").strip().lower() != "vllm":
        pytest.fail(
            "PRETRANSLATION_PROVIDER must be 'vllm' for AI booking integration tests",
            pytrace=False,
        )
    if not os.getenv("INFERENCE_ENDPOINT_URL", "").strip():
        pytest.fail(
            "INFERENCE_ENDPOINT_URL is required for AI booking integration tests",
            pytrace=False,
        )

MATCHER_VERSION = "ai-booking-semantic-v1"
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
    "my",
    "me",
    "is",
    "this",
    "that",
}


# ---------------------------------------------------------------------------
# Case models
# ---------------------------------------------------------------------------


class BookingQueryCase(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str = Field(min_length=1)
    source_lang: str = Field(pattern="^(gu|en)$")
    source_text: str = Field(min_length=1)
    # Each inner tuple is an OR-group; all groups must match for a positive booking case.
    required_any: tuple[tuple[str, ...], ...] = Field(min_length=1)
    forbidden_any: tuple[str, ...] = ()
    optional_any: tuple[str, ...] = ()
    expect_species: str | None = None  # "cow" | "buffalo" | None
    expect_booking_intent: bool = True
    notes: str = ""

    @field_validator("required_any")
    @classmethod
    def _required_any_must_have_content(
        cls, value: tuple[tuple[str, ...], ...]
    ) -> tuple[tuple[str, ...], ...]:
        for group in value:
            if not any(_normalize_english(item) for item in group):
                raise ValueError("each required_any group needs a non-empty alias")
        return value


class PretranslationBookingResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    source_lang: str
    source_text: str
    translation: str
    matched_groups: tuple[str, ...]


class BookingFailureLedgerEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    matcher_version: str
    case_id: str
    source_lang: str
    source_text: str
    required_any: tuple[tuple[str, ...], ...]
    forbidden_any: tuple[str, ...]
    optional_any: tuple[str, ...]
    translation: str
    notes: str = ""


# ---------------------------------------------------------------------------
# Matcher helpers (aligned with glossary integration semantics)
# ---------------------------------------------------------------------------


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


def _matches_any_group(translation: str, group: tuple[str, ...]) -> str | None:
    for alias in group:
        if _normalized_contains(translation, alias):
            return alias
    return None


def _validate_booking_translation(case: BookingQueryCase, translation: str) -> tuple[bool, tuple[str, ...], str]:
    matched: list[str] = []
    for group in case.required_any:
        hit = _matches_any_group(translation, group)
        if hit is None:
            return False, tuple(matched), f"missing required group {group!r}"
        matched.append(hit)

    for forbidden in case.forbidden_any:
        if _normalized_contains(translation, forbidden):
            return False, tuple(matched), f"forbidden phrase present: {forbidden!r}"

    if case.expect_species:
        species_group = (case.expect_species,)
        if _matches_any_group(translation, species_group) is None:
            return False, tuple(matched), f"missing expected species: {case.expect_species!r}"

    if case.optional_any:
        optional_hit = _matches_any_group(translation, case.optional_any)
        if optional_hit is None:
            # Optional groups are informational only; do not fail.
            pass

    if case.expect_booking_intent:
        booking_group = ("book", "booking", "schedule", "appointment", "want", "need")
        if _matches_any_group(translation, booking_group) is None:
            return False, tuple(matched), "missing booking intent (book/booking/schedule/appointment/want/need)"

    return True, tuple(matched), ""


def _failure_ledger(case: BookingQueryCase, translation: str, reason: str) -> str:
    entry = BookingFailureLedgerEntry(
        matcher_version=MATCHER_VERSION,
        case_id=case.case_id,
        source_lang=case.source_lang,
        source_text=case.source_text,
        required_any=case.required_any,
        forbidden_any=case.forbidden_any,
        optional_any=case.optional_any,
        translation=translation,
        notes=reason or case.notes,
    )
    return json.dumps(entry.model_dump(), ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# Shared alias groups
# ---------------------------------------------------------------------------

BOOKING_INTENT_ALIASES = (
    "book",
    "booking",
    "want to book",
    "need booking",
    "schedule",
    "appointment",
    "want artificial insemination",
    "need artificial insemination",
    "want beech daan",
    "book beech daan",
)

INSEMINATION_ALIASES = (
    "artificial insemination",
    "beech daan",
    "beej daan",
    "insemination",
    "ai service",
    "ai call",
)

COW_ALIASES = ("cow", "cattle")
BUFFALO_ALIASES = ("buffalo",)

CROP_SEED_FORBIDDEN = (
    "seed sowing",
    "sowing seed",
    "plant seed",
    "crop seed",
    "seed donation",
    "sowing seeds",
    "planting seed",
    "agricultural seed",
    "sow seeds",
)

# ---------------------------------------------------------------------------
# Case catalog — Gujarati turn-1 booking intents
# ---------------------------------------------------------------------------

GUJARATI_BOOKING_TURN1_CASES: tuple[BookingQueryCase, ...] = (
    BookingQueryCase(
        case_id="gu-book-cow-standard",
        source_lang="gu",
        source_text="મારી ગાય માટે બીજ દાન બુક કરાવવું છે",
        required_any=(BOOKING_INTENT_ALIASES, INSEMINATION_ALIASES),
        forbidden_any=CROP_SEED_FORBIDDEN,
        expect_species="cow",
        notes="Canonical cow booking with spaced બીજ દાન",
    ),
    BookingQueryCase(
        case_id="gu-book-cow-compound",
        source_lang="gu",
        source_text="મારી ગાયને બીજદાન કરાવવાનું છે",
        required_any=(INSEMINATION_ALIASES, ("want", "need", "book", "booking", "arrange", "schedule")),
        forbidden_any=CROP_SEED_FORBIDDEN,
        expect_species="cow",
        notes="Real-call style compound બીજદાન",
    ),
    BookingQueryCase(
        case_id="gu-book-generic",
        source_lang="gu",
        source_text="બીજ દાન બુક કરાવવું છે",
        required_any=(BOOKING_INTENT_ALIASES, INSEMINATION_ALIASES),
        forbidden_any=CROP_SEED_FORBIDDEN,
        notes="Species not stated; agent should ask cow vs buffalo later",
    ),
    BookingQueryCase(
        case_id="gu-book-buffalo-beech-variant",
        source_lang="gu",
        source_text="મારી ભેંસ માટે બીચ દાન જોઈએ છે",
        required_any=(INSEMINATION_ALIASES, ("want", "need", "book", "booking")),
        forbidden_any=CROP_SEED_FORBIDDEN,
        expect_species="buffalo",
        notes="ASR spelling variant બીચ દાન",
    ),
    BookingQueryCase(
        case_id="gu-book-krutrim-bijdan",
        source_lang="gu",
        source_text="કૃત્રિમ બીજદાન બુક કરવું છે",
        required_any=(BOOKING_INTENT_ALIASES, INSEMINATION_ALIASES),
        forbidden_any=CROP_SEED_FORBIDDEN,
        notes="Explicit કૃત્રિમ બીજદાન",
    ),
    BookingQueryCase(
        case_id="gu-book-technician-mention",
        source_lang="gu",
        source_text="મારી ગાય માટે બીજદાન બુક કરાવો, ટેકનિશિયન સાથે",
        required_any=(BOOKING_INTENT_ALIASES, INSEMINATION_ALIASES),
        forbidden_any=CROP_SEED_FORBIDDEN,
        optional_any=("technician", "ait", "inseminator"),
        expect_species="cow",
        notes="Farmer mentions technician generically; should still be booking not retrieval",
    ),
    BookingQueryCase(
        case_id="gu-book-buffalo-short",
        source_lang="gu",
        source_text="ભેંસ માટે બીજ દાન બુક કરાવવું છે",
        required_any=(BOOKING_INTENT_ALIASES, INSEMINATION_ALIASES),
        forbidden_any=CROP_SEED_FORBIDDEN,
        expect_species="buffalo",
    ),
    BookingQueryCase(
        case_id="gu-book-cow-need-appointment",
        source_lang="gu",
        source_text="મારે ગાય માટે કૃત્રિમ બીજદાનની એપોઇન્ટમેન્ટ જોઈએ છે",
        required_any=(INSEMINATION_ALIASES, ("appointment", "book", "booking", "want", "need", "schedule")),
        forbidden_any=CROP_SEED_FORBIDDEN,
        expect_species="cow",
    ),
)

# ---------------------------------------------------------------------------
# Case catalog — English turn-1 booking intents (canonical agent queries)
# ---------------------------------------------------------------------------

ENGLISH_BOOKING_TURN1_CASES: tuple[BookingQueryCase, ...] = (
    BookingQueryCase(
        case_id="en-book-beech-cow",
        source_lang="en",
        source_text="Book beech daan for my cow",
        required_any=(BOOKING_INTENT_ALIASES, INSEMINATION_ALIASES),
        forbidden_any=CROP_SEED_FORBIDDEN,
        expect_species="cow",
        notes="Voice pipeline prompt example",
    ),
    BookingQueryCase(
        case_id="en-book-ai-full",
        source_lang="en",
        source_text="Book artificial insemination for my cow",
        required_any=(BOOKING_INTENT_ALIASES, INSEMINATION_ALIASES),
        forbidden_any=CROP_SEED_FORBIDDEN,
        expect_species="cow",
    ),
    BookingQueryCase(
        case_id="en-book-buffalo",
        source_lang="en",
        source_text="I need to book artificial insemination for my buffalo",
        required_any=(BOOKING_INTENT_ALIASES, INSEMINATION_ALIASES),
        forbidden_any=CROP_SEED_FORBIDDEN,
        expect_species="buffalo",
    ),
    BookingQueryCase(
        case_id="en-book-farmer-selection",
        source_lang="en",
        source_text="Book beech daan",
        required_any=(BOOKING_INTENT_ALIASES, INSEMINATION_ALIASES),
        forbidden_any=CROP_SEED_FORBIDDEN,
        notes="Should trigger farmer-name selection when multiple farmers on mobile",
    ),
    BookingQueryCase(
        case_id="en-book-want-ai-cow",
        source_lang="en",
        source_text="I want to book AI for my cow",
        required_any=(BOOKING_INTENT_ALIASES, ("ai", "artificial insemination", "insemination", "beech daan")),
        forbidden_any=CROP_SEED_FORBIDDEN + ("amul ai assistant", "helpline"),
        expect_species="cow",
        notes="AI must mean insemination in livestock booking context, not the brand",
    ),
    BookingQueryCase(
        case_id="en-book-schedule-ait",
        source_lang="en",
        source_text="Please schedule an AI technician visit for my buffalo",
        required_any=(
            ("schedule", "book", "booking", "appointment"),
            (
                "technician",
                "ait",
                "inseminator",
                "artificial insemination",
                "beech daan",
                "insemination",
                "ai technician",
                "ai visit",
            ),
        ),
        forbidden_any=CROP_SEED_FORBIDDEN,
        expect_species="buffalo",
    ),
    BookingQueryCase(
        case_id="en-book-need-beej-daan",
        source_lang="en",
        source_text="Need beej daan booking for my cow",
        required_any=(BOOKING_INTENT_ALIASES, INSEMINATION_ALIASES),
        forbidden_any=CROP_SEED_FORBIDDEN,
        expect_species="cow",
    ),
)

# ---------------------------------------------------------------------------
# Case catalog — follow-up technician selection (both languages)
# ---------------------------------------------------------------------------

TECHNICIAN_SELECTION_CASES: tuple[BookingQueryCase, ...] = (
    BookingQueryCase(
        case_id="en-tech-ramesh",
        source_lang="en",
        source_text="Ramesh Patel",
        required_any=(("ramesh patel", "ramesh"),),
        forbidden_any=CROP_SEED_FORBIDDEN,
        expect_booking_intent=False,
        notes="Technician name only; no booking verb required",
    ),
    BookingQueryCase(
        case_id="en-tech-with-suresh",
        source_lang="en",
        source_text="Book with Suresh Patel",
        required_any=(("suresh patel", "suresh"), ("book", "booking", "with")),
        forbidden_any=CROP_SEED_FORBIDDEN,
        expect_booking_intent=False,
    ),
    BookingQueryCase(
        case_id="en-tech-please-ramesh",
        source_lang="en",
        source_text="With Ramesh Patel please",
        required_any=(("ramesh patel", "ramesh"),),
        forbidden_any=CROP_SEED_FORBIDDEN,
        expect_booking_intent=False,
    ),
    BookingQueryCase(
        case_id="gu-tech-ramesh",
        source_lang="gu",
        source_text="રમેશ પટેલ સાથે બુક કરો",
        required_any=(("ramesh patel", "ramesh"), ("book", "booking", "with")),
        forbidden_any=CROP_SEED_FORBIDDEN,
        expect_booking_intent=False,
    ),
    BookingQueryCase(
        case_id="gu-tech-suresh-only",
        source_lang="gu",
        source_text="સુરેશ પટેલ",
        required_any=(("suresh patel", "suresh"),),
        forbidden_any=CROP_SEED_FORBIDDEN,
        expect_booking_intent=False,
    ),
    BookingQueryCase(
        case_id="gu-tech-mahesh-with",
        source_lang="gu",
        source_text="મહેશ પરમાર સાથે કરાવવું છે",
        required_any=(("mahesh parmar", "mahesh"),),
        forbidden_any=CROP_SEED_FORBIDDEN,
        expect_booking_intent=False,
    ),
)

# ---------------------------------------------------------------------------
# Negative / non-booking cases (must NOT satisfy full booking semantics)
# ---------------------------------------------------------------------------

NEGATIVE_NON_BOOKING_CASES: tuple[BookingQueryCase, ...] = (
    BookingQueryCase(
        case_id="neg-gu-other-topic-bija",
        source_lang="gu",
        source_text="મારે બીજા વિષય વિશે પૂછવું છે",
        required_any=(("other", "another topic", "different topic", "jignasa", "curiosity"),),
        forbidden_any=INSEMINATION_ALIASES,
        expect_booking_intent=False,
        notes="બીજા (other) must not be read as બીજ દાન",
    ),
    BookingQueryCase(
        case_id="neg-gu-fodder-seed",
        source_lang="gu",
        source_text="ઘાસચારાનું બીજ ક્યાંથી મળે",
        required_any=(("fodder", "forage", "grass", "seed"),),
        forbidden_any=("artificial insemination", "beech daan", "insemination booking"),
        expect_booking_intent=False,
        notes="Crop/fodder seed query, not AI booking",
    ),
    BookingQueryCase(
        case_id="neg-en-optimal-ai-time",
        source_lang="en",
        source_text="What is the optimal AI time for my buffalo in heat?",
        required_any=(("optimal", "best time", "when"), ("heat", "estrus", "oestrus", "in heat")),
        forbidden_any=("book beech daan", "book artificial insemination", "booking"),
        expect_booking_intent=False,
        notes="Breeding advice, not a booking request",
    ),
    BookingQueryCase(
        case_id="neg-en-who-is-sarlaben",
        source_lang="en",
        source_text="What is your name?",
        required_any=(("name", "sarlaben", "who are you"),),
        forbidden_any=INSEMINATION_ALIASES,
        expect_booking_intent=False,
    ),
)

ALL_BOOKING_POSITIVE_CASES = (
    GUJARATI_BOOKING_TURN1_CASES
    + ENGLISH_BOOKING_TURN1_CASES
    + TECHNICIAN_SELECTION_CASES
)

ALL_GUJARATI_POSITIVE_CASES = GUJARATI_BOOKING_TURN1_CASES + tuple(
    case for case in TECHNICIAN_SELECTION_CASES if case.source_lang == "gu"
)


def _selected_cases(cases: tuple[BookingQueryCase, ...]) -> tuple[BookingQueryCase, ...]:
    raw_filter = os.getenv("PRETRANSLATION_AI_BOOKING_CASE_FILTER", "").strip()
    if not raw_filter:
        return cases
    tokens = [
        token.strip().lower()
        for token in re.split(r"[,\s]+", raw_filter)
        if token.strip()
    ]
    selected = [
        case for case in cases if any(token in case.case_id.lower() for token in tokens)
    ]
    if not selected:
        pytest.fail(
            "PRETRANSLATION_AI_BOOKING_CASE_FILTER did not match any case IDs. "
            f"filter={raw_filter!r}",
            pytrace=False,
        )
    return tuple(selected)


SELECTED_GU_BOOKING_CASES = _selected_cases(GUJARATI_BOOKING_TURN1_CASES)
SELECTED_GU_TECHNICIAN_CASES = _selected_cases(
    tuple(case for case in TECHNICIAN_SELECTION_CASES if case.source_lang == "gu")
)
SELECTED_GU_ALL_POSITIVE = _selected_cases(ALL_GUJARATI_POSITIVE_CASES)


# ---------------------------------------------------------------------------
# Unit tests — prompt / hints / matcher (no live model)
# ---------------------------------------------------------------------------


class TestBookingCaseCatalog:
    def test_positive_catalog_is_non_empty(self):
        assert len(ALL_BOOKING_POSITIVE_CASES) >= 10

    def test_gujarati_turn1_catalog_covers_core_variants(self):
        ids = {case.case_id for case in GUJARATI_BOOKING_TURN1_CASES}
        assert "gu-book-cow-standard" in ids
        assert "gu-book-cow-compound" in ids
        assert "gu-book-buffalo-beech-variant" in ids

    def test_english_turn1_catalog_covers_prompt_examples(self):
        ids = {case.case_id for case in ENGLISH_BOOKING_TURN1_CASES}
        assert "en-book-beech-cow" in ids
        assert "en-book-farmer-selection" in ids


class TestAmbiguityHintsForBeechDaan:
    @pytest.mark.parametrize(
        "query",
        [
            "મારી ગાય માટે બીજ દાન બુક કરાવવું છે",
            "મારી ગાયને બીજદાન કરાવવાનું છે",
            "બીજ દાન બુક કરાવવું છે",
            "beech daan booking",
            "beejdan for cow",
        ],
    )
    def test_beech_daan_triggers_artificial_insemination_rule(self, query: str):
        hints = get_ambiguity_hints_for_query(query)
        assert hints
        lowered = hints.lower()
        assert "artificial insemination" in lowered or "ai booking" in lowered
        assert "not seed" in lowered or "not crop" in lowered or "not seed sowing" in lowered

    def test_beech_daan_hints_omitted_for_unrelated_query(self):
        hints = get_ambiguity_hints_for_query("મારી ગાયને તાવ છે")
        assert "beech daan" not in hints.lower()
        assert "artificial insemination booking" not in hints.lower()

    def test_pretranslation_prompt_includes_beech_daan_disambiguation(self):
        messages = _build_openai_pretranslation_messages(
            "Gujarati",
            "gu",
            "મારી ગાય માટે બીજ દાન બુક કરાવવું છે",
        )
        prompt = messages[0]["content"]
        assert "Domain-specific disambiguation rules" in prompt
        assert "Artificial Insemination" in prompt or "artificial insemination" in prompt
        assert "NOT seed" in prompt or "not crop" in prompt or "NOT seed sowing" in prompt

    def test_pretranslation_prompt_for_booking_includes_glossary_usage_rule(self):
        messages = _build_openai_pretranslation_messages(
            "Gujarati",
            "gu",
            "મારી ગાય માટે બીજ દાન બુક કરાવવું છે",
        )
        prompt = messages[0]["content"]
        assert "Glossary usage rule" in prompt
        assert "right-hand English label" in prompt


class TestGlossaryHintsForBeechDaan:
    @pytest.mark.parametrize(
        "query, expected_en_fragment",
        [
            ("મારી ગાય માટે બીજ દાન બુક કરાવવું છે", "Insemination"),
            ("મારી ગાયને બીજદાન કરાવવાનું છે", "Insemination"),
            ("કૃત્રિમ બીજદાન બુક કરવું છે", "insemination"),
        ],
    )
    def test_glossary_hints_surface_insemination_terms(self, query: str, expected_en_fragment: str):
        hints = _get_glossary_hints_for_gu_query(query, max_results=20)
        assert expected_en_fragment in hints

    def test_glossary_hints_do_not_fire_for_unrelated_bija(self):
        hints = _get_glossary_hints_for_gu_query("મારે બીજા વિષય વિશે પૂછવું છે", max_results=20)
        normalized = _normalize_english(hints)
        assert "insemination" not in normalized


@pytest.mark.parametrize("case", ENGLISH_BOOKING_TURN1_CASES, ids=lambda case: case.case_id)
def test_english_canonical_booking_queries_match_matcher(case: BookingQueryCase):
    ok, matched, reason = _validate_booking_translation(case, case.source_text)
    assert ok, (
        f"Canonical English booking query failed matcher.\n"
        f"case_id={case.case_id}\n"
        f"reason={reason!r}\n"
        f"matched={matched!r}\n"
        f"text={case.source_text!r}"
    )


@pytest.mark.parametrize("case", TECHNICIAN_SELECTION_CASES, ids=lambda case: case.case_id)
def test_technician_selection_canonical_queries_match_matcher(case: BookingQueryCase):
    ok, matched, reason = _validate_booking_translation(case, case.source_text)
    assert ok, (
        f"Technician selection query failed matcher.\n"
        f"case_id={case.case_id}\n"
        f"reason={reason!r}\n"
        f"matched={matched!r}\n"
        f"text={case.source_text!r}"
    )


NEGATIVE_TRANSLATION_SAMPLES: tuple[tuple[str, str], ...] = (
    (
        "neg-gu-other-topic-bija",
        "I want to ask about another topic",
    ),
    (
        "neg-gu-fodder-seed",
        "Where can I get fodder seed for planting",
    ),
    (
        "neg-en-optimal-ai-time",
        "What is the optimal artificial insemination time for my buffalo in heat",
    ),
    (
        "neg-en-who-is-sarlaben",
        "What is your name",
    ),
)


@pytest.mark.parametrize("case_id, translation", NEGATIVE_TRANSLATION_SAMPLES, ids=lambda row: row[0])
def test_negative_translations_are_not_full_booking_intent(case_id: str, translation: str):
    booking_probe = BookingQueryCase(
        case_id=f"{case_id}-probe",
        source_lang="en",
        source_text=translation,
        required_any=(BOOKING_INTENT_ALIASES, INSEMINATION_ALIASES),
        forbidden_any=CROP_SEED_FORBIDDEN,
    )
    ok_booking, _, reason = _validate_booking_translation(booking_probe, translation)
    assert not ok_booking, (
        f"Negative sample unexpectedly satisfied full booking matcher.\n"
        f"case_id={case_id}\n"
        f"translation={translation!r}\n"
        f"reason={reason!r}"
    )


@pytest.mark.parametrize(
    "english_query",
    [case.source_text for case in ENGLISH_BOOKING_TURN1_CASES],
    ids=[case.case_id for case in ENGLISH_BOOKING_TURN1_CASES],
)
def test_english_queries_passthrough_pretranslation_unchanged(english_query: str):
    translated = asyncio.run(
        translate_to_english_with_gpt5_mini(english_query, "en")
    )
    assert translated == english_query


def test_gujarati_glossary_transliteration_does_not_corrupt_other_topic_bija():
    translated = _apply_exact_glossary_transliteration_replacements(
        "મારે બીજા વિષય વિશે પૂછવું છે",
        "I want to ask about another topic",
    )
    assert translated == "I want to ask about another topic"


# ---------------------------------------------------------------------------
# Live integration — Gujarati pretranslation -> English booking query
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("case", SELECTED_GU_BOOKING_CASES, ids=lambda case: case.case_id)
def test_vllm_pretranslation_gujarati_booking_turn1(case: BookingQueryCase):
    if not INTEGRATION_ENABLED:
        pytest.skip("Set PRETRANSLATION_AI_BOOKING_INTEGRATION=1 for live booking regressions")

    hints = _get_glossary_hints_for_gu_query(case.source_text, max_results=20)
    translation = asyncio.run(
        translate_to_english_with_gpt5_mini(case.source_text, "gu")
    )
    assert translation.strip(), f"Empty pretranslation for {case.case_id}"

    ok, matched, reason = _validate_booking_translation(case, translation)
    ledger = _failure_ledger(case, translation, reason)
    assert ok, (
        f"Gujarati booking pretranslation failed semantic checks.\n"
        f"case_id={case.case_id}\n"
        f"source_text={case.source_text!r}\n"
        f"translation={translation!r}\n"
        f"hints={hints!r}\n"
        f"matched={matched!r}\n"
        f"failure_ledger_json={ledger}"
    )

    PretranslationBookingResult(
        case_id=case.case_id,
        source_lang=case.source_lang,
        source_text=case.source_text,
        translation=translation,
        matched_groups=matched,
    )


@pytest.mark.integration
@pytest.mark.parametrize("case", SELECTED_GU_TECHNICIAN_CASES, ids=lambda case: case.case_id)
def test_vllm_pretranslation_gujarati_technician_selection(case: BookingQueryCase):
    if not INTEGRATION_ENABLED:
        pytest.skip("Set PRETRANSLATION_AI_BOOKING_INTEGRATION=1 for live booking regressions")

    translation = asyncio.run(
        translate_to_english_with_gpt5_mini(case.source_text, "gu")
    )
    assert translation.strip(), f"Empty pretranslation for {case.case_id}"

    ok, matched, reason = _validate_booking_translation(case, translation)
    ledger = _failure_ledger(case, translation, reason)
    assert ok, (
        f"Gujarati technician-selection pretranslation failed semantic checks.\n"
        f"case_id={case.case_id}\n"
        f"source_text={case.source_text!r}\n"
        f"translation={translation!r}\n"
        f"matched={matched!r}\n"
        f"failure_ledger_json={ledger}"
    )


@pytest.mark.integration
def test_vllm_pretranslation_negative_fodder_seed_not_insemination_booking():
    if not INTEGRATION_ENABLED:
        pytest.skip("Set PRETRANSLATION_AI_BOOKING_INTEGRATION=1 for live booking regressions")

    case = next(c for c in NEGATIVE_NON_BOOKING_CASES if c.case_id == "neg-gu-fodder-seed")
    translation = asyncio.run(
        translate_to_english_with_gpt5_mini(case.source_text, "gu")
    )
    assert translation.strip()

    booking_probe = BookingQueryCase(
        case_id="probe-full-booking",
        source_lang="gu",
        source_text=case.source_text,
        required_any=(BOOKING_INTENT_ALIASES, INSEMINATION_ALIASES),
        forbidden_any=(),
    )
    ok_booking, _, _ = _validate_booking_translation(booking_probe, translation)
    assert not ok_booking, (
        f"Fodder-seed query was misread as AI booking.\n"
        f"translation={translation!r}"
    )


@pytest.mark.integration
@pytest.mark.parametrize("case", ENGLISH_BOOKING_TURN1_CASES, ids=lambda case: case.case_id)
def test_english_booking_queries_remain_valid_after_passthrough(case: BookingQueryCase):
    """English turns skip translation; validate canonical strings still match booking semantics."""
    if not INTEGRATION_ENABLED:
        pytest.skip("Set PRETRANSLATION_AI_BOOKING_INTEGRATION=1 to run integration suite marker")

    translated = asyncio.run(
        translate_to_english_with_gpt5_mini(case.source_text, "en")
    )
    assert translated == case.source_text

    ok, matched, reason = _validate_booking_translation(case, translated)
    assert ok, (
        f"English passthrough booking query failed matcher in integration suite.\n"
        f"case_id={case.case_id}\n"
        f"reason={reason!r}\n"
        f"matched={matched!r}"
    )
