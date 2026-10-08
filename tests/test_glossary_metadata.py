from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agents.tools.terms import (
    ALIAS_TO_CANONICAL_EN,
    TermPair,
    get_mini_glossary_for_text,
)
from helpers.glossary_validation import (
    GlossaryValidationError,
    KNOWN_CANONICAL_FORBIDDEN_EXCEPTIONS,
    validate_glossary_assets,
)


ROOT = Path(__file__).resolve().parents[1]
GLOSSARY = json.loads((ROOT / "assets/glossary_terms.json").read_text(encoding="utf-8"))
POLICY = json.loads((ROOT / "assets/gu_term_policy.json").read_text(encoding="utf-8"))

FORMER_POLICY_ALIAS_BEHAVIOR = {
    "mammary gland": ("udder", "Udder -> બાવલું"),
    "udder region": ("udder", "Udder -> બાવલું"),
    "mammary area": ("udder", "Udder -> બાવલું"),
    "dewormer": ("deworming", "Deworming -> કૃમિનાશક દવા"),
    "deworm": ("deworming", "Deworming -> કૃમિનાશક દવા"),
    "ai": ("artificial insemination", "ARTIFICIAL INSEMINATION -> કૃત્રિમ બીજદાન"),
    "a.i.": ("artificial insemination", "ARTIFICIAL INSEMINATION -> કૃત્રિમ બીજદાન"),
    "ai service": ("artificial insemination", "ARTIFICIAL INSEMINATION -> કૃત્રિમ બીજદાન"),
    "ai receipt": ("artificial insemination", "ARTIFICIAL INSEMINATION -> કૃત્રિમ બીજદાન"),
    "track number": ("artificial insemination", "ARTIFICIAL INSEMINATION -> કૃત્રિમ બીજદાન"),
    "breeder": ("breeding", "Breeding -> પ્રજનન"),
    "udder infection": (
        "udder infection",
        "udder infection -> આઉનો/બાવલાનો સોજો\nInfection -> ચેપ\nUdder -> બાવલું",
    ),
    "udder edema": ("udder edema", "Udder Edema -> આઉનો સોજો\nUdder -> બાવલું"),
    "sub-clinical mastitis": (
        "sub-clinical mastitis",
        "Sub-clinical Mastitis -> સૂકો ગરિયો, સૂકો ગાળિયો\nMastitis -> આંચળનો સોજો",
    ),
    "subclinical mastitis": (
        "subclinical mastitis",
        "SUBCLINICAL MASTITIS -> સૂકો ઘડીયો\nMastitis -> આંચળનો સોજો",
    ),
    "insemination": ("insemination", "Insemination -> બીજદાન"),
    "semen dose": ("semen dose", "Semen Dose -> સીમેન ડોઝ"),
    "reproduction": ("reproduction", "Reproduction -> પ્રજનન"),
    "repeat breeder": (
        "repeat breeder",
        "Repeat Breeder -> ગરમીમાં ઉથલો મારતું પશુ\nBreeding -> પ્રજનન",
    ),
}

MIGRATED_GU_OUTPUT_ALIASES = {
    "ARTIFICIAL INSEMINATION": ["બીજદાન કરાવવું, ડોજ મુકાવવો"],
    "Breeding": ["બીજદાન"],
    "Deworming": ["કૃમિનાશક", "કૃમિ નિવારણ દવા"],
    "Mastitis": ["માસ્ટિટિસ"],
    "Udder": ["આઉ", "બાવલા"],
}


@pytest.mark.parametrize(
    ("alias", "expected_canonical", "expected_mini_glossary"),
    [
        (alias, expected[0], expected[1])
        for alias, expected in FORMER_POLICY_ALIAS_BEHAVIOR.items()
    ],
)
def test_former_policy_alias_behavior_is_unchanged(
    alias: str,
    expected_canonical: str,
    expected_mini_glossary: str,
) -> None:
    assert ALIAS_TO_CANONICAL_EN[alias] == expected_canonical
    assert get_mini_glossary_for_text(alias) == expected_mini_glossary


def test_glossary_owns_only_effective_english_aliases() -> None:
    aliases = {
        row["en"]: row["en_input_aliases"]
        for row in GLOSSARY
        if row.get("en_input_aliases")
    }
    assert aliases == {
        "ARTIFICIAL INSEMINATION": ["ai", "a.i.", "ai service", "ai receipt", "track number"],
        "Breeding": ["breeder"],
        "Deworming": ["dewormer", "deworm"],
        "Udder": ["mammary gland", "udder region", "mammary area"],
    }
    assert sum(map(len, aliases.values())) == 11


