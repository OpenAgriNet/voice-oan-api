"""Structural validation shared by glossary startup and CI checks."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any


REQUIRED_GLOSSARY_FIELDS = {
    "en",
    "gu",
    "transliteration",
    "gu_input_aliases",
    "transliteration_input_aliases",
}
OPTIONAL_GLOSSARY_FIELDS = {"en_input_aliases", "gu_output_aliases"}
GLOSSARY_FIELDS = REQUIRED_GLOSSARY_FIELDS | OPTIONAL_GLOSSARY_FIELDS
POLICY_FIELDS = {"forbidden"}

# These pre-existing semantic conflicts need domain review. Keeping the list
# explicit prevents this cleanup from changing farmer-facing terminology while
# ensuring no new conflict can be introduced unnoticed.
KNOWN_CANONICAL_FORBIDDEN_EXCEPTIONS = {
    ("bullock", "બળદ"),
    ("conception/pregnancy", "ગર્ભાધાન"),
    ("stress", "તણાવ"),
    ("udder infection", "આઉનો/બાવલાનો સોજો"),
}


class GlossaryValidationError(ValueError):
    """Raised when glossary assets are unsafe or internally inconsistent."""


def normalize_concept(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def load_json_object(path: Path) -> Any:
    """Load JSON without hiding malformed-file errors."""
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _require_nonblank_string(value: Any, location: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{location} must be a non-blank string")


def _validate_alias_list(
    value: Any,
    location: str,
    owner: str,
    owners: dict[str, set[str]],
    errors: list[str],
) -> set[str]:
    normalized_aliases: set[str] = set()
    if not isinstance(value, list):
        errors.append(f"{location} must be a list")
        return normalized_aliases

    for alias_index, alias in enumerate(value):
        alias_location = f"{location}[{alias_index}]"
        _require_nonblank_string(alias, alias_location, errors)
        if not isinstance(alias, str) or not alias.strip():
            continue
        normalized = normalize_concept(alias)
        if normalized in normalized_aliases:
            errors.append(f"{location} contains duplicate alias {alias!r}")
        normalized_aliases.add(normalized)
        owners[normalized].add(owner)
    return normalized_aliases


def _find_replacement_cycles(replacements: dict[str, str]) -> list[list[str]]:
    normalized = {
        normalize_concept(source): normalize_concept(target)
        for source, target in replacements.items()
        if isinstance(source, str)
        and source.strip()
        and isinstance(target, str)
        and target.strip()
    }
    cycles: list[list[str]] = []
    completed: set[str] = set()
    for start in normalized:
        if start in completed:
            continue
        order: list[str] = []
        positions: dict[str, int] = {}
        current = start
        while current in normalized and current not in completed:
            if current in positions:
                cycles.append(order[positions[current] :] + [current])
                break
            positions[current] = len(order)
            order.append(current)
            current = normalized[current]
        completed.update(order)
    return cycles


def validate_glossary_assets(glossary: Any, policy: Any) -> None:
    """Validate both terminology assets and raise one actionable error."""
    errors: list[str] = []

    if not isinstance(glossary, list):
        raise GlossaryValidationError("glossary must be a JSON list")
    if not isinstance(policy, dict):
        raise GlossaryValidationError("term policy must be a JSON object")

    unknown_policy_fields = set(policy) - POLICY_FIELDS
    missing_policy_fields = POLICY_FIELDS - set(policy)
    if unknown_policy_fields:
        errors.append(f"policy has unknown fields: {sorted(unknown_policy_fields)}")
    if missing_policy_fields:
        errors.append(f"policy is missing fields: {sorted(missing_policy_fields)}")

    forbidden = policy.get("forbidden")
    if not isinstance(forbidden, dict):
        errors.append("policy.forbidden must be an object")
        forbidden = {}
    else:
        for source, replacement in forbidden.items():
            _require_nonblank_string(source, "policy.forbidden key", errors)
            _require_nonblank_string(
                replacement, f"policy.forbidden[{source!r}]", errors
            )

    forbidden_sources = {
        normalize_concept(source)
        for source in forbidden
        if isinstance(source, str) and source.strip()
    }

    concepts: dict[str, int] = {}
    canonical_gu_values: dict[str, str] = {}
    en_alias_owners: dict[str, set[str]] = defaultdict(set)
    gu_alias_owners: dict[str, set[str]] = defaultdict(set)
    transliteration_alias_owners: dict[str, set[str]] = defaultdict(set)

    for index, row in enumerate(glossary):
        location = f"glossary[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{location} must be an object")
            continue

        unknown_fields = set(row) - GLOSSARY_FIELDS
        missing_fields = REQUIRED_GLOSSARY_FIELDS - set(row)
        if unknown_fields:
            errors.append(f"{location} has unknown fields: {sorted(unknown_fields)}")
        if missing_fields:
            errors.append(f"{location} is missing fields: {sorted(missing_fields)}")

        for field in ("en", "gu", "transliteration"):
            _require_nonblank_string(row.get(field), f"{location}.{field}", errors)

        english = row.get("en")
        gujarati = row.get("gu")
        if not isinstance(english, str) or not english.strip():
            continue
        concept = normalize_concept(english)
        if concept in concepts:
            errors.append(
                f"duplicate English concept {english!r} at rows {concepts[concept]} and {index}"
            )
        else:
            concepts[concept] = index

        if isinstance(gujarati, str) and gujarati.strip():
            canonical_gu_values[concept] = normalize_concept(gujarati)

        _validate_alias_list(
            row.get("en_input_aliases", []),
            f"{location}.en_input_aliases",
            concept,
            en_alias_owners,
            errors,
        )
        _validate_alias_list(
            row.get("gu_input_aliases"),
            f"{location}.gu_input_aliases",
            concept,
            gu_alias_owners,
            errors,
        )
        _validate_alias_list(
            row.get("transliteration_input_aliases"),
            f"{location}.transliteration_input_aliases",
            concept,
            transliteration_alias_owners,
            errors,
        )
        gu_output_aliases = _validate_alias_list(
            row.get("gu_output_aliases", []),
            f"{location}.gu_output_aliases",
            concept,
            defaultdict(set),
            errors,
        )
        canonical_gu = (
            normalize_concept(gujarati)
            if isinstance(gujarati, str) and gujarati.strip()
            else ""
        )
        for alias in gu_output_aliases:
            if alias == canonical_gu:
                errors.append(
                    f"{location}.gu_output_aliases contains canonical Gujarati value {gujarati!r}"
                )
            if alias in forbidden_sources:
                errors.append(
                    f"{location}.gu_output_aliases contains forbidden output {alias!r}"
                )

    for alias, assigned_concepts in en_alias_owners.items():
        if len(assigned_concepts) > 1:
            errors.append(
                f"English input alias {alias!r} belongs to multiple concepts: "
                f"{sorted(assigned_concepts)}"
            )
        if alias in concepts and alias not in assigned_concepts:
            errors.append(
                f"English input alias {alias!r} shadows canonical concept {alias!r}"
            )

    for label, owners in (
        ("Gujarati", gu_alias_owners),
        ("transliteration", transliteration_alias_owners),
    ):
        for alias, assigned_concepts in owners.items():
            if len(assigned_concepts) > 1:
                errors.append(
                    f"{label} input alias {alias!r} belongs to multiple concepts: "
                    f"{sorted(assigned_concepts)}"
                )

    actual_exceptions: set[tuple[str, str]] = set()
    for concept, gujarati in canonical_gu_values.items():
        if gujarati not in forbidden_sources:
            continue
        conflict = (concept, gujarati)
        if conflict in KNOWN_CANONICAL_FORBIDDEN_EXCEPTIONS:
            actual_exceptions.add(conflict)
        else:
            errors.append(
                f"canonical Gujarati value {gujarati!r} for {concept!r} is forbidden"
            )

    for conflict in sorted(KNOWN_CANONICAL_FORBIDDEN_EXCEPTIONS - actual_exceptions):
        errors.append(f"stale canonical/forbidden exception: {conflict!r}")

    for cycle in _find_replacement_cycles(forbidden):
        errors.append(f"forbidden replacement cycle: {' -> '.join(cycle)}")

    if errors:
        raise GlossaryValidationError("Invalid glossary assets:\n- " + "\n- ".join(errors))


def validate_files(glossary_path: Path, policy_path: Path) -> None:
    validate_glossary_assets(load_json_object(glossary_path), load_json_object(policy_path))


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    validate_files(root / "assets/glossary_terms.json", root / "assets/gu_term_policy.json")
    print("Glossary validation passed")