def test_glossary_documents_seven_noncanonical_gujarati_outputs() -> None:
    aliases = {
        row["en"]: row["gu_output_aliases"]
        for row in GLOSSARY
        if row.get("gu_output_aliases")
    }
    assert aliases == MIGRATED_GU_OUTPUT_ALIASES
    assert sum(map(len, aliases.values())) == 7


def test_optional_alias_fields_default_to_empty_lists() -> None:
    pair = TermPair(en="Example", gu="ઉદાહરણ", transliteration="udāharaṇ")
    assert pair.en_input_aliases == []
    assert pair.gu_output_aliases == []


def _row(glossary: list[dict], english: str) -> dict:
    return next(row for row in glossary if row["en"] == english)


def _assert_invalid(glossary: list[dict], policy: dict, match: str) -> None:
    with pytest.raises(GlossaryValidationError, match=match):
        validate_glossary_assets(glossary, policy)


@pytest.mark.parametrize("legacy_field", ["preferred", "input_aliases", "allowed_aliases"])
def test_policy_rejects_removed_sections(legacy_field: str) -> None:
    policy = copy.deepcopy(POLICY)
    policy[legacy_field] = {}
    _assert_invalid(GLOSSARY, policy, "policy has unknown fields")


@pytest.mark.parametrize("invalid_aliases", [[""], ["new alias", " NEW   ALIAS "]])
def test_english_aliases_reject_blank_or_duplicates(invalid_aliases: list[str]) -> None:
    glossary = copy.deepcopy(GLOSSARY)
    _row(glossary, "Udder")["en_input_aliases"] = invalid_aliases
    _assert_invalid(glossary, POLICY, "non-blank|string|duplicate alias")


def test_english_alias_rejects_multiple_owners() -> None:
    glossary = copy.deepcopy(GLOSSARY)
    _row(glossary, "Udder")["en_input_aliases"].append("shared alias")
    _row(glossary, "Deworming")["en_input_aliases"].append("shared alias")
    _assert_invalid(glossary, POLICY, "belongs to multiple concepts")


def test_english_alias_rejects_canonical_shadowing() -> None:
    glossary = copy.deepcopy(GLOSSARY)
    _row(glossary, "Udder")["en_input_aliases"].append("Mastitis")
    _assert_invalid(glossary, POLICY, "shadows canonical concept")


def test_gujarati_output_alias_rejects_own_canonical_value() -> None:
    glossary = copy.deepcopy(GLOSSARY)
    udder = _row(glossary, "Udder")
    udder["gu_output_aliases"].append(udder["gu"])
    _assert_invalid(glossary, POLICY, "contains canonical Gujarati value")


def test_gujarati_output_alias_rejects_forbidden_output() -> None:
    glossary = copy.deepcopy(GLOSSARY)
    forbidden_output = next(iter(POLICY["forbidden"]))
    _row(glossary, "Udder")["gu_output_aliases"].append(forbidden_output)
    _assert_invalid(glossary, POLICY, "contains forbidden output")


def test_new_canonical_forbidden_conflict_is_rejected() -> None:
    glossary = copy.deepcopy(GLOSSARY)
    forbidden_output = next(
        source
        for source in POLICY["forbidden"]
        if ("udder", source.casefold()) not in KNOWN_CANONICAL_FORBIDDEN_EXCEPTIONS
    )
    _row(glossary, "Udder")["gu"] = forbidden_output
    _assert_invalid(glossary, POLICY, "canonical Gujarati value.*is forbidden")


def test_stale_canonical_forbidden_exception_is_rejected() -> None:
    glossary = copy.deepcopy(GLOSSARY)
    _row(glossary, "Bullock")["gu"] = "અન્ય"
    _assert_invalid(glossary, POLICY, "stale canonical/forbidden exception")


def test_current_four_canonical_forbidden_exceptions_are_explicit() -> None:
    assert KNOWN_CANONICAL_FORBIDDEN_EXCEPTIONS == {
        ("bullock", "બળદ"),
        ("conception/pregnancy", "ગર્ભાધાન"),
        ("stress", "તણાવ"),
        ("udder infection", "આઉનો/બાવલાનો સોજો"),
    }
    validate_glossary_assets(GLOSSARY, POLICY)
